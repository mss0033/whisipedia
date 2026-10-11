"""Composition root: the one place that turns configuration into concrete port implementations.

Everything else (API, CLI commands, the orchestrator via :mod:`wsp_cli.wiring`) receives ports and
never chooses an implementation. Configuration is layered (PLAN §12.2): ``config/default.toml`` <
``config/local.toml`` (gitignored) < ``WSP__SECTION__KEY`` environment variables.

Real backends (selected when ``[api] backend = "real"``, or ``"auto"`` and ``vault.root`` exists):

* ``FileVault(vault.root)`` with domains from the registry, ``JsonlLedger(<vault.root>/ledger)``;
* ``TantivyIndex``, ``ParquetDocumentStore`` and ``SqliteChangeFeed`` under ``index.root``, which must
  be local disk (``ensure_local``, ADR-0002);
* ``HttpFetcher`` (or ``Aria2Fetcher`` with ``acquirer.fetcher = "aria2"``) and the adapters registered
  under the ``whisipedia.adapters`` entry points;
* the manifest/ledger signer from ``vault.signing_key``. Without one, an Ed25519 key is generated on
  first use under ``$XDG_CONFIG_HOME/whisipedia/`` (``~/.config/whisipedia/``), never inside the repo.

Otherwise the in-memory sample library from :mod:`wsp_api.testing.fakes` backs the API.
"""

from __future__ import annotations

import dataclasses
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from wsp_api import ApiSettings, Services
from wsp_api.services import LOOPBACK_HOSTS
from wsp_api.testing.fakes import (
    SAMPLE_SOURCES,
    InMemoryChangeFeed,
    InMemoryDocumentStore,
    InMemoryLedger,
    InMemoryLexicalIndex,
    InMemoryVault,
    seed_sample_library,
)
from wsp_core.config import load_config
from wsp_core.model import SourceSpec, SourceStatus
from wsp_core.ports import Catalog, ChangeFeed, Fetcher, Signer, SourceAdapter
from wsp_core.registry import load_registry

if TYPE_CHECKING:  # heavy imports stay lazy so `wsp search --url ...` starts fast
    from wsp_cli.monitor import LibraryMonitor
    from wsp_index import ParquetDocumentStore, SqliteChangeFeed, TantivyIndex
    from wsp_index.spelling import ReloadingSpeller
    from wsp_vault.ledger import JsonlLedger
    from wsp_vault.vault import FileVault

BACKEND_NOTE = "using in-memory sample backends (set vault.root / index.root for the real library)"
CYCLE_DURATIONS = {"monthly": "P1M", "quarterly": "P3M"}
SIGNING_KEY_NAME = "vault-signing-ed25519.pem"


class ConfigError(ValueError):
    """The configuration cannot build the requested component."""


@dataclass(frozen=True, slots=True)
class Layout:
    """Where the repository-relative inputs live. ``root`` is None outside a checkout."""

    root: Path | None
    config_dir: Path
    registry: Path
    schema: Path
    navigator_dist: Path


def find_root(start: Path | None = None) -> Path | None:
    here = (start or Path.cwd()).resolve()
    for parent in (here, *here.parents):
        if (parent / "registry" / "sources.yaml").is_file():
            return parent
    return None


def layout(config_dir: Path | None = None, *, environ: Mapping[str, str] | None = None) -> Layout:
    env = os.environ if environ is None else environ
    root = find_root()
    base = root or Path.cwd()
    cfg_dir = config_dir or Path(env.get("WSP_CONFIG_DIR", base / "config"))
    return Layout(
        root=root,
        config_dir=cfg_dir,
        registry=base / "registry" / "sources.yaml",
        schema=base / "registry" / "schema" / "source.schema.json",
        navigator_dist=base / "navigator" / "dist",
    )


def load(lay: Layout, *, environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    return load_config(lay.config_dir, environ=environ)


def section(cfg: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    value = cfg.get(name, {})
    return cast(Mapping[str, Any], value) if isinstance(value, Mapping) else {}


def _opt_path(value: object) -> Path | None:
    return Path(str(value)).expanduser() if value not in (None, "") else None


def load_sources(cfg: Mapping[str, Any], lay: Layout) -> Sequence[SourceSpec]:
    reg = section(cfg, "registry")
    registry = Path(reg.get("path", lay.registry))
    schema = Path(reg.get("schema", lay.schema))
    if not registry.is_file():
        return ()
    return load_registry(registry, schema, overlay=overlay_path(cfg, lay))


def overlay_path(cfg: Mapping[str, Any], lay: Layout) -> Path:
    """The local registry overlay written by auto-promotions (git-ignored, PLAN §5.4)."""
    configured = _opt_path(section(cfg, "curator").get("overlay"))
    return configured or lay.config_dir / "registry.local.yaml"


def effective_sources(specs: Sequence[SourceSpec], cfg: Mapping[str, Any]) -> tuple[SourceSpec, ...]:
    """Specs with the global cycle filled in where the registry has no override (for display/freshness)."""
    cycle = str(section(cfg, "acquirer").get("cycle", "monthly"))
    default = CYCLE_DURATIONS.get(cycle, "P1M")
    return tuple(s if s.cadence else dataclasses.replace(s, cadence=default) for s in specs)


def api_settings(cfg: Mapping[str, Any], lay: Layout, *, static_dir: Path | None = None) -> ApiSettings:
    api = section(cfg, "api")
    static = static_dir or (Path(api["static_dir"]) if api.get("static_dir") else lay.navigator_dist)
    extra = tuple(str(h) for h in api.get("allowed_hosts", ()))
    return ApiSettings(static_dir=static, allowed_hosts=(*LOOPBACK_HOSTS, *extra))


# --- paths ------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Paths:
    """Every location the real backends use, resolved from config."""

    vault_root: Path | None
    index_root: Path | None
    state_dir: Path | None  # local orchestrator/warden state (directory of orchestrator.state_db)
    workdir: Path | None
    proposals_dir: Path | None = None

    @property
    def incoming(self) -> Path:
        return self.require_vault() / "_incoming"

    @property
    def ledger_dir(self) -> Path:
        return self.require_vault() / "ledger"

    @property
    def refinery(self) -> Path:
        return self.require_index() / "refinery"

    @property
    def lexical(self) -> Path:
        return self.require_index() / "lexical"

    @property
    def changes_db(self) -> Path:
        return self.require_index() / "changes.sqlite"

    @property
    def gaps_log(self) -> Path:
        return self.require_index() / "gaps.jsonl"

    @property
    def queries_log(self) -> Path:
        return self.require_index() / "queries.jsonl"

    @property
    def proposals(self) -> Path:
        """Proposal files (local disk, next to the index root unless ``curator.proposals_dir``)."""
        return self.proposals_dir or self.require_index().parent / "proposals"

    @property
    def index_work(self) -> Path:
        return self.require_index() / "work"

    def require_vault(self) -> Path:
        if self.vault_root is None:
            raise ConfigError("vault.root is not set (config/local.toml or WSP__VAULT__ROOT)")
        return self.vault_root

    def require_index(self) -> Path:
        if self.index_root is None:
            raise ConfigError("index.root is not set (config/local.toml or WSP__INDEX__ROOT)")
        return self.index_root

    def require_state(self) -> Path:
        if self.state_dir is None:
            raise ConfigError("orchestrator.state_db is not set (local disk)")
        return self.state_dir


def paths(cfg: Mapping[str, Any]) -> Paths:
    orch = section(cfg, "orchestrator")
    state_db = _opt_path(orch.get("state_db"))
    return Paths(
        vault_root=_opt_path(section(cfg, "vault").get("root")),
        index_root=_opt_path(section(cfg, "index").get("root")),
        state_dir=state_db.parent if state_db else None,
        workdir=_opt_path(orch.get("workdir")),
        proposals_dir=_opt_path(section(cfg, "curator").get("proposals_dir")),
    )


def real_backends_available(cfg: Mapping[str, Any]) -> bool:
    backend = str(section(cfg, "api").get("backend", "auto"))
    if backend == "real":
        return True
    if backend == "sample":
        return False
    root = paths(cfg).vault_root
    return root is not None and root.is_dir()


# --- signer ---------------------------------------------------------------------------------------


def config_home(environ: Mapping[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    base = env.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base).expanduser() / "whisipedia"


def load_signer(
    cfg: Mapping[str, Any],
    lay: Layout | None = None,
    *,
    create: bool = True,
    environ: Mapping[str, str] | None = None,
) -> Signer:
    """The vault's manifest/ledger signer (``[vault] signer`` = ``ed25519`` | ``ssh``).

    ``ed25519`` reads a PKCS#8 PEM from ``vault.signing_key`` (default
    ``<config home>/vault-signing-ed25519.pem``) and generates it on first use when ``create`` is set.
    With ``create=False`` and no key, an ephemeral in-memory key is used (dry runs only: it can sign
    nothing that is kept). Keys inside the repository checkout are refused.
    """
    from wsp_vault.signing import Ed25519Signer, SshSigner

    vault = section(cfg, "vault")
    kind = str(vault.get("signer", "ed25519"))
    key = _opt_path(vault.get("signing_key")) or config_home(environ) / SIGNING_KEY_NAME
    key = key.resolve()
    if lay is not None and lay.root is not None and key.is_relative_to(lay.root.resolve()):
        raise ConfigError(f"refusing a signing key inside the repository: {key}")
    if kind == "ssh":
        return SshSigner(key)
    if kind != "ed25519":
        raise ConfigError(f"unknown vault.signer {kind!r} (ed25519 | ssh)")
    if key.is_file():
        return Ed25519Signer.from_private_pem(key.read_bytes())
    if not create:
        return Ed25519Signer.generate()
    signer = Ed25519Signer.generate()
    key.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(key, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(signer.private_pem())
    return signer


# --- components -----------------------------------------------------------------------------------


def user_agent(cfg: Mapping[str, Any]) -> str:
    return str(section(cfg, "acquirer").get("user_agent", "Whisipedia/0.0 (personal offline archive)"))


def build_vault(
    cfg: Mapping[str, Any], specs: Sequence[SourceSpec], signer: Signer, *, ledger: bool = True
) -> FileVault:
    """``FileVault`` at ``vault.root`` with registry domains and (optionally) the ledger attached."""
    from wsp_vault.parity import Par2Parity
    from wsp_vault.vault import FileVault, domains_from_specs

    p = paths(cfg)
    root = p.require_vault()
    parity = Par2Parity() if section(cfg, "vault").get("parity", True) and _has_par2() else None
    return FileVault(
        root,
        signer,
        domains=domains_from_specs(specs),
        ledger=build_ledger(cfg, signer) if ledger else None,
        parity=parity,
    )


def _has_par2() -> bool:
    import shutil

    return shutil.which("par2") is not None


def build_ledger(cfg: Mapping[str, Any], signer: Signer) -> JsonlLedger:
    from wsp_vault.ledger import JsonlLedger

    p = paths(cfg)
    lock = None if p.index_root is None else p.index_root.parent / "locks" / "ledger.lock"
    return JsonlLedger(p.ledger_dir, signer, lock_path=lock)


def build_fetcher(cfg: Mapping[str, Any]) -> Fetcher:
    """``HttpFetcher`` (default) or ``Aria2Fetcher`` (``acquirer.fetcher = "aria2"``) from ``[acquirer]``."""
    from wsp_fetch import Aria2Fetcher, FetchConfig, HttpFetcher, find_aria2c, load_wikimedia_catalog

    acq = section(cfg, "acquirer")
    fc = FetchConfig.from_mapping(acq)
    catalog = load_wikimedia_catalog()
    fc = dataclasses.replace(fc, host_connections={**catalog.host_connections(), **fc.host_connections})
    if str(acq.get("fetcher", "http")) == "aria2":
        if find_aria2c() is None:
            raise ConfigError("acquirer.fetcher = 'aria2' but aria2c is not on PATH")
        return Aria2Fetcher(fc)
    return HttpFetcher(fc)


def load_adapters() -> dict[str, SourceAdapter]:
    """Every adapter registered under the ``whisipedia.adapters`` entry points, by registry name."""
    from wsp_adapters import available_adapters, load_adapter

    return {name: load_adapter(name) for name in available_adapters()}


def index_languages(cfg: Mapping[str, Any]) -> tuple[str, ...]:
    langs = section(cfg, "index").get("languages", ["en"])
    return tuple(str(x) for x in cast(Sequence[object], langs))


def source_priors(cfg: Mapping[str, Any], specs: Sequence[SourceSpec]) -> dict[str, float]:
    """Per-source ranking bonus from ``[index.domain_priors]``, keyed by each source's first domain."""
    table = section(section(cfg, "index"), "domain_priors")
    priors: dict[str, float] = {}
    for spec in specs:
        bonus = table.get(spec.domain[0]) if spec.domain else None
        if isinstance(bonus, int | float) and bonus > 0:
            priors[spec.id] = float(bonus)
    return priors


def open_index(cfg: Mapping[str, Any], specs: Sequence[SourceSpec] = ()) -> TantivyIndex:
    from wsp_index import TantivyConfig, TantivyIndex, ensure_local

    p = paths(cfg)
    ensure_local(p.require_index())
    config = TantivyConfig(languages=index_languages(cfg), source_priors=source_priors(cfg, specs))
    return TantivyIndex(p.lexical, config)


def open_documents(cfg: Mapping[str, Any]) -> ParquetDocumentStore:
    from wsp_index import ParquetDocumentStore, ensure_local

    p = paths(cfg)
    ensure_local(p.require_index())
    p.refinery.mkdir(parents=True, exist_ok=True)
    return ParquetDocumentStore(p.refinery)


def open_changes(cfg: Mapping[str, Any]) -> SqliteChangeFeed:
    from wsp_index import SqliteChangeFeed, ensure_local

    p = paths(cfg)
    ensure_local(p.require_index())
    return SqliteChangeFeed(p.changes_db)


# --- API services -----------------------------------------------------------------------------------


def build_services(cfg: Mapping[str, Any], lay: Layout) -> Services:
    """Build the API's ports from config. Nothing here may open a network connection (PLAN §10.5)."""
    specs = tuple(load_sources(cfg, lay))
    if real_backends_available(cfg):
        from wsp_api.gaps import JsonlGapLog, JsonlQueryLog
        from wsp_api.zim import ZimAssetStore

        signer = load_signer(cfg, lay)
        vault = build_vault(cfg, specs, signer, ledger=False)
        changes = open_changes(cfg)
        from wsp_oracle.services import build_oracle

        live = [s for s in specs if s.status is not SourceStatus.EXCLUDED]
        oracle = build_oracle(cfg, lexical=open_index(cfg, specs), sources=[s.id for s in live])
        return Services(
            index=oracle.index,
            documents=open_documents(cfg),
            changes=changes,
            vault=vault,
            ledger=build_ledger(cfg, signer),
            sources=effective_sources([s for s in specs if s.status is not SourceStatus.EXCLUDED], cfg),
            catalog=build_catalog(cfg, specs, changes),
            assets=ZimAssetStore(vault),  # original HTML and images from sealed ZIMs (read-only)
            gaps=JsonlGapLog(paths(cfg).gaps_log),  # local disk next to the index, never the vault
            ops=build_monitor(cfg, specs, vault),
            queries=JsonlQueryLog(paths(cfg).queries_log)
            if section(cfg, "api").get("query_log", True)
            else None,
            speller=open_speller(cfg),
        )

    from wsp_index.spelling import Speller

    index, documents, changes = InMemoryLexicalIndex(), InMemoryDocumentStore(), InMemoryChangeFeed()
    vault, ledger = InMemoryVault(), InMemoryLedger()
    seed_sample_library(index=index, documents=documents, changes=changes, vault=vault, ledger=ledger)
    return Services(
        index=index,
        documents=documents,
        changes=changes,
        vault=vault,
        ledger=ledger,
        sources=effective_sources(specs, cfg) or SAMPLE_SOURCES,  # sample specs outside a checkout
        speller=Speller.from_texts(f"{d.title} {d.text}" for d in documents.all_current()),
    )


def open_speller(cfg: Mapping[str, Any]) -> ReloadingSpeller | None:
    """Spelling corrections ("did you mean") from ``<index>/lexical/spelling.bin`` (``wsp index spell``).

    Off with ``[oracle] spelling = false``. Picks up a rebuilt vocabulary without a restart."""
    from wsp_index.spelling import SPELL_FILE, ReloadingSpeller

    if not section(cfg, "oracle").get("spelling", True):
        return None
    return ReloadingSpeller(paths(cfg).lexical / SPELL_FILE)


def build_catalog(cfg: Mapping[str, Any], specs: Sequence[SourceSpec], changes: ChangeFeed) -> Catalog:
    """The Refinery-backed catalog: data precomputed by ``wsp index build`` under ``<index>/catalog``."""
    from wsp_index.catalog import CATALOG_DIR, RefineryCatalog, is_featured_source

    p = paths(cfg)
    return RefineryCatalog(
        p.refinery,
        p.require_index() / CATALOG_DIR,
        changes,
        sources=[s.id for s in specs],
        featured_sources=[s.id for s in specs if is_featured_source(s)],
    )


def build_monitor(cfg: Mapping[str, Any], specs: Sequence[SourceSpec], vault: FileVault) -> LibraryMonitor:
    """System Monitor data for ``/v1/system`` (read-only views of local state and the vault)."""
    from wsp_cli.monitor import LibraryMonitor, default_downloads_dir
    from wsp_cli.wiring import TB, domain_usage
    from wsp_orchestrator.curator import ProposalFiles
    from wsp_vault.paritykeeper import ParityCoverage, ParityKeeper

    p = paths(cfg)
    orch, v, ops = section(cfg, "orchestrator"), section(cfg, "vault"), section(cfg, "ops")
    local = p.state_dir or p.require_index()
    keeper = ParityKeeper(vault, None, state_dir=local, scratch=p.workdir or local)
    files = ProposalFiles(p.proposals)

    def coverage(targets: Sequence[tuple[str, str]]) -> ParityCoverage:
        return keeper.coverage(targets)

    budgets = cast(Mapping[str, object], section(cfg, "curator").get("domain_budget_tb", {}))
    cycle = str(section(cfg, "acquirer").get("cycle", "monthly"))
    return LibraryMonitor(
        sources=specs,
        vault=vault,
        state_db=_opt_path(orch.get("state_db")),
        scrub_db=p.state_dir / "scrub.sqlite" if p.state_dir else None,
        proposals=lambda: files.list(),
        downloads_dir=_opt_path(ops.get("downloads_dir")) or default_downloads_dir(),
        disks={"vault": p.vault_root, "index": p.index_root, "state": p.state_dir},
        cycle_days=91.0 if cycle == "quarterly" else 30.5,
        scrub_cycle_days=int(v.get("scrub_cycle_days", 30)),
        parity_scope=str(v.get("parity_scope", "all")),
        coverage=coverage,
        domain_usage=lambda: domain_usage(vault),
        budgets={str(k): int(float(str(x)) * TB) for k, x in budgets.items()},
    )


def backend_note(cfg: Mapping[str, Any]) -> str | None:
    return None if real_backends_available(cfg) else BACKEND_NOTE


# --- Secret redaction for `wsp config show` -----------------------------------------------------

_SECRET_KEY = re.compile(
    r"(secret|token|passw|credential|private|api[_-]?key|(^|_)key$|signing)", re.IGNORECASE
)
REDACTED = "***"


def redact(cfg: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in cfg.items():
        if isinstance(value, Mapping):
            out[key] = redact(cast(Mapping[str, Any], value))
        elif _SECRET_KEY.search(key):
            out[key] = REDACTED
        else:
            out[key] = value
    return out
