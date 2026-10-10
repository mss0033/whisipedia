"""Ports: the interfaces every swappable component implements (PLAN §12.2).

Each port has a reference implementation in its package and a contract test suite in
``wsp_core.testing``. A replacement passes the same suite and is selected by config.

Port revision v0.2 (ADR-0005): ``Fetcher.download`` returns a :class:`~wsp_core.model.FetchResult`;
``Vault`` gains ``adopt`` and upstream checksums on ``ingest``; integrity operations live on the
separate :class:`Warden` port; ``Ledger`` gains ``tail`` and idempotent appends; ``LexicalIndex`` and
``ChangeFeed`` gain ``close``; typed errors live in :mod:`wsp_core.errors`; the API's read-side seams
(``Catalog``, ``AssetStore``, ``GapLog``) moved here from ``wsp_api.seams``.
"""

from __future__ import annotations

import random
from collections.abc import AsyncIterator, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Protocol, runtime_checkable

from wsp_core.ids import DocId
from wsp_core.model import (
    Artifact,
    Asset,
    Category,
    ChangeRecord,
    Checksum,
    DayEvent,
    DocRecord,
    FetchPlan,
    FetchResult,
    Gap,
    Generation,
    LedgerEvent,
    ReleaseRef,
    RemoteFile,
    RemoteHead,
    SearchHit,
    SourceSpec,
    Verdict,
)


@dataclass(frozen=True, slots=True)
class Ctx:
    """Execution context passed to adapters. Keeps adapters free of global state."""

    source: SourceSpec
    fetcher: Fetcher
    workdir: Path  # scratch space on local disk for this source
    user_agent: str


# --- Acquisition -------------------------------------------------------------------------------


@runtime_checkable
class Fetcher(Protocol):
    """Downloads remote files politely and resumably (PLAN §6.5).

    Failures raise :class:`wsp_core.errors.FetchError` subclasses: ``NotFoundError`` for a 404,
    ``ChecksumMismatchError`` when the bytes do not match an upstream digest.
    """

    def get_bytes(self, url: str, *, max_bytes: int = 64 * 1024 * 1024) -> bytes:
        """Small metadata documents (catalogs, checksum files, status JSON)."""
        ...

    def download(self, remote: RemoteFile, dest: Path) -> FetchResult:
        """Download ``remote`` to ``dest``, resuming a partial file if present. Tries mirrors on failure.

        Every upstream checksum in ``remote`` is verified before ``dest`` appears; the result carries
        our own SHA-256/BLAKE3 so later steps need not re-hash the file.
        """
        ...


@runtime_checkable
class SupportsHead(Protocol):
    """Optional ``Fetcher`` capability: metadata without the body (health probes, size checks)."""

    def head(self, url: str) -> RemoteHead: ...


@runtime_checkable
class SourceAdapter(Protocol):
    """Knows one kind of upstream (PLAN §6.1). Registered under entry point ``whisipedia.adapters``."""

    id: str

    def discover(self, ctx: Ctx) -> Iterable[ReleaseRef]:
        """Releases currently offered upstream, newest first."""
        ...

    def plan(self, ctx: Ctx, have: Generation | None, want: ReleaseRef) -> FetchPlan: ...

    def fetch(self, ctx: Ctx, plan: FetchPlan) -> Iterable[FetchResult]:
        """Download into ``ctx.workdir``; yields one result per completed file, in plan order."""
        ...

    def verify(self, ctx: Ctx, remote: RemoteFile, local: Path) -> Verdict:
        """Check a downloaded file against upstream checksums."""
        ...

    def documents(self, ctx: Ctx, generation_dir: Path) -> Iterator[DocRecord]:
        """Stream normalized documents from a sealed generation."""
        ...


# --- Vault ---------------------------------------------------------------------------------------


@runtime_checkable
class Vault(Protocol):
    """Write-once golden storage (PLAN §7, ADR-0002).

    Where a source lives (``library/<domain>/<id>``) is the implementation's business; the reference
    vault resolves it from :attr:`SourceSpec.vault_domain` (ADR-0005).
    """

    def stage(self, source_id: str, generation_id: str) -> Path:
        """Create (or reopen) a staging directory for a new generation."""
        ...

    def ingest(
        self,
        source_id: str,
        generation_id: str,
        local: Path,
        relpath: str,
        *,
        upstream: Sequence[Checksum] = (),
        expected: FetchResult | None = None,
    ) -> Artifact:
        """Atomically copy a local file into staging; read back and hash it.

        ``upstream`` checksums are verified (when computable) and recorded in the manifest.
        ``expected`` is the fetcher's result: the read-back must match its size and digests, which
        closes the loop from download to vault without hashing the local file a second time.
        """
        ...

    def adopt(
        self,
        source_id: str,
        generation_id: str,
        path: Path,
        relpath: str,
        *,
        upstream: Sequence[Checksum] = (),
    ) -> Artifact:
        """Move a file that already sits on the vault filesystem into staging (no copy).

        The file is hashed in place and checked against ``upstream`` before the rename. Idempotent:
        adopting again after a crash past the rename returns the staged artifact.
        """
        ...

    def seal(self, source_id: str, generation_id: str) -> Generation:
        """Write and sign the manifest, mark the generation read-only."""
        ...

    def promote(self, source_id: str, generation_id: str) -> None:
        """Atomically point ``CURRENT.txt`` at a sealed generation."""
        ...

    def current(self, source_id: str) -> Generation | None: ...

    def generations(self, source_id: str) -> Sequence[Generation]: ...

    def generation_path(self, source_id: str, generation_id: str) -> Path: ...


class ManifestView(Protocol):
    """Read-only view of a sealed generation's signed manifest."""

    @property
    def source_id(self) -> str: ...

    @property
    def generation_id(self) -> str: ...

    @property
    def sealed_at(self) -> datetime: ...

    @property
    def total_bytes(self) -> int: ...

    def artifacts(self) -> tuple[Artifact, ...]: ...


@runtime_checkable
class Warden(Protocol):
    """Integrity operations on sealed generations (PLAN §8): scrub, repair and retention use these.

    Separate from :class:`Vault` so that read and ingest paths (API, release pipeline) never hold
    the authority to degrade or prune data (ADR-0005). The reference ``FileVault`` implements both.
    """

    def manifest(self, source_id: str, generation_id: str) -> ManifestView:
        """The signed manifest, after checking the signature; raises if it is missing or invalid."""
        ...

    def mark_degraded(self, source_id: str, generation_id: str, reason: str, files: Sequence[str]) -> Path:
        """Flag a generation DEGRADED (PLAN §8.4 rung 4). Data is left untouched."""
        ...

    def prune(self, source_id: str, generation_id: str) -> Path:
        """Retire a generation per retention policy. Refuses the current or newest sealed generation.

        Returns where the generation went; implementations may move it aside rather than delete it.
        """
        ...


@runtime_checkable
class Signer(Protocol):
    def sign(self, payload: bytes) -> bytes: ...

    def verify(self, payload: bytes, signature: bytes) -> bool: ...


@runtime_checkable
class ParityProvider(Protocol):
    def create(self, files: Sequence[Path], out_dir: Path, percent: int) -> Sequence[Path]: ...

    def repair(self, files: Sequence[Path], parity_dir: Path) -> bool: ...


@runtime_checkable
class Ledger(Protocol):
    """Append-only, hash-chained event log (PLAN §8.3)."""

    def append(
        self, kind: str, subject: str, data: Mapping[str, object], *, idempotency_key: str | None = None
    ) -> LedgerEvent:
        """Append one event. With ``idempotency_key``, an event already appended under the same key is
        returned instead of appending again (the key is stored as ``data["idempotency_key"]``)."""
        ...

    def events(self, since_seq: int = 0) -> Iterator[LedgerEvent]: ...

    def tail(self, n: int) -> Sequence[LedgerEvent]:
        """The newest ``n`` events, oldest first, without replaying the whole log."""
        ...

    def verify(self) -> bool:
        """Replay the chain; False if any link or checkpoint is broken."""
        ...


# --- Index & Oracle ------------------------------------------------------------------------------


@runtime_checkable
class LexicalIndex(Protocol):
    def upsert(self, docs: Iterable[DocRecord]) -> int: ...

    def delete(self, doc_ids: Iterable[DocId]) -> int: ...

    def search(
        self, query: str, *, limit: int = 10, offset: int = 0, sources: Sequence[str] | None = None
    ) -> Sequence[SearchHit]: ...

    def suggest(self, prefix: str, *, limit: int = 10) -> Sequence[str]:
        """Title and alias autocomplete (PLAN §9.3, < 10 ms)."""
        ...

    def commit(self) -> None: ...

    def close(self) -> None:
        """Release locks and file handles. Uncommitted writes are discarded."""
        ...


@runtime_checkable
class VectorIndex(Protocol):
    def upsert(self, ids: Sequence[str], vectors: Sequence[Sequence[float]]) -> None: ...

    def delete(self, ids: Sequence[str]) -> None: ...

    def search(self, vector: Sequence[float], *, limit: int = 50) -> Sequence[tuple[str, float]]: ...


@runtime_checkable
class Embedder(Protocol):
    dimensions: int

    def embed(self, texts: Sequence[str]) -> Sequence[Sequence[float]]: ...


@runtime_checkable
class Reranker(Protocol):
    def rerank(self, query: str, hits: Sequence[SearchHit]) -> Sequence[SearchHit]: ...


@runtime_checkable
class Generator(Protocol):
    """Local LLM used only for on-request, cited summaries (PLAN §9.3, §10.3)."""

    def stream(self, prompt: str, *, max_tokens: int = 512) -> AsyncIterator[str]: ...


# --- Documents & change feed ---------------------------------------------------------------------


@runtime_checkable
class DocumentStore(Protocol):
    """Read access to normalized documents and their history (backs /v1/documents).

    Generation ids sort chronologically. A document missing from a generation is *tombstoned* there
    (removed upstream); its earlier content stays reachable through ``at_generation`` and history.
    """

    def get(self, doc_id: DocId, *, at_generation: str | None = None) -> DocRecord | None:
        """The document as of the current generation, or as of the newest generation at or before
        ``at_generation`` (which need not exist itself). ``None`` if unknown or tombstoned there."""
        ...

    def history(self, doc_id: DocId) -> Sequence[tuple[str, str]]:
        """Content versions as ``(generation_id, content_hash)``, oldest first.

        Only content changes are listed: a generation whose content equals the previous one is
        skipped, a removal produces no entry, and re-appearing after a removal is a new entry.
        """
        ...


@runtime_checkable
class ChangeFeed(Protocol):
    def publish(self, changes: Iterable[ChangeRecord]) -> str:
        """Append changes; returns the new cursor."""
        ...

    def since(self, cursor: str | None, *, limit: int = 1000) -> tuple[Sequence[ChangeRecord], str]:
        """Changes after ``cursor`` (``None``: from the start) and the cursor to resume from.

        Raises :class:`wsp_core.errors.InvalidCursorError` for a cursor this feed did not issue.
        """
        ...

    def close(self) -> None: ...


# --- Browsing (Navigator portal, directory, original HTML, gaps) ----------------------------------------


@runtime_checkable
class Catalog(Protocol):
    """Browse the library: random pages, counts, newest changes, per-source categories."""

    def doc_count(self, source_id: str) -> int | None:
        """Live documents of a source, or None if unknown."""
        ...

    def random_doc(self, *, sources: Sequence[str] | None, rng: random.Random) -> DocId | None:
        """A uniformly random live document (restricted to ``sources`` if given)."""
        ...

    def recent(
        self, *, limit: int, sources: Sequence[str] | None = None
    ) -> tuple[Sequence[ChangeRecord], str | None]:
        """Newest changes first, plus the change-feed cursor they are current to (None: empty feed)."""
        ...

    def categories(self, source_id: str) -> Sequence[Category]: ...

    def category_docs(self, source_id: str, slug: str, *, limit: int) -> Sequence[DocId]: ...


@runtime_checkable
class AssetStore(Protocol):
    """Original HTML and embedded files inside sealed containers (ZIM)."""

    def html(self, source_id: str, generation_id: str, html_ref: str) -> str | None:
        """Original HTML for ``DocRecord.html_ref`` (e.g. ``zim:A/Alan_Turing``), or None."""
        ...

    def asset(self, source_id: str, generation_id: str, path: str) -> Asset | None:
        """A file inside the generation's containers, addressed like a native id (e.g. ``I/logo.png``)."""
        ...


@runtime_checkable
class GapLog(Protocol):
    """Append-only "not in library" reports (local disk, never the vault)."""

    def record(self, gap: Gap) -> None: ...


@runtime_checkable
class PortalSource(Protocol):
    """Optional :class:`Catalog` capability: start-page picks precomputed at index-build time.

    Checked with ``isinstance``; a catalog without it gets a random featured page and no
    "On this day" (PLAN §10.2).
    """

    def featured(self, day: date) -> DocId | None:
        """A well-linked encyclopedia article, the same for the whole ``day``."""
        ...

    def on_this_day(self, day: date, *, limit: int = 5) -> Sequence[DayEvent]:
        """Anniversaries for ``day``'s month and day (empty if no date pages are indexed)."""
        ...
