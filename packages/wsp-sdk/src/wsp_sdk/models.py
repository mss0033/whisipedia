"""Response models for API v1.

These deliberately mirror ``wsp_api.schemas`` instead of importing them, so that installing the SDK
does not pull in the server stack (FastAPI, Starlette, wsp-core). ``tests/test_sdk_models.py`` fails if
the two drift apart. Unknown fields are ignored, so a newer server (minor version) never breaks an
older client.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, RootModel

API_VERSION = "1.1.0"  # the server API version these models were written for


class _Wire(BaseModel):
    model_config = ConfigDict(frozen=True, json_schema_serialization_defaults_required=True)


# --- Shared enums -------------------------------------------------------------------------------


class DocKind(StrEnum):
    """What kind of site a source is: drives result filters, badges and the start page (PLAN §10.3)."""

    ENCYCLOPEDIA = "encyclopedia"
    BOOKS = "books"
    PAPERS = "papers"
    LAW = "law"
    MAPS = "maps"
    QA = "qa"
    DICTIONARY = "dictionary"
    DATA = "data"
    MEDICINE = "medicine"
    NEWS = "news"
    OTHER = "other"


class SourceHealth(StrEnum):
    """Status lamp: ``offline`` = nothing promoted, ``degraded`` = current generation failed scrub."""

    OK = "ok"
    STALE = "stale"
    DEGRADED = "degraded"
    OFFLINE = "offline"


class QueryIntent(StrEnum):
    """Router intents (PLAN §9.3)."""

    LOOKUP = "lookup"
    DEFINE = "define"
    HOW_TO = "how-to"
    NAVIGATE = "navigate"
    LOCATE = "locate"
    COMPARE = "compare"
    ASK = "ask"


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
    status: Literal["candidate", "active", "paused", "retired", "excluded"] = Field(
        description="Registry lifecycle status. See `health` for the status lamp."
    )
    license: str
    legal_basis: str
    domain: list[str]
    adapter: str | None = None
    cadence: str | None = Field(default=None, description="ISO 8601 duration, e.g. `P1M`.")
    strategy: str | None = None
    irreplaceable: bool = False
    address: str = Field(description="Navigator address of the source's site, e.g. `wsp://rfc`.")
    current_generation: str | None = None
    publisher: str | None = None
    description: str | None = None
    kind: DocKind = Field(default=DocKind.OTHER, description="Derived from the first registry domain.")
    health: SourceHealth = SourceHealth.OFFLINE
    doc_count: int | None = Field(default=None, description="Live documents; null if unknown.")
    bytes: int | None = Field(default=None, description="Size of the current generation.")
    as_of: datetime | None = Field(default=None, description="When the current generation was sealed.")
    home_doc_id: str | None = Field(default=None, description="Landing document of the site, if any.")
    related: list[str] = Field(
        default_factory=list[str], description="Source ids sharing a domain (webring bar)."
    )


class SourcePage(_Wire):
    items: list[SourceInfo]
    next_cursor: str | None = Field(default=None, description="Pass as `cursor` for the next page.")


class GenerationPage(_Wire):
    items: list[GenerationInfo]
    next_cursor: str | None = None


# --- Documents ---------------------------------------------------------------------------------


class DocumentLink(_Wire):
    href: str = Field(description="The link as recorded by the Refinery (a doc id or a native URL).")
    target: str | None = Field(description="Local doc id it resolves to, or null if not in the library.")


class VaultLocation(_Wire):
    """Where the bytes live, for "View Source Data" and the direct route (PLAN §7, §10.2)."""

    generation_id: str
    path: str = Field(description="File (or generation directory) relative to the vault root.")
    manifest: str = Field(description="`MANIFEST.json` that covers the file, relative to the vault root.")
    entry: str | None = Field(default=None, description="Entry inside a container file, e.g. a ZIM path.")
    size: int | None = None
    sha256: str | None = Field(default=None, description="Manifest entry hash of the file.")
    blake3: str | None = Field(default=None, description="Manifest entry hash of the file.")


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
    timestamp: datetime | None = Field(default=None, description="Upstream time of this revision.")
    url_native: str | None = Field(default=None, description="Upstream URL. Informational; never fetched.")
    html_ref: str | None = None
    entities: list[str] = Field(default_factory=list[str], description="Wikidata QIDs.")
    links: list[DocumentLink] = Field(
        default_factory=list[DocumentLink], description="Outbound links and whether each is held locally."
    )
    address: str = Field(description="Navigator address, e.g. `wsp://rfc/9110`.")
    source_name: str
    kind: DocKind
    generation_id: str | None = Field(default=None, description="Generation that holds these bytes.")
    format: Literal["html", "text"] = Field(
        default="text", description="`html` when the original HTML is available (then `html` is set)."
    )
    html: str | None = Field(
        default=None,
        description="Original HTML with images rewritten to `/v1/assets/…` and internal links to "
        "`wsp://` addresses. Untrusted: clients must sanitize it.",
    )
    vault: VaultLocation | None = None
    has_history: bool = Field(default=False, description="True if the history has more than one version.")


class DocumentVersion(_Wire):
    generation_id: str
    content_hash: str
    revision: str | None = None
    timestamp: datetime | None = None
    kind: Literal["added", "modified"] = "modified"


class HistoryPage(_Wire):
    doc_id: str
    items: list[DocumentVersion] = Field(description="Oldest first.")
    next_cursor: str | None = None


# --- Search ------------------------------------------------------------------------------------


class Hit(_Wire):
    doc_id: str
    score: float
    title: str
    snippet: str = Field(default="", description="Plain text, never HTML.")
    source_id: str = ""
    address: str
    source_name: str = ""
    kind: DocKind = DocKind.OTHER
    license: str | None = None
    as_of: datetime | None = None
    lang: str | None = None
    url_native: str | None = None
    highlights: list[tuple[int, int]] = Field(
        default_factory=list[tuple[int, int]],
        description="`[start, end)` ranges into `snippet`, in Unicode code points.",
    )


class KindFacet(_Wire):
    kind: DocKind
    count: int


class DefinitionSense(_Wire):
    part_of_speech: str
    definition: str
    example: str | None = None


class DefinitionAnswer(_Wire):
    type: Literal["definition"] = "definition"
    term: str
    pronunciation: str | None = None
    senses: list[DefinitionSense]
    doc_id: str
    license: str


class Fact(_Wire):
    label: str
    value: str
    doc_id: str | None = None


class FactAnswer(_Wire):
    type: Literal["fact"] = "fact"
    qid: str
    label: str
    description: str
    facts: list[Fact]
    doc_id: str
    license: str


class MapAnswer(_Wire):
    type: Literal["map"] = "map"
    place: str
    lat: float
    lon: float
    zoom: int
    description: str | None = None
    doc_id: str
    license: str


InstantAnswer = Annotated[DefinitionAnswer | FactAnswer | MapAnswer, Field(discriminator="type")]


class SearchResults(_Wire):
    query: str
    offset: int
    limit: int
    hits: list[Hit]
    next_offset: int | None = Field(default=None, description="Offset of the next page, if any.")
    kind: DocKind | None = Field(default=None, description="The `kind` filter that was applied.")
    total: int = Field(default=0, description="Matching documents (a lower bound if `total_is_estimate`).")
    total_is_estimate: bool = False
    took_ms: float = 0.0
    intent: QueryIntent = QueryIntent.LOOKUP
    facets: list[KindFacet] = Field(
        default_factory=list[KindFacet], description="Counts per kind, ignoring the `kind` filter."
    )
    navigate_to: str | None = Field(
        default=None,
        description="Doc id when the query names one page (`rfc 9110`, `wikipedia Alan Turing`).",
    )
    instant: InstantAnswer | None = Field(
        default=None, description="Instant-answer box (definition, fact, map). Reserved: not produced yet."
    )
    did_you_mean: str | None = None
    low_confidence: bool = Field(default=False, description="True when retrieval found little or nothing.")


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
    source_id: str = ""
    title: str | None = Field(default=None, description="Document title, if the document is known.")
    address: str = ""


class ChangePage(_Wire):
    items: list[Change]
    cursor: str = Field(description="Opaque. Pass as `since` to continue after these items.")


class RecentChanges(_Wire):
    items: list[Change] = Field(description="Newest first.")
    cursor: str | None = Field(
        description="Change-feed cursor these items are current to: follow `/v1/changes/stream?since=` "
        "from here for live updates. Null if the feed is empty."
    )


# --- Navigator: portal, directory, random --------------------------------------------------------


class DocRef(_Wire):
    doc_id: str
    source_id: str
    title: str
    address: str


class FeaturedDoc(_Wire):
    doc_id: str
    title: str
    address: str
    blurb: str


class OnThisDay(_Wire):
    year: int
    text: str
    doc_id: str | None = None


class DidYouKnow(_Wire):
    text: str
    doc_id: str | None = None


class Portal(_Wire):
    """Start page content (`wsp://home`, PLAN §10.2)."""

    date: date
    featured: FeaturedDoc | None = Field(description="Deterministic for the day.")
    on_this_day: list[OnThisDay] = Field(default_factory=list[OnThisDay])
    did_you_know: list[DidYouKnow] = Field(default_factory=list[DidYouKnow])
    newest: list[Change] = Field(description="Newest changes first.")
    random: DocRef | None = Field(description="A random page to try.")
    random_sources: list[str] = Field(description="Sources that have pages to pick at random.")
    sites: list[SourceInfo]


class DirectoryCrumb(_Wire):
    path: str
    name: str


class DirectoryCategory(_Wire):
    path: str = Field(description="Pass as `path` to open it.")
    name: str
    count: int
    crosslink: bool = Field(default=False, description="Listed under another category too (`@`).")


class DirectoryEntry(_Wire):
    title: str
    address: str
    source_id: str
    doc_id: str | None = Field(default=None, description="Null when the entry is a whole site.")
    description: str | None = None


class DirectoryPage(_Wire):
    path: str = Field(description="Slash-separated slugs; `''` is Top.")
    name: str
    trail: list[DirectoryCrumb] = Field(description="Ancestors and this node, Top excluded.")
    children: list[DirectoryCategory]
    entries: list[DirectoryEntry]


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
    health: SourceHealth = SourceHealth.OFFLINE


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
    api_version: str = API_VERSION
    uptime_seconds: float = 0.0


# --- Summaries ---------------------------------------------------------------------------------


class SummarizeRequest(_Wire):
    query: str = Field(min_length=1, max_length=1000)
    doc_ids: list[str] = Field(min_length=1, max_length=20, description="Results to summarize, ranked.")
    max_tokens: int = Field(default=512, ge=16, le=4096)


class SummaryToken(_Wire):
    type: Literal["token"] = "token"
    text: str


class SummaryCitation(_Wire):
    type: Literal["citation"] = "citation"
    index: int = Field(description="1-based position in the request's `doc_ids`.")
    doc_id: str
    title: str
    address: str


class SummaryNotice(_Wire):
    type: Literal["notice"] = "notice"
    text: str


class SummaryDone(_Wire):
    type: Literal["done"] = "done"


class SummaryError(_Wire):
    type: Literal["error"] = "error"
    message: str


class SummaryEvent(
    RootModel[
        Annotated[
            SummaryToken | SummaryCitation | SummaryNotice | SummaryDone | SummaryError,
            Field(discriminator="type"),
        ]
    ]
):
    """One server-sent event of ``POST /v1/summarize``: ``event: <type>``, ``data: <this JSON>``.

    Order: ``notice``* then ``token``/``citation`` interleaved, then ``done`` (or ``error``).
    """

    model_config = ConfigDict(frozen=True)


SummaryItem = SummaryToken | SummaryCitation | SummaryNotice | SummaryDone | SummaryError


# --- Gaps --------------------------------------------------------------------------------------


class GapReport(_Wire):
    href: str = Field(min_length=1, max_length=2048, description="The link that is not in the library.")
    from_doc_id: str | None = Field(default=None, max_length=2048, description="Page the link was on.")


# --- Addresses ---------------------------------------------------------------------------------


class Resolution(_Wire):
    address: str = Field(description="Canonical form, e.g. `wsp://rfc/9110`.")
    kind: Literal[
        "home", "search", "directory", "system", "whatsnew", "random", "client", "source", "document"
    ] = Field(description="`client`: a page drawn entirely by the Navigator (history, hotlist, …).")
    href: str | None = Field(description="API resource to fetch; null for pages rendered client-side.")


class StreamedChange(_Wire):
    """SDK-only: a change from the SSE stream plus the cursor to resume from (if the event carried one)."""

    change: Change
    cursor: str | None = None
