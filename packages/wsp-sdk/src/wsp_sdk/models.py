"""Response models for API v1.

These deliberately mirror ``wsp_api.schemas`` instead of importing them, so that installing the SDK
does not pull in the server stack (FastAPI, Starlette, wsp-core). ``tests/test_sdk_models.py`` fails if
the two drift apart. Unknown fields are ignored, so a newer server (minor version) never breaks an
older client.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue


class _Wire(BaseModel):
    model_config = ConfigDict(frozen=True)


class ErrorBody(_Wire):
    code: str
    message: str


class ErrorResponse(_Wire):
    error: ErrorBody


class Health(_Wire):
    status: Literal["ok"] = "ok"
    api_version: str = "1.0.0"
    sources: int


class GenerationInfo(_Wire):
    source_id: str
    generation_id: str
    status: Literal["staging", "sealed", "degraded", "pruned"]
    sealed_at: datetime | None = None
    artifact_count: int = 0
    total_bytes: int = 0
    current: bool = False


class SourceInfo(_Wire):
    id: str
    name: str
    tier: Literal["T1", "T2", "T3", "X"]
    status: Literal["candidate", "active", "paused", "retired", "excluded"]
    license: str
    legal_basis: str
    domain: list[str]
    adapter: str | None = None
    cadence: str | None = None
    strategy: str | None = None
    irreplaceable: bool = False
    address: str
    current_generation: str | None = None


class SourcePage(_Wire):
    items: list[SourceInfo]
    next_cursor: str | None = None


class GenerationPage(_Wire):
    items: list[GenerationInfo]
    next_cursor: str | None = None


class Document(_Wire):
    doc_id: str
    source_id: str
    native_id: str
    title: str
    lang: str
    license: str
    text: str
    content_hash: str
    revision: str | None = None
    timestamp: datetime | None = None
    url_native: str | None = None
    html_ref: str | None = None
    entities: list[str] = Field(default_factory=list[str])
    links: list[str] = Field(default_factory=list[str])
    address: str


class DocumentVersion(_Wire):
    generation_id: str
    content_hash: str


class HistoryPage(_Wire):
    doc_id: str
    items: list[DocumentVersion]
    next_cursor: str | None = None


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
    next_offset: int | None = None


class Suggestions(_Wire):
    query: str
    suggestions: list[str]


class Change(_Wire):
    doc_id: str
    kind: Literal["added", "modified", "removed", "renamed"]
    generation_id: str
    old_hash: str | None = None
    new_hash: str | None = None
    renamed_from: str | None = None


class ChangePage(_Wire):
    items: list[Change]
    cursor: str


class SourceFreshness(_Wire):
    source_id: str
    name: str
    status: str
    cadence: str | None = None
    current_generation: str | None = None
    sealed_at: datetime | None = None
    age_seconds: float | None = None
    stale: bool


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
    ledger_tail: list[LedgerEntry]
    ledger_ok: bool


class SummarizeRequest(_Wire):
    query: str = Field(min_length=1, max_length=1000)
    doc_ids: list[str] = Field(min_length=1, max_length=20)
    max_tokens: int = Field(default=512, ge=16, le=4096)


class Citation(_Wire):
    n: int
    doc_id: str
    title: str
    address: str


class SummaryChunk(_Wire):
    type: Literal["citations", "text", "done"]
    citations: list[Citation] | None = None
    text: str | None = None


class Resolution(_Wire):
    address: str
    kind: Literal["home", "search", "directory", "system", "whatsnew", "source", "document"]
    href: str | None


class StreamedChange(_Wire):
    """SDK-only: a change from the SSE stream plus the cursor to resume from (if the event carried one)."""

    change: Change
    cursor: str | None = None
