/**
 * Typed client for the Whisipedia HTTP API v1. Same-origin only: the daemon
 * serves both the API and this bundle from 127.0.0.1, and the CSP forbids
 * anything else.
 */
import type {
  ApiError,
  ChangesResponse,
  DirectoryResponse,
  DocId,
  DocumentResponse,
  HistoryResponse,
  PortalResponse,
  RandomResponse,
  SearchParams,
  SearchResponse,
  SourcesResponse,
  SuggestResponse,
  SummarizeEvent,
  SummarizeRequest,
  SystemResponse,
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
  /** Endpoint not implemented by this daemon (used for PROPOSED endpoints). */
  get isUnavailable(): boolean {
    return this.status === 404 || this.status === 501 || this.status === 503;
  }
}

export type FetchLike = (input: string, init?: RequestInit) => Promise<Response>;

export interface ClientOptions {
  base?: string;
  fetch?: FetchLike;
}

type Query = Record<string, string | number | undefined>;

export function buildQuery(params: Query): string {
  const qs = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined || v === '') continue;
    qs.set(k, String(v));
  }
  const s = qs.toString();
  return s ? `?${s}` : '';
}

export function docPath(id: DocId): string {
  return `/v1/documents/${encodeURIComponent(id)}`;
}

/** Incremental parser for `text/event-stream` bodies. */
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

export class WspClient {
  private readonly base: string;
  private readonly fetchImpl: FetchLike;

  constructor(opts: ClientOptions = {}) {
    this.base = opts.base ?? '';
    this.fetchImpl = opts.fetch ?? ((input, init) => fetch(input, init));
  }

  private async get<T>(path: string, signal?: AbortSignal): Promise<T> {
    const res = await this.fetchImpl(this.base + path, {
      headers: { Accept: 'application/json' },
      ...(signal ? { signal } : {}),
    });
    return this.parse<T>(res);
  }

  private async parse<T>(res: Response): Promise<T> {
    if (!res.ok) {
      let code = `http_${res.status}`;
      let message = res.statusText || `HTTP ${res.status}`;
      try {
        const body = (await res.json()) as Partial<ApiError>;
        if (body.error) {
          code = body.error.code;
          message = body.error.message;
        }
      } catch {
        /* non-JSON error body */
      }
      throw new ApiRequestError(res.status, code, message);
    }
    return (await res.json()) as T;
  }

  search(params: SearchParams, signal?: AbortSignal): Promise<SearchResponse> {
    return this.get(
      `/v1/search${buildQuery({
        q: params.q,
        kind: params.kind,
        source: params.source,
        page: params.page,
        page_size: params.page_size,
        lang: params.lang,
      })}`,
      signal,
    );
  }

  suggest(q: string, limit = 8, signal?: AbortSignal): Promise<SuggestResponse> {
    return this.get(`/v1/suggest${buildQuery({ q, limit })}`, signal);
  }

  document(
    id: DocId,
    opts: { at?: string | undefined; version?: string | undefined } = {},
    signal?: AbortSignal,
  ): Promise<DocumentResponse> {
    return this.get(`${docPath(id)}${buildQuery({ at: opts.at, version: opts.version })}`, signal);
  }

  history(id: DocId, signal?: AbortSignal): Promise<HistoryResponse> {
    return this.get(`${docPath(id)}/history`, signal);
  }

  sources(signal?: AbortSignal): Promise<SourcesResponse> {
    return this.get('/v1/sources', signal);
  }

  changes(since?: string, limit = 50, signal?: AbortSignal): Promise<ChangesResponse> {
    return this.get(`/v1/changes${buildQuery({ since, limit })}`, signal);
  }

  system(signal?: AbortSignal): Promise<SystemResponse> {
    return this.get('/v1/system', signal);
  }

  /** PROPOSED endpoint. */
  portal(signal?: AbortSignal): Promise<PortalResponse> {
    return this.get('/v1/portal', signal);
  }

  /** PROPOSED endpoint. */
  directory(path: string, signal?: AbortSignal): Promise<DirectoryResponse> {
    return this.get(`/v1/directory${buildQuery({ path })}`, signal);
  }

  /** PROPOSED endpoint. */
  random(source?: string, signal?: AbortSignal): Promise<RandomResponse> {
    return this.get(`/v1/random${buildQuery({ source })}`, signal);
  }

  /** Streams summary events. Throws ApiRequestError when unavailable. */
  async *summarize(req: SummarizeRequest, signal?: AbortSignal): AsyncGenerator<SummarizeEvent> {
    const res = await this.fetchImpl(`${this.base}/v1/summarize`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
      body: JSON.stringify(req),
      ...(signal ? { signal } : {}),
    });
    if (!res.ok || !res.body) {
      await this.parse<never>(res.ok ? new Response(null, { status: 501 }) : res);
      return;
    }
    const reader = res.body.pipeThrough(new TextDecoderStream()).getReader();
    const parser = new SseParser();
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      for (const data of parser.push(value)) {
        let ev: SummarizeEvent;
        try {
          ev = JSON.parse(data) as SummarizeEvent;
        } catch {
          continue;
        }
        yield ev;
        if (ev.type === 'done' || ev.type === 'error') return;
      }
    }
  }
}

export const api = new WspClient();
