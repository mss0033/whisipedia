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

from pydantic import BaseModel, ConfigDict, Field, JsonValue

API_VERSION = "1.4.0"  # the server API version these models were written for


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
    VIDEO = "video"
    AUDIO = "audio"
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
    readable: bool = Field(
        default=False,
        description="The site has something to read (held, with documents or served from ZIM archives); "
        "planned sources and raw data are not linked as sites.",
    )
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


class Rendition(_Wire):
    source_id: str
    generation_id: str
    doc_id: str


class OriginalFile(_Wire):
    url: str
    name: str
    media_type: str
    inline: bool
    preview_url: str | None = None


class Document(_Wire):
    doc_id: str = Field(description="Stable id `wsp:<source_id>/<native_id>` (PLAN §9.2).")
    source_id: str
    native_id: str
    title: str
    lang: str
    license: str
    text: str | None = Field(
        description="Plain text. Null for an HTML page unless asked for with `fields=text`; always set "
        "when `format` is `text`."
    )
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
    currency: str | None = Field(
        default=None,
        description="Edition status for document collections (current, superseded, historical ...).",
    )
    banner: str | None = Field(
        default=None, description="Reader warning to show above the document (PLAN D13)."
    )
    rendition: Rendition | None = None
    original: OriginalFile | None = None


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


class SearchScope(_Wire):
    site: str = Field(description="The site named in the query, as typed (`rfc` in `rfc 9110`).")
    sources: list[str] = Field(description="Source ids that site names.")
    fallback: bool = Field(
        default=False,
        description="True when those sources had nothing for the query and the whole library was searched.",
    )


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
    timings_ms: dict[str, float] = Field(
        default_factory=dict[str, float],
        description="Diagnostics: milliseconds per stage. `oracle.*` are the ranking pipeline's own, from "
        "the run that produced the results (a cached result repeats them). Not a contract; stages may "
        "change.",
    )
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
    did_you_mean: str | None = Field(
        default=None,
        description="A spelling correction from the library's vocabulary, offered when results are sparse "
        "or a query word is unknown.",
    )
    scope: SearchScope | None = Field(
        default=None,
        description="Set when the query named a site (`rfc 9110`): totals and facets count that scope.",
    )
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
    total: int | None = None


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


class SourceSlo(_Wire):
    """Freshness against the source's SLO (PLAN §6.6)."""

    source_id: str
    slo_class: Literal["wikimedia", "zim", "scholarly", "law", "default"]
    target_days: float = Field(description="Allowed lag behind upstream.")
    lag_days: float | None = Field(
        default=None, description="How long upstream has had a newer release than ours; 0 if current."
    )
    within_slo: bool | None = Field(default=None, description="Null when upstream was never observed.")
    upstream_newest: str | None = Field(default=None, description="Newest version upstream offered.")
    paused: bool = False
    pause_reasons: list[str] = Field(default_factory=list[str])


class ScrubStatus(_Wire):
    files_known: int
    verified_in_cycle: int
    failing: int
    fraction: float = Field(description="Share of known files verified within the scrub cycle.")
    cycle_days: int
    oldest_check: datetime | None = None


class ParityStatus(_Wire):
    scope: str = Field(description="`all` or `irreplaceable` (vault.parity_scope).")
    available: bool = Field(description="False when par2 is not installed or parity is off.")
    generations: int
    covered: int
    bytes_total: int
    bytes_covered: int
    fraction: float
    states: dict[str, int] = Field(description="Generations per parity state (none, partial, sidecar, ...).")
    last_run: dict[str, JsonValue] | None = None


class DiskUsage(_Wire):
    name: Literal["vault", "index", "state"]
    total_bytes: int
    free_bytes: int
    used_bytes: int


class DomainBudget(_Wire):
    domain: str
    used_bytes: int
    budget_bytes: int | None = None


class DownloadLane(_Wire):
    """A bulk download lane (aria2 session) from the bootstrap downloads."""

    name: str
    state: Literal["active", "idle", "finished", "unknown"]
    last_activity: datetime | None = None
    completed: int = Field(default=0, description="Files reported complete in the lane's log.")
    last_completed: str | None = Field(default=None, description="File name of the newest completion.")
    progress: str | None = Field(default=None, description="Latest progress line, if the lane is running.")


class QueueDepth(_Wire):
    kind: str
    status: Literal["pending", "running"]
    count: int


class AlertInfo(_Wire):
    key: str
    severity: str
    message: str
    raised_at: datetime


class BotRun(_Wire):
    name: str
    at: datetime | None = None
    ok: bool | None = None
    candidates: int = 0
    error: str | None = None


class ProposalInfo(_Wire):
    id: int
    key: str
    title: str
    status: Literal["open", "applied", "accepted", "rejected"]
    decision: Literal["auto-promote", "review", "reject"]
    score: float | None = None
    kind: str
    found_by: str
    created_at: datetime | None = None
    decided_at: datetime | None = None
    body: str = Field(default="", description="Markdown: score breakdown and checks.")


class ProposalPage(_Wire):
    items: list[ProposalInfo] = Field(description="Newest first.")


class ProposalSummary(_Wire):
    open: int = 0
    applied: int = 0
    accepted: int = 0
    rejected: int = 0
    latest: list[ProposalInfo] = Field(default_factory=list[ProposalInfo], description="Newest open ones.")


class SourceIntegrity(_Wire):
    source_id: str
    channel: str = Field(
        description="Weakest checksum-channel status of the last checked release: `independent`,"
        " `same-origin`, `unavailable`, `mismatch` or `never checked` (ADR-0009)."
    )
    version: str | None = None
    verdict: str | None = Field(
        default=None, description="Strongest integrity action: info, alert, hold, quarantine."
    )
    checked_at: datetime | None = None


class HeldRelease(_Wire):
    source_id: str
    version: str
    state: str
    reason: str | None = None


class IntegrityStatus(_Wire):
    """Source integrity (ADR-0009): provenance, verification channels, holds and origin drift."""

    provenance_generations: int = Field(description="Sealed generations checked for PROVENANCE.json.")
    provenance_covered: int
    provenance_fraction: float
    provenance_invalid: list[str] = Field(
        description="Generations whose PROVENANCE.json fails its signature."
    )
    channels: list[SourceIntegrity]
    held: list[HeldRelease] = Field(description="Releases held for `wsp approve` / `wsp reject`.")
    quarantined: list[HeldRelease] = Field(description="Releases quarantined as possibly compromised.")
    drift_alerts: list[AlertInfo] = Field(description="Open origin-drift and integrity alerts.")
    pinned_hosts: int = 0


class AutonomyStatus(_Wire):
    """The plan's phase-5 exit (PLAN §14): 30 days unattended, every SLO met, no manual repairs."""

    goal_days: int = 30
    days: float = Field(description="Days since the streak began (0 while a critical alert is open).")
    since: datetime | None = Field(default=None, description="When the streak began.")
    reset_by: str = Field(description="What began it: the newest critical alert or owner intervention.")
    critical_open: int = Field(description="Critical alerts open now (the streak cannot grow).")
    slo_failing: int = Field(description="Sources behind their freshness SLO now.")


class TrawlProblem(_Wire):
    at: datetime
    address: str
    source_id: str
    problem: str = Field(description="The first problem found on the page.")


class TrawlerStatus(_Wire):
    """What the trawler (navigator/scripts/trawl.mjs) found opening pages nobody had opened yet."""

    pages: int = Field(description="Pages checked so far.")
    failed: int = Field(description="Pages with at least one problem, in all.")
    failed_last_week: int
    with_defects: int = Field(
        default=0, description="Pages that work but have a missing image or a link into nothing."
    )
    sources_checked: int
    catalog_without_link: int = Field(description="Catalog cards that do not link their book directly.")
    updated: datetime | None = None
    recent: list[TrawlProblem] = Field(description="The latest problems, newest first (at most 20).")


class OpsStatus(_Wire):
    """Operations data for the System Monitor (PLAN §12.6); every part tolerates missing state."""

    slo: list[SourceSlo]
    scrub: ScrubStatus | None = None
    parity: ParityStatus | None = None
    disk: list[DiskUsage]
    budgets: list[DomainBudget]
    lanes: list[DownloadLane]
    queue: list[QueueDepth]
    alerts: list[AlertInfo]
    bots: list[BotRun]
    proposals: ProposalSummary
    orchestrator_state: bool = Field(description="False until the orchestrator has created its state DB.")
    integrity: IntegrityStatus | None = Field(default=None, description="Source integrity (ADR-0009).")
    trawler: TrawlerStatus | None = Field(default=None, description="Null until the trawler has run.")
    autonomy: AutonomyStatus | None = Field(default=None, description="Null without orchestrator data.")


class SystemStatus(_Wire):
    generated_at: datetime
    freshness: list[SourceFreshness]
    degraded: list[GenerationInfo]
    ledger_tail: list[LedgerEntry] = Field(description="Most recent events, oldest first.")
    ledger_ok: bool | None = Field(
        description="Result of replaying the ledger hash chain; null until the first replay has finished."
    )
    api_version: str = API_VERSION
    uptime_seconds: float = 0.0
    ops: OpsStatus | None = Field(default=None, description="Null without orchestrator data.")


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
