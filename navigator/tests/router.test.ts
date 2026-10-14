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

  it('routes the practice hall and a pool with its exam parameters', () => {
    expect(resolveRoute('wsp://practice')).toEqual({
      page: 'practice',
      pool: '',
      group: undefined,
      mode: 'study',
      seed: undefined,
      index: 0,
    });
    expect(resolveRoute('wsp://practice/ncvec:tech:2026?group=T1A&n=3')).toMatchObject({
      pool: 'ncvec:tech:2026',
      group: 'T1A',
      index: 3,
    });
    expect(resolveRoute('wsp://practice/p?mode=exam&seed=42&group=bogus&n=-1')).toMatchObject({
      mode: 'exam',
      seed: 42,
      group: undefined,
      index: 0,
    });
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
    expect(resolveRoute('wsp://wikipedia-en-zim/Alan_Turing?at=2026-01-01#Legacy')).toEqual({
      page: 'document',
      source: 'wikipedia-en-zim',
      nativeId: 'Alan_Turing',
      docId: 'wsp:wikipedia-en-zim/Alan_Turing',
      at: '2026-01-01',
      diff: false,
      fragment: 'Legacy',
    });
    expect(resolveRoute('wsp://rfc/9110?at=..%2Fx')).toMatchObject({ at: undefined });
    expect(resolveRoute('wsp://rfc/9110?at=%20')).toMatchObject({ at: undefined });
    // Real generation ids are not all dates (audit 2026-10-12 F3).
    for (const at of [
      '2026-08',
      'pubmed26-u1671',
      '2026-10-12-r2',
      '2026-10-11.nj',
      '2026-10-11.ri.set00001',
    ]) {
      expect(resolveRoute(`wsp://rfc/9110?at=${at}`)).toMatchObject({ at });
    }
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
