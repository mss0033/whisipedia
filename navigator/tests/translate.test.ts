import { describe, expect, it } from 'vitest';
import type { WspClient } from '../src/api/client.ts';
import {
  applyToBlock,
  blockText,
  buildTranslated,
  sourceLanguage,
  splitSegment,
  translatableBlocks,
  translationBar,
} from '../src/core/translate.ts';

function frag(html: string): HTMLElement {
  const div = document.createElement('div');
  div.innerHTML = html;
  document.body.append(div);
  return div;
}

describe('a block as a segment and back (ADR-0013)', () => {
  it('stands links and formatting as <xN> and pictures as ⟦N⟧, and rebuilds them from clones', () => {
    const div = frag(
      '<p>Die <a href="#wsp:a" data-wsp="wsp:a">Stadt</a> liegt am <b>Rhein</b> <img src="/v1/x.png" alt="Karte"> und x &lt; 5.</p>',
    );
    const p = div.querySelector('p')!;
    const bt = blockText(p);
    expect(bt.text).toBe('Die <x1>Stadt</x1> liegt am <x2>Rhein</x2> ⟦3⟧ und x < 5.');
    const t = applyToBlock(p, bt, 'The <x1>city</x1> lies on the <x2>Rhine</x2> ⟦3⟧ and x < 5.');
    expect(t).not.toBeNull();
    const a = p.querySelector('a')!;
    expect(a.textContent).toBe('city');
    expect(a.getAttribute('data-wsp')).toBe('wsp:a'); // the original link, cloned
    expect(p.querySelector('b')?.textContent).toBe('Rhine');
    expect(p.querySelector('img')?.getAttribute('alt')).toBe('Karte'); // the very picture
    expect(p.textContent).toContain('and x < 5.');
    expect(p.dataset.mt).toBe('translated');
    // The block's own switch shows the original again, and back.
    p.querySelector<HTMLButtonElement>('.mt-toggle')!.click();
    expect(p.textContent).toContain('Die Stadt liegt am Rhein');
    expect(p.querySelector('img')).not.toBeNull();
    p.querySelector<HTMLButtonElement>('.mt-toggle')!.click();
    expect(p.textContent).toContain('The city lies');
    div.remove();
  });

  it('refuses a translation whose placeholders do not fit the block', () => {
    const div = frag('<p>Die <a href="#x">Stadt</a> <img src="/v1/a.png"></p>');
    const bt = blockText(div.querySelector('p')!);
    expect(buildTranslated('The city ⟦2⟧', bt)).toBeNull(); // the link was lost
    expect(buildTranslated('The <x1>city ⟦2⟧', bt)).toBeNull(); // unclosed
    expect(buildTranslated('The <x1>city</x1> <x1>again</x1> ⟦2⟧', bt)).toBeNull(); // twice
    expect(buildTranslated('The <x1>city</x1> ⟦2⟧ ⟦9⟧', bt)).toBeNull(); // invented
    expect(
      buildTranslated('<script>alert(1)</script> The <x1>city</x1> ⟦2⟧', bt)?.map(
        (n) => n.nodeName,
      ),
    ).toEqual(['#text', 'A', '#text', 'IMG']); // anything else the model writes is text, never markup
    div.remove();
  });

  it('text that looks like a placeholder is not one', () => {
    const div = frag('<p>Schreibe &lt;x1&gt; und ⟦2⟧ wörtlich.</p>');
    expect(blockText(div.querySelector('p')!).text).toBe('Schreibe ‹x1› und |2| wörtlich.');
    div.remove();
  });

  it('takes the blocks with words of their own, not code, and a nested list as its own segment', () => {
    const div = frag(
      '<h2>Geschichte</h2><ul><li>Gründung <ul><li>Erste Siedlung</li></ul></li></ul><pre><code>x = 1</code></pre><p>1999</p><td>…</td>',
    );
    const blocks = translatableBlocks(div);
    expect(blocks.map((b) => b.textContent?.trim().slice(0, 12))).toEqual([
      'Geschichte',
      'Gründung Ers',
      'Erste Siedlu',
    ]);
    expect(blockText(blocks[1]!).text).toBe('Gründung ⟦1⟧');
    div.remove();
  });

  it('knows which pages are not English', () => {
    expect(sourceLanguage('de')).toBe('de');
    expect(sourceLanguage('pt-BR')).toBe('pt');
    expect(sourceLanguage('en')).toBeNull();
    expect(sourceLanguage('mul')).toBeNull();
    expect(sourceLanguage(null)).toBeNull();
  });
});

describe('the translation bar and banner', () => {
  it('offers the translation, then shows the banner that cannot be dismissed and translates the page', async () => {
    const asked: string[][] = [];
    let busy = 1;
    const api = {
      translationStatus: async () => ({
        available: true,
        model: 'MiLMMT-46-12B v1.0 (Q8_0)',
        state: 'stopped',
        languages: ['de'],
        reason: null,
        error: null,
      }),
      translate: async (_lang: string, segments: string[]) => {
        if (busy-- > 0) return { retryAfter: 0, message: 'the translator is starting' };
        asked.push(segments);
        return {
          model: 'MiLMMT-46-12B v1.0 (Q8_0)',
          prompt_version: 'milmmt-tags-1',
          segments: segments.map((s) =>
            s.includes('11')
              ? { text: null, ok: false, reason: 'numbers differ (missing 11)', cached: false }
              : { text: `EN ${s}`, ok: true, reason: null, cached: false },
          ),
        };
      },
    } as unknown as WspClient;
    const page = frag(
      '<h1 id="doc-title">Berlin</h1><div class="doc-body"><p>Berlin ist groß.</p><p>Es hat 11 Bezirke.</p></div>',
    );
    const content = page.querySelector<HTMLElement>('.doc-body')!;
    const bar = translationBar(api, { lang: 'de', title: 'Berlin' }, content, {
      legal: true,
      heading: () => page.querySelector('h1'),
    })!;
    page.prepend(bar);
    await new Promise((r) => setTimeout(r, 0));
    expect(bar.hidden).toBe(false);
    bar.querySelector<HTMLButtonElement>('.mt-start')!.click();
    for (
      let i = 0;
      i < 50 && !page.querySelector('.mt-progress')?.textContent?.includes('passages translated');
      i++
    )
      await new Promise((r) => setTimeout(r, 10));
    const banner = page.querySelector('.mt-banner')!;
    expect(banner.getAttribute('role')).toBe('note');
    expect(banner.textContent).toContain(
      'Machine translation from German by MiLMMT-46-12B v1.0 (Q8_0)',
    );
    expect(banner.textContent).toContain('The original is the authority.');
    expect(banner.textContent).toContain('Not a legal translation');
    expect(banner.querySelector('button.close, [aria-label="Close"]')).toBeNull(); // nothing dismisses it
    expect(asked).toEqual([['Berlin', 'Berlin ist groß.', 'Es hat 11 Bezirke.']]); // after one wait
    const [first, second] = Array.from(content.querySelectorAll('p'));
    expect(first?.textContent).toContain('EN Berlin ist groß.');
    expect(second?.textContent).toContain(
      'Es hat 11 Bezirke. (not translated: numbers differ (missing 11))',
    );
    expect(page.querySelector('.mt-title')?.textContent).toBe('EN Berlin (machine translation)');
    expect(banner.textContent).toContain('1 of 2 passages translated; 1 left in German');
    // The whole page back to the original, and again.
    banner.querySelector<HTMLButtonElement>('.mt-page-toggle')!.click();
    expect(first?.textContent).toContain('Berlin ist groß.');
    banner.querySelector<HTMLButtonElement>('.mt-page-toggle')!.click();
    expect(first?.textContent).toContain('EN Berlin');
    page.remove();
  });

  it('offers nothing on an English page', () => {
    const api = {} as WspClient;
    expect(
      translationBar(api, { lang: 'en', title: 'x' }, document.createElement('div')),
    ).toBeNull();
  });
});

describe('a long block in pieces (German Wikipedia\'s "Rhein" is one paragraph of 100,000 characters)', () => {
  it('splits at sentence ends, never inside a placeholder pair, and the pieces join back', () => {
    const sentence = 'Der Rhein ist ein Strom in Europa. ';
    const text =
      `${sentence.repeat(30)}Er fließt durch <x1>Basel. Und Köln.</x1> ${sentence.repeat(30)}`.trim();
    const { pieces, seps } = splitSegment(text, 300);
    expect(pieces.length).toBeGreaterThan(3);
    expect(pieces.every((p) => p.length <= 400)).toBe(true);
    const joined = pieces.reduce((acc, p, n) => acc + (n ? seps[n - 1] : '') + p, '');
    expect(joined).toBe(text);
    const withPair = pieces.find((p) => p.includes('<x1>'))!;
    expect(withPair).toContain('</x1>'); // the pair is in one piece
    expect(splitSegment('kurz')).toEqual({ pieces: ['kurz'], seps: [] });
  });

  it('splits a block that is one wrapper from end to end inside it (2026-10-26 review #115)', () => {
    const sentence = 'Der Rhein ist ein Strom in Europa. ';
    const text = `<x1>${sentence.repeat(40).trim()}</x1>`;
    const { pieces, seps, head, tail } = splitSegment(text, 300);
    expect(pieces.length).toBeGreaterThan(3);
    expect(pieces.every((p) => !p.includes('<x1>') && !p.includes('</x1>'))).toBe(true);
    expect([head, tail]).toEqual(['<x1>', '</x1>']);
    const joined = pieces.reduce((acc, p, n) => acc + (n ? seps[n - 1] : '') + p, '');
    expect(`${head}${joined}${tail}`).toBe(text);
  });
});

describe('a long block translated piece by piece', () => {
  it('shows the translated pieces, keeps a failed piece in its own language, and says so', async () => {
    const s = 'Der Rhein fließt durch Europa und ist lang. ';
    const long = `${s.repeat(40)}FEHLER hier. ${s.repeat(40)}`;
    const api = {
      translationStatus: async () => ({
        available: true,
        model: 'M',
        state: 'ready',
        languages: ['de'],
        reason: null,
        error: null,
      }),
      translate: async (_lang: string, segments: string[]) => ({
        model: 'M',
        prompt_version: 'v',
        segments: segments.map((seg) =>
          seg.includes('FEHLER')
            ? { text: null, ok: false, reason: 'numbers differ (added 2)', cached: false }
            : {
                text: seg.replaceAll(
                  'Der Rhein fließt durch Europa und ist lang.',
                  'The Rhine flows through Europe.',
                ),
                ok: true,
                reason: null,
                cached: false,
              },
        ),
      }),
    } as unknown as WspClient;
    const page = frag(`<div class="doc-body"><p>${long}</p></div>`);
    const content = page.querySelector<HTMLElement>('.doc-body')!;
    const bar = translationBar(api, { lang: 'de', title: 'Rhein' }, content)!;
    page.prepend(bar);
    await new Promise((r) => setTimeout(r, 0));
    bar.querySelector<HTMLButtonElement>('.mt-start')!.click();
    for (
      let i = 0;
      i < 100 && !page.querySelector('.mt-progress')?.textContent?.includes('passages translated');
      i++
    )
      await new Promise((r) => setTimeout(r, 10));
    const p = content.querySelector('p')!;
    expect(p.textContent).toContain('The Rhine flows through Europe.');
    expect(p.textContent).toContain('FEHLER hier.'); // the failed piece, in its own language
    expect(p.querySelector('.mt-failed')?.textContent).toBe(
      ' (part of this passage is not translated: numbers differ (added 2))',
    );
    // Not counted as translated: part of it is not (2026-10-25: "1 of 1 passages translated" over a
    // passage whose every piece but one had failed).
    expect(page.querySelector('.mt-progress')?.textContent).toBe(
      '0 of 1 passages translated, 1 in part (marked).',
    );
    page.remove();
  });
});
