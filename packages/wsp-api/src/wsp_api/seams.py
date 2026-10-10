"""Read-side seams of the API. Moved to :mod:`wsp_core.ports` / :mod:`wsp_core.model` in port
revision v0.2 (ADR-0005); re-exported here so existing imports keep working.

Defaults when a composition root leaves them unset (see ``wsp_api.services.Services``):

* :class:`Catalog`: :class:`wsp_api.catalog.FeedCatalog`, derived from the change feed.
* :class:`AssetStore`: :class:`wsp_api.zim.ZimAssetStore` over the vault.
* :class:`GapLog`: none, so ``POST /v1/gaps`` answers ``501``; :class:`wsp_api.gaps.JsonlGapLog`.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol, runtime_checkable

from wsp_api.schemas import OpsStatus, ProposalInfo
from wsp_core.model import Asset, Category, Gap
from wsp_core.ports import AssetStore, Catalog, GapLog


@runtime_checkable
class OpsProvider(Protocol):
    """Operations data for ``/v1/system`` and ``/v1/proposals`` (orchestrator state, scrub, parity,
    downloads, proposals). Implemented by the composition root (``wsp_cli.monitor``); read-only."""

    def status(self, now: datetime) -> OpsStatus: ...

    def proposals(self, status: str | None = None) -> Sequence[ProposalInfo]: ...


@runtime_checkable
class QueryLog(Protocol):
    """Local-only search log for the gap miner (PLAN §5.3): query text and result count."""

    def record(self, at: datetime, query: str, total: int, low_confidence: bool) -> None: ...


__all__ = ["Asset", "AssetStore", "Catalog", "Category", "Gap", "GapLog", "OpsProvider", "QueryLog"]
