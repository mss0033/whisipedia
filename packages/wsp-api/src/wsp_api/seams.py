"""Read-side seams of the API. Moved to :mod:`wsp_core.ports` / :mod:`wsp_core.model` in port
revision v0.2 (ADR-0005); re-exported here so existing imports keep working.

Defaults when a composition root leaves them unset (see ``wsp_api.services.Services``):

* :class:`Catalog`: :class:`wsp_api.catalog.FeedCatalog`, derived from the change feed.
* :class:`AssetStore`: :class:`wsp_api.zim.ZimAssetStore` over the vault.
* :class:`GapLog`: none, so ``POST /v1/gaps`` answers ``501``; :class:`wsp_api.gaps.JsonlGapLog`.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol, cast, runtime_checkable

from wsp_api.schemas import OpsStatus, ProposalInfo
from wsp_core.ids import DocId
from wsp_core.model import Asset, Category, Gap, Generation
from wsp_core.ports import AssetStore, Catalog, GapLog


@runtime_checkable
class OpsProvider(Protocol):
    """Operations data for ``/v1/system`` and ``/v1/proposals`` (orchestrator state, scrub, parity,
    downloads, proposals). Implemented by the composition root (``wsp_cli.monitor``); read-only."""

    def status(self, now: datetime) -> OpsStatus: ...

    def proposals(self, status: str | None = None) -> Sequence[ProposalInfo]: ...


@runtime_checkable
class SpellingCorrector(Protocol):
    """Spelling correction ("did you mean") over the library's own vocabulary (``wsp_index.spelling``)."""

    def correct(self, query: str, *, only: Iterable[str] | None = None) -> str | None:
        """The corrected query, or None when nothing changes. ``only``: words that may change."""
        ...

    def unknown(self, query: str) -> Sequence[str]:
        """Words of the query (lower case) that the vocabulary does not know."""
        ...


@runtime_checkable
class Translator(Protocol):
    """Machine translation into English on the reader's request (ADR-0013; ``wsp_oracle.translation``)."""

    def status(self) -> Mapping[str, object]:
        """``available``, ``reason``, ``model``, ``state`` (stopped, starting, ready), ``error``,
        ``languages``."""
        ...

    def translate(self, lang: str, segments: Sequence[str]) -> Sequence[Any]:
        """Each segment checked: objects with ``text`` (None when it failed its checks), ``ok``,
        ``reason``, ``cached``. Raises an exception with ``retry_after`` while it cannot run (starting,
        the GPU busy), ``ValueError`` for a language it does not translate."""
        ...

    def close(self) -> None: ...


class VaultView(Protocol):
    """The read side of :class:`wsp_core.ports.Vault` that the API uses (the server answers it from
    the in-memory library snapshot, :class:`wsp_api.snapshot.SnapshotVault`)."""

    def current(self, source_id: str) -> Generation | None: ...

    def current_set(self, source_id: str) -> Sequence[Generation]: ...

    def generations(self, source_id: str) -> Sequence[Generation]: ...

    def generation_path(self, source_id: str, generation_id: str) -> Path: ...


@runtime_checkable
class DocumentFields(Protocol):
    """Projected reads from the document store: some columns of the current version of a document,
    without decoding its text (titles for lists, existence, hit metadata; audit 2026-10-11 #9).

    Optional: a store without it is read with ``get``. Columns are ``DocRecord`` field names."""

    def fields(self, doc_id: DocId, columns: Sequence[str]) -> Mapping[str, object] | None:
        """The requested columns (plus ``doc_id``), or None when the document is not there."""
        ...


@runtime_checkable
class QueryLog(Protocol):
    """Local-only search log for the gap miner (PLAN §5.3): query text and result count."""

    def record(self, at: datetime, query: str, total: int, low_confidence: bool) -> None: ...


__all__ = [
    "Asset",
    "AssetStore",
    "Catalog",
    "Category",
    "DocumentFields",
    "Gap",
    "GapLog",
    "OpsProvider",
    "QueryLog",
    "SpellingCorrector",
    "VaultView",
]


def load_generation(vault: object, source_id: str, generation_id: str) -> Generation | None:
    """One generation with its artifacts: the vault's own lookup, else from its list of them."""
    one = getattr(vault, "generation", None)
    if callable(one):
        return cast(Generation | None, one(source_id, generation_id))
    gens = cast(Sequence[Generation], getattr(vault, "generations")(source_id))  # noqa: B009
    return next((g for g in gens if g.generation_id == generation_id), None)


def full_generation(vault: object, gen: Generation) -> Generation:
    """``gen`` with its artifacts, from a :class:`SnapshotVault` (whose lists leave them out); any
    other vault lists them already."""
    full = getattr(vault, "full", None)
    return cast(Generation, full(gen)) if callable(full) else gen
