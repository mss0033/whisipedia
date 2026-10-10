"""Orchestrator composition root: ``orchestrator.wiring = "wsp_cli.wiring:build_wiring"``.

Builds a :class:`wsp_orchestrator.orchestrator.Wiring` of real components from the layered config:
registry sources, entry-point adapters, ``HttpFetcher``/``Aria2Fetcher``, ``FileVault`` +
``JsonlLedger`` on the NAS, the Refinery/Tantivy indexer on local disk, and the Warden hooks
(rolling scrub with the repair ladder, monthly drill, retention via ``FileVault.prune``).

Phase 5 ("Autonomy") adds: the Curator bots and their proposal files plus the local registry overlay
for auto-promotions (PLAN §5.3-5.5), the parity backfill (``vault.parity_scope``), and real retention:
the orchestrator's cached upstream offers are the last-copy oracle, pruned generations are moved
under ``pruned/`` (``orchestrator.prune_dry_run`` only records the plan).
"""

from __future__ import annotations

import random
import shutil
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from wsp_cli import bootstrap
from wsp_cli.indexer import Indexer
from wsp_core.model import Generation, GenerationStatus, SourceSpec
from wsp_orchestrator.bots import Bot
from wsp_orchestrator.curator import (
    AdapterSmokeBot,
    GapMiner,
    KiwixWatcher,
    ProposalFiles,
    RegistryHealthBot,
    WikimediaRankingBot,
)
from wsp_orchestrator.curator.registry_edit import upsert_overlay
from wsp_orchestrator.orchestrator import LastCopyHook, Wiring
from wsp_orchestrator.pipeline import RefineResult
from wsp_orchestrator.policy import GateConfig, Proposal
from wsp_vault.drill import DrillKind, run_drill
from wsp_vault.parity import Par2Parity
from wsp_vault.paritykeeper import GIB, ParityKeeper
from wsp_vault.paritymeta import is_parity_path
from wsp_vault.repair import RepairLadder
from wsp_vault.scrub import Scrubber, ScrubFinding
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


DEFAULT_BOTS = ("kiwix-opds", "wikimedia-ranking", "registry-health", "adapter-contract", "gap-miner")
DEFAULT_KIWIX_CATEGORIES = (
    "wikipedia",
    "wiktionary",
    "wikibooks",
    "wikivoyage",
    "wikiquote",
    "wikiversity",
    "wikisource",
    "stack_exchange",
    "other",
)
DEFAULT_LICENSES = (
    "CC0-1.0",
    "CC-BY-4.0",
    "CC-BY-SA-3.0",
    "CC-BY-SA-4.0",
    "ODbL-1.0",
    "LicenseRef-PD",
    "LicenseRef-US-Gov-PD",
)
TB = 10**12


def build_bots(cfg: Mapping[str, Any]) -> list[Bot]:
    """The Curator bots named in ``curator.bots`` (PLAN §5.3, §5.5)."""
    cur = bootstrap.section(cfg, "curator")
    p = bootstrap.paths(cfg)
    names = [str(n) for n in cast(Sequence[object], cur.get("bots", DEFAULT_BOTS))]
    out: list[Bot] = []
    for name in names:
        match name:
            case "kiwix-opds":
                cats = cast(Sequence[object], cur.get("kiwix_categories", DEFAULT_KIWIX_CATEGORIES))
                langs = cast(Sequence[object], cur.get("kiwix_languages", ["eng"]))
                out.append(KiwixWatcher(categories=[str(c) for c in cats], languages=[str(x) for x in langs]))
            case "wikimedia-ranking":
                out.append(WikimediaRankingBot())
            case "registry-health":
                out.append(RegistryHealthBot())
            case "adapter-contract":
                out.append(AdapterSmokeBot())
            case "gap-miner":
                if p.index_root is not None:
                    out.append(
                        GapMiner(
                            gaps_path=p.gaps_log,
                            queries_path=p.queries_log,
                            min_count=int(cur.get("gap_min_count", 5)),
                            window_days=int(cur.get("gap_window_days", 90)),
                        )
                    )
            case _:
                raise bootstrap.ConfigError(f"unknown curator bot {name!r}")
    return out


def domain_usage(vault: FileVault) -> dict[str, int]:
    """Sealed bytes per vault domain (for the storage-budget check of the policy gate)."""
    used: dict[str, int] = {}
    for sid in vault.sources():
        domain = vault.domain_of(sid)
        for g in vault.generations(sid):
            if g.status is not GenerationStatus.STAGING:
                used[domain] = used.get(domain, 0) + sum(a.size for a in g.artifacts)
    return used


def gate_config(cfg: Mapping[str, Any], vault: FileVault | None = None) -> GateConfig:
    cur = bootstrap.section(cfg, "curator")
    budgets = cast(Mapping[str, object], cur.get("domain_budget_tb", {}))
    return GateConfig(
        license_allowlist=frozenset(
            str(x) for x in cast(Sequence[object], cur.get("license_allowlist", DEFAULT_LICENSES))
        ),
        kiwix_categories=frozenset(
            str(x)
            for x in cast(Sequence[object], cur.get("kiwix_auto_categories", ["wikivoyage", "wikibooks"]))
        ),
        domain_budget_bytes={str(k): int(float(str(v)) * TB) for k, v in budgets.items()},
        domain_used_bytes=domain_usage(vault) if vault is not None else {},
        min_score=float(cur.get("min_score", 3.5)),
    )


def parity_keeper(
    cfg: Mapping[str, Any], vault: FileVault, scrub: Scrubber | None = None
) -> ParityKeeper | None:
    """The Warden's parity keeper, or None when parity is off or par2 is not installed."""
    v = bootstrap.section(cfg, "vault")
    if not v.get("parity", True) or shutil.which("par2") is None:
        return None
    p = bootstrap.paths(cfg)
    rate = v.get("parity_bytes_per_sec")
    return ParityKeeper(
        vault,
        Par2Parity(low_priority=True, threads=int(v["parity_threads"]) if v.get("parity_threads") else None),
        state_dir=p.require_state(),
        scratch=p.workdir or p.require_state(),
        percent=int(v.get("parity_percent", 10)),
        max_set_bytes=int(float(v.get("parity_max_set_gb", 64)) * GIB),
        bytes_per_sec=float(rate) if rate else None,
        verified=scrub.verified_ok if scrub is not None else None,
    )


def parity_targets(
    vault: FileVault, specs: Sequence[SourceSpec], scope: str, last_copy: LastCopyHook | None
) -> list[tuple[str, str]]:
    """Sealed generations parity must cover, oldest first: all, or only irreplaceable/last copies."""
    by_id = {s.id: s for s in specs}
    if scope not in ("all", "irreplaceable"):
        raise bootstrap.ConfigError(f"vault.parity_scope must be 'all' or 'irreplaceable', not {scope!r}")
    out: list[tuple[Any, str, str]] = []
    for sid in vault.sources():
        spec = by_id.get(sid)
        for g in vault.generations(sid):
            if g.status is not GenerationStatus.SEALED:
                continue
            wanted = scope == "all" or (spec is not None and spec.irreplaceable)
            if not wanted and last_copy is not None:
                wanted = last_copy(sid, g)
            if wanted:
                out.append((g.sealed_at or datetime.min.replace(tzinfo=UTC), sid, g.generation_id))
    return [(sid, gid) for _, sid, gid in sorted(out)]


def split_parity_findings(
    vault: FileVault, findings: Sequence[ScrubFinding]
) -> tuple[list[ScrubFinding], list[tuple[str, str]]]:
    """Data findings for the repair ladder, and generations whose (derived) parity must be rebuilt."""
    data: list[ScrubFinding] = []
    parity: set[tuple[str, str]] = set()
    for f in findings:
        if is_parity_path(f.relpath) and not vault.manifest(f.source_id, f.generation_id).entry(
            f.relpath or ""
        ):
            parity.add((f.source_id, f.generation_id))
        else:
            data.append(f)
    return data, sorted(parity)


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

    keeper = parity_keeper(config, vault, scrub)
    vcfg = bootstrap.section(config, "vault")
    scope = str(vcfg.get("parity_scope", "all"))
    per_run = int(float(vcfg.get("parity_bytes_per_run", 200e9)))

    def scrub_slice(index: int, count: int) -> None:
        budget = max(1, sealed_bytes(vault) // max(count, slices, 1))
        report = scrub.run(max_bytes=budget)
        data, parity_damage = split_parity_findings(vault, report.findings)
        if data:
            ladder.repair_findings(data)
        if keeper is not None:
            for sid, gid in parity_damage:  # derived data: set aside, the backfill makes it again
                keeper.invalidate(sid, gid, "scrub found damaged parity")

    def prune(source_id: str, generation_id: str) -> None:
        vault.prune(source_id, generation_id)  # moves aside under pruned/, never deletes

    def parity(last_copy: LastCopyHook) -> Mapping[str, object]:
        if keeper is None:
            return {"pending": 0, "note": "parity off or par2 not installed"}
        targets = parity_targets(vault, specs, scope, last_copy)
        return keeper.run(targets, max_bytes=per_run).to_json()

    files = ProposalFiles(bootstrap.paths(config).proposals)
    overlay = bootstrap.overlay_path(config, lay)

    def promote(proposal: Proposal) -> None:
        entry = dict(proposal.candidate.registry_entry)
        if entry.get("id"):
            upsert_overlay(overlay, entry)  # takes effect when the daemon restarts

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
        last_copy=None,  # the orchestrator's cached upstream offers decide (PLAN §6.4)
        bots=build_bots(config),
        gate=gate_config(config),
        gate_provider=lambda: gate_config(config, vault),
        promoter=promote,
        proposal_recorder=files.write,
        parity=parity,
    )
