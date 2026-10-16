import { describe, expect, it } from 'vitest';
import type { DocumentResponse } from '../src/api/types.ts';
import { copiesNotice } from '../src/pages/document.ts';

const names = new Map([
  ['gutenberg-zim', 'Project Gutenberg (Kiwix ZIM)'],
  ['kiwix-wikisource-en-all', 'Wikisource (Kiwix ZIM)'],
]);
const copy = (source_id: string, why: string) => ({
  doc_id: `wsp:${source_id}/x`,
  source_id,
  address: `wsp://${source_id}/x`,
  why,
});

describe('other copies', () => {
  it('offers the illustrated edition of a long text', () => {
    const doc = {
      copies: [copy('gutenberg-zim', 'the illustrated edition')],
      text: 'x'.repeat(5000),
      html: null,
    };
    const el = copiesNotice(doc as unknown as DocumentResponse, names)!;
    expect(el.textContent).toContain(
      'Also in the library: the illustrated edition (Project Gutenberg (Kiwix ZIM)).',
    );
    expect(el.querySelector('a')?.getAttribute('href')).toBe('#wsp://gutenberg-zim/x');
    expect(el.querySelector('a')?.textContent).toBe('the illustrated edition');
  });

  it('sends a reader of a stub to the complete copy', () => {
    const doc = {
      copies: [copy('kiwix-wikisource-en-all', 'the complete text as rendered by Wikisource')],
      text: '',
      html: null,
    };
    expect(copiesNotice(doc as unknown as DocumentResponse, names)!.textContent).toMatch(
      /^This page has little text here\. Read it in/,
    );
  });

  it('says nothing without copies', () => {
    expect(
      copiesNotice({ copies: [], text: '', html: null } as unknown as DocumentResponse, names),
    ).toBeNull();
  });
});
