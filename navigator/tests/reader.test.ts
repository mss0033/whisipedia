// The book reader in jsdom against the mock API: chapters, navigation, memory, and the start page.
import { beforeEach, describe, expect, it } from 'vitest';
import { WspClient } from '../src/api/client.ts';
import { App } from '../src/app.ts';
import { positionOf } from '../src/core/book.ts';
import { resolveRoute } from '../src/core/router.ts';
import { MemoryStore, SafeStorage } from '../src/core/storage.ts';
import { mockFetch, until } from './helpers.ts';

const BOOK = 'wsp://gutenberg/84';

function boot() {
  document.body.innerHTML = '<div id="app"></div>';
  history.replaceState(null, '', '/');
  const root = document.getElementById('app')!;
  const storage = new SafeStorage(new MemoryStore());
  const app = new App(root, { api: new WspClient({ fetch: mockFetch() }), storage });
  app.start();
  const main = () => document.querySelector('main')!;
  const settled = (pred: () => boolean) =>
    until(() => !root.classList.contains('is-loading') && pred());
  return { app, main, settled, storage };
}

describe('book reader', () => {
  beforeEach(() => {
    document.documentElement.removeAttribute('data-theme');
  });

  it('parses the chapter parameter', () => {
    expect(resolveRoute(`${BOOK}?ch=2`)).toMatchObject({ page: 'document', chapter: 2 });
    expect(resolveRoute(`${BOOK}?ch=all`)).toMatchObject({ chapter: 'all' });
    expect(resolveRoute(`${BOOK}?ch=x`)).toMatchObject({ chapter: undefined });
  });

  it('opens a book a chapter at a time with contents and navigation', async () => {
    const { app, main, settled } = boot();
    app.navigate(BOOK);
    await settled(() => !!main().querySelector('.reader'));
    const toc = [...main().querySelectorAll('.reader-toc li')].map((li) => li.textContent);
    expect(toc).toEqual([
      'Frankenstein; Or, The Modern Prometheus by Mary Wollstonecraft Shelley',
      'LETTER 1',
      'LETTER 2',
      'CHAPTER I',
      'CHAPTER II',
    ]);
    expect(main().querySelector('.reader-where')?.textContent).toContain('Chapter 1 of 5');
    expect(main().querySelector('.reader-chapter h2')?.textContent).toContain('Frankenstein');
    expect(main().querySelector('.reader-nav')?.textContent).toContain('Next chapter');
    main().querySelector<HTMLAnchorElement>('.reader-nav a')!.click();
    await settled(
      () => main().querySelector('.reader-where')?.textContent?.includes('Chapter 2') ?? false,
    );
    expect(main().querySelector('.reader-chapter h2')?.textContent).toBe('LETTER 1');
    expect(main().querySelector('.reader-chapter p')?.textContent).toContain('To Mrs. Saville');
    expect(location.hash).toBe(`#${BOOK}?ch=1`);
  });

  it('remembers the place and offers to continue on the start page', async () => {
    const { app, main, settled, storage } = boot();
    app.navigate(`${BOOK}?ch=3`);
    await settled(() => main().querySelector('.reader-chapter h2')?.textContent === 'CHAPTER I');
    expect(positionOf(storage, 'wsp:gutenberg/84')).toMatchObject({
      chapter: 3,
      paragraph: 0,
      chapters: 5,
    });
    app.navigate(BOOK); // no chapter asked: back where we were
    await settled(() => !!main().querySelector('.reader-where .badge'));
    expect(main().querySelector('.reader-chapter h2')?.textContent).toBe('CHAPTER I');
    app.navigate('wsp://home');
    await settled(() => !!main().querySelector('.continue-reading'));
    const item = main().querySelector('.continue-reading li')!;
    expect(item.textContent).toContain('Frankenstein');
    expect(item.textContent).toContain('chapter 4 of 5');
    expect(item.querySelector('a')?.dataset.wsp).toBe(BOOK);
  });

  it('shows the whole book on one page when asked', async () => {
    const { app, main, settled } = boot();
    app.navigate(`${BOOK}?ch=all`);
    await settled(() => main().querySelectorAll('.reader-chapter').length === 5);
    expect(main().querySelector('.reader-nav')).toBeNull();
    expect(main().querySelector('.reader-toc .fineprint a')?.textContent).toBe(
      'A chapter at a time',
    );
  });
});

describe('reader scroll tracking', () => {
  it('hears scrolls of the viewport element, marks the end of the last chapter, and stops with the page', async () => {
    const { vi } = await import('vitest');
    const { trackScroll } = await import('../src/pages/reader.ts');
    vi.useFakeTimers();
    try {
      document.body.innerHTML = '<main class="viewport"><section></section></main>';
      const main = document.querySelector('main')!;
      const body = main.querySelector('section')!;
      const paras = [0, 1, 2].map((i) => {
        const p = document.createElement('p');
        p.dataset.paragraph = String(i);
        body.append(p);
        return p;
      });
      // Paragraph 0 is above the top; paragraphs 1 and 2 are on screen.
      const tops = [-200, 100, 400];
      paras.forEach((p, i) => {
        p.getBoundingClientRect = () => ({ bottom: tops[i]! + 100 }) as DOMRect;
      });
      const seen: number[] = [];
      const ac = new AbortController();
      trackScroll(body, (n) => seen.push(n), false, ac.signal);
      main.dispatchEvent(new Event('scroll')); // does not bubble: only a capturing listener hears it
      vi.advanceTimersByTime(2000);
      expect(seen).toEqual([1]);

      const seenLast: number[] = [];
      trackScroll(body, (n) => seenLast.push(n), true, ac.signal);
      main.dispatchEvent(new Event('scroll'));
      vi.advanceTimersByTime(2000);
      expect(seenLast).toEqual([-1]); // the last paragraph's bottom is on screen: finished
      expect(seen).toEqual([1, 1]); // the first tracker heard that scroll too

      ac.abort();
      main.dispatchEvent(new Event('scroll'));
      vi.advanceTimersByTime(2000);
      expect(seen).toEqual([1, 1]); // the page left: nothing more is remembered
      expect(seenLast).toEqual([-1]);
    } finally {
      vi.useRealTimers();
    }
  });
});

describe('reading a long book a page at a time (2026-10-16 field exam)', () => {
  const para = (n: number) => `Paragraph ${n}. ${'The story goes on. '.repeat(50)}`;
  const doc = (text: string) =>
    ({
      doc_id: 'wsp:gutenberg/1',
      source_id: 'gutenberg',
      native_id: '1',
      title: 'Long',
      lang: 'en',
      kind: 'books',
      format: 'text',
      text,
    }) as unknown as import('../src/api/types.ts').DocumentResponse;
  const read = async (text: string, chapter: number | undefined) => {
    const { renderBook } = await import('../src/pages/reader.ts');
    const storage = new SafeStorage(new MemoryStore());
    const node = renderBook(doc(text), text, {
      app: { storage },
      chapter,
      address: 'wsp://gutenberg/1',
    });
    return { node, storage };
  };
  const chapter = (title: string, n: number) =>
    [title, ...Array.from({ length: n }, (_, i) => para(i))].join('\n\n');
  // Front matter, a short chapter, a chapter of ~200,000 characters, a short chapter.
  const BOOK_TEXT = [
    'By A. Writer.',
    chapter('CHAPTER I', 5),
    chapter('CHAPTER II', 200),
    chapter('CHAPTER III', 5),
  ].join('\n\n');

  it('cuts a long chapter into pages with next page, then next chapter', async () => {
    const { node, storage } = await read(BOOK_TEXT, 2);
    expect(node.querySelector('.reader-where')?.textContent).toBe('Chapter 3 of 4 · page 1 of 4');
    expect(node.querySelector('.reader-nav')?.textContent).toBe('← Previous chapter · Next page →');
    const toc = [...node.querySelectorAll('.reader-toc li')].map((li) => li.textContent);
    expect(toc).toEqual(['Long', 'CHAPTER I', 'CHAPTER II (4 pages)', 'CHAPTER III']);
    expect(positionOf(storage, 'wsp:gutenberg/1')).toMatchObject({
      where: 'Chapter 3 of 4 · page 1 of 4',
    });
    const last = (await read(BOOK_TEXT, 5)).node;
    expect(last.querySelector('.reader-chapter h2')?.textContent).toBe('CHAPTER II (continued)');
    expect(last.querySelector('.reader-nav')?.textContent).toBe('← Previous page · Next chapter →');
    expect(last.querySelector('.reader-toc li.is-current')?.textContent).toBe(
      'CHAPTER II (4 pages)',
    );
    // every paragraph of the chapter is on one of its pages, in order
    const seen: string[] = [];
    for (let ch = 2; ch <= 5; ch++) {
      const { node: page } = await read(BOOK_TEXT, ch);
      seen.push(...[...page.querySelectorAll('.reader-chapter p')].map((p) => p.textContent!));
    }
    expect(seen).toEqual(Array.from({ length: 200 }, (_, i) => para(i).trim()));
  });

  it('reads a long book with no chapters by pages listed by their opening words', async () => {
    const text = Array.from({ length: 200 }, (_, i) => para(i)).join('\n\n');
    const { node } = await read(text, undefined);
    expect(node.querySelector('.reader-where')?.textContent).toBe('Page 1 of 4');
    expect(node.querySelector('.reader-toc summary')?.textContent).toBe('Contents (4 pages)');
    expect(node.querySelector('.reader-toc li:nth-child(2)')?.textContent).toMatch(
      /^Page 2: Paragraph \d+\. The story goes on\. The story…$/,
    );
    expect(node.querySelector('.reader-nav')?.textContent).toBe('First page · Next page →');
  });

  it('keeps a short book with no chapters on one page, verse with its lines', async () => {
    const text = `${'A line of prose that fills the width of the text as prose does. '.repeat(30).replace(/(.{60,68}) /g, '$1\n')}\n\nThe sea is calm tonight.\nThe tide is full, the moon lies fair`;
    const { node } = await read(text, undefined);
    expect(node.querySelector('.reader-nav')).toBeNull();
    const verse = node.querySelector('p.verse');
    expect(verse?.textContent).toBe(
      'The sea is calm tonight.\nThe tide is full, the moon lies fair',
    );
  });
});
