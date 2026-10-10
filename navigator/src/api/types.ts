/**
 * Whisipedia HTTP API v1 — response and request types used by the Navigator.
 *
 * Single source of truth for the front end's view of the API. Derived from
 * docs/PLAN.md §9.2, §9.3, §10 and §11.1 while the backend is built in
 * parallel. When the OpenAPI 3.1 spec lands, regenerate or align these types
 * with it; everything the UI touches is declared here.
 *
 * Conventions:
 * - All timestamps are RFC 3339 strings in UTC (`2026-10-09T12:00:00Z`).
 * - All dates without time are `YYYY-MM-DD`.
 * - `doc_id` is `wsp:<source_id>/<native_id>` (PLAN §9.2). In URL paths it is
 *   percent-encoded as a single segment: `/v1/documents/wsp%3Arfc%2F9110`.
 * - Endpoints marked PROPOSED are not in PLAN §11.1. The Navigator degrades
 *   gracefully (404/501 => fallback) when they are missing.
 */

/** `wsp:<source_id>/<native_id>` */
export type DocId = string;
/** Registry source id, e.g. `wikipedia-en`, `rfc`, `gutenberg`. */
export type SourceId = string;
/** BLAKE3 hex of the normalized text (PLAN §9.2). */
export type ContentHash = string;
/** Names an immutable, reproducible view of the whole library (PLAN §11.1). */
export type GenerationId = string;
/** RFC 3339 timestamp. */
export type Timestamp = string;

/** Result kinds used for filtering on the search results page (PLAN §10.3). */
export const DOC_KINDS = [
  'encyclopedia',
  'books',
  'papers',
  'law',
  'maps',
  'qa',
  'dictionary',
  'data',
  'medicine',
  'news',
  'other',
] as const;
export type DocKind = (typeof DOC_KINDS)[number];

/** Router intents (PLAN §9.3). */
export type QueryIntent =
  | 'lookup'
  | 'define'
  | 'how-to'
  | 'navigate'
  | 'locate'
  | 'compare'
  | 'ask';

/** Standard error envelope for any non-2xx response. */
export interface ApiError {
  error: {
    code: string;
    message: string;
  };
}

/* ------------------------------------------------------------------ */
/* GET /v1/search?q=&kind=&source=&page=&page_size=&lang=              */
/* ------------------------------------------------------------------ */

export interface SearchParams {
  q: string;
  kind?: DocKind | undefined;
  source?: SourceId | undefined;
  /** 1-based. */
  page?: number | undefined;
  /** Default 10. */
  page_size?: number | undefined;
  lang?: string | undefined;
}

export interface SearchHit {
  doc_id: DocId;
  source_id: SourceId;
  /** Human name of the source site, e.g. "Wikipedia (English)". */
  source_name: string;
  kind: DocKind;
  title: string;
  /** Plain text (never HTML). */
  snippet: string;
  /**
   * Highlight ranges into `snippet` as [start, end) Unicode code point
   * offsets. Optional: when absent the client highlights query terms itself.
   */
  highlights?: Array<[number, number]> | undefined;
  /** SPDX id or short license name. */
  license: string;
  /** When this content was captured upstream ("as of" date). */
  as_of: Timestamp;
  lang: string;
  score: number;
  /** Original upstream URL, informational only (never fetched). */
  url_native?: string | undefined;
}

export interface KindFacet {
  kind: DocKind;
  count: number;
}

export type InstantAnswer = DefinitionAnswer | FactAnswer | MapAnswer;

export interface DefinitionAnswer {
  type: 'definition';
  term: string;
  pronunciation?: string | undefined;
  senses: Array<{ part_of_speech: string; definition: string; example?: string | undefined }>;
  doc_id: DocId;
  license: string;
}

export interface FactAnswer {
  type: 'fact';
  /** Wikidata QID. */
  qid: string;
  label: string;
  description: string;
  facts: Array<{ label: string; value: string; doc_id?: DocId | undefined }>;
  doc_id: DocId;
  license: string;
}

export interface MapAnswer {
  type: 'map';
  place: string;
  lat: number;
  lon: number;
  zoom: number;
  description?: string | undefined;
  doc_id: DocId;
  license: string;
}

export interface SearchResponse {
  query: string;
  intent: QueryIntent;
  /** Estimated total matches. */
  total: number;
  page: number;
  page_size: number;
  /** Server-side time to produce the response. */
  took_ms: number;
  generation_id: GenerationId;
  hits: SearchHit[];
  facets: KindFacet[];
  instant?: InstantAnswer | null | undefined;
  /**
   * Set when the router is confident the query names a single page
   * ("rfc 9110", "wikipedia Alan Turing"). The Navigator jumps straight there.
   */
  navigate_to?: DocId | null | undefined;
  did_you_mean?: string | null | undefined;
  /** True when retrieval confidence is low (PLAN §9.3 summary rules). */
  low_confidence?: boolean | undefined;
}

/* ------------------------------------------------------------------ */
/* GET /v1/suggest?q=&limit=                                           */
/* ------------------------------------------------------------------ */

export interface Suggestion {
  text: string;
  doc_id?: DocId | undefined;
  kind?: DocKind | undefined;
}

export interface SuggestResponse {
  query: string;
  suggestions: Suggestion[];
}

/* ------------------------------------------------------------------ */
/* GET /v1/documents/{doc_id}[?at=YYYY-MM-DD | ?version=<hash>]        */
/* ------------------------------------------------------------------ */

export type DocumentFormat = 'html' | 'text';

export interface DocumentLink {
  /** href as it appears in the document body. */
  href: string;
  /** Resolved local target, or null when not in the library. */
  target: DocId | null;
}

export interface VaultLocation {
  /** Path of the file inside the vault (human-browsable, PLAN §7). */
  path: string;
  /** Path of the manifest that covers it. */
  manifest: string;
  /** Manifest entry hash for the file. */
  sha256: string;
  /** Byte offset/length for records inside container files (ZIM, Parquet). */
  offset?: number | undefined;
  length?: number | undefined;
}

export interface DocumentResponse {
  doc_id: DocId;
  source_id: SourceId;
  source_name: string;
  native_id: string;
  kind: DocKind;
  title: string;
  lang: string;
  url_native: string;
  license: string;
  revision: string;
  /** Upstream timestamp of this revision ("as of"). */
  timestamp: Timestamp;
  content_hash: ContentHash;
  generation_id: GenerationId;
  format: DocumentFormat;
  /** Present when format === 'html'. Untrusted; the client sanitizes. */
  html?: string | undefined;
  /** Present when format === 'text', and optionally alongside html. */
  text?: string | undefined;
  /** Outbound links with resolution status (PLAN §10.2). */
  links: DocumentLink[];
  /** Wikidata QIDs mentioned in the document. */
  entities: string[];
  vault: VaultLocation;
  /** True when /history has more than one version. */
  has_history: boolean;
}

/* ------------------------------------------------------------------ */
/* GET /v1/documents/{doc_id}/history                                  */
/* ------------------------------------------------------------------ */

export type ChangeOp = 'add' | 'modify' | 'tombstone';

export interface DocumentVersion {
  content_hash: ContentHash;
  revision: string;
  timestamp: Timestamp;
  generation_id: GenerationId;
  op: ChangeOp;
}

export interface HistoryResponse {
  doc_id: DocId;
  /** Newest first. */
  versions: DocumentVersion[];
}

/* ------------------------------------------------------------------ */
/* GET /v1/sources                                                     */
/* ------------------------------------------------------------------ */

export interface SourceInfo {
  id: SourceId;
  name: string;
  publisher: string;
  kind: DocKind;
  domain: string[];
  license: string;
  tier: string;
  /** Short blurb for the site's front page. */
  description: string;
  doc_count: number;
  bytes: number;
  as_of: Timestamp;
  status: 'ok' | 'stale' | 'degraded' | 'offline';
  /** Optional landing document for the source "site". */
  home_doc_id?: DocId | null | undefined;
  /** Related sources for the webring bar. */
  related: SourceId[];
}

export interface SourcesResponse {
  generation_id: GenerationId;
  sources: SourceInfo[];
}

/* ------------------------------------------------------------------ */
/* GET /v1/changes?since=<cursor>&limit=  (+ SSE, not used yet)        */
/* ------------------------------------------------------------------ */

export interface ChangeEntry {
  seq: number;
  doc_id: DocId;
  source_id: SourceId;
  title: string;
  op: ChangeOp;
  content_hash: ContentHash | null;
  timestamp: Timestamp;
  generation_id: GenerationId;
}

export interface ChangesResponse {
  cursor: string | null;
  next_cursor: string | null;
  changes: ChangeEntry[];
}

/* ------------------------------------------------------------------ */
/* GET /v1/system                                                      */
/* ------------------------------------------------------------------ */

export type HealthStatus = 'ok' | 'stale' | 'degraded' | 'offline';

export interface SystemResponse {
  version: string;
  generation_id: GenerationId;
  uptime_s: number;
  freshness: Array<{
    source_id: SourceId;
    as_of: Timestamp;
    slo_days: number;
    age_days: number;
    status: HealthStatus;
  }>;
  scrub: {
    /** 0..1 */
    progress: number;
    bytes_verified: number;
    bytes_total: number;
    last_completed: Timestamp | null;
    errors_found: number;
    errors_repaired: number;
  };
  degraded: Array<{ path: string; reason: string; since: Timestamp }>;
  disk: Array<{ name: string; used_bytes: number; total_bytes: number }>;
  ledger_tail: Array<{ ts: Timestamp; event: string; detail: string }>;
  latency_ms?: { search_p50: number; search_p95: number } | undefined;
}

/* ------------------------------------------------------------------ */
/* POST /v1/summarize  (text/event-stream)                              */
/* ------------------------------------------------------------------ */

export interface SummarizeRequest {
  query: string;
  /** Results to summarize, in display order; citations index into this. */
  doc_ids: DocId[];
  max_sentences?: number | undefined;
}

/**
 * Server-sent events, one JSON object per `data:` line:
 * - `{"type":"token","text":"..."}` incremental text
 * - `{"type":"citation","index":1,"doc_id":"wsp:..."}` 1-based index into doc_ids
 * - `{"type":"notice","text":"Low confidence: ..."}`
 * - `{"type":"done"}`
 * - `{"type":"error","message":"..."}`
 * 404/501/503 mean "no local LLM available".
 */
export type SummarizeEvent =
  | { type: 'token'; text: string }
  | { type: 'citation'; index: number; doc_id: DocId }
  | { type: 'notice'; text: string }
  | { type: 'done' }
  | { type: 'error'; message: string };

/* ------------------------------------------------------------------ */
/* PROPOSED: GET /v1/portal   (start page content)                     */
/* ------------------------------------------------------------------ */

export interface PortalResponse {
  date: string;
  generation_id: GenerationId;
  featured: { doc_id: DocId; title: string; blurb: string } | null;
  on_this_day: Array<{ year: number; text: string; doc_id?: DocId | undefined }>;
  did_you_know: Array<{ text: string; doc_id?: DocId | undefined }>;
}

/* ------------------------------------------------------------------ */
/* PROPOSED: GET /v1/directory?path=Arts/Literature                    */
/* ------------------------------------------------------------------ */

export interface DirectoryResponse {
  /** Slash-separated slugs; '' is Top. */
  path: string;
  /** Names along the path, Top excluded. */
  trail: Array<{ path: string; name: string }>;
  name: string;
  children: Array<{ path: string; name: string; count: number; crosslink?: boolean | undefined }>;
  entries: Array<{ doc_id: DocId; title: string; description: string; source_id: SourceId }>;
}

/* ------------------------------------------------------------------ */
/* PROPOSED: GET /v1/random?source=<id>                                */
/* ------------------------------------------------------------------ */

export interface RandomResponse {
  doc_id: DocId;
  title: string;
}
