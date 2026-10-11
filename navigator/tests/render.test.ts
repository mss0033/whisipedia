import { describe, expect, it } from 'vitest';
import { search } from '../mock/handler.ts';
import { diffLines, diffStats } from '../src/core/diff.ts';
import { highlightSnippet, queryTerms, segmentsFromRanges } from '../src/core/highlight.ts';
import { resolveRoute } from '../src/core/router.ts';
import { renderResultsPage } from '../src/pages/search.ts';

describe('highlighting', () => {
  it('extracts meaningful query terms', () => {
    expect(queryTerms('How do vaccines train the immune system?')).toEqual([
      'vaccines',
      'train',
      'immune',
      'system',
    ]);
  });

  it('uses server ranges as code point offsets', () => {
    expect(
      segmentsFromRanges('héllo wörld', [
        [6, 11],
        [0, 1],
        [0, 2],
      ]),
    ).toEqual([
      { text: 'hé', hit: true },
      { text: 'llo ', hit: false },
      { text: 'wörld', hit: true },
    ]);
  });

  it('falls back to client-side term matching with word extension', () => {
    const div = document.createElement('div');
    div.append(highlightSnippet('A vaccine trains immunity; Vaccines work.', 'vaccine'));
    expect([...div.querySelectorAll('b.hl')].map((b) => b.textContent)).toEqual([
      'vaccine',
      'Vaccines',
    ]);
  });

  it('never interprets snippet text as HTML', () => {
    const div = document.createElement('div');
    div.append(highlightSnippet('<img src=x onerror=alert(1)> rfc', 'rfc'));
    expect(div.querySelector('img')).toBeNull();
  });
});

describe('diffLines', () => {
  it('produces a minimal line diff', () => {
    const ops = diffLines('a\nb\nc\nd', 'a\nB\nc\nd\ne')!;
    expect(ops.map((o) => `${o.op}:${o.text}`)).toEqual([
      'eq:a',
      'del:b',
      'add:B',
      'eq:c',
      'eq:d',
      'add:e',
    ]);
    expect(diffStats(ops)).toEqual({ added: 2, removed: 1 });
  });

  it('refuses pathological sizes', () => {
    const big = Array.from({ length: 3000 }, (_, i) => `x${i}`).join('\n');
    const other = Array.from({ length: 3000 }, (_, i) => `y${i}`).join('\n');
    expect(diffLines(big, other)).toBeNull();
  });
});

describe('results page render budget', () => {
  it('renders ten hits well under one frame (16 ms)', () => {
    const resp = search(new URLSearchParams({ q: 'http caching', page_size: '10' }));
    expect(resp.hits).toHaveLength(10);
    const route = resolveRoute('wsp://search?q=http+caching') as Extract<
      ReturnType<typeof resolveRoute>,
      { page: 'search' }
    >;
    // warm up (JIT), then take the median of several runs
    for (let i = 0; i < 5; i += 1) renderResultsPage(resp, route, null);
    const times: number[] = [];
    let last: DocumentFragment | null = null;
    for (let i = 0; i < 15; i += 1) {
      const t0 = performance.now();
      last = renderResultsPage(resp, route, null);
      times.push(performance.now() - t0);
    }
    times.sort((a, b) => a - b);
    const median = times[Math.floor(times.length / 2)]!;
    const div = document.createElement('div');
    div.append(last!);
    expect(div.querySelectorAll('li.result')).toHaveLength(10);
    expect(div.querySelector('.result-address')?.textContent).toMatch(/^wsp:\/\//);
    // jsdom is several times slower than a browser; holding it to 16 ms is conservative.
    expect(median).toBeLessThan(16);
  });
});

describe('did you mean and site scope', () => {
  const route = (q: string) =>
    resolveRoute(`wsp://search?q=${encodeURIComponent(q)}`) as Extract<
      ReturnType<typeof resolveRoute>,
      { page: 'search' }
    >;
  const render = (q: string) => {
    const resp = search(new URLSearchParams({ q, page_size: '10' }));
    const div = document.createElement('div');
    div.append(
      renderResultsPage(resp, route(q), null, new Map([['gutenberg', 'Project Gutenberg']])),
    );
    return div;
  };

  it('offers a correction in the period style', () => {
    const p = render('vacine').querySelector('.did-you-mean');
    expect(p?.textContent).toBe('Did you mean: vaccine?');
    expect(p?.querySelector('a')?.getAttribute('href')).toContain('vaccine');
  });

  it('says quietly when a named site had nothing and the whole library was searched', () => {
    const div = render('gutenberg vaccines');
    const hint = div.querySelector('.scope-hint');
    expect(hint?.textContent).toBe(
      'No results on site Project Gutenberg; showing results from the whole library.',
    );
    expect(div.querySelector('.notice-warn.scope-hint')).toBeNull();
    expect(render('rfc caching').querySelector('.scope-hint')?.textContent).toBe(
      'Showing results from rfc.',
    );
    expect(render('http caching').querySelector('.scope-hint')).toBeNull();
  });
});
