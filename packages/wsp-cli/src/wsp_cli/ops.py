"""Operator commands that act on the real library: ingest, index build, scrub, drill, approve/reject/resume,
status. Each builds its components through :mod:`wsp_cli.bootstrap` and prints a plain-text report."""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
from collections import Counter
from collections.abc import Generator, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO, cast

from wsp_cli import bootstrap, doccollections
from wsp_cli.doctor import Doctor, Level
from wsp_cli.doctor import exit_code as doctor_exit
from wsp_cli.doctor import render as render_doctor
from wsp_cli.indexer import Indexer
from wsp_cli.ingest import Ingestor, VersionStatus, render
from wsp_cli.locks import LockHeldError, exclusive
from wsp_cli.service import ServiceManager, unit_dir
from wsp_cli.wiring import drill_once, parity_targets, repair_ladder, scrubber
from wsp_core.model import GenerationStatus, SourceSpec
from wsp_core.ports import Fetcher
from wsp_core.registry import RegistryError, load_registry
from wsp_index.spelling import SPELL_FILE
from wsp_orchestrator.curator import ProposalFiles
from wsp_orchestrator.curator.registry_edit import apply_to_registry_text, remove_from_overlay
from wsp_orchestrator.orchestrator import Orchestrator
from wsp_orchestrator.settings import OrchestratorSettings
from wsp_orchestrator.store import StateStore
from wsp_vault.drill import DrillKind
from wsp_vault.vault import FileVault

EXIT_INCOMPLETE = 4  # `wsp ingest`: some version still has files missing or downloading


def _context(args: argparse.Namespace) -> tuple[bootstrap.Layout, dict[str, Any], tuple[SourceSpec, ...]]:
    lay = bootstrap.layout(args.config_dir)
    cfg = bootstrap.load(lay)
    return lay, cfg, tuple(bootstrap.load_sources(cfg, lay))


def _lock_dir(cfg: Mapping[str, Any]) -> Path:
    """Local-disk lock directory, next to the index root."""
    return bootstrap.paths(cfg).require_index().parent / "locks"


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
        with exclusive(_lock_dir(cfg), "ingest"):
            return _ingest(args, out, lay, cfg, specs)
    except LockHeldError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3


def _ingest(
    args: argparse.Namespace,
    out: TextIO,
    lay: bootstrap.Layout,
    cfg: dict[str, Any],
    specs: tuple[SourceSpec, ...],
) -> int:
    dry: bool = args.dry_run
    vault = _vault(cfg, lay, specs, writable=not dry)
    fetcher: Fetcher | None = None if args.offline else bootstrap.build_fetcher(cfg)
    ing = Ingestor(
        vault,
        specs,
        fetcher=fetcher,
        out=None if args.json else out,
        user_agent=bootstrap.user_agent(cfg),
        settle_seconds=float(args.settle_seconds),
    )
    try:
        if args.allow_mirror:
            if args.source != "gutenberg":
                print("error: --allow-mirror needs --source gutenberg", file=sys.stderr)
                return 2
            vp = ing.ingest_mirror(dry_run=dry)
            out.write(
                f"{vp.source_id}/{vp.generation_id}: {vp.status.value} {len(vp.files)} files {vp.reason}\n"
            )
            return 1 if vp.status is VersionStatus.FAILED else 0
        report = ing.run(source_id=args.source, dry_run=dry)
    finally:
        close = getattr(fetcher, "close", None)
        if callable(close):
            close()
    if args.json:
        out.write(json.dumps(report.to_json(), indent=2) + "\n")
    else:
        render(report, out)
    if any(v.status is VersionStatus.FAILED for v in report.versions):
        return 1
    # Not an error, but not done either: scripts must not mistake it for success (exit 0).
    return EXIT_INCOMPLETE if any(v.status is VersionStatus.INCOMPLETE for v in report.versions) else 0


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
    )
    try:
        yield ix
    finally:
        ix.close()


def cmd_index_build(args: argparse.Namespace, out: TextIO) -> int:
    lay, cfg, specs = _context(args)
    try:
        with exclusive(_lock_dir(cfg), "index"), open_indexer(cfg, lay, specs, out) as ix:
            reports = ix.build([args.source] if args.source else None)
            changed = any(r.upserted or r.deleted or r.refined for r in reports)
            if not getattr(args, "no_rank", False) and (changed or not ix.index.rank_path.is_file()):
                ix.rank()
            if changed or not (ix.index.path / SPELL_FILE).is_file():
                ix.spell()
    except LockHeldError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3
    for r in reports:
        if r.skipped:
            out.write(f"{r.source_id}: skipped ({r.skipped})\n")
        else:
            refined = ", ".join(f"{g} ({n:,} docs)" for g, n in r.refined) or "nothing new"
            out.write(f"{r.source_id}: refined {refined}; indexed {r.indexed}\n")
    if not reports:
        out.write("nothing to index: no sealed generations\n")
    return 0


def cmd_index_catalog(args: argparse.Namespace, out: TextIO) -> int:
    """Rebuild only the portal/directory/random catalog of already indexed sources."""
    from wsp_index.refinery import current_generation

    lay, cfg, specs = _context(args)
    built = 0
    with open_indexer(cfg, lay, specs, out) as ix:
        for spec in specs:
            if args.source and spec.id != args.source:
                continue
            gen = current_generation(ix.refinery, spec.id)
            if gen is None:
                continue
            built += ix.build_catalog(spec, gen) is not None
    out.write(f"catalog: {built} source(s) rebuilt\n")
    return 0


def cmd_index_rank(args: argparse.Namespace, out: TextIO) -> int:
    """Recompute only the link-analysis static rank (PageRank) and what depends on it."""
    from wsp_index.linkrank import RankConfig

    lay, cfg, specs = _context(args)
    rc = RankConfig(damping=args.damping, mix=args.mix)
    try:
        with exclusive(_lock_dir(cfg), "index"), open_indexer(cfg, lay, specs, out) as ix:
            stats = ix.rank(rc)
    except LockHeldError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3
    if stats is None:
        out.write("rank: nothing indexed\n")
    return 0


def cmd_index_spell(args: argparse.Namespace, out: TextIO) -> int:
    """Rebuild only the "did you mean" vocabulary from the indexed Refinery generations."""
    lay, cfg, specs = _context(args)
    try:
        with exclusive(_lock_dir(cfg), "index"), open_indexer(cfg, lay, specs, out) as ix:
            stats = ix.spell()
    except LockHeldError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3
    if stats is None:
        out.write("spell: nothing indexed\n")
    return 0


# --- warden ----------------------------------------------------------------------------------------------


def cmd_scrub(args: argparse.Namespace, out: TextIO) -> int:
    lay, cfg, specs = _context(args)
    vault = _vault(cfg, lay, specs, writable=True)
    budget = int(float(args.budget_gb) * 1e9) if args.budget_gb is not None else None
    report = scrubber(cfg, vault).run(max_bytes=budget)
    out.write(
        f"scrub: {report.files_checked} file(s), {report.bytes_hashed / 1e9:.2f} GB hashed, "
        f"{len(report.findings)} finding(s), {'pass complete' if report.complete else 'budget reached'}\n"
    )
    for f in report.findings:
        out.write(
            f"  {f.source_id}/{f.generation_id} {f.relpath or '(manifest)'}: {f.status.value} {f.detail}\n"
        )
    if report.findings:
        for res in repair_ladder(cfg, vault).repair_findings(report.findings):
            state = "DEGRADED" if res.degraded else "repaired"
            out.write(f"  repair {res.source_id}/{res.generation_id}: {state} {dict(res.repaired)}\n")
            if res.rolled_back_to:
                out.write(f"    CURRENT rolled back to {res.rolled_back_to}\n")
    return 1 if report.findings else 0


def cmd_drill(args: argparse.Namespace, out: TextIO) -> int:
    lay, cfg, specs = _context(args)
    vault = _vault(cfg, lay, specs, writable=True)
    kind = DrillKind(args.kind) if args.kind else None
    result = drill_once(cfg, vault, kind=kind, source_id=args.source)
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
    held = [r for r in orch.store.releases(source_id=source_id) if r.hold_reason is not None]
    return held[-1].version if held else None


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
        if action == "approve":
            orch.approve_release(args.source, version, note=args.note or "")
            out.write(f"{args.source}/{version}: approved; promotion queued\n")
        else:
            orch.reject_release(args.source, version, reason=args.note or "rejected by owner")
            out.write(f"{args.source}/{version}: rejected (quarantined)\n")
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
    lay, cfg, _ = _context(args)
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
    if doc.get("doc_review") and lay.root is not None:  # per-document review (doc-collection, D13)
        rules = doccollections.apply_review_decision(lay.root, doc, accept=args.decision == "accept")
        out.write(f"{rules}: recorded the decision for every listed document; review and commit it\n")
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
