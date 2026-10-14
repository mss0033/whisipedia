import { describe, expect, it } from 'vitest';
import type { SearchResults } from '../src/api/types.ts';
import { scopeHint } from '../src/pages/search.ts';

describe('scope note', () => {
  it('says when a site is searched through another copy', () => {
    const resp = {
      scope: null,
      scope_note:
        'wikipedia-en-zim is searched through wikipedia-top10, which holds the same pages',
    } as unknown as SearchResults;
    expect(scopeHint(resp, new Map())?.textContent).toContain('searched through wikipedia-top10');
    expect(
      scopeHint({ scope: null, scope_note: null } as unknown as SearchResults, new Map()),
    ).toBeNull();
  });
});
