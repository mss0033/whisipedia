import { handle } from '../mock/handler.ts';
import type { FetchLike } from '../src/api/client.ts';

/** fetch() backed by the mock API handler, no network. */
export function mockFetch(log: string[] = []): FetchLike {
  return async (input, init) => {
    const url = new URL(input, 'http://127.0.0.1');
    log.push(`${init?.method ?? 'GET'} ${url.pathname}${url.search}`);
    const body = init?.body ? JSON.parse(String(init.body)) : undefined;
    const r = handle(init?.method ?? 'GET', url.pathname, url.searchParams, body);
    if ('stream' in r) {
      const text = r.stream
        .map((e) => `event: ${e.event.type}\ndata: ${JSON.stringify(e.event)}\n\n`)
        .join('');
      return new Response(text, { status: 200, headers: { 'Content-Type': 'text/event-stream' } });
    }
    if ('hold' in r) {
      return new Response(`retry: ${r.retryMs}\n\n`, {
        status: 200,
        headers: { 'Content-Type': 'text/event-stream' },
      });
    }
    if (!('json' in r)) return new Response(null, { status: r.status });
    return new Response(JSON.stringify(r.json), {
      status: r.status,
      headers: { 'Content-Type': 'application/json' },
    });
  };
}

export async function until(cond: () => boolean, timeoutMs = 2000): Promise<void> {
  const start = Date.now();
  while (!cond()) {
    if (Date.now() - start > timeoutMs) throw new Error('timed out waiting for condition');
    await new Promise((r) => setTimeout(r, 5));
  }
}
