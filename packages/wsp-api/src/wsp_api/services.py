"""What the API needs from the rest of the system: ports only, injected by a composition root."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from wsp_api.seams import OpsProvider, QueryLog, SpellingCorrector, Translator
from wsp_core.model import SourceSpec
from wsp_core.ports import (
    AssetStore,
    Catalog,
    ChangeFeed,
    DocumentStore,
    GapLog,
    Ledger,
    LexicalIndex,
    Vault,
)


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class Services:
    """Ports backing the API. Real ones come from ``wsp_cli.bootstrap``; fakes from ``wsp_api.testing``."""

    index: LexicalIndex
    documents: DocumentStore
    changes: ChangeFeed
    vault: Vault
    ledger: Ledger
    sources: Sequence[SourceSpec]
    clock: Callable[[], datetime] = _utcnow
    # Browsing ports (wsp_core.ports, v0.2). None picks the default noted on each.
    catalog: Catalog | None = None  # None: wsp_api.catalog.FeedCatalog over the change feed
    assets: AssetStore | None = None  # None: wsp_api.zim.ZimAssetStore over the vault
    gaps: GapLog | None = None  # None: POST /v1/gaps answers 501
    ops: OpsProvider | None = None  # None: /v1/system has no `ops`, /v1/proposals is empty
    queries: QueryLog | None = None  # None: searches are not logged
    speller: SpellingCorrector | None = None  # None: no "did you mean"
    translator: Translator | None = None  # None: /v1/translate says translation is not available
    _by_id: dict[str, SourceSpec] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "_by_id", {s.id: s for s in self.sources})

    def source(self, source_id: str) -> SourceSpec | None:
        return self._by_id.get(source_id)


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8737
LOOPBACK_HOSTS: tuple[str, ...] = ("127.0.0.1", "localhost", "::1")


@dataclass(frozen=True, slots=True)
class ApiSettings:
    """Server behaviour that is not a port. Built from the ``[api]`` config section."""

    static_dir: Path | None = None  # Navigator bundle (navigator/dist); None serves a placeholder page
    allowed_hosts: tuple[str, ...] = LOOPBACK_HOSTS  # Host-header allow-list (DNS-rebinding defence)
    sse_poll_seconds: float = 1.0
    sse_heartbeat_seconds: float = 15.0
    ledger_tail: int = 20
    ledger_verify_seconds: float = 300.0  # the ledger chain is replayed at most this often
    activity_marker: Path | None = None  # touched on interactive requests; scrub/parity yield (ADR-0009)
    # Library metadata (wsp_api.snapshot). `background` (the server): refreshed by background threads
    # every `snapshot_seconds`, and requests never read the vault for it. Otherwise (tests, the
    # in-process CLI) a request rebuilds what is older than `snapshot_seconds`.
    background: bool = False
    snapshot_seconds: float = 0.0
    ops_seconds: float = 0.0  # operations status, proposals and ledger tail (background: period)
    cache_dir: Path | None = None  # local disk for derived data (the ZIM book index); None: memory only
    gazetteer: Path | None = None  # the Atlas's place index (wsp maps gazetteer); None: no place search
    weather: Path | None = None  # wsp weather's local data (hot forecast runs, stations); None: none
    # ZIM reads: worker processes so a SIGBUS from the NAS cannot kill the API (wsp_api.zimpool);
    # 0 reads in-process (tests, the sample backend, the CLI).
    zim_workers: int = 0
    zim_max_open: int = 0  # open archives per worker; 0: one per Kiwix source (at least 4)
    zim_timeout_seconds: float = 15.0
    page_cache_bytes: int = 64 * 1024 * 1024  # rendered pages (rewritten HTML and text)
    # Bounded executors for routes that may block on the NAS or a ZIM worker (wsp_api.offload).
    nas_workers: int = 8
    nas_queue: int = 32
    nas_timeout_seconds: float = 20.0
    search_workers: int = 8
    search_queue: int = 32
    search_timeout_seconds: float = 30.0
    slow_request_seconds: float = 2.0  # requests slower than this (or failing) are logged
