"""Read-side seams of the API. Moved to :mod:`wsp_core.ports` / :mod:`wsp_core.model` in port
revision v0.2 (ADR-0005); re-exported here so existing imports keep working.

Defaults when a composition root leaves them unset (see ``wsp_api.services.Services``):

* :class:`Catalog`: :class:`wsp_api.catalog.FeedCatalog`, derived from the change feed.
* :class:`AssetStore`: :class:`wsp_api.zim.ZimAssetStore` over the vault.
* :class:`GapLog`: none, so ``POST /v1/gaps`` answers ``501``; :class:`wsp_api.gaps.JsonlGapLog`.
"""

from __future__ import annotations

from wsp_core.model import Asset, Category, Gap
from wsp_core.ports import AssetStore, Catalog, GapLog

__all__ = ["Asset", "AssetStore", "Catalog", "Category", "Gap", "GapLog"]
