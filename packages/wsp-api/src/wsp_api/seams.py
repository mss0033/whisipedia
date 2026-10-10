"""Read-side seams the API needs beyond ``wsp_core.ports`` (Navigator portal, directory, assets, gaps).

They are defined here, not in ``wsp_core.ports``, while the core ports are being revised. Each one is
small and has a working default so the composition root does not have to wire it yet:

* :class:`Catalog`: browse the library (random pages, counts, newest changes, per-source categories).
  Default: :class:`wsp_api.catalog.FeedCatalog`, derived from the change feed.
* :class:`AssetStore`: original HTML and images inside sealed container files (ZIM).
  Default: :class:`wsp_api.zim.ZimAssetStore` over the vault.
* :class:`GapLog`: "not in library" reports for the gap miner (PLAN §5.3, §10.2). Default: none, so
  ``POST /v1/gaps`` answers ``501``; :class:`wsp_api.gaps.JsonlGapLog` is the reference implementation.

TODO(port): move these Protocols to ``wsp_core.ports`` with contract tests in ``wsp_core.testing``.
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

from wsp_core.ids import DocId
from wsp_core.model import ChangeRecord


@dataclass(frozen=True, slots=True)
class Category:
    """A top-level category of one source (e.g. a Gutenberg LCC class or a Wikidata class)."""

    slug: str  # path segment, [a-z0-9-]+
    name: str
    count: int


@runtime_checkable
class Catalog(Protocol):
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


@dataclass(frozen=True, slots=True)
class Asset:
    content: bytes
    mimetype: str


@runtime_checkable
class AssetStore(Protocol):
    def html(self, source_id: str, generation_id: str, html_ref: str) -> str | None:
        """Original HTML for ``DocRecord.html_ref`` (e.g. ``zim:A/Alan_Turing``), or None."""
        ...

    def asset(self, source_id: str, generation_id: str, path: str) -> Asset | None:
        """A file inside the generation's containers, addressed like a native id (e.g. ``I/logo.png``)."""
        ...


@dataclass(frozen=True, slots=True)
class Gap:
    at: datetime
    href: str  # the link as it appeared, or a wsp:// address that did not resolve
    from_doc_id: str | None = None


@runtime_checkable
class GapLog(Protocol):
    def record(self, gap: Gap) -> None: ...
