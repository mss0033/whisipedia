/**
 * Whisipedia HTTP API v1 as the Navigator sees it.
 *
 * The wire types are GENERATED from docs/api/openapi.json (the contract) into
 * ./generated.ts by `npm run gen:api`; `npm run check` fails when that file
 * is stale. This module only re-exports them under the names the UI uses and
 * adds a few UI-side types. Do not hand-write wire shapes here.
 *
 * Conventions:
 * - Timestamps are RFC 3339 strings in UTC; dates are `YYYY-MM-DD`.
 * - `doc_id` is `wsp:<source_id>/<native_id>` (PLAN §9.2). In URL paths it is
 *   ONE percent-encoded segment: `/v1/documents/wsp%3Arfc%2F9110`.
 * - Nullable fields are always present (possibly `null`).
 */
import type * as G from './generated.ts';

export type {
  Change,
  ChangePage,
  DefinitionAnswer,
  DirectoryCategory,
  DirectoryEntry,
  DirectoryPage,
  DocKind,
  DocRef,
  DocumentLink,
  DocumentVersion,
  ErrorResponse,
  FactAnswer,
  GapReport,
  HistoryPage,
  Hit,
  KindFacet,
  MapAnswer,
  Portal,
  QueryIntent,
  RecentChanges,
  Routes,
  SearchResults,
  SourceHealth,
  SourceInfo,
  SourcePage,
  Suggestions,
  SummarizeRequest,
  SummaryEvent,
  SystemStatus,
  VaultLocation,
} from './generated.ts';
export { API_VERSION } from './generated.ts';

/** `wsp:<source_id>/<native_id>` */
export type DocId = string;
/** Registry source id, e.g. `wikipedia-en-zim`, `rfc`, `gutenberg`. */
export type SourceId = string;

/** `Document` would shadow the DOM's global type, so the UI calls it this. */
export type DocumentResponse = G.Document;
export type InstantAnswer = NonNullable<G.SearchResults['instant']>;

/** Result kinds in display order (PLAN §10.3). Checked against the spec below. */
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
] as const satisfies readonly G.DocKind[];

// Fails to compile if the spec gains a kind that DOC_KINDS does not list.
type _AllKindsListed = G.DocKind extends (typeof DOC_KINDS)[number] ? true : never;
const _allKindsListed: _AllKindsListed = true;
void _allKindsListed;

/** Navigator search parameters (`page` is 1-based; the API uses offset/limit). */
export interface SearchParams {
  q: string;
  kind?: G.DocKind | undefined;
  source?: SourceId | undefined;
  page?: number | undefined;
  page_size?: number | undefined;
}

/** A location-bar or search-box suggestion: from the API (titles) or from local history. */
export interface Suggestion {
  text: string;
  doc_id?: DocId | undefined;
  kind?: G.DocKind | undefined;
}
