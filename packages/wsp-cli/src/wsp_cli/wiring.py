"""Orchestrator composition root: ``orchestrator.wiring = "wsp_cli.wiring:build_wiring"``.

Builds a :class:`wsp_orchestrator.orchestrator.Wiring` of real components from the layered config:
registry sources, entry-point adapters, ``HttpFetcher``/``Aria2Fetcher``, ``FileVault`` +
``JsonlLedger`` on the NAS, the Refinery/Tantivy indexer on local disk, and the Warden hooks
(rolling scrub with the repair ladder, monthly drill, retention via ``FileVault.prune``).

Retention stays a dry run (a ``retention.plan`` ledger event) until a last-copy oracle exists: the
orchestrator only prunes when both ``pruner`` and ``last_copy`` are wired (PLAN §6.4).
"""

from __future__ import annotations

import random
import shutil
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from wsp_cli import bootstrap
from wsp_cli.indexer import Indexer
from wsp_core.model import Generation, GenerationStatus, SourceSpec
from wsp_orchestrator.orchestrator import Wiring
from wsp_orchestrator.pipeline import RefineResult
from wsp_vault.drill import DrillKind, run_drill
from wsp_vault.parity import Par2Parity
from wsp_vault.repair import RepairLadder
from wsp_vault.scrub import Scrubber
from wsp_vault.vault import FileVault


def scrubber(cfg: Mapping[str, Any], vault: FileVault) -> Scrubber:
    vault_cfg = bootstrap.section(cfg, "vault")
    state = bootstrap.paths(cfg).require_state() / "scrub.sqlite"
    rate = vault_cfg.get("scrub_bytes_per_sec")
    return Scrubber(vault, state, ledger=vault.ledger, bytes_per_sec=float(rate) if rate else None)


def repair_ladder(cfg: Mapping[str, Any], vault: FileVault) -> RepairLadder:
    if vault.ledger is None:
        raise bootstrap.ConfigError("the repair ladder needs the vault ledger")
    mirror = bootstrap.section(cfg, "vault").get("mirror_root")
    parity = Par2Parity() if shutil.which("par2") else None
    return RepairLadder(
        vault,
        vault.ledger,
        parity=parity,
        mirror_root=Path(str(mirror)).expanduser() if mirror else None,
        scratch=bootstrap.paths(cfg).workdir,
    )


def sealed_bytes(vault: FileVault) -> int:
    return sum(
        a.size
        for sid in vault.sources()
        for g in vault.generations(sid)
        if g.status is GenerationStatus.SEALED
        for a in g.artifacts
    )


def drill_target(vault: FileVault, source_id: str | None = None) -> Generation | None:
    """The smallest sealed generation (drills copy the generation to local scratch)."""
    gens = [
        g
        for sid in ([source_id] if source_id else vault.sources())
        for g in vault.generations(sid)
        if g.status is GenerationStatus.SEALED and g.artifacts
    ]
    return min(gens, key=lambda g: sum(a.size for a in g.artifacts), default=None)


def drill_once(
    cfg: Mapping[str, Any],
    vault: FileVault,
    *,
    kind: DrillKind | None = None,
    source_id: str | None = None,
    rng: random.Random | None = None,
) -> dict[str, object]:
    rng = rng or random.Random()  # noqa: S311 - fault placement, not cryptography
    target = drill_target(vault, source_id)
    if target is None:
        return {"ok": True, "skipped": "no sealed generation to drill"}
    kind = kind or rng.choice(list(DrillKind))
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    workdir = bootstrap.paths(cfg).workdir
    if workdir is None:
        raise bootstrap.ConfigError("orchestrator.workdir is not set (local disk)")
    sandbox = workdir / "drills" / f"{stamp}-{kind.value}"
    try:
        result = run_drill(
            vault, target.source_id, target.generation_id, sandbox, kind, rng=rng, ledger=vault.ledger
        )
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)  # local scratch copy only
    return {
        "ok": result.passed,
        "kind": result.kind.value,
        "generation": f"{result.source_id}/{result.generation_id}",
        "target": result.target,
        "detected": result.detected,
        "repaired": result.repaired,
        "detail": result.detail,
    }


class IndexHooks:
    """The pipeline's REFINED/INDEXED hooks, opening the local stores per call."""

    def __init__(self, cfg: Mapping[str, Any], vault: FileVault, specs: Sequence[SourceSpec]) -> None:
        self.cfg = cfg
        self.vault = vault
        self.specs = specs
        self.adapters = bootstrap.load_adapters()

    def _indexer(self) -> Indexer:
        p = bootstrap.paths(self.cfg)
        return Indexer(
            self.vault,
            self.specs,
            self.adapters,
            p.require_index(),
            bootstrap.open_index(self.cfg),
            bootstrap.open_changes(self.cfg),
            user_agent=bootstrap.user_agent(self.cfg),
        )

    def refine(self, source: SourceSpec, generation: Generation, generation_dir: Path) -> RefineResult:
        ix = self._indexer()
        try:
            return ix.refine(source, generation, generation_dir)
        finally:
            ix.index.close()
            ix.feed.close()

    def index(self, source: SourceSpec, generation: Generation) -> None:
        ix = self._indexer()
        try:
            ix.index_generation(source, generation.generation_id)
        finally:
            ix.index.close()
            ix.feed.close()


def build_wiring(config: Mapping[str, Any]) -> Wiring:
    lay = bootstrap.layout()
    specs = tuple(bootstrap.load_sources(config, lay))
    signer = bootstrap.load_signer(config, lay)
    vault = bootstrap.build_vault(config, specs, signer)
    assert vault.ledger is not None
    hooks = IndexHooks(config, vault, specs)
    scrub = scrubber(config, vault)
    ladder = repair_ladder(config, vault)
    slices = int(bootstrap.section(config, "orchestrator").get("scrub_slices", 720))

    def scrub_slice(index: int, count: int) -> None:
        budget = max(1, sealed_bytes(vault) // max(count, slices, 1))
        report = scrub.run(max_bytes=budget)
        if report.findings:
            ladder.repair_findings(report.findings)

    def prune(source_id: str, generation_id: str) -> None:
        vault.prune(source_id, generation_id)  # moves aside under pruned/, never deletes

    return Wiring(
        sources=specs,
        adapters=hooks.adapters,
        fetcher=bootstrap.build_fetcher(config),
        vault=vault,
        ledger=vault.ledger,
        refine=hooks.refine,
        index=hooks.index,
        scrub=scrub_slice,
        drill=lambda: drill_once(config, vault),
        pruner=prune,
        last_copy=None,  # TODO(integration): history oracle; until then retention is a dry run
    )
