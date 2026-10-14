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
  first use under ``$XDG_CONFIG_HOME/whisipedia/`` (``~/.config/whisipedia/``), never inside the repo,
  and only while the vault holds no signed content (see :func:`load_signer`).

Otherwise the in-memory sample library from :mod:`wsp_api.testing.fakes` backs the API.
"""

from __future__ import annotations

import dataclasses
import os
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from wsp_core.config import load_config
from wsp_core.model import SourceSpec, SourceStatus
from wsp_core.ports import Catalog, ChangeFeed, Fetcher, Signer, SourceAdapter

if TYPE_CHECKING:  # heavy imports stay lazy so `wsp search --url ...` starts fast (audit #11)
    from wsp_api import ApiSettings, Services
    from wsp_api.feed import ReadOnlyChangeFeed
    from wsp_cli.monitor import LibraryMonitor
    from wsp_core.ids import DocId
    from wsp_core.model import DocRecord
    from wsp_index import ParquetDocumentStore, SqliteChangeFeed, TantivyIndex
    from wsp_index.feedarchive import ChangeArchive
    from wsp_index.spelling import ReloadingSpeller
    from wsp_index.titles import TitleSources
    from wsp_vault.ledger import JsonlLedger
    from wsp_vault.vault import FileVault

LOOPBACK_HOSTS: tuple[str, ...] = ("127.0.0.1", "localhost", "::1")  # = wsp_api.services.LOOPBACK_HOSTS

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
    from wsp_core.registry import load_registry

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
    """Settings for an app run in-process by a CLI command: no background threads or worker processes;
    library metadata is read when a request needs it and reused for ``[api] snapshot_seconds``."""
    from wsp_api import ApiSettings

    api = section(cfg, "api")
    static = static_dir or (Path(api["static_dir"]) if api.get("static_dir") else lay.navigator_dist)
    extra = tuple(str(h) for h in api.get("allowed_hosts", ()))
    marker = activity_marker(cfg)
    every = _number(api, "snapshot_seconds", 30.0)
    return ApiSettings(
        static_dir=static,
        allowed_hosts=(*LOOPBACK_HOSTS, *extra),
        activity_marker=marker,
        snapshot_seconds=every,
        ops_seconds=every,
        cache_dir=api_cache_dir(cfg),
    )


def serve_settings(cfg: Mapping[str, Any], lay: Layout, *, static_dir: Path | None = None) -> ApiSettings:
    """Settings for ``wsp serve``: library metadata refreshed in the background (requests never read the
    vault for it), warm-up at start, ZIMs read in worker processes (a NAS ``SIGBUS`` kills a worker,
    not the server). Tunables under ``[api]``: ``snapshot_seconds`` (30), ``zim_workers`` (2),
    ``zim_timeout_seconds`` (15), ``page_cache_mb`` (64), ``nas_workers`` (8), ``nas_queue`` (32),
    ``nas_timeout_seconds`` (20), ``slow_request_seconds`` (2), ``cache_dir`` (local disk)."""
    api = section(cfg, "api")
    base = api_settings(cfg, lay, static_dir=static_dir)
    return dataclasses.replace(
        base,
        background=True,
        zim_workers=int(_number(api, "zim_workers", 2)),
        zim_timeout_seconds=_number(api, "zim_timeout_seconds", 15.0),
        page_cache_bytes=int(_number(api, "page_cache_mb", 64) * 1024 * 1024),
        nas_workers=max(1, int(_number(api, "nas_workers", 8))),
        nas_queue=int(_number(api, "nas_queue", 32)),
        nas_timeout_seconds=_number(api, "nas_timeout_seconds", 20.0),
        slow_request_seconds=_number(api, "slow_request_seconds", 2.0),
    )


def _number(table: Mapping[str, Any], key: str, default: float) -> float:
    value = table.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int | float) or value < 0:
        raise ConfigError(f"[api] {key} must be a non-negative number, not {value!r}")
    return float(value)


def api_cache_dir(cfg: Mapping[str, Any]) -> Path | None:
    """Local-disk cache of derived API data (the ZIM book index): ``[api] cache_dir``, else
    ``<index.root>/../cache/api``; None without an index root (kept in memory only)."""
    configured = _opt_path(section(cfg, "api").get("cache_dir"))
    root = paths(cfg).index_root
    return configured or (None if root is None else root.parent / "cache" / "api")


def activity_marker(cfg: Mapping[str, Any]) -> Path | None:
    """``<state_dir>/last-interactive``: the API marks interactive use, the Warden yields to it."""
    from wsp_core.activity import default_marker

    return default_marker(paths(cfg).state_dir)


# --- paths ------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Paths:
    """Every location the real backends use, resolved from config."""

    vault_root: Path | None
    index_root: Path | None
    state_dir: Path | None  # local orchestrator/warden state (directory of orchestrator.state_db)
    workdir: Path | None
    proposals_dir: Path | None = None
    refinery_root: Path | None = None  # [index] refinery_root: the Refinery elsewhere (the NAS, ADR-0011)
    changes_archive: Path | None = None  # [index] changes_archive: sealed old change-feed ranges (ADR-0011)

    @property
    def incoming(self) -> Path:
        return self.require_vault() / "_incoming"

    @property
    def ledger_dir(self) -> Path:
        return self.require_vault() / "ledger"

    @property
    def refinery(self) -> Path:
        return self.refinery_root or self.require_index() / "refinery"

    @property
    def refinery_maps(self) -> Path | None:
        """Local home of the Refinery's id-map sidecars when the Refinery is elsewhere (ADR-0011)."""
        return None if self.refinery_root is None else self.require_index() / "refinery-maps"

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
        refinery_root=_opt_path(section(cfg, "index").get("refinery_root")),
        changes_archive=_opt_path(section(cfg, "index").get("changes_archive")),
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
    ``<config home>/vault-signing-ed25519.pem``). Keys inside the repository checkout are refused.

    When that file is missing (vault-fetch audit #1):

    * ``create=True`` generates a key **only if the vault has no signed content yet** (no published
      key under ``keys/`` and no signed manifest). Otherwise :class:`ConfigError` explains how to
      restore the key: a new key would make every existing manifest look forged.
    * ``create=False`` (readers, dry runs) returns a verify-only keyring of the vault's published
      keys, or, for a vault without any, an ephemeral key that can sign nothing that is kept.

    Whatever key is loaded, :class:`~wsp_vault.vault.FileVault` refuses to write with one that is
    not pinned under ``keys/``.
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
    from wsp_vault.signing import KeyringVerifier
    from wsp_vault.vault import first_signed_manifest, pinned_keys

    root = paths(cfg).vault_root
    pinned = pinned_keys(root) if root is not None and root.is_dir() else []
    if not create:
        if pinned:
            return KeyringVerifier.from_public_texts([text for _, text in pinned])
        return Ed25519Signer.generate()
    signed = first_signed_manifest(root) if root is not None and root.is_dir() else None
    if pinned or signed is not None:
        labels = ", ".join(label for label, _ in pinned) or "none published"
        raise ConfigError(
            f"no signing key at {key}, but the vault at {root} already holds signed content (pinned "
            f"keys: {labels}). Refusing to create a new key: it would make every existing manifest "
            "look forged. Restore the original private key from your backup to that path, or point "
            "vault.signing_key (WSP__VAULT__SIGNING_KEY) at it."
        )
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
    from wsp_cli.locks import lock_dir_for
    from wsp_vault.parity import Par2Parity
    from wsp_vault.vault import FileVault, domains_from_specs, partitioned_from_specs

    p = paths(cfg)
    root = p.require_vault()
    parity = Par2Parity() if section(cfg, "vault").get("parity", True) and _has_par2() else None
    # Vault mutation locks: their own sub-directory (own namespace) of the machine's local lock dir.
    # They are taken inside FileVault calls only, so any wsp_cli lock (SCRUB, INGEST, ...) a caller
    # holds is always taken first: one order, no deadlock.
    lock_dir = None if p.index_root is None else lock_dir_for(p.index_root) / "vault"
    return FileVault(
        root,
        signer,
        domains=domains_from_specs(specs),
        partitioned=partitioned_from_specs(specs),
        ledger=build_ledger(cfg, signer) if ledger else None,
        parity=parity,
        lock_dir=lock_dir,
        pace=ingest_pace(cfg),
    )


INGEST_BUSY_SHARE = 0.25
"""While the owner uses the library, an ingest keeps this share of its rate: it slows down rather than
stops. A pause that raised (the scrub's way) failed the seal step of every release in flight and
counted an attempt against it (2026-10-14 01:07)."""


def ingest_pace(cfg: Mapping[str, Any]) -> Callable[[int], None] | None:
    """A rate limit for the bytes an ingest reads and writes (``[vault] ingest_bytes_per_sec``; 0 = none),
    a quarter of it while the owner uses the library (``yield_to_interactive_seconds``): on the NAS,
    one lane adopting PDFs at the link's full speed starved the API's Refinery reads (2026-10-13)."""
    from wsp_core.activity import recently_active
    from wsp_vault.ratelimit import RateLimiter

    vcfg = section(cfg, "vault")
    rate = float(vcfg.get("ingest_bytes_per_sec", 40e6))
    if rate <= 0:
        return None
    marker = activity_marker(cfg)
    window = float(vcfg.get("yield_to_interactive_seconds", 300))
    full = RateLimiter(rate, burst_seconds=5.0)
    quiet = RateLimiter(rate * INGEST_BUSY_SHARE, burst_seconds=5.0)

    def pace(n: int) -> None:
        limiter = quiet if window > 0 and recently_active(marker, window) else full
        limiter.consume(n)

    return pace


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
    if not fc.lock_dir and (index_root := paths(cfg).index_root) is not None:
        # Per-destination fetch locks in the machine's lock directory, like every other lock: the
        # orchestrator runs with PrivateTmp, so locks in /tmp never excluded a CLI fetch or a lane
        # fetching the same file (audit 2026-10-12, M4).
        from wsp_cli.locks import lock_dir_for

        fc = dataclasses.replace(fc, lock_dir=str(lock_dir_for(index_root) / "fetch"))
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
        own = spec.raw.get("rank_prior")  # the registry's per-source override (e.g. pubmed: 0)
        if isinstance(own, int | float):
            if own > 0:
                priors[spec.id] = float(own)
            continue
        bonus = table.get(spec.domain[0]) if spec.domain else None
        if isinstance(bonus, int | float) and bonus > 0:
            priors[spec.id] = float(bonus)
    return priors


def open_index(
    cfg: Mapping[str, Any], specs: Sequence[SourceSpec] = (), *, documents: ParquetDocumentStore | None = None
) -> TantivyIndex:
    """The lexical index; with ``documents`` its search snippets are cut from the Refinery's text."""
    from wsp_index import TantivyConfig, TantivyIndex, ensure_local

    p = paths(cfg)
    ensure_local(p.require_index())
    config = TantivyConfig(languages=index_languages(cfg), source_priors=source_priors(cfg, specs))
    return TantivyIndex(
        p.lexical,
        config,
        texts=None if documents is None else documents.texts,
        titles=lambda: indexed_titles(cfg),
    )


def indexed_titles(cfg: Mapping[str, Any]) -> TitleSources:
    """Titles and aliases of the indexed generations, for a full suggest rebuild (ADR-0011). The scans
    are paced by ``[index] refinery_read_mb_per_sec`` (the Refinery on the NAS shares the site's link)."""
    import json

    from wsp_cli.indexer import STATE_FILE, indexed_title_sources

    p = paths(cfg)
    root = p.require_index()
    state_path = root / STATE_FILE
    state = dict(json.loads(state_path.read_text(encoding="utf-8"))) if state_path.is_file() else {}
    rate = float(section(cfg, "index").get("refinery_read_mb_per_sec", 30))
    return indexed_title_sources(p.refinery, root / "work", state, read_mb_per_sec=rate)


def open_documents(cfg: Mapping[str, Any]) -> ParquetDocumentStore:
    from wsp_index import ParquetDocumentStore, ensure_local

    p = paths(cfg)
    ensure_local(p.require_index())
    p.refinery.mkdir(parents=True, exist_ok=True)
    return ParquetDocumentStore(p.refinery, map_root=p.refinery_maps)


def open_changes(cfg: Mapping[str, Any]) -> SqliteChangeFeed:
    from wsp_index import SqliteChangeFeed, ensure_local

    p = paths(cfg)
    ensure_local(p.require_index())
    return SqliteChangeFeed(p.changes_db, archive=change_archive(cfg))


def change_archive(cfg: Mapping[str, Any]) -> ChangeArchive | None:
    """The change feed's archive of sealed old ranges, when ``[index] changes_archive`` is set."""
    from wsp_index.feedarchive import ChangeArchive

    root = paths(cfg).changes_archive
    return None if root is None else ChangeArchive(root)


# --- API services -----------------------------------------------------------------------------------


def build_services(cfg: Mapping[str, Any], lay: Layout) -> Services:
    """Build the API's ports from config. Nothing here may open a network connection (PLAN §10.5), and
    nothing here writes to the vault: the signing key is only read (``create=False``, audit #14) and the
    change feed is opened read-only (audit #13)."""
    from wsp_api import Services

    specs = tuple(load_sources(cfg, lay))
    if real_backends_available(cfg):
        from wsp_api.gaps import JsonlGapLog, JsonlQueryLog
        from wsp_api.snapshot import memoize_domains
        from wsp_oracle.services import build_oracle

        signer = load_signer(cfg, lay, create=False)
        vault = build_vault(cfg, specs, signer, ledger=False)
        memoize_domains(vault)  # TODO(vault): FileVault.domain_of lists the NAS on every call
        changes = open_feed_read_only(cfg)
        live = [s for s in specs if s.status is not SourceStatus.EXCLUDED]
        documents = open_documents(cfg)  # one store: documents and search snippets share its caches
        oracle = build_oracle(
            cfg,
            lexical=open_index(cfg, specs, documents=documents),
            sources=[s.id for s in live],
            content_hashes=documents.content_hashes,
        )
        return Services(
            index=oracle.index,
            documents=RefineryDocuments(documents),
            changes=changes,
            vault=vault,
            ledger=build_ledger(cfg, signer),
            sources=effective_sources(live, cfg),
            catalog=build_catalog(cfg, specs, changes),
            # No `assets`: the API reads the sealed ZIMs itself, through its library snapshot (in worker
            # processes when serving; wsp_api.zimpool).
            gaps=JsonlGapLog(paths(cfg).gaps_log),  # local disk next to the index, never the vault
            ops=build_monitor(cfg, specs, vault),
            queries=JsonlQueryLog(paths(cfg).queries_log)
            if section(cfg, "api").get("query_log", True)
            else None,
            speller=open_speller(cfg),
        )

    from wsp_api.testing.fakes import (
        SAMPLE_SOURCES,
        InMemoryChangeFeed,
        InMemoryDocumentStore,
        InMemoryLedger,
        InMemoryLexicalIndex,
        InMemoryVault,
        seed_sample_library,
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


def open_feed_read_only(cfg: Mapping[str, Any]) -> ReadOnlyChangeFeed | SqliteChangeFeed:
    """The change feed for the API: read-only (``mode=ro``, a connection per thread). Only a missing
    feed (a library never indexed) is created, by the writer's class."""
    from wsp_api.feed import ReadOnlyChangeFeed

    db = paths(cfg).changes_db
    return ReadOnlyChangeFeed(db, archive=change_archive(cfg)) if db.is_file() else open_changes(cfg)


class RefineryDocuments:
    """``ParquetDocumentStore`` plus projected reads (:class:`wsp_api.seams.DocumentFields`): titles,
    existence and hit metadata read only their columns, never the article text (audit #9).
    TODO(index): ``ParquetDocumentStore.fields`` itself."""

    def __init__(self, store: ParquetDocumentStore) -> None:
        self.store = store

    def get(self, doc_id: DocId, *, at_generation: str | None = None) -> DocRecord | None:
        return self.store.get(doc_id, at_generation=at_generation)

    def history(self, doc_id: DocId) -> Sequence[tuple[str, str]]:
        return self.store.history(doc_id)

    def generations(self, source_id: str) -> list[str]:
        return self.store.generations(source_id)

    def fields(self, doc_id: DocId, columns: Sequence[str]) -> Mapping[str, object] | None:
        from wsp_core.ids import InvalidDocIdError, parse_doc_id
        from wsp_index.refinery import current_generation

        try:
            source_id, _ = parse_doc_id(doc_id)
        except InvalidDocIdError:
            return None
        gen = current_generation(self.store.root, source_id)
        reader = None if gen is None else self.store.reader(source_id, gen)
        return None if reader is None else reader.lookup(doc_id, tuple(columns))


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
        map_root=p.refinery_maps,  # the same sidecars the document store maps
    )


def build_monitor(cfg: Mapping[str, Any], specs: Sequence[SourceSpec], vault: FileVault) -> LibraryMonitor:
    """System Monitor data for ``/v1/system`` (read-only views of local state and the vault)."""
    from wsp_cli.monitor import CachedProposals, LibraryMonitor, default_downloads_dir
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
        proposals=CachedProposals(files.files),  # parsed once per file change, with libyaml
        downloads_dir=_opt_path(ops.get("downloads_dir")) or default_downloads_dir(),
        disks={"vault": p.vault_root, "index": p.index_root, "state": p.state_dir},
        cycle_days=91.0 if cycle == "quarterly" else 30.5,
        scrub_cycle_days=int(v.get("scrub_cycle_days", 30)),
        parity_scope=str(v.get("parity_scope", "all")),
        coverage=coverage,
        domain_usage=lambda: domain_usage(vault),
        budgets={str(k): int(float(str(x)) * TB) for k, x in budgets.items()},
        provenance_coverage=_provenance_coverage(vault, specs),
    )


def _provenance_coverage(vault: FileVault, specs: Sequence[SourceSpec]) -> Callable[[], Any]:
    """Signed PROVENANCE.json coverage of the sealed generations (slow: reads the vault)."""
    from wsp_cli.integrity import provenance_coverage

    ids = [s.id for s in specs]
    return lambda: provenance_coverage(vault, ids)


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
