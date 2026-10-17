/**
 * The book reader: a plain-text book (kind `books`) shown a chapter at a time with a table of
 * contents, previous/next, and the place the viewer left off remembered in this browser
 * (`core/book.ts`). A long chapter, or a long book with no chapters, is read a page at a time.
 * `?ch=N` picks a chapter or page (counted together, from 0), `?ch=all` the whole text at once.
 */
import type { DocumentResponse } from '../api/types.ts';
import { makeAddress } from '../core/address.ts';
import { type Chapter, pages, positionOf, rememberPosition, splitChapters } from '../core/book.ts';
import { h } from '../core/dom.ts';
import { wspLink } from '../ui/components.ts';
import type { AppServices } from './types.ts';

export interface ReaderContext {
  app: Pick<AppServices, 'storage'>;
  /** Chapter asked for: a number, 'all', or undefined (resume, else the first). */
  chapter: number | 'all' | undefined;
  address: string;
  /** Aborted when the page is left: ends the scroll tracking. */
  signal?: AbortSignal;
}

/** Id of the paragraph to resume at; the page result scrolls to it once the page is in place. */
export const RESUME_ID = 'reader-resume';

const SAVE_EVERY_MS = 1500;

export function isBook(doc: Pick<DocumentResponse, 'kind' | 'format'>): boolean {
  return doc.kind === 'books' && doc.format !== 'html';
}

/** The chapter to open: asked for, else where the viewer left off, else the first. */
export function chapterToOpen(
  asked: number | 'all' | undefined,
  remembered: number | null,
  count: number,
): number | 'all' {
  if (asked === 'all') return 'all';
  if (typeof asked === 'number') return Math.min(Math.max(asked, 0), count - 1);
  if (remembered !== null) return Math.min(Math.max(remembered, 0), count - 1);
  return 0;
}

export function renderBook(doc: DocumentResponse, text: string, ctx: ReaderContext): HTMLElement {
  const book = splitChapters(text, doc.title);
  const chapters = pages(book);
  const wrap = h('div', { class: 'doc-body reader', lang: doc.lang });
  if (chapters.length < 2) {
    wrap.append(chapterBody(chapters[0]!, false));
    return wrap;
  }
  const saved = positionOf(ctx.app.storage, doc.doc_id);
  const open = chapterToOpen(ctx.chapter, saved?.chapter ?? null, chapters.length);
  const here = (ch: number | 'all') => makeAddress(doc.source_id, doc.native_id, { ch });
  wrap.append(tableOfContents(chapters, open, here));
  if (open === 'all') {
    for (const [i, c] of book.entries()) wrap.append(chapterNode(c, i, false));
    return wrap;
  }
  const chapter = chapters[open]!;
  const body = chapterNode(chapter, open, true);
  const number = chapterNumbers(chapters);
  const page = chapter.page;
  const where = [
    book.length > 1 ? `Chapter ${number[open]! + 1} of ${book.length}` : '',
    page ? `${book.length > 1 ? ' · page' : 'Page'} ${page.index + 1} of ${page.of}` : '',
  ].join('');
  wrap.append(
    h(
      'p',
      { class: 'reader-where' },
      where,
      saved && saved.chapter === open && ctx.chapter === undefined
        ? h('span', { class: 'badge' }, 'where you left off')
        : null,
    ),
    body,
    navigation(chapters, open, here),
  );
  const remember = (paragraph: number) =>
    rememberPosition(ctx.app.storage, {
      docId: doc.doc_id,
      address: ctx.address,
      title: doc.title,
      chapter: open,
      paragraph,
      chapters: chapters.length,
      where,
    });
  remember(saved && saved.chapter === open && ctx.chapter === undefined ? saved.paragraph : 0);
  trackScroll(body, remember, open === chapters.length - 1, ctx.signal);
  if (saved && saved.chapter === open && ctx.chapter === undefined && saved.paragraph > 0) {
    // The body is not in the document yet: the page result scrolls to this id (pages/document.ts).
    body.querySelector(`[data-paragraph="${saved.paragraph}"]`)?.setAttribute('id', RESUME_ID);
  }
  return wrap;
}

/** For each page, the number (from 0) of the chapter it is part of. */
function chapterNumbers(units: readonly Chapter[]): number[] {
  let n = -1;
  return units.map((u) => (u.page && u.page.index > 0 ? n : ++n));
}

/** The first words of a page: what a contents line says of a page of a book with no chapters. */
function opening(c: Chapter): string {
  const words = (c.paragraphs[0] ?? '').split(/\s+/).slice(0, 8).join(' ');
  return words.length < (c.paragraphs[0] ?? '').length ? `${words}…` : words;
}

/** Paragraphs of a chapter: folded headings as subheadings, verse with its lines. */
function chapterBody(chapter: Chapter, numbered: boolean): HTMLElement {
  const box = h('div', { class: 'reader-text' });
  const subheads = new Set(chapter.subheads ?? []);
  for (const [i, p] of chapter.paragraphs.entries()) {
    const attrs: Record<string, string> = {};
    if (numbered) attrs['data-paragraph'] = String(i);
    if (p.includes('\n')) attrs.class = 'verse';
    box.append(h(subheads.has(i) ? 'h3' : 'p', attrs, p));
  }
  return box;
}

function chapterNode(chapter: Chapter, index: number, single: boolean): HTMLElement {
  const continued = chapter.page && chapter.page.index > 0;
  return h(
    'section',
    { class: 'reader-chapter', id: `chapter-${index + 1}` },
    h('h2', null, continued ? `${chapter.title} (continued)` : chapter.title),
    chapterBody(chapter, single),
  );
}

function tableOfContents(
  chapters: Chapter[],
  open: number | 'all',
  here: (ch: number | 'all') => string,
): HTMLElement {
  // One line per chapter, at its first page; a book with no chapters lists its pages by their opening.
  const number = chapterNumbers(chapters);
  const count = number.at(-1)! + 1;
  const current = open === 'all' ? -1 : number[open]!;
  const items = chapters.flatMap((c, i) => {
    if (count > 1 && c.page && c.page.index > 0) return [];
    const title =
      count > 1
        ? c.title + (c.page ? ` (${c.page.of} pages)` : '')
        : `Page ${i + 1}: ${opening(c)}`;
    const isCurrent = count > 1 ? number[i] === current : i === open;
    const label = i === open ? h('b', null, title) : wspLink(here(i), title);
    return [h('li', { class: isCurrent ? 'is-current' : '' }, label)];
  });
  return h(
    'details',
    { class: 'reader-toc', open: open === 'all' ? undefined : '' },
    h(
      'summary',
      null,
      count > 1 ? `Contents (${count} chapters)` : `Contents (${chapters.length} pages)`,
    ),
    h('ol', null, items),
    h(
      'p',
      { class: 'fineprint' },
      open === 'all'
        ? wspLink(here(0), 'A chapter at a time')
        : wspLink(here('all'), 'The whole book on one page'),
    ),
  );
}

/** Previous and next: a page of the same chapter when there is one, else the chapter. */
function navigation(
  units: readonly Chapter[],
  open: number,
  here: (ch: number | 'all') => string,
): HTMLElement {
  const page = units[open]!.page;
  const prevPage = !!page && page.index > 0;
  const nextPage = !!page && page.index + 1 < page.of;
  const paged = !!page && page.of === units.length; // a book with no chapters: pages only
  return h(
    'p',
    { class: 'reader-nav' },
    open > 0
      ? wspLink(here(open - 1), prevPage ? '← Previous page' : '← Previous chapter')
      : h('span', { class: 'dim' }, paged ? 'First page' : 'First chapter'),
    ' · ',
    open + 1 < units.length
      ? wspLink(here(open + 1), nextPage ? 'Next page →' : 'Next chapter →')
      : h('span', { class: 'dim' }, 'The end'),
  );
}

/**
 * Remember the first paragraph on screen as the viewer scrolls, a little after they stop; at the
 * end of the last chapter, remember the book as finished (paragraph -1, `core/book.ts`).
 *
 * The page scrolls inside `main.viewport`, not the window, and an element's scroll event does not
 * bubble: the listener captures on the document. It ends with the page (`signal`) or, without one,
 * at the first scroll after the chapter left the document.
 */
export function trackScroll(
  body: HTMLElement,
  remember: (paragraph: number) => void,
  lastChapter: boolean,
  signal?: AbortSignal,
): void {
  let timer: ReturnType<typeof setTimeout> | null = null;
  const stop = () => {
    document.removeEventListener('scroll', onScroll, { capture: true });
    if (timer) clearTimeout(timer);
  };
  const onScroll = () => {
    if (!body.isConnected) {
      stop();
      return;
    }
    if (timer) clearTimeout(timer);
    timer = setTimeout(() => {
      if (!body.isConnected) return;
      const ps = body.querySelectorAll<HTMLElement>('[data-paragraph]');
      let first = 0;
      for (const p of ps) {
        if (p.getBoundingClientRect().bottom > 0) {
          first = Number(p.dataset.paragraph);
          break;
        }
      }
      const last = ps[ps.length - 1];
      const atEnd =
        lastChapter && !!last && last.getBoundingClientRect().bottom <= window.innerHeight;
      remember(atEnd ? -1 : first);
    }, SAVE_EVERY_MS);
  };
  if (signal?.aborted) return;
  document.addEventListener('scroll', onScroll, { capture: true, passive: true });
  signal?.addEventListener('abort', stop, { once: true });
}
