/**
 * Live change feed: `GET /v1/changes/stream` (SSE) through EventSource, which
 * resumes by itself with Last-Event-ID. If EventSource is missing or the
 * stream is refused, falls back to polling `GET /v1/changes?since=`.
 */
import type { WspClient } from './client.ts';
import type { Change } from './types.ts';

export const POLL_MS = 30_000;

export interface ChangeWatcher {
  readonly mode: 'sse' | 'poll';
  close(): void;
}

export interface WatchOptions {
  /** Injected for tests; defaults to the global EventSource if there is one. */
  eventSource?: typeof EventSource | undefined;
  pollMs?: number;
  /**
   * For readers of recent changes only: when a backlog is waiting (a whole generation landing), skip
   * to the feed head instead of receiving every record, and call this so the page reloads its list.
   */
  onSkipped?: () => void;
}

const POLL_BATCH = 200;

export function watchChanges(
  api: WspClient,
  cursor: string | null,
  onChange: (change: Change) => void,
  opts: WatchOptions = {},
): ChangeWatcher {
  const ES = opts.eventSource ?? (typeof EventSource === 'undefined' ? undefined : EventSource);
  let closed = false;
  let since = cursor;
  let timer: ReturnType<typeof setTimeout> | undefined;
  let source: EventSource | undefined;
  let mode: 'sse' | 'poll' = ES ? 'sse' : 'poll';

  const poll = (): void => {
    if (closed) return;
    api
      .changes(since, POLL_BATCH)
      .then(async (page) => {
        if (opts.onSkipped && page.items.length >= POLL_BATCH) {
          since = (await api.recentChanges(1)).cursor ?? page.cursor; // the head
          opts.onSkipped();
          return;
        }
        for (const c of page.items) onChange(c);
        since = page.cursor;
      })
      .catch(() => {
        /* daemon away: try again next tick */
      })
      .finally(() => {
        if (!closed) timer = setTimeout(poll, opts.pollMs ?? POLL_MS);
      });
  };

  const startPolling = (): void => {
    mode = 'poll';
    source?.close();
    source = undefined;
    timer = setTimeout(poll, opts.pollMs ?? POLL_MS);
  };

  if (ES) {
    const params = new URLSearchParams();
    if (since) params.set('since', since);
    if (opts.onSkipped) params.set('summarize', 'true');
    const qs = params.toString() ? `?${params}` : '';
    source = new ES(api.url(`/v1/changes/stream${qs}`));
    source.addEventListener('skipped', (ev) => {
      const msg = ev as MessageEvent<string>;
      if (msg.lastEventId) since = msg.lastEventId;
      opts.onSkipped?.();
    });
    source.addEventListener('change', (ev) => {
      const msg = ev as MessageEvent<string>;
      if (msg.lastEventId) since = msg.lastEventId;
      try {
        onChange(JSON.parse(msg.data) as Change);
      } catch {
        /* malformed event: skip */
      }
    });
    // EventSource retries transient errors itself; CLOSED means it gave up (e.g. a 4xx/5xx).
    source.addEventListener('error', () => {
      if (!closed && source?.readyState === 2) startPolling();
    });
  } else {
    startPolling();
  }

  return {
    get mode() {
      return mode;
    },
    close() {
      closed = true;
      source?.close();
      if (timer !== undefined) clearTimeout(timer);
    },
  };
}
