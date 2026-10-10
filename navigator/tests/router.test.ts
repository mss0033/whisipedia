import { describe, expect, it } from 'vitest';
import { resolveRoute } from '../src/core/router.ts';

describe('resolveRoute', () => {
  it('routes built-in sites', () => {
    expect(resolveRoute('wsp://home')).toEqual({ page: 'home' });
    expect(resolveRoute('wsp://whatsnew').page).toBe('whatsnew');
    expect(resolveRoute('wsp://system').page).toBe('system');
    expect(resolveRoute('wsp://directory/Arts/Literature')).toEqual({
      page: 'directory',
      path: 'Arts/Literature',
    });
    expect(resolveRoute('wsp://help/search')).toEqual({ page: 'help', topic: 'search' });
  });

  it('parses search parameters defensively', () => {
    expect(resolveRoute('wsp://search?q=%20lisbon%20&kind=maps&page=3')).toEqual({
      page: 'search',
      query: 'lisbon',
      kind: 'maps',
      source: undefined,
      pageNo: 3,
      lucky: false,
    });
    const r = resolveRoute('wsp://search?q=x&kind=bogus&page=-4&lucky=1');
    expect(r).toMatchObject({ kind: undefined, pageNo: 1, lucky: true });
  });

  it('routes source sites and documents', () => {
    expect(resolveRoute('wsp://rfc')).toEqual({ page: 'site', source: 'rfc' });
    expect(resolveRoute('wsp://wikipedia-en/Alan_Turing?at=2026-01-01#Legacy')).toEqual({
      page: 'document',
      source: 'wikipedia-en',
      nativeId: 'Alan_Turing',
      docId: 'wsp:wikipedia-en/Alan_Turing',
      at: '2026-01-01',
      diff: false,
      fragment: 'Legacy',
    });
    expect(resolveRoute('wsp://rfc/9110?at=yesterday')).toMatchObject({ at: undefined });
  });

  it('handles random, offline and invalid input', () => {
    expect(resolveRoute('wsp://random?source=rfc')).toEqual({ page: 'random', source: 'rfc' });
    expect(resolveRoute('wsp://offline?url=http%3A%2F%2Fx.org')).toEqual({
      page: 'offline',
      url: 'http://x.org',
    });
    expect(resolveRoute('nonsense').page).toBe('invalid');
  });
});
