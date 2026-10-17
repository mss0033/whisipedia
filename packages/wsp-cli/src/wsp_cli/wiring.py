"""Orchestrator composition root: ``orchestrator.wiring = "wsp_cli.wiring:build_wiring"``.

Builds a :class:`wsp_orchestrator.orchestrator.Wiring` of real components from the layered config:
registry sources, entry-point adapters, ``HttpFetcher``/``Aria2Fetcher``, ``FileVault`` +
``JsonlLedger`` on the NAS, the Refinery/Tantivy indexer on local disk, and the Warden hooks
(rolling scrub with the repair ladder, monthly drill, retention via ``FileVault.prune``).

Phase 5 ("Autonomy") adds: the Curator bots and their proposal files plus the local registry overlay
for auto-promotions (PLAN §5.3-5.5), the parity backfill (``vault.parity_scope``), and real retention:
the orchestrator's cached upstream offers are the last-copy oracle, pruned generations are moved
under ``pruned/`` (``orchestrator.prune_dry_run`` only records the plan).

Source integrity (ADR-0009) adds the guard's hooks (``wsp_cli.integrity``): signed provenance and
promotion reports, content profiles, scrub-verified settling. Scrub and parity stop on shutdown
(``Wiring.on_shutdown``) and pause while the API marks interactive use
(``vault.yield_to_interactive_seconds``).

Heavy work never runs in the daemon (audit 2026-10-11, C2/C3/H7): refine, index and the content
profile run as child units (:class:`wsp_orchestrator.isolation.IsolatedRunner`; ``systemd-run`` in
``whisipedia-background.slice`` with ``[orchestrator.heavy]`` limits, a plain child process without
systemd). The child functions below take the machine-wide ``HEAVY`` lock and, when they write the
Refinery or the index, the ``INDEX`` lock (``wsp_cli.locks``); a profile takes ``PROFILE`` instead
(paced and hours long, it must not hold the heavy jobs up); a busy lock comes back as
``ResourceBusyError`` and the step is deferred without counting an attempt. Refining never opens the
Tantivy index. Scrub slices take the ``SCRUB`` lock and drills the ``DRILL`` lock, shared with
``wsp scrub`` / ``wsp drill``.

Document policy (audit H3): :func:`document_screen` is the pipeline's policy screen for adapters
whose ``verify`` mixes checksums and policy (``doc-collection``); checksum-only adapters are listed in
:data:`CHECKSUM_ONLY_ADAPTERS`; for any other adapter it returns ``None`` and the pipeline always runs
the adapter's own ``verify`` (fail closed).
"""

from __future__ import annotations

import contextlib
import os
import random
import shutil
import threading
from collections.abc import Callable, Generator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from wsp_cli import bootstrap, doccollections
from wsp_cli.indexer import Indexer
from wsp_cli.integrity import integrity_hooks
from wsp_cli.locks import DRILL, HEAVY, INDEX, SCRUB, exclusive, lock_dir_for
from wsp_core.activity import recently_active
from wsp_core.model import Generation, GenerationStatus, RemoteFile, SourceSpec, Verdict, VerdictStatus
from wsp_core.ports import Ctx, SourceAdapter
from wsp_orchestrator.alerts import AlertSink
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
from wsp_orchestrator.durations import IsoDuration
from wsp_orchestrator.errors import StorageUnavailableError
from wsp_orchestrator.isolation import IsolatedRunner, Launcher, UnitLimits
from wsp_orchestrator.orchestrator import LastCopyHook, Wiring
from wsp_orchestrator.pipeline import RefineResult, Withheld
from wsp_orchestrator.policy import GateConfig, Proposal
from wsp_vault.attest import is_attestation_path
from wsp_vault.drill import DrillKind, run_drill
from wsp_vault.parity import Par2Parity
from wsp_vault.paritykeeper import GIB, ParityKeeper
from wsp_vault.paritymeta import is_parity_path
from wsp_vault.repair import RepairCircuitOpenError, RepairLadder
from wsp_vault.scrub import Scrubber, ScrubFinding
from wsp_vault.vault import FileVault, KeyPinError


def scrubber(
    cfg: Mapping[str, Any],
    vault: FileVault,
    *,
    should_stop: Callable[[], bool] | None = None,
    should_pause: Callable[[], bool] | None = None,
) -> Scrubber:
    vault_cfg = bootstrap.section(cfg, "vault")
    state = bootstrap.paths(cfg).require_state() / "scrub.sqlite"
    rate = vault_cfg.get("scrub_bytes_per_sec")
    return Scrubber(
        vault,
        state,
        ledger=vault.ledger,
        bytes_per_sec=float(rate) if rate else None,
        should_stop=should_stop,
        should_pause=should_pause,
    )


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
    cfg: Mapping[str, Any],
    vault: FileVault,
    scrub: Scrubber | None = None,
    *,
    should_stop: Callable[[], bool] | None = None,
    should_pause: Callable[[], bool] | None = None,
) -> ParityKeeper | None:
    """The Warden's parity keeper, or None when parity is off or par2 is not installed.

    par2 runs at nice 19 / idle IO with ``vault.parity_threads`` threads (default 2; 0 = par2's
    default of one per core) and ``vault.parity_memory_mb`` (``-m``; default 256, 0 = par2's own).
    """
    v = bootstrap.section(cfg, "vault")
    if not v.get("parity", True) or shutil.which("par2") is None:
        return None
    p = bootstrap.paths(cfg)
    rate = v.get("parity_bytes_per_sec")
    threads = int(v.get("parity_threads", 2))
    memory = int(v.get("parity_memory_mb", 256))
    return ParityKeeper(
        vault,
        Par2Parity(
            low_priority=True,
            threads=threads or None,
            memory_mb=memory or None,
            should_stop=should_stop,
        ),
        state_dir=p.require_state(),
        scratch=p.workdir or p.require_state(),
        percent=int(v.get("parity_percent", 10)),
        max_set_bytes=int(float(v.get("parity_max_set_gb", 64)) * GIB),
        bytes_per_sec=float(rate) if rate else None,
        verified=scrub.verified_md5 if scrub is not None else None,  # parity bound to verified bytes
        should_stop=should_stop,
        should_pause=should_pause,
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
        if f.unreadable:
            continue  # an I/O error says nothing about the bytes: never repaired, never set aside
        if is_attestation_path(f.relpath):
            continue  # a bad PROVENANCE/PROMOTION signature is evidence: reported, never "repaired"
        if is_parity_path(f.relpath) and not vault.manifest(f.source_id, f.generation_id).entry(
            f.relpath or ""
        ):
            parity.add((f.source_id, f.generation_id))
        else:
            data.append(f)
    return data, sorted(parity)


# --- heavy steps in child units -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HeavySettings:
    """``[orchestrator.heavy]``: limits and time budgets of the refine/index/profile child units."""

    limits: UnitLimits = field(default_factory=UnitLimits)
    refine_timeout: float | None = 24 * 3600.0
    index_timeout: float | None = 24 * 3600.0
    profile_timeout: float | None = 24 * 3600.0

    @classmethod
    def from_config(cls, cfg: Mapping[str, Any]) -> HeavySettings:
        sec = bootstrap.section(bootstrap.section(cfg, "orchestrator"), "heavy")
        base = UnitLimits()

        def budget(key: str, default: str) -> float | None:
            raw = str(sec.get(key, default))
            return None if raw in ("", "none", "0") else IsoDuration.parse(raw).approx().total_seconds()

        return cls(
            limits=UnitLimits(
                memory_high=str(sec.get("memory_high", base.memory_high)),
                memory_max=str(sec.get("memory_max", base.memory_max)),
                memory_swap_max=str(sec.get("memory_swap_max", base.memory_swap_max)),
                timeout_stop_sec=int(sec.get("timeout_stop_sec", base.timeout_stop_sec)),
            ),
            refine_timeout=budget("refine_timeout", "PT24H"),
            index_timeout=budget("index_timeout", "PT24H"),
            # Paced to a share of the time ([integrity] profile_busy_share), a big ZIM takes most of a day.
            profile_timeout=budget("profile_timeout", "PT24H"),
        )


def heavy_runner(
    cfg: Mapping[str, Any], *, should_stop: Callable[[], bool], launcher: Launcher | None = None
) -> IsolatedRunner:
    """The runner for heavy steps: child units under ``<orchestrator.workdir>/_steps``."""
    p = bootstrap.paths(cfg)
    work = p.workdir or p.require_state()
    env = {"WSP_MAX_WORKERS": os.environ.get("WSP_MAX_WORKERS", "4")}
    return IsolatedRunner(
        work,
        launcher=launcher,
        limits=HeavySettings.from_config(cfg).limits,
        should_stop=should_stop,
        env=env,
        cwd=bootstrap.layout().root or Path.cwd(),
    )


@dataclass(frozen=True, slots=True)
class ChildContext:
    """What a heavy child rebuilds from its request: config, registry, a read-only vault."""

    cfg: Mapping[str, Any]
    specs: tuple[SourceSpec, ...]
    vault: FileVault
    spec: SourceSpec
    generation: Generation | None

    @property
    def lock_dir(self) -> Path:
        return lock_dir_for(bootstrap.paths(self.cfg).require_index())

    def indexer(self) -> Indexer:
        """An Indexer whose Tantivy index and change feed are opened only on first use."""
        p = bootstrap.paths(self.cfg)
        cfg, specs = self.cfg, self.specs
        return Indexer(
            self.vault,
            specs,
            bootstrap.load_adapters(),
            p.require_index(),
            lambda: bootstrap.open_index(cfg, specs),
            lambda: bootstrap.open_changes(cfg),
            user_agent=bootstrap.user_agent(cfg),
            refinery=p.refinery,
            map_root=p.refinery_maps,
        )


def child_context(payload: Mapping[str, Any]) -> ChildContext:
    cfg = cast(Mapping[str, Any], payload["config"])
    lay = bootstrap.layout()
    specs = tuple(bootstrap.load_sources(cfg, lay))
    vault = bootstrap.build_vault(cfg, specs, bootstrap.load_signer(cfg, lay, create=False), ledger=False)
    source_id = str(payload["source_id"])
    spec = next((s for s in specs if s.id == source_id), None)
    if spec is None:
        raise LookupError(f"source {source_id!r} is not in the registry")
    gid = payload.get("generation_id")
    gen = None
    if gid is not None:
        gen = next(
            (
                g
                for g in vault.generations(source_id)
                if g.generation_id == gid and g.status is GenerationStatus.SEALED
            ),
            None,
        )
        if gen is None:
            raise LookupError(f"{source_id}/{gid} is not a sealed generation")
    return ChildContext(cfg, specs, vault, spec, gen)


@contextlib.contextmanager
def _locks(lock_dir: Path, *names: str) -> Generator[None]:
    """Take ``names`` in order (HEAVY before INDEX); a held one raises ``LockHeldError`` at once."""
    with contextlib.ExitStack() as stack:
        for name in names:
            stack.enter_context(exclusive(lock_dir, name))
        yield


def _payload_lock_dir(payload: Mapping[str, Any]) -> Path:
    """The lock directory, from the config alone (no registry, no vault reads)."""
    return lock_dir_for(bootstrap.paths(cast(Mapping[str, Any], payload["config"])).require_index())


def refine_child(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Child unit: refine one sealed generation into Refinery Parquet (never opens Tantivy).

    The locks come first: while a rebuild holds HEAVY, a deferred step must cost nothing but a unit
    start, not a registry load and a manifest read over the NAS every retry (audit 2026-10-12, L1).
    """
    with _locks(_payload_lock_dir(payload), HEAVY, INDEX):
        ctx = child_context(payload)
        assert ctx.generation is not None
        ix = ctx.indexer()
        try:
            result = ix.refine(
                ctx.spec, ctx.generation, ctx.vault.generation_path(ctx.spec.id, ctx.generation.generation_id)
            )
        finally:
            ix.close()
    return {"doc_count": result.doc_count}


def index_child(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Child unit: bring the search index to one generation (the union of partitions, ADR-0010).
    Locks before the context, as :func:`refine_child`."""
    with _locks(_payload_lock_dir(payload), HEAVY, INDEX):
        ctx = child_context(payload)
        assert ctx.generation is not None
        ix = ctx.indexer()
        try:
            if ctx.spec.partitioned:
                report = ix.index_partitioned(ctx.spec, include=ctx.generation.generation_id)
            else:
                report = ix.index_generation(ctx.spec, ctx.generation.generation_id)
            ix.finish()  # one suggest rebuild, after the step's commits
        finally:
            ix.close()
    return {"indexed": report.indexed, "upserted": report.upserted, "deleted": report.deleted}


class IndexHooks:
    """The pipeline's refine/index hooks: each call runs in a child unit (module docstring)."""

    def __init__(
        self,
        cfg: Mapping[str, Any],
        vault: FileVault,
        specs: Sequence[SourceSpec],
        runner: IsolatedRunner,
        heavy: HeavySettings | None = None,
    ) -> None:
        self.cfg = cfg
        self.vault = vault
        self.specs = specs
        self.runner = runner
        self.heavy = heavy or HeavySettings.from_config(cfg)
        self.adapters: dict[str, SourceAdapter] = bootstrap.load_adapters()

    def _payload(self, source: SourceSpec, generation: Generation) -> dict[str, Any]:
        return {"config": dict(self.cfg), "source_id": source.id, "generation_id": generation.generation_id}

    def refine(self, source: SourceSpec, generation: Generation, generation_dir: Path) -> RefineResult:
        value = self.runner.call(
            "refine",
            "wsp_cli.wiring:refine_child",
            self._payload(source, generation),
            key=f"{source.id}/{generation.generation_id}",
            timeout=self.heavy.refine_timeout,
        )
        count = cast(Mapping[str, Any], value).get("doc_count")
        return RefineResult(int(count) if count is not None else None)

    def index(self, source: SourceSpec, generation: Generation) -> None:
        self.runner.call(
            "index",
            "wsp_cli.wiring:index_child",
            self._payload(source, generation),
            key=f"{source.id}/{generation.generation_id}",
            timeout=self.heavy.index_timeout,
        )


# --- document policy screen (audit H3) ------------------------------------------------------------

# Adapters whose ``verify`` is a pure checksum comparison (``verify_file``): no document policy to run.
CHECKSUM_ONLY_ADAPTERS = frozenset(
    {
        "kiwix-opds",
        "wikimedia-dumps",
        "wikidata",
        "arxiv",
        "pubmed",
        "courtlistener",
        "open-library",
        "gutenberg",
        "osm-planet",
        "us-law",  # the OLRC's and GovInfo's own XML of federal law: nothing in it to screen
        "dailymed",  # NLM's own release of FDA drug labels, MD5-checked
        "ncbi-bookshelf",  # NLM's open-access book archives (chosen books, licence read per book)
    }
)
SCREENED_ADAPTERS = frozenset({"doc-collection"})  # policy screened by document_screen()


def document_screen(
    adapters: Mapping[str, SourceAdapter],
) -> Callable[[SourceSpec, Ctx, RemoteFile, Path], Verdict | None]:
    """The pipeline's policy screen (``Wiring.document_screen``).

    ``doc-collection``: the well-formedness check and the distribution/content screen its ``verify``
    runs, without the checksum part. A checksum-only adapter: ``ok``. Anything else: ``None``, so
    the pipeline runs the adapter's ``verify`` every time (a new adapter fails closed).
    """
    from wsp_adapters.doccollection import DocCollectionAdapter
    from wsp_adapters.doccollection.adapter import screen_file, well_formed
    from wsp_adapters.doccollection.policy import Verdict as PolicyVerdict

    def screen(spec: SourceSpec, ctx: Ctx, remote: RemoteFile, local: Path) -> Verdict | None:
        name = spec.adapter or spec.id
        adapter = adapters.get(name)
        if isinstance(adapter, DocCollectionAdapter):
            problem = well_formed(local)
            if problem:
                return Verdict(VerdictStatus.MISMATCH, problem)
            decision = screen_file(local, adapter.config(spec).rules).decision
            if decision.verdict is PolicyVerdict.DENY:
                return Verdict(VerdictStatus.REJECTED, f"policy {decision}")
            if decision.verdict is PolicyVerdict.REVIEW:
                return Verdict(VerdictStatus.HELD, f"policy {decision}")
            return Verdict(VerdictStatus.OK, "policy allow")
        if name in CHECKSUM_ONLY_ADAPTERS and adapter is not None:
            return Verdict(VerdictStatus.OK, "no document policy")
        return None

    return screen


def storage_probe(root: Path | None) -> Callable[[], bool]:
    """True while the vault mount answers (a dead SMB soft mount raises or is empty)."""

    def probe() -> bool:
        if root is None:
            return True
        try:
            if not root.is_dir():
                return False
            with os.scandir(root) as entries:  # an unmounted mount point is an empty directory
                return any(True for _ in entries)
        except OSError:
            return False

    return probe


def _alert_sinks(config: Mapping[str, Any]) -> list[AlertSink]:
    from wsp_cli.notify import alert_sinks

    return alert_sinks(config)


def build_wiring(config: Mapping[str, Any]) -> Wiring:
    lay = bootstrap.layout()
    specs = tuple(bootstrap.load_sources(config, lay))
    signer = bootstrap.load_signer(config, lay)
    vault = bootstrap.build_vault(config, specs, signer)
    assert vault.ledger is not None
    stopping = threading.Event()  # set on SIGTERM: scrub, parity, par2 and heavy units stop promptly
    runner = heavy_runner(config, should_stop=stopping.is_set)
    hooks = IndexHooks(config, vault, specs, runner)
    locks = lock_dir_for(bootstrap.paths(config).require_index())
    vcfg = bootstrap.section(config, "vault")
    marker = bootstrap.activity_marker(config)
    window = float(vcfg.get("yield_to_interactive_seconds", 300))

    def busy() -> bool:  # the owner used the library recently: background integrity work waits
        return window > 0 and recently_active(marker, window)

    scrub = scrubber(config, vault, should_stop=stopping.is_set, should_pause=busy)
    ladder = repair_ladder(config, vault)
    slices = int(bootstrap.section(config, "orchestrator").get("scrub_slices", 720))

    keeper = parity_keeper(config, vault, scrub, should_stop=stopping.is_set, should_pause=busy)
    scope = str(vcfg.get("parity_scope", "all"))
    per_run = int(float(vcfg.get("parity_bytes_per_run", 200e9)))

    def scrub_slice(index: int, count: int) -> Mapping[str, object]:
        with exclusive(locks, SCRUB):  # `wsp scrub` holds it: the slice is deferred (no attempt)
            budget = max(1, sealed_bytes(vault) // max(count, slices, 1))
            try:
                report = scrub.run(max_bytes=budget)
                if report.aborted is not None:  # the share is gone: defer, record nothing (audit #2)
                    raise StorageUnavailableError(f"scrub slice aborted: {report.aborted}")
                data, parity_damage = split_parity_findings(vault, report.findings)
                if data:
                    ladder.repair_findings(data)
            except (RepairCircuitOpenError, KeyPinError) as exc:
                # Widespread signature failures or a foreign key: nothing was touched; the owner must look.
                return {"critical": f"{type(exc).__name__}: {exc}"}
            if keeper is not None:
                for sid, gid in parity_damage:  # derived data: set aside, the backfill makes it again
                    keeper.invalidate(sid, gid, "scrub found damaged parity")
            return {"unreadable": sum(1 for f in report.findings if f.unreadable)}

    def drill() -> Mapping[str, object]:
        with exclusive(locks, DRILL):
            return drill_once(config, vault)

    def prune(source_id: str, generation_id: str) -> None:
        vault.prune(source_id, generation_id)  # moves aside under pruned/, never deletes

    def parity(last_copy: LastCopyHook) -> Mapping[str, object]:
        if keeper is None:
            return {"pending": 0, "note": "parity off or par2 not installed"}
        targets = parity_targets(vault, specs, scope, last_copy)
        return keeper.run(targets, max_bytes=per_run).to_json()

    files = ProposalFiles(bootstrap.paths(config).proposals)
    overlay = bootstrap.overlay_path(config, lay)

    def on_withheld(spec: SourceSpec, version: str, items: Sequence[Withheld]) -> None:
        """Held documents of a release (ADR-0010): one owner-review proposal, as `collections fetch` does."""
        rows = [
            doccollections.withheld_review_row(w.name, w.url, w.detail) for w in items if w.status == "held"
        ]
        doccollections.write_review_proposal(config, spec, version, rows)

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
        drill=drill,
        pruner=prune,
        last_copy=None,  # the orchestrator's cached upstream offers decide (PLAN §6.4)
        bots=build_bots(config),
        gate=gate_config(config),
        gate_provider=lambda: gate_config(config, vault),
        promoter=promote,
        proposal_recorder=files.write,
        parity=parity,
        integrity=integrity_hooks(
            config, vault, scrub, runner=runner, profile_timeout=hooks.heavy.profile_timeout
        ),
        on_withheld=on_withheld,
        on_shutdown=[stopping.set],
        alert_sinks=_alert_sinks(config),
        document_screen=document_screen(hooks.adapters),
        storage_available=storage_probe(bootstrap.paths(config).vault_root),
    )
