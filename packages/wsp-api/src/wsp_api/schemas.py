"""Wire models for HTTP ``/v1`` (PLAN §11.1, §12.2).

This module is the source of truth for the public JSON shapes. It imports nothing but pydantic so it
can be read (and mirrored by ``wsp_sdk.models``) without pulling in the server stack. Adding an
optional field is a minor change; removing or renaming one is breaking and needs an ADR.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

API_VERSION = "1.0.0"


class _Wire(BaseModel):
    model_config = ConfigDict(frozen=True)


# --- Errors ------------------------------------------------------------------------------------


class ErrorBody(_Wire):
    code: str = Field(description="Stable machine-readable error code, e.g. `document_not_found`.")
    message: str = Field(description="Human-readable explanation. Not stable; do not parse.")


class ErrorResponse(_Wire):
    """Every non-2xx response has this shape."""

    error: ErrorBody


# --- Health ------------------------------------------------------------------------------------


class Health(_Wire):
    status: Literal["ok"] = "ok"
    api_version: str = API_VERSION
    sources: int = Field(description="Number of sources in the registry.")


# --- Sources & generations ---------------------------------------------------------------------


class GenerationInfo(_Wire):
    source_id: str
    generation_id: str = Field(description="Snapshot handle, e.g. `2026-10-01`. Immutable once sealed.")
    status: Literal["staging", "sealed", "degraded", "pruned"]
    sealed_at: datetime | None = None
    artifact_count: int = 0
    total_bytes: int = 0
    current: bool = Field(default=False, description="True if this is the promoted generation.")


class SourceInfo(_Wire):
    id: str
    name: str
    tier: Literal["T1", "T2", "T3", "X"]
    status: Literal["candidate", "active", "paused", "retired", "excluded"]
    license: str
    legal_basis: str
    domain: list[str]
    adapter: str | None = None
    cadence: str | None = Field(default=None, description="ISO 8601 duration, e.g. `P1M`.")
    strategy: str | None = None
    irreplaceable: bool = False
    address: str = Field(description="Navigator address of the source's site, e.g. `wsp://rfc`.")
    current_generation: str | None = None


class SourcePage(_Wire):
    items: list[SourceInfo]
    next_cursor: str | None = Field(default=None, description="Pass as `cursor` for the next page.")


class GenerationPage(_Wire):
    items: list[GenerationInfo]
    next_cursor: str | None = None


# --- Documents ---------------------------------------------------------------------------------


class Document(_Wire):
    doc_id: str = Field(description="Stable id `wsp:<source_id>/<native_id>` (PLAN §9.2).")
    source_id: str
    native_id: str
    title: str
    lang: str
    license: str
    text: str
    content_hash: str = Field(description="BLAKE3 of the normalized text: the content-version id.")
    revision: str | None = None
    timestamp: datetime | None = None
    url_native: str | None = None
    html_ref: str | None = None
    entities: list[str] = Field(default_factory=list[str])
    links: list[str] = Field(default_factory=list[str])
    address: str = Field(description="Navigator address, e.g. `wsp://rfc/9110`.")


class DocumentVersion(_Wire):
    generation_id: str
    content_hash: str


class HistoryPage(_Wire):
    doc_id: str
    items: list[DocumentVersion] = Field(description="Oldest first.")
    next_cursor: str | None = None


# --- Search ------------------------------------------------------------------------------------


class Hit(_Wire):
    doc_id: str
    score: float
    title: str
    snippet: str = ""
    source_id: str = ""
    address: str


class SearchResults(_Wire):
    query: str
    offset: int
    limit: int
    hits: list[Hit]
    next_offset: int | None = Field(default=None, description="Offset of the next page, if any.")


class Suggestions(_Wire):
    query: str
    suggestions: list[str]


# --- Change feed -------------------------------------------------------------------------------


class Change(_Wire):
    doc_id: str
    kind: Literal["added", "modified", "removed", "renamed"]
    generation_id: str
    old_hash: str | None = None
    new_hash: str | None = None
    renamed_from: str | None = None


class ChangePage(_Wire):
    items: list[Change]
    cursor: str = Field(description="Opaque. Pass as `since` to continue after these items.")


# --- System Monitor ----------------------------------------------------------------------------


class SourceFreshness(_Wire):
    source_id: str
    name: str
    status: str
    cadence: str | None = None
    current_generation: str | None = None
    sealed_at: datetime | None = None
    age_seconds: float | None = None
    stale: bool = Field(description="True if older than the cadence, or active with nothing promoted.")


class LedgerEntry(_Wire):
    seq: int
    at: datetime
    kind: str
    subject: str
    data: dict[str, JsonValue]
    hash: str


class SystemStatus(_Wire):
    generated_at: datetime
    freshness: list[SourceFreshness]
    degraded: list[GenerationInfo]
    ledger_tail: list[LedgerEntry] = Field(description="Most recent events, oldest first.")
    ledger_ok: bool = Field(description="Result of replaying the ledger hash chain.")


# --- Summaries ---------------------------------------------------------------------------------


class SummarizeRequest(_Wire):
    query: str = Field(min_length=1, max_length=1000)
    doc_ids: list[str] = Field(min_length=1, max_length=20, description="Results to summarize, ranked.")
    max_tokens: int = Field(default=512, ge=16, le=4096)


class Citation(_Wire):
    n: int = Field(description="Marker used in the text, e.g. `[1]`.")
    doc_id: str
    title: str
    address: str


class SummaryChunk(_Wire):
    """One line of the ``application/x-ndjson`` stream from ``POST /v1/summarize``.

    Order: one ``citations`` chunk, then ``text`` chunks, then one ``done`` chunk.
    """

    type: Literal["citations", "text", "done"]
    citations: list[Citation] | None = None
    text: str | None = None


# --- Addresses ---------------------------------------------------------------------------------


class Resolution(_Wire):
    address: str = Field(description="Canonical form, e.g. `wsp://rfc/9110`.")
    kind: Literal["home", "search", "directory", "system", "whatsnew", "source", "document"]
    href: str | None = Field(description="API resource to fetch; null for pages rendered client-side.")
