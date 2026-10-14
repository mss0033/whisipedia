import { describe, expect, it } from 'vitest';
import { nearMisses } from '../src/pages/document.ts';

describe('near misses of a page id', () => {
  it('tries a trailing slash, then the other case of the last part', () => {
    expect(nearMisses('wsp:kiwix-curated-t2/wikibooks_en_all/Camping/Safety')).toEqual([
      'wsp:kiwix-curated-t2/wikibooks_en_all/Camping/Safety/',
      'wsp:kiwix-curated-t2/wikibooks_en_all/Camping/safety',
    ]);
    expect(nearMisses('wsp:x/xkcd.com/935/')).toEqual([]);
    expect(nearMisses('wsp:gutenberg/84')).toEqual(['wsp:gutenberg/84/']);
  });
});
