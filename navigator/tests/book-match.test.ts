import { describe, expect, it } from 'vitest';
import { bestBookMatch } from '../src/pages/document.ts';

describe('catalog card book match', () => {
  it('prefers the complete book over a Wikisource editions page', () => {
    const hits = [
      { title: 'Moby-Dick', source_id: 'kiwix-wikisource-en-all' },
      { title: 'Moby Dick; Or, The Whale', source_id: 'gutenberg' },
      { title: 'Moby Dick (disambiguation)', source_id: 'kiwix-wikisource-en-all' },
    ];
    expect(bestBookMatch(hits, 'moby dick')?.source_id).toBe('gutenberg');
    expect(
      bestBookMatch(
        [
          { title: 'Moby-Dick', doc_id: 'wsp:wikisource-en/Moby-Dick' },
          { title: 'Moby Dick, or the Whale', doc_id: 'wsp:gutenberg/2701' },
        ],
        'moby dick',
      )?.doc_id,
    ).toBe('wsp:gutenberg/2701');
  });

  it('still finds a title followed by its author and ignores other books', () => {
    expect(
      bestBookMatch([{ title: 'Emma by Jane Austen', source_id: 'wikisource-en' }], 'emma')?.title,
    ).toBe('Emma by Jane Austen');
    expect(bestBookMatch([{ title: 'Emmanuel', source_id: 'gutenberg' }], 'emma')).toBeUndefined();
  });
});
