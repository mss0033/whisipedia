"""Operator commands that act on the real library: ingest, index build, scrub, drill, approve/reject/resume,
status. Each builds its components through :mod:`wsp_cli.bootstrap` and prints a plain-text report."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
import time
from collections import Counter
from collections.abc import Generator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO, cast

from wsp_cli import bootstrap, doccollections
from wsp_cli.doctor import Doctor, Level
from wsp_cli.doctor import exit_code as doctor_exit
from wsp_cli.doctor import render as render_doctor
from wsp_cli.indexer import Indexer, SourceReport
from wsp_cli.ingest import Ingestor, VersionStatus, config_alert, render
from wsp_cli.locks import (
    DRILL,
    EXIT_BUSY,
    HEAVY,
    INDEX,
    INGEST,
    SCRUB,
    LockHeldError,
    exclusive,
    lock_dir_for,
)
from wsp_cli.service import ServiceManager, unit_dir
from wsp_cli.wiring import drill_once, parity_targets, repair_ladder, scrubber, storage_probes
from wsp_core.model import GenerationStatus, SourceSpec
from wsp_core.partitions import PartitionError, check_partition, partition_generation_id, partition_of
from wsp_core.ports import Fetcher
from wsp_core.registry import RegistryError, load_registry
from wsp_orchestrator.curator import ProposalFiles
from wsp_orchestrator.curator.registry_edit import apply_to_registry_text, remove_from_overlay
from wsp_orchestrator.orchestrator import Orchestrator
from wsp_orchestrator.pipeline import IllegalTransitionError
from wsp_orchestrator.settings import OrchestratorSettings
from wsp_orchestrator.store import ConcurrentUpdateError, StateStore
from wsp_orchestrator.versions import version_key
from wsp_vault.drill import DrillKind
from wsp_vault.vault import FileVault

# `wsp ingest` exit codes (docs/runbooks/document-collections.md, "Exit codes"): 0 every version
# (with --version: that version) is ingested or already sealed; 1 a version failed; 2 usage;
# 3 another ingest runs, or this one yielded after --max-seconds (EXIT_BUSY); 4 files still missing
# or downloading; 8 with --version only: no download plan names that version (nothing was fetched
# for it, so there is nothing to ingest).
EXIT_INCOMPLETE = 4  # `wsp ingest`: some version still has files missing or downloading
EXIT_NOT_PLANNED = 8  # `wsp ingest --version V`: no download plan names V
EXIT_UNAVAILABLE = 5  # the vault share is unreachable (scrub stopped without recording anything)


def _context(args: argparse.Namespace) -> tuple[bootstrap.Layout, dict[str, Any], tuple[SourceSpec, ...]]:
    lay = bootstrap.layout(args.config_dir)
    cfg = bootstrap.load(lay)
    return lay, cfg, tuple(bootstrap.load_sources(cfg, lay))


def _lock_dir(cfg: Mapping[str, Any]) -> Path:
    """Local-disk lock directory, next to the index root."""
    return lock_dir_for(bootstrap.paths(cfg).require_index())


@contextlib.contextmanager
def _index_writer(cfg: Mapping[str, Any]) -> Generator[None]:
    """``HEAVY`` then ``INDEX`` (the global order): one heavy job on the machine, one index writer.

    Shared with the orchestrator's refine/index units and the nightly timer; a held lock raises
    :class:`LockHeldError` at once (exit :data:`EXIT_BUSY`).
    """
    with exclusive(_lock_dir(cfg), HEAVY), exclusive(_lock_dir(cfg), INDEX):
        yield


def _busy(exc: LockHeldError) -> int:
    print(f"error: {exc}", file=sys.stderr)
    return EXIT_BUSY


def _vault(
    cfg: Mapping[str, Any], lay: bootstrap.Layout, specs: Sequence[SourceSpec], *, writable: bool
) -> FileVault:
    root = bootstrap.paths(cfg).require_vault()
    if not root.is_dir():
        raise bootstrap.ConfigError(f"vault.root {root} does not exist")
    signer = bootstrap.load_signer(cfg, lay, create=writable)
    return bootstrap.build_vault(cfg, specs, signer, ledger=writable)


# --- ingest -------------------------------------------------------------------------------------------


def cmd_ingest(args: argparse.Namespace, out: TextIO) -> int:
    lay, cfg, specs = _context(args)
    if args.dry_run:
        return _ingest(args, out, lay, cfg, specs)
    try:
        wait: float | None = getattr(args, "wait", None) or None  # callers building their own args omit it
        priority = bool(getattr(args, "priority", False))
        with exclusive(_lock_dir(cfg), "ingest", wait=wait, queue=wait is not None, priority=priority):
            return _ingest(args, out, lay, cfg, specs)
    except LockHeldError as exc:
        return _busy(exc)


def _ingest(
    args: argparse.Namespace,
    out: TextIO,
    lay: bootstrap.Layout,
    cfg: dict[str, Any],
    specs: tuple[SourceSpec, ...],
) -> int:
    dry: bool = args.dry_run
    version = _ingest_version(args, specs)
    if isinstance(version, int):
        return version
    vault = _vault(cfg, lay, specs, writable=not dry)
    fetcher: Fetcher | None = None if args.offline else bootstrap.build_fetcher(cfg)
    ing = Ingestor(
        vault,
        specs,
        fetcher=fetcher,
        out=None if args.json else out,
        user_agent=bootstrap.user_agent(cfg),
        settle_seconds=float(args.settle_seconds),
        max_seconds=getattr(args, "max_seconds", None),
        # An arXiv chunk given up on after its PDF was fetched again three times: an alert, not
        # only a FAILED line in a job's log. A dry run changes nothing, so it raises nothing.
        alert=None if dry else config_alert(cfg),
    )
    try:
        if args.allow_mirror:
            if args.source != "gutenberg" or version is not None:
                print("error: --allow-mirror needs --source gutenberg (and no --version)", file=sys.stderr)
                return 2
            vp = ing.ingest_mirror(dry_run=dry)
            out.write(
                f"{vp.source_id}/{vp.generation_id}: {vp.status.value} {len(vp.files)} files {vp.reason}\n"
            )
            return 1 if vp.status is VersionStatus.FAILED else 0
        report = ing.run(source_id=args.source, version=version, dry_run=dry)
    finally:
        close = getattr(fetcher, "close", None)
        if callable(close):
            close()
    if args.json:
        out.write(json.dumps(report.to_json(), indent=2) + "\n")
    else:
        render(report, out)
    if version is not None and not report.versions:
        return EXIT_NOT_PLANNED  # a lane must not take "nothing to do" for "this cycle landed"
    if any(v.status is VersionStatus.FAILED for v in report.versions):
        return 1
    if any(v.status is VersionStatus.YIELDED for v in report.versions):
        print(
            "ingest yielded (--max-seconds) so other ingests can run; run it again to continue",
            file=sys.stderr,
        )
        return EXIT_BUSY  # as a lock held: try again shortly
    # Not an error, but not done either: scripts must not mistake it for success (exit 0).
    return EXIT_INCOMPLETE if any(v.status is VersionStatus.INCOMPLETE for v in report.versions) else 0


def _ingest_version(args: argparse.Namespace, specs: Sequence[SourceSpec]) -> str | int | None:
    """The incoming version ``--version``/``--partition`` name (as ``wsp collections fetch`` names it:
    ``<cycle>.<partition>`` for a partitioned source), ``None`` for all versions, or an exit code."""
    cycle: str | None = getattr(args, "version", None)
    part: str | None = getattr(args, "partition", None)
    if cycle is None and part is None:
        return None
    if not args.source or cycle is None:
        print("error: --version needs --source, and --partition needs --version", file=sys.stderr)
        return 2
    spec = next((s for s in specs if s.id == args.source), None)
    if part is None:
        if spec is not None and spec.partitioned and partition_of(cycle) is None:
            print(
                f"error: {spec.id} is partitioned: add --partition (or give <cycle>.<partition>)",
                file=sys.stderr,
            )
            return 2
        return cycle
    if spec is not None and not spec.partitioned:
        print(f"error: {spec.id} is not partitioned (access.partitioned): drop --partition", file=sys.stderr)
        return 2
    try:
        return partition_generation_id(cycle, check_partition(part.lower()))
    except PartitionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


# --- index build -----------------------------------------------------------------------------------------


@contextlib.contextmanager
def open_indexer(
    cfg: Mapping[str, Any], lay: bootstrap.Layout, specs: Sequence[SourceSpec], out: TextIO | None
) -> Generator[Indexer]:
    vault = _vault(cfg, lay, specs, writable=False)
    p = bootstrap.paths(cfg)
    ix = Indexer(
        vault,
        specs,
        bootstrap.load_adapters(),
        p.require_index(),
        lambda: bootstrap.open_index(cfg, specs),  # opened on first use (refining needs neither)
        lambda: bootstrap.open_changes(cfg),
        user_agent=bootstrap.user_agent(cfg),
        out=out,
        refinery=p.refinery,
        map_root=p.refinery_maps,
        read_pace=bootstrap.ingest_pace(cfg),
        storage_available=storage_probes(p.vault_root, p.refinery_root),
    )
    try:
        yield ix
    finally:
        ix.close()


def cmd_index_build(args: argparse.Namespace, out: TextIO) -> int:
    lay, cfg, specs = _context(args)
    try:
        with _index_writer(cfg):
            if not _full_build(args):
                with open_indexer(cfg, lay, specs, out) as ix:
                    return _index_build(args, cfg, ix, out)
            # Marked only once the locks are held (a busy run marks nothing), and around the indexer:
            # a close that fails (the share gone) must not leave a mark that says "ended cleanly".
            with _full_build_marked(cfg) as mark, open_indexer(cfg, lay, specs, out) as ix:
                mark.exit = _index_build(args, cfg, ix, out)
            return mark.exit
    except LockHeldError as exc:
        return _busy(exc)


def _index_build(args: argparse.Namespace, cfg: Mapping[str, Any], ix: Indexer, out: TextIO) -> int:
    """``index build`` under its locks; returns the exit code."""
    ix.defer_suggest = bool(getattr(args, "no_suggest", False))
    # The suggest trie is rebuilt once, after the rank (its priors), not also before it.
    if getattr(args, "reindex", False):
        if not args.source:
            out.write("--reindex needs --source\n")
            return 2
        reports = [ix.reindex(args.source)]
    elif getattr(args, "refine_again", False):
        if not args.source:
            out.write("--refine-again needs --source\n")
            return 2
        swap = bool(getattr(args, "swap", False))
        reports = [ix.refine_again(args.source, swap=swap, finish=False)]
    else:
        reports = ix.build([args.source] if args.source else None, finish=False)
    if any(r.error for r in reports):
        # A source failed: the rank, the suggest trie's rebuild and spell read every indexed
        # generation, and would be rebuilt without what that source holds (or from what a
        # sick share still let them read). The files of the last good run are kept; what was
        # indexed is committed and searchable; the next run rebuilds them (2026-11-06 review).
        out.write("rank, suggest and spell left as they were: a source failed\n")
        if _full_build(args):
            _shared_phases_skipped(cfg, ix.root, [r.source_id for r in reports if r.error], out)
        return _index_build_summary(reports, out)
    try:
        if not getattr(args, "no_rank", False):
            ix.rank_if_due()  # kept unless the link graph can have changed (or a week passed)
    except Exception:
        ix.finish()  # rank did not rebuild the trie: what was indexed still reaches it
        raise
    ix.finish()  # nothing to do when rank just rebuilt the trie
    # Built again only when the generations it reads changed since it was built (a refine
    # that changed nothing ran spell for an hour, 2026-11-05 deep dive), compared with what it
    # was built from, not with this run: a failed spell run is tried again the next night
    # (2026-11-06 review).
    if not getattr(args, "no_spell", False):
        ix.spell_if_due()
    if _full_build(args):  # rank, suggest and spell all ran (or were not due): fresh again
        _shared_phases_ran(cfg, ix.root)
    return _index_build_summary(reports, out)


FULL_BUILD_MARK = "index-full-build.json"
"""``<state_dir>/index-full-build.json`` (local disk): the last nightly build, written by the build.

``started`` and ``pid`` when it has its locks; ``ended`` and ``exit`` (the exit code, or the exception's
class name) on every way out that Python sees. A build that was killed (a shutdown, SIGTERM, an OOM
kill, a power loss) writes nothing more, so ``started`` without ``ended`` means "cut short":
``tools/lanes/resume-after-reboot.sh`` starts such a build again, once, instead of leaving it to the
next night (2026-11-10: killed at 04:59 by a shutdown, nothing ran until 04:43 the next day; the
timer's ``Persistent=true`` only replays a run that never started). One that failed by itself has
``ended`` and is not started again: a failure that repeats must not loop. ``last_ok`` is the end of
the last build that exited 0 and ``since`` the first time a build was marked at all; ``tools/wsp-stall``
raises ``index-missed`` from them when no build finished cleanly for 36 hours."""


@dataclass
class _BuildMark:
    exit: int = 0


def _utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _mark_full_build(path: Path, doc: Mapping[str, object]) -> None:
    """Replace the mark in one step. It must never fail a build: an error is only reported."""
    tmp = path.with_name(path.name + ".partial")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(dict(doc), fh, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except OSError as exc:
        print(f"warning: {path.name} not written ({type(exc).__name__}: {exc})", file=sys.stderr)
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)


@contextlib.contextmanager
def _full_build_marked(cfg: Mapping[str, Any]) -> Generator[_BuildMark]:
    """Mark the nightly build's start now and its end on the way out (:data:`FULL_BUILD_MARK`)."""
    mark = _BuildMark()
    state_dir = bootstrap.paths(cfg).state_dir
    if state_dir is None:
        yield mark
        return
    path = state_dir / FULL_BUILD_MARK
    before: dict[str, object] = {}
    with contextlib.suppress(OSError, ValueError):
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            before = cast(dict[str, object], loaded)
    doc: dict[str, object] = {"started": _utc_now(), "pid": os.getpid()}
    doc["since"] = before["since"] if isinstance(before.get("since"), str) else doc["started"]
    if isinstance(before.get("last_ok"), str):
        doc["last_ok"] = before["last_ok"]
    _mark_full_build(path, doc)
    result: int | str = "BaseException"
    try:
        yield mark
        result = mark.exit
    except BaseException as exc:
        result = type(exc).__name__
        raise
    finally:
        doc["ended"] = _utc_now()
        doc["exit"] = result
        if result == 0:
            doc["last_ok"] = doc["ended"]
        _mark_full_build(path, doc)


SHARED_SKIPPED_FILE = "shared-phases-skipped.json"
SHARED_SKIPPED_ALERT = "index:shared-phases-skipped"
SHARED_SKIPPED_NIGHTS = 2  # the nights in a row that raise the alert


def _full_build(args: argparse.Namespace) -> bool:
    """The nightly build: every source, and rank, suggest and spell after it. Only it counts the days
    the shared phases were skipped, or clears them: a lane's ``--source X --no-rank --no-spell`` (or a
    reindex, a refine-again) neither ran them nor skipped them for the library (2026-11-06 review)."""
    return not any(
        getattr(args, flag, False)
        for flag in ("source", "reindex", "refine_again", "no_rank", "no_spell", "no_suggest")
    )


def _shared_phases_skipped(cfg: Mapping[str, Any], root: Path, failed: Sequence[str], out: TextIO) -> None:
    """Count the days on which a failed source kept rank, suggest and spell from being rebuilt
    (``<index>/shared-phases-skipped.json``); from the second in a row, an alert: each night's failure
    exits 1, but a source that keeps failing would leave them stale for good (2026-11-06 review).
    Best effort: never fails the build."""
    path = root / SHARED_SKIPPED_FILE
    today = datetime.now(UTC).date().isoformat()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        days = [str(d) for d in data.get("days", [])]
    except (OSError, ValueError, AttributeError):
        days = []
    days = sorted({*days, today})[-60:]  # since the shared phases last ran (bounded)
    try:
        tmp = path.with_name(f"{path.name}.{os.getpid()}.partial")
        tmp.write_text(json.dumps({"days": days, "failed": list(failed)}) + "\n", encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        out.write(f"warning: cannot record the skipped phases in {path}: {exc}\n")
    if len(days) < SHARED_SKIPPED_NIGHTS:
        return
    message = (
        f"rank, suggest and spell not rebuilt for {len(days)} days (since {days[0]}): "
        f"{', '.join(failed)} keep failing in `wsp index build`"
    )
    out.write(f"ALERT: {message}\n")
    # An alert already open keeps its first text (the store does not rewrite an open alert, and
    # resolving to raise it again would notify every night); the count of days is in this run's
    # output and in the counter file.
    _alert(cfg, SHARED_SKIPPED_ALERT, message)


def _shared_phases_ran(cfg: Mapping[str, Any], root: Path) -> None:
    """Clear the count and resolve the alert, unconditionally: the counter file can be gone (removed
    by hand, an index root rebuilt) while the alert is still open."""
    with contextlib.suppress(OSError):
        (root / SHARED_SKIPPED_FILE).unlink(missing_ok=True)
    _alert(cfg, SHARED_SKIPPED_ALERT, None)


def _alert(cfg: Mapping[str, Any], key: str, message: str | None) -> None:
    """Raise (``message``) or resolve (None) an orchestrator alert; best effort."""
    try:
        from wsp_cli.notify import _alerter  # pyright: ignore[reportPrivateUsage]
        from wsp_orchestrator.alerts import Alert, Severity

        pair = _alerter(cfg)
        if pair is None:
            return
        alerter, store = pair
        try:
            if message is None:
                alerter.resolve(key)
            else:
                alerter.raise_(Alert(key, Severity.WARNING, message))
        finally:
            store.close()
    except Exception as exc:  # an alert that cannot be raised never fails the build
        print(f"warning: alert {key}: {exc}", file=sys.stderr)


def _index_build_summary(reports: Sequence[SourceReport], out: TextIO) -> int:
    """Write what each source's build did; 1 when one failed (the alert fires), else 0."""
    for r in reports:
        left_out = f"; {r.left_out:,} left out (invalid ids)" if r.left_out else ""
        if r.error:
            out.write(f"{r.source_id}: FAILED ({r.error})\n")
        elif r.skipped:
            out.write(f"{r.source_id}: skipped ({r.skipped}){left_out}\n")
        else:
            refined = ", ".join(f"{g} ({n:,} docs)" for g, n in r.refined) or "nothing new"
            out.write(f"{r.source_id}: refined {refined}; indexed {r.indexed}{left_out}\n")
    if not reports:
        out.write("nothing to index: no sealed generations\n")
    # A source that failed did not stop the others (2026-11-06), but the run is not a success: the
    # exit code still raises the alert.
    if failed := [r.source_id for r in reports if r.error]:
        out.write(f"index build: {len(failed)} source(s) failed: {', '.join(failed)}\n")
        return 1
    return 0


def cmd_index_catalog(args: argparse.Namespace, out: TextIO) -> int:
    """Rebuild only the portal/directory/random catalog of already indexed sources."""
    from wsp_index.refinery import current_generation

    lay, cfg, specs = _context(args)
    built = 0
    try:
        with _index_writer(cfg), open_indexer(cfg, lay, specs, out) as ix:
            for spec in specs:
                if args.source and spec.id != args.source:
                    continue
                gen = current_generation(ix.refinery, spec.id)
                if gen is None:
                    continue
                built += ix.build_catalog(spec, gen) is not None
    except LockHeldError as exc:
        return _busy(exc)
    out.write(f"catalog: {built} source(s) rebuilt\n")
    return 0


def cmd_index_rank(args: argparse.Namespace, out: TextIO) -> int:
    """Recompute only the link-analysis static rank (PageRank) and what depends on it."""
    from wsp_index.linkrank import RankConfig

    lay, cfg, specs = _context(args)
    rc = RankConfig(damping=args.damping, mix=args.mix)
    try:
        with _index_writer(cfg), open_indexer(cfg, lay, specs, out) as ix:
            stats = ix.rank(rc, reuse=args.reuse, full_scan=bool(getattr(args, "full_scan", False)))
    except LockHeldError as exc:
        return _busy(exc)
    if stats is None and not args.reuse:
        out.write("rank: nothing indexed\n")
    return 0


def cmd_index_spell(args: argparse.Namespace, out: TextIO) -> int:
    """Rebuild only the "did you mean" vocabulary from the indexed Refinery generations."""
    lay, cfg, specs = _context(args)
    try:
        with _index_writer(cfg), open_indexer(cfg, lay, specs, out) as ix:
            stats = ix.spell()
    except LockHeldError as exc:
        return _busy(exc)
    if stats is None:
        out.write("spell: nothing indexed\n")
    return 0


def cmd_index_archive_changes(args: argparse.Namespace, out: TextIO) -> int:
    """Move old change-feed ranges into the sealed archive (``[index] changes_archive``, ADR-0011)."""
    _, cfg, _ = _context(args)
    sec = bootstrap.section(cfg, "index")
    if bootstrap.paths(cfg).changes_archive is None:
        out.write("archive-changes: [index] changes_archive is not set\n")
        return 2
    try:
        with _index_writer(cfg):
            feed = bootstrap.open_changes(cfg)
            try:
                report = feed.archive_old(
                    keep_rows=int(sec.get("changes_keep_rows", 2_000_000)),
                    keep_per_source=int(sec.get("changes_keep_per_source", 1000)),
                    say=lambda line: out.write(line + "\n"),
                )
                out.write(
                    f"archive-changes: {report.ranges} range(s) sealed, {report.deleted:,} rows removed, "
                    f"archived through {report.archived_through:,}\n"
                )
                if args.compact:
                    out.write("archive-changes: compacting the database (rewrites it once)\n")
                    feed.compact()
            finally:
                feed.close()
    except LockHeldError as exc:
        return _busy(exc)
    return 0


@contextlib.contextmanager
def _index_writer_waiting(
    cfg: Mapping[str, Any], out: TextIO, *, every: float = 60.0, limit: float = 12 * 3600.0
) -> Generator[None]:
    """:func:`_index_writer`, waiting for a holder to finish instead of failing at once (a day-long
    rebuild must not give up at its last step because the nightly job holds the lock)."""
    deadline = time.monotonic() + limit
    stack = contextlib.ExitStack()
    while True:
        try:
            stack.enter_context(_index_writer(cfg))
            break
        except LockHeldError as exc:
            if time.monotonic() >= deadline:
                raise
            out.write(f"waiting for the index lock ({exc})\n")
            out.flush()
            time.sleep(every)
    with stack:
        yield


MERGE_TOOL = (
    Path(__file__).resolve().parents[4] / "tools" / "tantivy-merge" / "target" / "release" / "tantivy-merge"
)


def cmd_index_merge(args: argparse.Namespace, out: TextIO) -> int:
    """Merge the search index's segments into fewer (``tools/tantivy-merge``), holding the index-writer
    locks so no refine, release index step or nightly build writes meanwhile. Searches go on: readers
    switch to the merged segments when ``meta.json`` changes."""
    import subprocess

    tool = Path(args.tool) if args.tool else MERGE_TOOL
    if not tool.is_file():
        out.write(f"error: {tool} is not built; run: cd tools/tantivy-merge && cargo build --release\n")
        return 2
    _, cfg, _ = _context(args)
    directory = bootstrap.paths(cfg).lexical / "tantivy"
    argv = [str(tool), str(directory), "--target-docs", str(args.target_docs)]
    if args.dry_run:
        return subprocess.run([*argv, "--dry-run"], check=False).returncode  # noqa: S603 - our own tool
    try:
        with _index_writer_waiting(cfg, out, limit=args.wait_hours * 3600.0):
            out.write(f"merging {directory}\n")
            out.flush()
            return subprocess.run(argv, check=False).returncode  # noqa: S603 - our own tool
    except LockHeldError as exc:
        return _busy(exc)


def cmd_index_rebuild_lexical(args: argparse.Namespace, out: TextIO) -> int:
    """Build a fresh search index in ``--into`` from the indexed generations (``wsp_cli.relex``)."""
    from wsp_cli.relex import Relexer, relex
    from wsp_index import TantivyConfig, TantivyIndex
    from wsp_index.lexical import point_tantivy, tantivy_dir
    from wsp_vault.ratelimit import RateLimiter

    _, cfg, specs = _context(args)
    p = bootstrap.paths(cfg)
    into = Path(args.into).expanduser().resolve()
    if into == p.lexical.resolve():
        out.write("rebuild-lexical: --into must not be the live lexical directory\n")
        return 2
    into.mkdir(parents=True, exist_ok=True)
    if args.tantivy_dir:
        target = Path(args.tantivy_dir).expanduser().resolve()
        if tantivy_dir(into) not in (into / "tantivy", target):
            out.write(f"rebuild-lexical: {into} already builds into {tantivy_dir(into)}\n")
            return 2
        point_tantivy(into, target)
    config = TantivyConfig(
        languages=bootstrap.index_languages(cfg), source_priors=bootstrap.source_priors(cfg, specs)
    )
    state_path = p.require_index() / "index-state.json"

    def state() -> dict[str, str]:
        return dict(json.loads(state_path.read_text(encoding="utf-8"))) if state_path.is_file() else {}

    def say(line: str) -> None:
        out.write(line + "\n")
        out.flush()

    index = TantivyIndex(into, config, titles=lambda: bootstrap.indexed_titles(cfg))
    try:
        limit = float(args.read_mb_per_sec)
        limiter = RateLimiter(limit * 1e6, burst_seconds=5.0) if limit > 0 else None
        relexer = Relexer(index, p.refinery, state, say=say, read_limiter=limiter)
        report = relex(relexer, p.lexical, lambda: _index_writer_waiting(cfg, out))
    except LockHeldError as exc:
        return _busy(exc)
    finally:
        index.close()
    out.write(
        f"rebuild-lexical: {report.sources} sources, {report.documents:,} documents loaded, "
        f"caught up: {', '.join(report.caught_up) or 'nothing'}\n"
    )
    if report.behind:
        out.write(
            f"NOT ready to switch: {', '.join(report.behind)} still behind the live index (see the "
            "lines above); run rebuild-lexical again (it resumes) once their generations are readable.\n"
        )
        return 1
    out.write("Next: docs/runbooks/move-to-nas.md, 'Switch to the rebuilt search index'.\n")
    return 0


def cmd_index_point_tantivy(args: argparse.Namespace, out: TextIO) -> int:
    """Make a verified copy of a lexical directory's Tantivy index the one it uses (e.g. on the NAS)."""
    import tantivy

    from wsp_index.lexical import point_tantivy, tantivy_dir

    lexical = Path(args.lexical).expanduser().resolve()
    target = Path(args.target).expanduser().resolve()
    current = tantivy_dir(lexical)
    for path in (current, target):
        if not (path / "meta.json").is_file():
            out.write(f"point-tantivy: {path} is not a Tantivy index\n")
            return 2
    if (current / "meta.json").read_bytes() != (target / "meta.json").read_bytes():
        out.write(f"point-tantivy: {target} is not a copy of {current} (meta.json differs)\n")
        return 2
    mine = {f.name: f.stat().st_size for f in current.iterdir() if f.is_file() and not f.name.startswith(".")}
    theirs = {
        f.name: f.stat().st_size for f in target.iterdir() if f.is_file() and not f.name.startswith(".")
    }
    if mine != theirs:
        out.write(f"point-tantivy: {target} does not hold the same files as {current}\n")
        return 2
    tantivy.Index.open(str(target))  # opens: segments and schema readable
    point_tantivy(lexical, target)
    out.write(f"point-tantivy: {lexical} now uses {target} (the old copy at {current} can go)\n")
    return 0


# --- warden ----------------------------------------------------------------------------------------------


def cmd_scrub(args: argparse.Namespace, out: TextIO) -> int:
    """One scrub pass (the ``SCRUB`` lock, shared with the orchestrator's slices). Exit 1 on findings."""
    lay, cfg, specs = _context(args)
    try:
        with exclusive(_lock_dir(cfg), SCRUB):
            return _scrub(cfg, lay, specs, args, out)
    except LockHeldError as exc:
        return _busy(exc)


def _scrub(
    cfg: Mapping[str, Any],
    lay: bootstrap.Layout,
    specs: Sequence[SourceSpec],
    args: argparse.Namespace,
    out: TextIO,
) -> int:
    from wsp_vault.repair import RepairCircuitOpenError
    from wsp_vault.vault import KeyPinError

    vault = _vault(cfg, lay, specs, writable=True)
    budget = int(float(args.budget_gb) * 1e9) if args.budget_gb is not None else None
    report = scrubber(cfg, vault).run(max_bytes=budget)
    if report.aborted is not None:
        print(f"error: scrub stopped, the vault is unavailable: {report.aborted}", file=sys.stderr)
        return EXIT_UNAVAILABLE
    out.write(
        f"scrub: {report.files_checked} file(s), {report.bytes_hashed / 1e9:.2f} GB hashed, "
        f"{len(report.findings)} finding(s), {'pass complete' if report.complete else 'budget reached'}\n"
    )
    for f in report.findings:
        out.write(
            f"  {f.source_id}/{f.generation_id} {f.relpath or '(manifest)'}: {f.status.value} {f.detail}\n"
        )
    repairable = [f for f in report.findings if not f.unreadable]  # I/O errors are never "repaired"
    if repairable:
        try:
            results = repair_ladder(cfg, vault).repair_findings(repairable)
        except (RepairCircuitOpenError, KeyPinError) as exc:
            print(f"CRITICAL: repair stopped, nothing was changed: {exc}", file=sys.stderr)
            return 2
        for res in results:
            state = "DEGRADED" if res.degraded else "repaired"
            out.write(f"  repair {res.source_id}/{res.generation_id}: {state} {dict(res.repaired)}\n")
            if res.rolled_back_to:
                out.write(f"    CURRENT rolled back to {res.rolled_back_to}\n")
    return 1 if report.findings else 0


def cmd_undegrade(args: argparse.Namespace, out: TextIO) -> int:
    """Fully re-verify a DEGRADED generation; clear the flag only if every file is intact."""
    from wsp_vault.undegrade import reverify_and_clear

    lay, cfg, specs = _context(args)
    try:
        with exclusive(_lock_dir(cfg), SCRUB):
            vault = _vault(cfg, lay, specs, writable=True)
            res = reverify_and_clear(vault, scrubber(cfg, vault), args.source, args.generation)
    except LockHeldError as exc:
        return _busy(exc)
    out.write(
        f"{args.source}/{args.generation}: {'cleared' if res.cleared else 'not cleared'} ({res.detail})\n"
    )
    return 0 if res.cleared else 1


def cmd_ledger_recover(args: argparse.Namespace, out: TextIO) -> int:
    """Cut a torn ledger day back to its last valid hash-chained line (cut bytes are kept as .torn)."""
    from wsp_vault.ledger import JsonlLedger

    lay, cfg, specs = _context(args)
    try:
        with exclusive(_lock_dir(cfg), INGEST):  # no ingest appends while the owner repairs the ledger
            vault = _vault(cfg, lay, specs, writable=True)
            ledger = vault.ledger
            if not isinstance(ledger, JsonlLedger):
                print("error: this vault has no recoverable ledger", file=sys.stderr)
                return 2
            event = ledger.recover(reason=args.note or "owner-requested recovery")
    except LockHeldError as exc:
        return _busy(exc)
    out.write("ledger: nothing to cut\n" if event is None else f"ledger: recovered ({event.data})\n")
    return 0


def cmd_drill(args: argparse.Namespace, out: TextIO) -> int:
    """One chaos drill (the ``DRILL`` lock, shared with the orchestrator). Exit 1 if it failed."""
    lay, cfg, specs = _context(args)
    try:
        with exclusive(_lock_dir(cfg), DRILL):
            vault = _vault(cfg, lay, specs, writable=True)
            kind = DrillKind(args.kind) if args.kind else None
            result = drill_once(cfg, vault, kind=kind, source_id=args.source)
    except LockHeldError as exc:
        return _busy(exc)
    out.write(json.dumps(result, indent=2) + "\n")
    return 0 if result.get("ok") else 1


# --- owner actions -----------------------------------------------------------------------------------------


@contextlib.contextmanager
def open_orchestrator(cfg: Mapping[str, Any]) -> Generator[Orchestrator]:
    from wsp_cli.wiring import build_wiring

    orch = Orchestrator(OrchestratorSettings.from_config(cfg), build_wiring(cfg))
    try:
        yield orch
    finally:
        orch.close()


def _held_version(orch: Orchestrator, source_id: str) -> str | None:
    """The newest release of the source that waits for the owner (natural version order, M8)."""
    held = [
        r.version
        for r in orch.store.releases(source_id=source_id)
        if r.hold_reason is not None and r.state.value not in ("promoted", "quarantined")
    ]
    return max(held, key=version_key, default=None)


def cmd_owner(args: argparse.Namespace, out: TextIO) -> int:
    _, cfg, _ = _context(args)
    with open_orchestrator(cfg) as orch:
        action: str = args.owner_action
        if action == "resume":
            orch.resume_source(args.source)
            out.write(f"{args.source}: resumed; discovery queued\n")
            return 0
        version = args.version or _held_version(orch, args.source)
        if version is None:
            print(f"error: {args.source} has no release held for review; give a version", file=sys.stderr)
            return 2
        try:
            if action == "approve":
                force = bool(getattr(args, "force", False))
                orch.approve_release(args.source, version, note=args.note or "", force=force)
                out.write(f"{args.source}/{version}: approved; promotion queued\n")
            elif action == "retry":
                orch.retry_release(args.source, version, note=args.note or "")
                out.write(f"{args.source}/{version}: queued for a fresh attempt\n")
                if orch.store.source_health(args.source).paused:
                    # A paused source runs no release step: the retry waits until it is resumed
                    # (2026-10-14: a retried quarantine sat behind its own integrity pause).
                    out.write(
                        f"note: {args.source} is paused, so the attempt waits; run "
                        f"`wsp resume {args.source}` to let it go\n"
                    )
            else:
                orch.reject_release(args.source, version, reason=args.note or "rejected by owner")
                out.write(f"{args.source}/{version}: rejected (quarantined)\n")
        except (IllegalTransitionError, ConcurrentUpdateError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
    return 0


# --- status -----------------------------------------------------------------------------------------------


def cmd_status(args: argparse.Namespace, out: TextIO) -> int:
    lay, cfg, specs = _context(args)
    p = bootstrap.paths(cfg)
    vault_ok = p.vault_root is not None and p.vault_root.is_dir()
    out.write(f"vault:  {p.vault_root} {'(ok)' if vault_ok else '(not found)'}\n")
    out.write(f"index:  {p.index_root}\n")
    degraded: list[str] = []
    if vault_ok:
        vault = _vault(cfg, lay, specs, writable=False)
        out.write(f"\n{'source':<28} {'status':<10} {'current':<12} {'generations':>11}  size\n")
        for sid in sorted({s.id for s in specs} | set(vault.sources())):
            spec = next((s for s in specs if s.id == sid), None)
            try:
                current = vault.current(sid)
            except Exception as exc:  # a broken pointer is itself worth reporting
                out.write(f"{sid:<28} error: {exc}\n")
                continue
            gens = vault.generations(sid)
            if not gens and (spec is None or spec.status.value in ("candidate", "excluded")):
                continue
            degraded += [f"{sid}/{g.generation_id}" for g in gens if g.status is GenerationStatus.DEGRADED]
            size = sum(a.size for a in current.artifacts) if current else 0
            status = spec.status.value if spec else "unlisted"
            cur = current.generation_id if current else "-"
            out.write(f"{sid:<28} {status:<10} {cur:<12} {len(gens):>11}  {size / 1e9:,.1f} GB\n")
        staged = sum(
            1
            for sid in vault.sources()
            for g in vault.generations(sid)
            if g.status is GenerationStatus.STAGING
        )
        out.write(
            f"\ndegraded: {', '.join(degraded) if degraded else 'none'}\nstaging: {staged} generation(s)\n"
        )
    state_db = Path(str(bootstrap.section(cfg, "orchestrator").get("state_db", "")))
    if state_db.is_file():
        store = StateStore(state_db, check_local=False)
        try:
            depth = Counter({f"{k}:{s}": n for (k, s), n in _queue_depth(store).items()})
            releases = Counter(r.state.value for r in store.releases())
            alerts = store.open_alerts()
        finally:
            store.close()
        out.write(f"queue: {dict(depth) or 'empty'}\nreleases: {dict(releases) or 'none'}\n")
        out.write(f"open alerts: {len(alerts)}\n")
    else:
        out.write("orchestrator: no state database yet\n")
    return 1 if degraded else 0


def _queue_depth(store: StateStore) -> dict[tuple[str, str], int]:
    rows = store.query(
        "SELECT kind, status, COUNT(*) AS n FROM jobs"
        " WHERE status IN ('pending', 'running') GROUP BY kind, status"
    )
    return {(str(r["kind"]), str(r["status"])): int(r["n"]) for r in rows}


# --- curator proposals -------------------------------------------------------------------------------


def _proposal_files(cfg: Mapping[str, Any]) -> ProposalFiles:
    return ProposalFiles(bootstrap.paths(cfg).proposals)


def _mark_state(cfg: Mapping[str, Any], proposal_id: int, status: str) -> None:
    raw = str(bootstrap.section(cfg, "orchestrator").get("state_db") or "")
    state_db = Path(raw).expanduser() if raw else None
    if state_db is not None and state_db.is_file():
        store = StateStore(state_db, check_local=False)
        try:
            store.set_proposal_status(proposal_id, status)
        finally:
            store.close()


def cmd_proposals_list(args: argparse.Namespace, out: TextIO) -> int:
    _, cfg, _ = _context(args)
    docs = _proposal_files(cfg).list(status=args.status)
    if args.json:
        out.write(json.dumps(docs, indent=2, default=str) + "\n")
        return 0
    if not docs:
        out.write("no proposals\n")
    for d in sorted(docs, key=lambda d: int(d.get("id", 0)), reverse=True):
        score = d.get("score")
        out.write(
            f"{int(d.get('id', 0)):>5}  {d.get('status')!s:<9} {d.get('decision')!s:<13}"
            f" {'-' if score is None else f'{float(score):.2f}':>5}  {d.get('title')}\n"
        )
    return 0


def cmd_proposals_show(args: argparse.Namespace, out: TextIO) -> int:
    _, cfg, _ = _context(args)
    files = _proposal_files(cfg)
    path = files.find(args.id)
    if path is None:
        print(f"error: no proposal {args.id}", file=sys.stderr)
        return 2
    out.write(path.read_text(encoding="utf-8"))
    return 0


def cmd_proposals_decide(args: argparse.Namespace, out: TextIO) -> int:
    """Accept (write the drafted entry into registry/sources.yaml) or reject a proposal."""
    lay, cfg, specs = _context(args)
    files = _proposal_files(cfg)
    path = files.find(args.id)
    if path is None:
        print(f"error: no proposal {args.id}", file=sys.stderr)
        return 2
    doc = files.load(path)
    if doc.get("status") in ("accepted", "rejected"):
        print(f"error: proposal {args.id} is already {doc.get('status')}", file=sys.stderr)
        return 2
    now = datetime.now(UTC)
    chosen = cast(list[str] | None, getattr(args, "only" if args.decision == "accept" else "deny", None))
    if doc.get("doc_review"):  # per-document review (doc-collection, D13): decisions per row
        if lay.root is None:
            print("error: run document reviews from inside the Whisipedia checkout", file=sys.stderr)
            return 2
        if args.decision == "accept" or chosen:
            try:
                decided = doccollections.apply_review_decision(
                    lay.root,
                    specs,
                    doc,
                    allow=args.decision == "accept",
                    keys=chosen,
                    proposal=args.id,
                    at=now,
                )
            except doccollections.ReviewDecisionError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 2
            verb = "allowed" if args.decision == "accept" else "denied"
            out.write(f"{decided.path}: {verb} {len(decided.keys)} document(s); review and commit it\n")
        else:
            out.write("closing the review without a decision on any document (deny rows with --deny KEY)\n")
    elif chosen:
        print("error: --only/--deny apply to document reviews only", file=sys.stderr)
        return 2
    if args.decision == "reject":
        files.decide(args.id, "rejected", at=now, note=args.note)
        _mark_state(cfg, args.id, "rejected")
        out.write(f"proposal {args.id}: rejected\n")
        return 0
    candidate = cast(dict[str, Any], doc.get("candidate") or {})
    entry = cast(dict[str, Any], candidate.get("registry_entry") or {})
    if entry.get("id"):
        registry = Path(str(bootstrap.section(cfg, "registry").get("path", lay.registry)))
        text = registry.read_text(encoding="utf-8")
        updated = apply_to_registry_text(text, entry)
        tmp = registry.with_name(registry.name + ".partial")
        tmp.write_text(updated, encoding="utf-8")
        try:
            load_registry(tmp, lay.schema)
        except RegistryError as exc:
            tmp.unlink()
            print(f"error: the drafted entry does not validate: {exc}", file=sys.stderr)
            return 1
        tmp.replace(registry)
        removed = remove_from_overlay(bootstrap.overlay_path(cfg, lay), str(entry["id"]))
        moved = " (moved from the overlay)" if removed else ""
        out.write(f"registry/sources.yaml: wrote entry {entry['id']!r}{moved}\n")
        out.write("review and commit:  git diff registry/sources.yaml\n")
    else:
        out.write("nothing to write to the registry (a task for the owner); marked accepted\n")
    files.decide(args.id, "accepted", at=now, note=args.note)
    _mark_state(cfg, args.id, "accepted")
    out.write(f"proposal {args.id}: accepted\n")
    return 0


# --- services and doctor ----------------------------------------------------------------------------


def _services(args: argparse.Namespace) -> ServiceManager:
    lay = bootstrap.layout(args.config_dir)
    if lay.root is None:
        raise bootstrap.ConfigError("run `wsp service` from inside the Whisipedia checkout")
    target = Path(args.unit_dir) if args.unit_dir else unit_dir()
    return ServiceManager(repo=lay.root, config_dir=lay.config_dir.resolve(), target=target)


def cmd_service(args: argparse.Namespace, out: TextIO) -> int:
    mgr = _services(args)
    match args.service_command:
        case "install":
            return mgr.install(out, enable=not args.no_enable, now=args.now, dry_run=args.dry_run)
        case "uninstall":
            return mgr.uninstall(out, dry_run=args.dry_run)
        case _:
            return mgr.status(out)


def cmd_doctor(args: argparse.Namespace, out: TextIO) -> int:
    lay = bootstrap.layout(args.config_dir)
    cfg = bootstrap.load(lay)
    require = list(args.require or [])
    checks = Doctor(cfg, lay).checks(require or None)
    if not args.quiet:
        render_doctor(checks, out)
    else:
        for c in checks:
            if c.level is not Level.OK:
                print(f"{c.level.value}: {c.title}: {c.detail}", file=sys.stderr)
    return doctor_exit(checks, require)


def cmd_parity_status(args: argparse.Namespace, out: TextIO) -> int:
    """Parity coverage of sealed generations (reads manifests; never writes)."""
    from wsp_vault.paritykeeper import ParityKeeper

    lay, cfg, specs = _context(args)
    vault = _vault(cfg, lay, specs, writable=False)
    p = bootstrap.paths(cfg)
    local = p.state_dir or p.require_index()
    keeper = ParityKeeper(vault, None, state_dir=local, scratch=p.workdir or local)
    scope = str(bootstrap.section(cfg, "vault").get("parity_scope", "all"))
    targets = parity_targets(vault, specs, scope, None)
    cov = keeper.coverage(targets)
    out.write(
        f"parity ({scope}): {cov.covered}/{cov.generations} generations, "
        f"{cov.bytes_covered / 1e12:.2f}/{cov.bytes_total / 1e12:.2f} TB ({cov.fraction:.1%}); "
        f"states {cov.states}\n"
    )
    for subject in cov.uncovered[:50]:
        out.write(f"  missing: {subject}\n")
    return 0
