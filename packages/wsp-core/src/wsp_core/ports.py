"""Ports: the interfaces every swappable component implements (PLAN §12.2).

Each port has a reference implementation in its package and a contract test suite in
``wsp_core.testing``. A replacement passes the same suite and is selected by config.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from wsp_core.ids import DocId
from wsp_core.model import (
    Artifact,
    ChangeRecord,
    DocRecord,
    FetchPlan,
    Generation,
    LedgerEvent,
    ReleaseRef,
    RemoteFile,
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
    """Downloads remote files politely and resumably (PLAN §6.5)."""

    def get_bytes(self, url: str, *, max_bytes: int = 64 * 1024 * 1024) -> bytes:
        """Small metadata documents (catalogs, checksum files, status JSON)."""
        ...

    def download(self, remote: RemoteFile, dest: Path) -> Path:
        """Download ``remote`` to ``dest``, resuming a partial file if present. Tries mirrors on failure."""
        ...


@runtime_checkable
class SourceAdapter(Protocol):
    """Knows one kind of upstream (PLAN §6.1). Registered under entry point ``whisipedia.adapters``."""

    id: str

    def discover(self, ctx: Ctx) -> Iterable[ReleaseRef]:
        """Releases currently offered upstream, newest first."""
        ...

    def plan(self, ctx: Ctx, have: Generation | None, want: ReleaseRef) -> FetchPlan: ...

    def fetch(self, ctx: Ctx, plan: FetchPlan) -> Iterable[Path]:
        """Download into ``ctx.workdir``; yields local paths of completed files."""
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
    """Write-once golden storage (PLAN §7, ADR-0002)."""

    def stage(self, source_id: str, generation_id: str) -> Path:
        """Create (or reopen) a staging directory for a new generation."""
        ...

    def ingest(self, source_id: str, generation_id: str, local: Path, relpath: str) -> Artifact:
        """Atomically copy a verified local file into staging; read back and hash it."""
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

    def append(self, kind: str, subject: str, data: Mapping[str, object]) -> LedgerEvent: ...

    def events(self, since_seq: int = 0) -> Iterator[LedgerEvent]: ...

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
        """Title autocomplete (PLAN §9.3, < 10 ms)."""
        ...

    def commit(self) -> None: ...


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
    """Read access to normalized documents and their history (backs /v1/documents)."""

    def get(self, doc_id: DocId, *, at_generation: str | None = None) -> DocRecord | None: ...

    def history(self, doc_id: DocId) -> Sequence[tuple[str, str]]:
        """``(generation_id, content_hash)`` pairs, oldest first."""
        ...


@runtime_checkable
class ChangeFeed(Protocol):
    def publish(self, changes: Iterable[ChangeRecord]) -> str:
        """Append changes; returns the new cursor."""
        ...

    def since(self, cursor: str | None, *, limit: int = 1000) -> tuple[Sequence[ChangeRecord], str]: ...
