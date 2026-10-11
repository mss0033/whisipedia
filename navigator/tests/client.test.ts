import { describe, expect, it } from 'vitest';
import { GAPS } from '../mock/handler.ts';
import { watchChanges } from '../src/api/changes.ts';
import {
  ApiRequestError,
  buildQuery,
  docPath,
  routePath,
  SseParser,
  WspClient,
} from '../src/api/client.ts';
import type { Change } from '../src/api/types.ts';
import { mockFetch, until } from './helpers.ts';

describe('WspClient', () => {
  const log: string[] = [];
  const api = new WspClient({ fetch: mockFetch(log) });

  it('builds query strings without empty params', () => {
    expect(buildQuery({ q: 'a b', kind: undefined, page: 2, x: '', y: null })).toBe(
      '?q=a+b&page=2',
    );
    expect(buildQuery({ sources: ['a', 'b'] })).toBe('?sources=a&sources=b');
    expect(buildQuery({})).toBe('');
  });

  it('puts a doc id in ONE percent-encoded path segment', () => {
    expect(docPath('wsp:rfc/9110')).toBe('/v1/documents/wsp%3Arfc%2F9110');
    expect(routePath('GET /v1/documents/{doc_id}/history', { doc_id: 'wsp:x/notes/history' })).toBe(
      '/v1/documents/wsp%3Ax%2Fnotes%2Fhistory/history',
    );
  });

  it('searches with offset paging and the spec parameter names', async () => {
    const r = await api.search({
      q: 'how do vaccines train the immune system',
      page: 2,
      page_size: 5,
    });
    expect(r.offset).toBe(5);
    expect(r.limit).toBe(5);
    expect(r.hits.length).toBeLessThanOrEqual(5);
    expect(r.hits[0]?.doc_id).toMatch(/^wsp:/);
    expect(log.at(-1)).toContain('limit=5&offset=5');
    await api.search({ q: 'x', source: 'rfc', kind: 'papers' });
    expect(log.at(-1)).toContain('kind=papers&sources=rfc');
  });

  it('gets navigate_to for navigational queries', async () => {
    expect((await api.search({ q: 'rfc 9110' })).navigate_to).toBe('wsp:rfc/9110');
    expect((await api.search({ q: 'wikipedia alan turing' })).navigate_to).toBe(
      'wsp:wikipedia-en-zim/Alan_Turing',
    );
  });

  it('fetches documents (with vault location) and history by doc_id', async () => {
    const d = await api.document('wsp:wikipedia-en-zim/Alan_Turing');
    expect(d.title).toBe('Alan Turing');
    expect(d.vault?.manifest).toMatch(/MANIFEST\.json$/);
    const h = await api.history('wsp:wikipedia-en-zim/Alan_Turing');
    expect(h.items.length).toBe(3);
    const oldest = h.items[0]!;
    const old = await api.document('wsp:wikipedia-en-zim/Alan_Turing', {
      at: oldest.generation_id,
    });
    expect(old.content_hash).toBe(oldest.content_hash);
    expect(old.revision).not.toBe(d.revision);
  });

  it('lists every source and the recent changes', async () => {
    const sources = await api.sources();
    expect(sources.find((s) => s.id === 'rfc')?.health).toBe('ok');
    const recent = await api.recentChanges(3);
    expect(recent.items).toHaveLength(3);
    expect(recent.cursor).not.toBeNull();
  });

  it('raises ApiRequestError with the server error envelope', async () => {
    const err = await api.document('wsp:rfc/0').catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiRequestError);
    expect((err as ApiRequestError).status).toBe(404);
    expect((err as ApiRequestError).code).toBe('document_not_found');
    expect((err as ApiRequestError).isUnavailable).toBe(true);
  });

  it('reports each gap once and never throws', async () => {
    const before = GAPS.length;
    await api.reportGap({ href: 'https://example.org/a', from_doc_id: 'wsp:rfc/9110' });
    await api.reportGap({ href: 'https://example.org/a', from_doc_id: 'wsp:rfc/9110' });
    expect(GAPS.length).toBe(before + 1);
    const down = new WspClient({ fetch: () => Promise.reject(new TypeError('offline')) });
    await expect(down.reportGap({ href: 'x', from_doc_id: null })).resolves.toBeUndefined();
  });
});

describe('watchChanges', () => {
  it('uses EventSource when there is one', () => {
    const opened: string[] = [];
    class FakeES {
      readyState = 0;
      constructor(url: string) {
        opened.push(url);
      }
      addEventListener() {}
      close() {}
    }
    const api = new WspClient({ fetch: mockFetch() });
    const w = watchChanges(api, '7', () => {}, {
      eventSource: FakeES as unknown as typeof EventSource,
    });
    expect(w.mode).toBe('sse');
    expect(opened).toEqual(['/v1/changes/stream?since=7']);
    w.close();
  });

  it('falls back to polling the feed from the cursor', async () => {
    const log: string[] = [];
    const api = new WspClient({ fetch: mockFetch(log) });
    const seen: Change[] = [];
    const w = watchChanges(api, '0', (c) => seen.push(c), { pollMs: 1 });
    expect(w.mode).toBe('poll');
    await until(() => seen.length > 0);
    w.close();
    expect(log.some((l) => l.startsWith('GET /v1/changes?since=0'))).toBe(true);
  });
});

describe('SseParser', () => {
  it('handles split chunks, CRLF and multi-line data', () => {
    const p = new SseParser();
    expect(p.push('data: {"a"')).toEqual([]);
    expect(p.push(':1}\r\n\r\ndata: x\ndata: y\n\n: comment\n\n')).toEqual(['{"a":1}', 'x\ny']);
  });
});
