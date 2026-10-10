"""Operator commands that act on the real library: ingest, index build, scrub, drill, approve/reject/resume,
status. Each builds its components through :mod:`wsp_cli.bootstrap` and prints a plain-text report."""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
from collections import Counter
from collections.abc import Generator, Mapping, Sequence
from pathlib import Path
from typing import Any, TextIO

from wsp_cli import bootstrap
from wsp_cli.indexer import Indexer
from wsp_cli.ingest import Ingestor, VersionStatus, render
from wsp_cli.wiring import drill_once, repair_ladder, scrubber
from wsp_core.model import GenerationStatus, SourceSpec
from wsp_core.ports import Fetcher
from wsp_orchestrator.orchestrator import Orchestrator
from wsp_orchestrator.settings import OrchestratorSettings
from wsp_orchestrator.store import StateStore
from wsp_vault.drill import DrillKind
from wsp_vault.vault import FileVault


def _context(args: argparse.Namespace) -> tuple[bootstrap.Layout, dict[str, Any], tuple[SourceSpec, ...]]:
    lay = bootstrap.layout(args.config_dir)
    cfg = bootstrap.load(lay)
    return lay, cfg, tuple(bootstrap.load_sources(cfg, lay))


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
    return 1 if any(v.status is VersionStatus.FAILED for v in report.versions) else 0


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
        bootstrap.open_index(cfg),
        bootstrap.open_changes(cfg),
        user_agent=bootstrap.user_agent(cfg),
        out=out,
    )
    try:
        yield ix
    finally:
        ix.index.close()
        ix.feed.close()


def cmd_index_build(args: argparse.Namespace, out: TextIO) -> int:
    lay, cfg, specs = _context(args)
    with open_indexer(cfg, lay, specs, out) as ix:
        reports = ix.build([args.source] if args.source else None)
    for r in reports:
        if r.skipped:
            out.write(f"{r.source_id}: skipped ({r.skipped})\n")
        else:
            refined = ", ".join(f"{g} ({n:,} docs)" for g, n in r.refined) or "nothing new"
            out.write(f"{r.source_id}: refined {refined}; indexed {r.indexed}\n")
    if not reports:
        out.write("nothing to index: no sealed generations\n")
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
