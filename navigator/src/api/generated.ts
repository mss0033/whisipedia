/**
 * Whisipedia API v1.4.0: types generated from docs/api/openapi.json
 * by scripts/gen-api.mjs. Do not edit; run `npm run gen:api`.
 */

export const API_VERSION = '1.4.0';

/** Marks a `text/event-stream` response; `T` is the JSON in each `data:` line. */
export type EventStream<T> = { readonly __events: T };

export interface AlertInfo {
  key: string;
  severity: string;
  message: string;
  raised_at: string;
}

export interface BotRun {
  name: string;
  at: string | null;
  ok: boolean | null;
  candidates: number;
  error: string | null;
}

export interface Change {
  doc_id: string;
  kind: 'added' | 'modified' | 'removed' | 'renamed';
  generation_id: string;
  old_hash: string | null;
  new_hash: string | null;
  renamed_from: string | null;
  source_id: string;
  /** Document title, if the document is known. */
  title: string | null;
  address: string;
}

export interface ChangePage {
  items: Array<Change>;
  /** Opaque. Pass as `since` to continue after these items. */
  cursor: string;
}

export interface DefinitionAnswer {
  type: 'definition';
  term: string;
  pronunciation: string | null;
  senses: Array<DefinitionSense>;
  doc_id: string;
  license: string;
}

export interface DefinitionSense {
  part_of_speech: string;
  definition: string;
  example: string | null;
}

export interface DidYouKnow {
  text: string;
  doc_id: string | null;
}

export interface DirectoryCategory {
  /** Pass as `path` to open it. */
  path: string;
  name: string;
  count: number;
  /** Listed under another category too (`@`). */
  crosslink: boolean;
}

export interface DirectoryCrumb {
  path: string;
  name: string;
}

export interface DirectoryEntry {
  title: string;
  address: string;
  source_id: string;
  /** Null when the entry is a whole site. */
  doc_id: string | null;
  description: string | null;
}

export interface DirectoryPage {
  /** Slash-separated slugs; `''` is Top. */
  path: string;
  name: string;
  /** Ancestors and this node, Top excluded. */
  trail: Array<DirectoryCrumb>;
  children: Array<DirectoryCategory>;
  entries: Array<DirectoryEntry>;
}

export interface DiskUsage {
  name: 'vault' | 'index' | 'state';
  total_bytes: number;
  free_bytes: number;
  used_bytes: number;
}

/** What kind of site a source is: drives result filters, badges and the start page (PLAN §10.3). */
export type DocKind = 'encyclopedia' | 'books' | 'papers' | 'law' | 'maps' | 'qa' | 'dictionary' | 'data' | 'medicine' | 'news' | 'other';

export interface DocRef {
  doc_id: string;
  source_id: string;
  title: string;
  address: string;
}

export interface Document {
  /** Stable id `wsp:<source_id>/<native_id>` (PLAN §9.2). */
  doc_id: string;
  source_id: string;
  native_id: string;
  title: string;
  lang: string;
  license: string;
  /** Plain text. Null for an HTML page unless asked for with `fields=text`; always set when `format` is `text`. */
  text: string | null;
  /** BLAKE3 of the normalized text: the content-version id. */
  content_hash: string;
  revision: string | null;
  /** Upstream time of this revision. */
  timestamp: string | null;
  /** Upstream URL. Informational; never fetched. */
  url_native: string | null;
  html_ref: string | null;
  /** Wikidata QIDs. */
  entities: Array<string>;
  /** Outbound links and whether each is held locally. */
  links: Array<DocumentLink>;
  /** Navigator address, e.g. `wsp://rfc/9110`. */
  address: string;
  source_name: string;
  kind: DocKind;
  /** Generation that holds these bytes. */
  generation_id: string | null;
  /** `html` when the original HTML is available (then `html` is set). */
  format: 'html' | 'text';
  /** Original HTML with images rewritten to `/v1/assets/…` and internal links to `wsp://` addresses. Untrusted: clients must sanitize it. */
  html: string | null;
  vault: VaultLocation | null;
  /** True if the history has more than one version. */
  has_history: boolean;
  /** Edition status for document collections: current, superseded, historical, rescinded or withdrawn-from-index (PLAN D13). Absent for sources without editions. */
  currency: string | null;
  /** Reader warning to show above the document, e.g. 'Historical document — methods may be outdated or unsafe.' (PLAN D13). Absent when there is nothing to warn about. */
  banner: string | null;
}

export interface DocumentLink {
  /** The link as recorded by the Refinery (a doc id or a native URL). */
  href: string;
  /** Local doc id it resolves to, or null if not in the library. */
  target: string | null;
}

export interface DocumentVersion {
  generation_id: string;
  content_hash: string;
  revision: string | null;
  timestamp: string | null;
  kind: 'added' | 'modified';
}

export interface DomainBudget {
  domain: string;
  used_bytes: number;
  budget_bytes: number | null;
}

/** A bulk download lane (aria2 session) from the bootstrap downloads. */
export interface DownloadLane {
  name: string;
  state: 'active' | 'idle' | 'finished' | 'unknown';
  last_activity: string | null;
  /** Files reported complete in the lane's log. */
  completed: number;
  /** File name of the newest completion. */
  last_completed: string | null;
  /** Latest progress line, if the lane is running. */
  progress: string | null;
}

export interface ErrorBody {
  /** Stable machine-readable error code, e.g. `document_not_found`. */
  code: string;
  /** Human-readable explanation. Not stable; do not parse. */
  message: string;
}

/** Every non-2xx response has this shape. */
export interface ErrorResponse {
  error: ErrorBody;
}

export interface Fact {
  label: string;
  value: string;
  doc_id: string | null;
}

export interface FactAnswer {
  type: 'fact';
  qid: string;
  label: string;
  description: string;
  facts: Array<Fact>;
  doc_id: string;
  license: string;
}

export interface FeaturedDoc {
  doc_id: string;
  title: string;
  address: string;
  blurb: string;
}

export interface GapReport {
  /** The link that is not in the library. */
  href: string;
  /** Page the link was on. */
  from_doc_id?: string | null;
}

export interface GenerationInfo {
  source_id: string;
  /** Snapshot handle, e.g. `2026-10-01`. Immutable once sealed. */
  generation_id: string;
  status: 'staging' | 'sealed' | 'degraded' | 'pruned';
  sealed_at: string | null;
  artifact_count: number;
  total_bytes: number;
  /** True if this is the promoted generation. */
  current: boolean;
}

export interface GenerationPage {
  items: Array<GenerationInfo>;
  next_cursor: string | null;
}

export interface Health {
  status: 'ok';
  api_version: string;
  /** Number of sources in the registry. */
  sources: number;
}

export interface HeldRelease {
  source_id: string;
  version: string;
  state: string;
  reason: string | null;
}

export interface HistoryPage {
  doc_id: string;
  /** Oldest first. */
  items: Array<DocumentVersion>;
  next_cursor: string | null;
}

export interface Hit {
  doc_id: string;
  score: number;
  title: string;
  /** Plain text, never HTML. */
  snippet: string;
  source_id: string;
  address: string;
  source_name: string;
  kind: DocKind;
  license: string | null;
  as_of: string | null;
  lang: string | null;
  url_native: string | null;
  /** `[start, end)` ranges into `snippet`, in Unicode code points. */
  highlights: Array<[number, number]>;
}

/** Source integrity (ADR-0009): provenance, verification channels, holds and origin drift. */
export interface IntegrityStatus {
  /** Sealed generations checked for PROVENANCE.json. */
  provenance_generations: number;
  provenance_covered: number;
  provenance_fraction: number;
  /** Generations whose PROVENANCE.json fails its signature. */
  provenance_invalid: Array<string>;
  channels: Array<SourceIntegrity>;
  /** Releases held for `wsp approve` / `wsp reject`. */
  held: Array<HeldRelease>;
  /** Releases quarantined as possibly compromised. */
  quarantined: Array<HeldRelease>;
  /** Open origin-drift and integrity alerts. */
  drift_alerts: Array<AlertInfo>;
  pinned_hosts: number;
}

export type JsonValue = unknown;

export interface KindFacet {
  kind: DocKind;
  count: number;
}

export interface LedgerEntry {
  seq: number;
  at: string;
  kind: string;
  subject: string;
  data: Record<string, JsonValue>;
  hash: string;
}

export interface MapAnswer {
  type: 'map';
  place: string;
  lat: number;
  lon: number;
  zoom: number;
  description: string | null;
  doc_id: string;
  license: string;
}

export interface OnThisDay {
  year: number;
  text: string;
  doc_id: string | null;
}

/** Operations data for the System Monitor (PLAN §12.6); every part tolerates missing state. */
export interface OpsStatus {
  slo: Array<SourceSlo>;
  scrub: ScrubStatus | null;
  parity: ParityStatus | null;
  disk: Array<DiskUsage>;
  budgets: Array<DomainBudget>;
  lanes: Array<DownloadLane>;
  queue: Array<QueueDepth>;
  alerts: Array<AlertInfo>;
  bots: Array<BotRun>;
  proposals: ProposalSummary;
  /** False until the orchestrator has created its state DB. */
  orchestrator_state: boolean;
  /** Source integrity (ADR-0009). */
  integrity: IntegrityStatus | null;
}

export interface ParityStatus {
  /** `all` or `irreplaceable` (vault.parity_scope). */
  scope: string;
  /** False when par2 is not installed or parity is off. */
  available: boolean;
  generations: number;
  covered: number;
  bytes_total: number;
  bytes_covered: number;
  fraction: number;
  /** Generations per parity state (none, partial, sidecar, ...). */
  states: Record<string, number>;
  last_run: Record<string, JsonValue> | null;
}

/** Start page content (`wsp://home`, PLAN §10.2). */
export interface Portal {
  date: string;
  /** Deterministic for the day. */
  featured: FeaturedDoc | null;
  on_this_day: Array<OnThisDay>;
  did_you_know: Array<DidYouKnow>;
  /** Newest changes first. */
  newest: Array<Change>;
  /** A random page to try. */
  random: DocRef | null;
  /** Sources that have pages to pick at random. */
  random_sources: Array<string>;
  sites: Array<SourceInfo>;
}

export interface ProposalInfo {
  id: number;
  key: string;
  title: string;
  status: 'open' | 'applied' | 'accepted' | 'rejected';
  decision: 'auto-promote' | 'review' | 'reject';
  score: number | null;
  kind: string;
  found_by: string;
  created_at: string | null;
  decided_at: string | null;
  /** Markdown: score breakdown and checks. */
  body: string;
}

export interface ProposalPage {
  /** Newest first. */
  items: Array<ProposalInfo>;
}

export interface ProposalSummary {
  open: number;
  applied: number;
  accepted: number;
  rejected: number;
  /** Newest open ones. */
  latest: Array<ProposalInfo>;
}

/** Router intents (PLAN §9.3). */
export type QueryIntent = 'lookup' | 'define' | 'how-to' | 'navigate' | 'locate' | 'compare' | 'ask';

export interface QueueDepth {
  kind: string;
  status: 'pending' | 'running';
  count: number;
}

export interface RecentChanges {
  /** Newest first. */
  items: Array<Change>;
  /** Change-feed cursor these items are current to: follow `/v1/changes/stream?since=` from here for live updates. Null if the feed is empty. */
  cursor: string | null;
}

export interface Resolution {
  /** Canonical form, e.g. `wsp://rfc/9110`. */
  address: string;
  /** `client`: a page drawn entirely by the Navigator (history, hotlist, …). */
  kind: 'home' | 'search' | 'directory' | 'system' | 'whatsnew' | 'random' | 'client' | 'source' | 'document';
  /** API resource to fetch; null for pages rendered client-side. */
  href: string | null;
}

export interface ScrubStatus {
  files_known: number;
  verified_in_cycle: number;
  failing: number;
  /** Share of known files verified within the scrub cycle. */
  fraction: number;
  cycle_days: number;
  oldest_check: string | null;
}

export interface SearchResults {
  query: string;
  offset: number;
  limit: number;
  hits: Array<Hit>;
  /** Offset of the next page, if any. */
  next_offset: number | null;
  /** The `kind` filter that was applied. */
  kind: DocKind | null;
  /** Results for the query (and `kind`), counted in the same ranked list the page comes from (at most 200; a lower bound if `total_is_estimate`). */
  total: number;
  total_is_estimate: boolean;
  took_ms: number;
  intent: QueryIntent;
  /** Results per kind in the same ranked list, ignoring the `kind` filter: filtering by a kind shows exactly its count. Kinds with no results are left out. */
  facets: Array<KindFacet>;
  /** Doc id when the query names one page (`rfc 9110`, `wikipedia Alan Turing`). */
  navigate_to: string | null;
  /** Instant-answer box (definition, fact, map). Reserved: not produced yet. */
  instant: DefinitionAnswer | FactAnswer | MapAnswer | null;
  /** A spelling correction from the library's vocabulary, offered when results are sparse or a query word is unknown. */
  did_you_mean: string | null;
  /** Set when the query named a site (`rfc 9110`): totals and facets count that scope. */
  scope: SearchScope | null;
  /** True when retrieval found little or nothing. */
  low_confidence: boolean;
}

export interface SearchScope {
  /** The site named in the query, as typed (`rfc` in `rfc 9110`). */
  site: string;
  /** Source ids that site names. */
  sources: Array<string>;
  /** True when those sources had nothing for the query and the whole library was searched. */
  fallback: boolean;
}

export interface SourceFreshness {
  source_id: string;
  name: string;
  status: string;
  cadence: string | null;
  current_generation: string | null;
  sealed_at: string | null;
  age_seconds: number | null;
  /** True if older than the cadence, or active with nothing promoted. */
  stale: boolean;
  health: SourceHealth;
}

/** Status lamp: ``offline`` = nothing promoted, ``degraded`` = current generation failed scrub. */
export type SourceHealth = 'ok' | 'stale' | 'degraded' | 'offline';

export interface SourceInfo {
  id: string;
  name: string;
  tier: 'T1' | 'T2' | 'T3' | 'X';
  /** Registry lifecycle status. See `health` for the status lamp. */
  status: 'candidate' | 'active' | 'paused' | 'retired' | 'excluded';
  license: string;
  legal_basis: string;
  domain: Array<string>;
  adapter: string | null;
  /** ISO 8601 duration, e.g. `P1M`. */
  cadence: string | null;
  strategy: string | null;
  irreplaceable: boolean;
  /** Navigator address of the source's site, e.g. `wsp://rfc`. */
  address: string;
  current_generation: string | null;
  publisher: string | null;
  description: string | null;
  /** Derived from the first registry domain. */
  kind: DocKind;
  health: SourceHealth;
  /** Live documents; null if unknown. */
  doc_count: number | null;
  /** Size of the current generation. */
  bytes: number | null;
  /** When the current generation was sealed. */
  as_of: string | null;
  /** Landing document of the site, if any. */
  home_doc_id: string | null;
  /** The site has something to read (held, with documents or served from ZIM archives); planned sources and raw data are not linked as sites. */
  readable: boolean;
  /** Source ids sharing a domain (webring bar). */
  related: Array<string>;
}

export interface SourceIntegrity {
  source_id: string;
  /** Weakest checksum-channel status of the last checked release: `independent`, `same-origin`, `unavailable`, `mismatch` or `never checked` (ADR-0009). */
  channel: string;
  version: string | null;
  /** Strongest integrity action: info, alert, hold, quarantine. */
  verdict: string | null;
  checked_at: string | null;
}

export interface SourcePage {
  items: Array<SourceInfo>;
  /** Pass as `cursor` for the next page. */
  next_cursor: string | null;
}

/** Freshness against the source's SLO (PLAN §6.6). */
export interface SourceSlo {
  source_id: string;
  slo_class: 'wikimedia' | 'zim' | 'scholarly' | 'law' | 'default';
  /** Allowed lag behind upstream. */
  target_days: number;
  /** How long upstream has had a newer release than ours; 0 if current. */
  lag_days: number | null;
  /** Null when upstream was never observed. */
  within_slo: boolean | null;
  /** Newest version upstream offered. */
  upstream_newest: string | null;
  paused: boolean;
  pause_reasons: Array<string>;
}

export interface Suggestions {
  query: string;
  suggestions: Array<string>;
}

export interface SystemStatus {
  generated_at: string;
  freshness: Array<SourceFreshness>;
  degraded: Array<GenerationInfo>;
  /** Most recent events, oldest first. */
  ledger_tail: Array<LedgerEntry>;
  /** Result of replaying the ledger hash chain; null until the first replay has finished. */
  ledger_ok: boolean | null;
  api_version: string;
  uptime_seconds: number;
  /** Null without orchestrator data. */
  ops: OpsStatus | null;
}

/** Where the bytes live, for "View Source Data" and the direct route (PLAN §7, §10.2). */
export interface VaultLocation {
  generation_id: string;
  /** File (or generation directory) relative to the vault root. */
  path: string;
  /** `MANIFEST.json` that covers the file, relative to the vault root. */
  manifest: string;
  /** Entry inside a container file, e.g. a ZIM path. */
  entry: string | null;
  size: number | null;
  /** Manifest entry hash of the file. */
  sha256: string | null;
  /** Manifest entry hash of the file. */
  blake3: string | null;
}

/** Every operation: `"METHOD /path"` → path params, query params, body and 200 response. */
export interface Routes {
  /** Health */
  'GET /v1/health': {
    path: Record<string, never>;
    query: Record<string, never>;
    body: null;
    response: Health;
  };
  /** List Sources */
  'GET /v1/sources': {
    path: Record<string, never>;
    query: {
      /** Opaque cursor from a previous page's `next_cursor`. */
      cursor?: string | null | undefined;
      /** Page size. */
      limit?: number | undefined;
    };
    body: null;
    response: SourcePage;
  };
  /** Get Source */
  'GET /v1/sources/{source_id}': {
    path: {
      source_id: string;
    };
    query: Record<string, never>;
    body: null;
    response: SourceInfo;
  };
  /** List Generations */
  'GET /v1/sources/{source_id}/generations': {
    path: {
      source_id: string;
    };
    query: {
      /** Opaque cursor from a previous page's `next_cursor`. */
      cursor?: string | null | undefined;
      /** Page size. */
      limit?: number | undefined;
    };
    body: null;
    response: GenerationPage;
  };
  /** Document History */
  'GET /v1/documents/{doc_id}/history': {
    path: {
      /** `wsp:<source_id>/<native_id>`, percent-encoded as one segment (`/` → `%2F`). */
      doc_id: string;
    };
    query: {
      /** Opaque cursor from a previous page's `next_cursor`. */
      cursor?: string | null | undefined;
      /** Page size. */
      limit?: number | undefined;
    };
    body: null;
    response: HistoryPage;
  };
  /** Get Document */
  'GET /v1/documents/{doc_id}': {
    path: {
      /** `wsp:<source_id>/<native_id>`, percent-encoded as one segment (`/` → `%2F`). */
      doc_id: string;
    };
    query: {
      /** Generation id: the document as of that snapshot. */
      at?: string | null | undefined;
      /** Extra fields: `text` also sends the plain text of an HTML page. */
      fields?: Array<'text'> | null | undefined;
    };
    body: null;
    response: Document;
  };
  /** Run Search */
  'GET /v1/search': {
    path: Record<string, never>;
    query: {
      /** Plain-language query. */
      q: string;
      /** Restrict to these source ids (repeat or comma-separate). */
      sources?: Array<string> | null | undefined;
      /** Restrict to sources of this kind. */
      kind?: DocKind | null | undefined;
      limit?: number | undefined;
      /** Results are ranked 200 deep; pages end there. */
      offset?: number | undefined;
    };
    body: null;
    response: SearchResults;
  };
  /** Suggest */
  'GET /v1/suggest': {
    path: Record<string, never>;
    query: {
      /** Title prefix. */
      q: string;
      limit?: number | undefined;
    };
    body: null;
    response: Suggestions;
  };
  /** Changes */
  'GET /v1/changes': {
    path: Record<string, never>;
    query: {
      /** Cursor from a previous page; omit to start. */
      since?: string | null | undefined;
      limit?: number | undefined;
    };
    body: null;
    response: ChangePage;
  };
  /** Recent Changes */
  'GET /v1/changes/recent': {
    path: Record<string, never>;
    query: {
      limit?: number | undefined;
      /** Only changes of this source. */
      source?: string | null | undefined;
    };
    body: null;
    response: RecentChanges;
  };
  /** Changes Stream */
  'GET /v1/changes/stream': {
    path: Record<string, never>;
    query: {
      /** Start after this cursor. */
      since?: string | null | undefined;
      /** Close the stream after this long (then reconnect). */
      max_seconds?: number | null | undefined;
    };
    body: null;
    response: EventStream<Change>;
  };
  /** Proposals */
  'GET /v1/proposals': {
    path: Record<string, never>;
    query: {
      /** Filter by status. */
      status?: 'open' | 'applied' | 'accepted' | 'rejected' | null | undefined;
    };
    body: null;
    response: ProposalPage;
  };
  /** System */
  'GET /v1/system': {
    path: Record<string, never>;
    query: {
      ledger_tail?: number | undefined;
    };
    body: null;
    response: SystemStatus;
  };
  /** Portal */
  'GET /v1/portal': {
    path: Record<string, never>;
    query: Record<string, never>;
    body: null;
    response: Portal;
  };
  /** Directory */
  'GET /v1/directory': {
    path: Record<string, never>;
    query: {
      /** `''` (Top), `<domain>`, `<domain>/<source>`, … */
      path?: string | undefined;
    };
    body: null;
    response: DirectoryPage;
  };
  /** Random Page */
  'GET /v1/random': {
    path: Record<string, never>;
    query: {
      /** Pick from this source only. */
      source?: string | null | undefined;
      /** Make the pick reproducible. */
      seed?: number | null | undefined;
    };
    body: null;
    response: DocRef;
  };
  /** Asset */
  'GET /v1/assets/{source_id}/{generation_id}/{path}': {
    path: {
      source_id: string;
      generation_id: string;
      path: string;
    };
    query: Record<string, never>;
    body: null;
    response: Blob;
  };
  /** Report Gap */
  'POST /v1/gaps': {
    path: Record<string, never>;
    query: Record<string, never>;
    body: GapReport;
    response: null;
  };
  /** Go */
  'GET /go/{address}': {
    path: {
      /** `wsp://…` address, with or without the scheme. */
      address: string;
    };
    query: {
      /** Answer 307 to the resource instead of JSON. */
      redirect?: boolean | undefined;
    };
    body: null;
    response: Resolution;
  };
}
