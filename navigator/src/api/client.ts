/**
 * Typed client for the Whisipedia HTTP API v1. Same-origin only: the daemon
 * serves both the API and this bundle from 127.0.0.1, and the CSP forbids
 * anything else. Every call names an operation of the generated `Routes`
 * table, so an endpoint or parameter that leaves the spec stops compiling.
 */
import type {
  ChangePage,
  DirectoryPage,
  DocId,
  DocRef,
  DocumentResponse,
  ErrorResponse,
  GapReport,
  HistoryPage,
  OutsideCopy,
  Places,
  Portal,
  RecentChanges,
  Routes,
  SearchParams,
  SearchResults,
  SourceInfo,
  SourcePage,
  Suggestions,
  SystemStatus,
  Tilesets,
} from './types.ts';

export class ApiRequestError extends Error {
  readonly status: number;
  readonly code: string;
  constructor(status: number, code: string, message: string) {
    super(message);
    this.name = 'ApiRequestError';
    this.status = status;
    this.code = code;
  }
  /** The daemon lacks this feature (older daemon, or component not configured). */
  get isUnavailable(): boolean {
    return this.status === 404 || this.status === 501 || this.status === 503;
  }
  /** The route itself is missing (an older daemon): a 404 with no specific code, or 501. */
  get isMissingRoute(): boolean {
    return (
      (this.status === 404 && (this.code === 'not_found' || this.code === 'http_404')) ||
      this.status === 501
    );
  }
  /** The library is busy or warming up: retry, never treat it as an answer. */
  get isBusy(): boolean {
    return this.status === 503;
  }
}

export type FetchLike = (input: string, init?: RequestInit) => Promise<Response>;

export interface ClientOptions {
  base?: string;
  fetch?: FetchLike;
}

type QueryValue = string | number | boolean | null | undefined | ReadonlyArray<string>;

export function buildQuery(params: Readonly<Record<string, QueryValue>>): string {
  const qs = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined || v === null || v === '') continue;
    if (Array.isArray(v)) for (const item of v) qs.append(k, item);
    else qs.set(k, String(v));
  }
  const s = qs.toString();
  return s ? `?${s}` : '';
}

/** `/v1/documents/<doc_id as ONE percent-encoded segment>` (docs/api/addresses.md). */
export function docPath(id: DocId): string {
  return `/v1/documents/${encodeURIComponent(id)}`;
}

/** Fill `{name}` placeholders of a spec path; each value is one encoded segment. */
export function routePath<K extends keyof Routes>(route: K, path: Routes[K]['path']): string {
  const values = path as Readonly<Record<string, string>>;
  return route
    .slice(route.indexOf(' ') + 1)
    .replace(/\{(\w+)\}/g, (_, name: string) => encodeURIComponent(values[name] ?? ''));
}

/** Incremental parser for `text/event-stream` bodies; yields each event's data. */
export class SseParser {
  private buf = '';
  push(chunk: string): string[] {
    this.buf += chunk.replace(/\r\n?/g, '\n');
    const out: string[] = [];
    let idx = this.buf.indexOf('\n\n');
    while (idx !== -1) {
      const block = this.buf.slice(0, idx);
      this.buf = this.buf.slice(idx + 2);
      const data = block
        .split('\n')
        .filter((l) => l.startsWith('data:'))
        .map((l) => l.slice(5).replace(/^ /, ''))
        .join('\n');
      if (data) out.push(data);
      idx = this.buf.indexOf('\n\n');
    }
    return out;
  }
}

const NO_PATH = {} as Record<string, never>;

export class WspClient {
  private readonly base: string;
  private readonly fetchImpl: FetchLike;
  private readonly reportedGaps = new Set<string>();

  constructor(opts: ClientOptions = {}) {
    this.base = opts.base ?? '';
    this.fetchImpl = opts.fetch ?? ((input, init) => fetch(input, init));
  }

  /** Base URL, for EventSource (which cannot use the injected fetch). */
  url(path: string): string {
    return this.base + path;
  }

  private async get<K extends keyof Routes>(
    route: K,
    path: Routes[K]['path'],
    query: Routes[K]['query'],
    signal?: AbortSignal,
  ): Promise<Routes[K]['response']> {
    const res = await this.fetchImpl(
      this.base + routePath(route, path) + buildQuery(query as Record<string, QueryValue>),
      { headers: { Accept: 'application/json' }, ...(signal ? { signal } : {}) },
    );
    return (await this.parse(res)) as Routes[K]['response'];
  }

  private async parse(res: Response): Promise<unknown> {
    if (!res.ok) {
      let code = `http_${res.status}`;
      let message = res.statusText || `HTTP ${res.status}`;
      try {
        const body = (await res.json()) as Partial<ErrorResponse>;
        if (body.error) {
          code = body.error.code;
          message = body.error.message;
        }
      } catch {
        /* non-JSON error body */
      }
      throw new ApiRequestError(res.status, code, message);
    }
    if (res.status === 204) return null;
    return res.json();
  }

  search(params: SearchParams, signal?: AbortSignal): Promise<SearchResults> {
    const size = params.page_size ?? 10;
    return this.get(
      'GET /v1/search',
      NO_PATH,
      {
        q: params.q,
        kind: params.kind,
        sources: params.source ? [params.source] : undefined,
        limit: size,
        offset: ((params.page ?? 1) - 1) * size,
      },
      signal,
    );
  }

  suggest(q: string, limit = 8, signal?: AbortSignal): Promise<Suggestions> {
    return this.get('GET /v1/suggest', NO_PATH, { q, limit }, signal);
  }

  /**
   * `at` is a generation id (time machine). An HTML page's plain `text` is sent only with
   * `text: true` (it doubles the response); a text page always has it.
   */
  document(
    id: DocId,
    opts: { at?: string | undefined; text?: boolean } = {},
    signal?: AbortSignal,
  ): Promise<DocumentResponse> {
    const fields: Array<'text'> | undefined = opts.text ? ['text'] : undefined;
    return this.get('GET /v1/documents/{doc_id}', { doc_id: id }, { at: opts.at, fields }, signal);
  }

  history(id: DocId, signal?: AbortSignal): Promise<HistoryPage> {
    return this.get('GET /v1/documents/{doc_id}/history', { doc_id: id }, { limit: 200 }, signal);
  }

  /** Every source (follows `next_cursor`). */
  async sources(signal?: AbortSignal): Promise<SourceInfo[]> {
    const out: SourceInfo[] = [];
    let cursor: string | null = null;
    do {
      const page: SourcePage = await this.get(
        'GET /v1/sources',
        NO_PATH,
        { cursor, limit: 200 },
        signal,
      );
      out.push(...page.items);
      cursor = page.next_cursor;
    } while (cursor);
    return out;
  }

  /** Change feed, oldest first, from a cursor. */
  changes(since?: string | null, limit = 50, signal?: AbortSignal): Promise<ChangePage> {
    return this.get('GET /v1/changes', NO_PATH, { since, limit }, signal);
  }

  /** Newest changes first, plus the cursor to follow live updates from. */
  recentChanges(limit = 50, source?: string, signal?: AbortSignal): Promise<RecentChanges> {
    return this.get('GET /v1/changes/recent', NO_PATH, { limit, source }, signal);
  }

  system(signal?: AbortSignal): Promise<SystemStatus> {
    return this.get('GET /v1/system', NO_PATH, {}, signal);
  }

  /** The map tilesets the library holds (the Atlas). */
  tilesets(signal?: AbortSignal): Promise<Tilesets> {
    return this.get('GET /v1/maps/tilesets', NO_PATH, {}, signal);
  }

  /** Named places matching `q` (the gazetteer); `near`, the point looked at, puts the matches
   * around it first. 503 until it is built. */
  places(
    q: string,
    limit = 10,
    signal?: AbortSignal,
    near?: { lat: number; lon: number },
  ): Promise<Places> {
    const at = near ? { lat: near.lat, lon: near.lon } : {};
    return this.get('GET /v1/maps/places', NO_PATH, { q, limit, ...at }, signal);
  }

  /** Named places within `radius` metres of a point, nearest first. */
  nearby(
    lat: number,
    lon: number,
    radius = 5000,
    limit = 10,
    signal?: AbortSignal,
  ): Promise<Places> {
    return this.get('GET /v1/maps/nearby', NO_PATH, { lat, lon, radius, limit }, signal);
  }

  portal(signal?: AbortSignal): Promise<Portal> {
    return this.get('GET /v1/portal', NO_PATH, {}, signal);
  }

  directory(path: string, signal?: AbortSignal): Promise<DirectoryPage> {
    return this.get('GET /v1/directory', NO_PATH, { path }, signal);
  }

  random(source?: string, signal?: AbortSignal): Promise<DocRef> {
    return this.get('GET /v1/random', NO_PATH, { source }, signal);
  }

  /** The library's copy of an outside URL (a Wikipedia article, an RFC), or null when it holds none. */
  async outsideCopy(url: string, signal?: AbortSignal): Promise<OutsideCopy | null> {
    try {
      return await this.get('GET /v1/outside', NO_PATH, { url }, signal);
    } catch (err) {
      if (err instanceof ApiRequestError && (err.code === 'not_held' || err.isMissingRoute))
        return null;
      throw err;
    }
  }

  /**
   * Tell the gap miner about a "not in library" link (PLAN §5.3). Best effort:
   * never throws, and each link is reported once per session.
   */
  /** Which of `addresses` the library holds no page for (at most 500 asked at a time). An error is
   * "none known": marking links must never disturb browsing. */
  async missingLinks(
    addresses: string[],
    signal?: AbortSignal,
  ): Promise<{ missing: string[]; unchecked: string[] }> {
    if (!addresses.length) return { missing: [], unchecked: [] };
    const body: Routes['POST /v1/links/missing']['body'] = { addresses: addresses.slice(0, 500) };
    try {
      const res = await this.fetchImpl(this.base + routePath('POST /v1/links/missing', NO_PATH), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
        body: JSON.stringify(body),
        ...(signal ? { signal } : {}),
      });
      const out = (await this.parse(res)) as Routes['POST /v1/links/missing']['response'];
      return { missing: out.missing, unchecked: out.unchecked ?? [] };
    } catch {
      return { missing: [], unchecked: [] };
    }
  }

  async reportGap(report: GapReport): Promise<void> {
    const key = `${report.href}\n${report.from_doc_id ?? ''}`;
    if (this.reportedGaps.has(key)) return;
    this.reportedGaps.add(key);
    const body: Routes['POST /v1/gaps']['body'] = report;
    try {
      await this.fetchImpl(this.base + routePath('POST /v1/gaps', NO_PATH), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
    } catch {
      /* reporting must never disturb browsing */
    }
  }
}

export const api = new WspClient();
