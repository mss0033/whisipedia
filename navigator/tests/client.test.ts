import { describe, expect, it } from 'vitest';
import { ApiRequestError, buildQuery, docPath, SseParser, WspClient } from '../src/api/client.ts';
import type { SummarizeEvent } from '../src/api/types.ts';
import { mockFetch } from './helpers.ts';

describe('WspClient', () => {
  const log: string[] = [];
  const api = new WspClient({ fetch: mockFetch(log) });

  it('builds query strings without empty params', () => {
    expect(buildQuery({ q: 'a b', kind: undefined, page: 2, x: '' })).toBe('?q=a+b&page=2');
    expect(buildQuery({})).toBe('');
    expect(docPath('wsp:rfc/9110')).toBe('/v1/documents/wsp%3Arfc%2F9110');
  });

  it('searches and returns typed results', async () => {
    const r = await api.search({ q: 'how do vaccines train the immune system', page_size: 10 });
    expect(r.hits.length).toBeGreaterThan(0);
    expect(r.hits.length).toBeLessThanOrEqual(10);
    expect(r.hits[0]?.doc_id).toMatch(/^wsp:/);
    expect(log.at(-1)).toContain('/v1/search?q=');
  });

  it('fetches documents and history by doc_id', async () => {
    const d = await api.document('wsp:wikipedia-en/Alan_Turing');
    expect(d.title).toBe('Alan Turing');
    const old = await api.document('wsp:wikipedia-en/Alan_Turing', { at: '2026-01-01' });
    expect(old.revision).not.toBe(d.revision);
    const h = await api.history('wsp:wikipedia-en/Alan_Turing');
    expect(h.versions.length).toBe(3);
  });

  it('raises ApiRequestError with the server error envelope', async () => {
    const err = await api.document('wsp:rfc/0').catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiRequestError);
    expect((err as ApiRequestError).status).toBe(404);
    expect((err as ApiRequestError).code).toBe('not_found');
    expect((err as ApiRequestError).isUnavailable).toBe(true);
  });

  it('streams summaries', async () => {
    const events: SummarizeEvent[] = [];
    for await (const ev of api.summarize({
      query: 'x',
      doc_ids: ['wsp:rfc/9110', 'wsp:rfc/9111'],
    })) {
      events.push(ev);
    }
    expect(events.at(-1)).toEqual({ type: 'done' });
    expect(events.filter((e) => e.type === 'citation')).toHaveLength(2);
  });

  it('reports a missing summarizer as unavailable', async () => {
    const none = new WspClient({ fetch: async () => new Response('', { status: 501 }) });
    const gen = none.summarize({ query: 'x', doc_ids: ['a'] });
    await expect(gen.next()).rejects.toMatchObject({ status: 501, isUnavailable: true });
  });
});

describe('SseParser', () => {
  it('handles split chunks, CRLF and multi-line data', () => {
    const p = new SseParser();
    expect(p.push('data: {"a"')).toEqual([]);
    expect(p.push(':1}\r\n\r\ndata: x\ndata: y\n\n: comment\n\n')).toEqual(['{"a":1}', 'x\ny']);
  });
});
