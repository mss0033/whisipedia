/**
 * The book reader: a plain-text book (kind `books`) shown a chapter at a time with a table of
 * contents, previous/next, and the place the viewer left off remembered in this browser
 * (`core/book.ts`). `?ch=N` picks a chapter, `?ch=all` the whole text at once.
 */
import type { DocumentResponse } from '../api/types.ts';
import { makeAddress } from '../core/address.ts';
import { type Chapter, positionOf, rememberPosition, splitChapters } from '../core/book.ts';
import { h } from '../core/dom.ts';
import { renderPlainText } from '../core/text.ts';
import { wspLink } from '../ui/components.ts';
import type { AppServices } from './types.ts';

export interface ReaderContext {
  app: Pick<AppServices, 'storage'>;
  /** Chapter asked for: a number, 'all', or undefined (resume, else the first). */
  chapter: number | 'all' | undefined;
  address: string;
}

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
  const chapters = splitChapters(text, doc.title);
  const wrap = h('div', { class: 'doc-body reader', lang: doc.lang });
  if (chapters.length < 2) {
    wrap.append(renderPlainText(text, false));
    return wrap;
  }
  const saved = positionOf(ctx.app.storage, doc.doc_id);
  const open = chapterToOpen(ctx.chapter, saved?.chapter ?? null, chapters.length);
  const here = (ch: number | 'all') => makeAddress(doc.source_id, doc.native_id, { ch });
  wrap.append(tableOfContents(chapters, open, here));
  if (open === 'all') {
    for (const [i, c] of chapters.entries()) wrap.append(chapterNode(c, i, false));
    return wrap;
  }
  const chapter = chapters[open]!;
  const body = chapterNode(chapter, open, true);
  wrap.append(
    h(
      'p',
      { class: 'reader-where' },
      `Chapter ${open + 1} of ${chapters.length}`,
      saved && saved.chapter === open && ctx.chapter === undefined
        ? h('span', { class: 'badge' }, 'where you left off')
        : null,
    ),
    body,
    navigation(open, chapters.length, here),
  );
  const remember = (paragraph: number) =>
    rememberPosition(ctx.app.storage, {
      docId: doc.doc_id,
      address: ctx.address,
      title: doc.title,
      chapter: open,
      paragraph,
      chapters: chapters.length,
    });
  remember(saved && saved.chapter === open && ctx.chapter === undefined ? saved.paragraph : 0);
  trackScroll(body, remember);
  if (saved && saved.chapter === open && ctx.chapter === undefined && saved.paragraph > 0) {
    queueMicrotask(() =>
      body.querySelector(`[data-paragraph="${saved.paragraph}"]`)?.scrollIntoView(),
    );
  }
  return wrap;
}

function chapterNode(chapter: Chapter, index: number, single: boolean): HTMLElement {
  const section = h(
    'section',
    { class: 'reader-chapter', id: `chapter-${index + 1}` },
    h('h2', null, chapter.title),
  );
  for (const [i, p] of chapter.paragraphs.entries()) {
    section.append(h('p', single ? { 'data-paragraph': String(i) } : null, p));
  }
  return section;
}

function tableOfContents(
  chapters: Chapter[],
  open: number | 'all',
  here: (ch: number | 'all') => string,
): HTMLElement {
  const items = chapters.map((c, i) =>
    h(
      'li',
      { class: i === open ? 'is-current' : '' },
      i === open ? h('b', null, c.title) : wspLink(here(i), c.title),
    ),
  );
  return h(
    'details',
    { class: 'reader-toc', open: open === 'all' ? undefined : '' },
    h('summary', null, `Contents (${chapters.length} chapters)`),
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

function navigation(
  open: number,
  count: number,
  here: (ch: number | 'all') => string,
): HTMLElement {
  return h(
    'p',
    { class: 'reader-nav' },
    open > 0
      ? wspLink(here(open - 1), '← Previous chapter')
      : h('span', { class: 'dim' }, 'First chapter'),
    ' · ',
    open + 1 < count
      ? wspLink(here(open + 1), 'Next chapter →')
      : h('span', { class: 'dim' }, 'The end'),
  );
}

/** Remember the first paragraph on screen as the viewer scrolls, a little after they stop. */
function trackScroll(body: HTMLElement, remember: (paragraph: number) => void): void {
  let timer: ReturnType<typeof setTimeout> | null = null;
  const onScroll = () => {
    if (!body.isConnected) {
      window.removeEventListener('scroll', onScroll);
      return;
    }
    if (timer) clearTimeout(timer);
    timer = setTimeout(() => {
      const ps = body.querySelectorAll<HTMLElement>('[data-paragraph]');
      let first = 0;
      for (const p of ps) {
        if (p.getBoundingClientRect().bottom > 0) {
          first = Number(p.dataset.paragraph);
          break;
        }
      }
      remember(first);
    }, SAVE_EVERY_MS);
  };
  window.addEventListener('scroll', onScroll, { passive: true });
}
