import { describe, expect, it } from 'vitest';
import { markUncaptured, originalWebAddress } from '../src/pages/document.ts';
import type { PageContext } from '../src/pages/types.ts';

function page(): HTMLElement {
  const body = document.createElement('div');
  body.innerHTML = `
    <p><a data-wsp="wsp://site/www.ex.org/a/">A</a> <a data-wsp="wsp://site/www.ex.org/contact">Contact</a>
    <a data-wsp="wsp://site/www.ex.org/contact">Contact again</a>
    <a data-wsp="wsp://site/x" data-missing="https://far.example/">Remote</a>
    <a data-wsp="wsp://site/www.ex.org/a/#part">A, a section</a></p>
    <p class="reader-nav"><a data-wsp="wsp://site/book?ch=1">Next chapter →</a></p>`;
  return body;
}

describe('links to pages the site archive never captured (2026-10-19 audit)', () => {
  it('are marked once the library has said which, with a note', async () => {
    const asked: string[][] = [];
    const ctx = {
      signal: new AbortController().signal,
      app: {
        api: {
          missingLinks: async (a: string[]) => {
            asked.push(a);
            return ['wsp://site/www.ex.org/contact'];
          },
        },
      },
    } as unknown as Pick<PageContext, 'app' | 'signal'>;
    const body = page();
    const note = document.createElement('p');
    note.hidden = true;
    await markUncaptured(ctx, body, note);
    // Each address once; links already known to lead outside are not asked about.
    expect(asked).toEqual([['wsp://site/www.ex.org/a/', 'wsp://site/www.ex.org/contact']]);
    const marked = [...body.querySelectorAll('a.uncaptured-link')].map((a) => a.textContent);
    expect(marked).toEqual(['Contact', 'Contact again']);
    expect(body.querySelector('a.uncaptured-link')!.getAttribute('title')).toBe(
      'Not in library: wsp://site/www.ex.org/contact',
    );
    expect(note.hidden).toBe(false);
    expect(note.textContent).toContain('2 link(s)');
    expect(body.dataset.linksChecked).toBe('1');
  });

  it('name the web address a page of an archived website had', () => {
    expect(originalWebAddress('www.nhs.uk/social-care-and-support/')).toBe(
      'https://www.nhs.uk/social-care-and-support/',
    );
    expect(originalWebAddress('Talk:2798:_Room_Temperature')).toBeNull();
    expect(originalWebAddress('Proposal:Shepherd.s_hut')).toBeNull();
    expect(originalWebAddress('1.5/x')).toBeNull();
  });
});
