/**
 * wsp://<source>/<native_id> — a document rendered inside the period page
 * frame, with webring bar, provenance footer, time machine and View Source
 * Data.
 */
import { ApiRequestError } from '../api/client.ts';
import type { DocumentResponse, SearchResults, SourceInfo } from '../api/types.ts';
import { docIdToAddress, makeAddress, searchAddress } from '../core/address.ts';
import { diffLines, diffStats } from '../core/diff.ts';
import { type Child, h } from '../core/dom.ts';
import { formatDate } from '../core/format.ts';
import { sanitizeDocumentHtml } from '../core/links.ts';
import {
  formatLabel,
  mediaKindOf,
  readSkin,
  remember,
  resumeAt,
  writeSkin,
} from '../core/media.ts';
import type { Route } from '../core/router.ts';
import { renderPlainText } from '../core/text.ts';
import {
  asOf,
  badge,
  errorPage,
  KIND_LABELS,
  licenseBadge,
  pageHeading,
  wspLink,
} from '../ui/components.ts';
import { audioDeck, videoPlayer } from '../ui/player.ts';
import { comicBar, comicView, isComic, looksLikeComic } from './comics.ts';
import { parseCase, renderCase } from './legal.ts';
import { isManPage, looksLikeManPage, manPageView } from './manpages.ts';
import { looksLikePlanetMath, planetMathView } from './planetmath.ts';
import { POOL_SOURCE } from './practice.ts';
import { isBook, RESUME_ID, type ReaderContext, renderBook } from './reader.ts';
import { isThread, threadView } from './threads.ts';
import type { AppServices, PageContext, PageResult } from './types.ts';

type DocRoute = Extract<Route, { page: 'document' }>;
type SearchHit = SearchResults['hits'][number];

/**
 * Small LRU so Back/Forward are instant, bounded by entries and by size: forty 1-2 MB articles
 * held 100-160 MB of tab memory (review 2026-10-14).
 */
export class Lru<V> {
  private readonly map = new Map<string, V>();
  private total = 0;
  constructor(
    private readonly max: number,
    private readonly maxSize = Number.POSITIVE_INFINITY,
    private readonly sizeOf: (v: V) => number = () => 0,
  ) {}
  get(k: string): V | undefined {
    const v = this.map.get(k);
    if (v !== undefined) {
      this.map.delete(k);
      this.map.set(k, v);
    }
    return v;
  }
  set(k: string, v: V): void {
    this.delete(k);
    this.map.set(k, v);
    this.total += this.sizeOf(v);
    while (this.map.size > 1 && (this.map.size > this.max || this.total > this.maxSize)) {
      this.delete(this.map.keys().next().value!);
    }
  }
  delete(k: string): void {
    const old = this.map.get(k);
    if (old === undefined) return;
    this.total -= this.sizeOf(old);
    this.map.delete(k);
  }
  get size(): number {
    return this.map.size;
  }
}

/** UTF-16 code units of a document's bodies, about its bytes in the tab. */
function docSize(doc: DocumentResponse): number {
  return 2 * ((doc.html?.length ?? 0) + (doc.text?.length ?? 0)) + 4096;
}

const cache = new Lru<DocumentResponse>(40, 24 * 1024 * 1024, docSize);

/** A document fetched elsewhere (the dictionary's lookup) served by the next visit instead of again. */
export function rememberDocument(id: string, doc: DocumentResponse): void {
  cache.set(`${id}@`, doc);
}

/**
 * A catalog card's "does the library hold this book?" search, remembered by title: Back, Forward
 * and a second visit ran the 2-5 s search again (review 2026-10-14).
 */
const lookups = new Lru<Promise<SearchHit[]>>(200);

function lookupBooks(ctx: PageContext, title: string): Promise<SearchHit[]> {
  const key = title.toLowerCase();
  const hit = ctx.opts.reload ? undefined : lookups.get(key);
  if (hit) return hit;
  const found = ctx.app.api.search({ q: title, kind: 'books', page_size: 10 }).then((r) => r.hits);
  lookups.set(key, found);
  found.catch(() => lookups.delete(key)); // a failure is not remembered
  return found;
}

/** Ids a mistyped or older link may have meant: with a trailing slash, and with the last part's first
 * letter in the other case. At most two extra requests, only after a 404. */
export function nearMisses(id: string): string[] {
  const out: string[] = [];
  if (!id.endsWith('/')) out.push(`${id}/`);
  const cut = id.lastIndexOf('/') + 1;
  const first = id.charAt(cut);
  const flipped = first === first.toLowerCase() ? first.toUpperCase() : first.toLowerCase();
  if (flipped !== first) out.push(id.slice(0, cut) + flipped + id.slice(cut + 1));
  return out;
}

/** `text`: also the plain text of an HTML page (only the diff needs it). */
async function loadDoc(
  ctx: PageContext,
  id: string,
  at?: string,
  text = false,
): Promise<DocumentResponse> {
  const key = `${id}@${at ?? ''}${text ? '+text' : ''}`;
  if (!ctx.opts.reload) {
    const hit = cache.get(key);
    if (hit) return hit;
  }
  let doc: DocumentResponse;
  try {
    doc = await ctx.app.api.document(id, { at, text }, ctx.signal);
  } catch (err) {
    // A link or a typed address that lost a native id's trailing slash (`xkcd.com/935` for
    // `xkcd.com/935/`): one more try with it, as the API's resolver does.
    if (!(err instanceof ApiRequestError && err.status === 404)) throw err;
    // Variants of a near miss, tried in order: the trailing slash some native ids end with
    // (`xkcd.com/935` for `xkcd.com/935/`), then the other case of the last part's first letter
    // (`/Safety` for `/safety`, 2026-10-14 field exam).
    let found: DocumentResponse | null = null;
    for (const variant of nearMisses(id)) {
      found = await ctx.app.api.document(variant, { at, text }, ctx.signal).catch(() => null);
      if (found) break;
    }
    if (!found) throw err;
    doc = found;
  }
  cache.set(key, doc);
  return doc;
}

function asOfText(doc: DocumentResponse): string {
  return doc.timestamp ? formatDate(doc.timestamp) : (doc.generation_id ?? 'an unknown date');
}

export function isPreformatted(doc: Pick<DocumentResponse, 'source_id' | 'kind'>): boolean {
  return doc.source_id === 'rfc' || doc.kind === 'data';
}

/** The page heading already shows the title: drop a first line that only repeats it. */
export function withoutTitleLine(text: string, title: string): string {
  const m = /^\s*([^\n]*)\n?/.exec(text);
  const norm = (x: string) => x.replace(/\s+/g, ' ').trim().toLowerCase();
  return m && norm(m[1]!) === norm(title) && norm(title) ? text.slice(m[0].length) : text;
}

export function renderBody(
  doc: DocumentResponse,
  reader?: ReaderContext,
): { node: HTMLElement; broken: number } {
  if (reader && isBook(doc) && doc.text?.trim()) {
    return { node: renderBook(doc, withoutTitleLine(doc.text, doc.title), reader), broken: 0 };
  }
  const wrap = h('div', { class: 'doc-body', lang: doc.lang });
  if (doc.format === 'html' && doc.html) {
    const { fragment, stats } = sanitizeDocumentHtml(rewritePage(doc.html, doc.source_id), {
      sourceId: doc.source_id,
      nativeId: doc.native_id,
      docId: doc.doc_id,
      links: doc.links,
    });
    wrap.appendChild(fragment);
    return { node: wrap, broken: stats.broken };
  }
  const text = withoutTitleLine(doc.text ?? '', doc.title);
  if (!text.trim() && !doc.original) {
    // e.g. a wiki page built only from templates: the dump's copy has no text of its own.
    wrap.appendChild(
      h(
        'p',
        { class: 'notice' },
        'The library’s copy of this page has no text of its own (upstream it is assembled from other pages). ',
        wspLink(searchAddress(doc.title.replace(/[_/]+/g, ' ')), 'Search the library for it'),
        '.',
      ),
    );
    return { node: wrap, broken: 0 };
  }
  // Court opinions: a caption and paragraphs (pages/legal.ts), not raw text.
  const opinion = doc.kind === 'law' ? parseCase(doc.text ?? '', doc.title) : null;
  wrap.appendChild(opinion ? renderCase(opinion) : renderPlainText(text, isPreformatted(doc)));
  return { node: wrap, broken: wrap.querySelectorAll('.broken-link').length };
}

/** What a catalog card needs to say where the book is: the book sites, and a lookup among them. */
export interface CatalogHelp {
  /** Ids of the sources whose documents are books. */
  bookSources: ReadonlySet<string>;
  /** The library's books matching a title, best first (a search with `kind=books`). */
  lookup?:
    | ((
        title: string,
      ) => Promise<
        ReadonlyArray<{ doc_id: string; title: string; address: string; source_name: string }>
      >)
    | undefined;
  now?: Date | undefined;
}

/** US public domain: a work published in year Y is free from 1 January of Y + 96 (1930 in 2026). */
export function publicDomainLine(now = new Date()): number {
  return now.getFullYear() - 96;
}

/**
 * Sources of complete, readable books, best first. A Wikisource page named after a work is often a
 * list of its editions: "Moby Dick" sent readers to a disambiguation page while Gutenberg held the
 * novel (2026-10-14 field exam).
 */
const BOOK_SOURCE_RANK = [
  'gutenberg',
  'standard-ebooks',
  'gutenberg-zim',
  'kiwix-wikisource-en-all',
  'wikisource-en',
];

/** The best search hit that is this book: same title (a subtitle or "by" may follow), best source first. */
export function bestBookMatch<
  T extends { title: string; doc_id?: string; source_id?: string | null },
>(hits: readonly T[], key: string): T | undefined {
  const same = hits.filter((hit) => {
    const k = titleKey(hit.title);
    if (/\bdisambiguation\b/.test(k)) return false;
    return (
      k === key ||
      k.startsWith(`${key} by `) ||
      k.startsWith(`${key} or `) ||
      k.startsWith(`${key} a `)
    );
  });
  const rank = (hit: T) => {
    const source = hit.source_id ?? hit.doc_id?.replace(/^wsp:/, '').split('/')[0] ?? '';
    const i = BOOK_SOURCE_RANK.indexOf(source);
    return i === -1 ? BOOK_SOURCE_RANK.length : i;
  };
  return same.sort((a, b) => rank(a) - rank(b))[0];
}

function titleKey(title: string): string {
  return title
    .normalize('NFKD')
    .replace(/[\u0300-\u036f]/g, '')
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, ' ')
    .trim();
}

/** The year a card says the book came out ("First published: 1818", "Published: Lackington, 1818"). */
export function cardYear(text: string): number | null {
  const m = /^(?:First published|Published):[^\n]*?\b(1[5-9]\d\d|20\d\d)\b/m.exec(text);
  return m ? Number(m[1]) : null;
}

const BOOK_LINK_RE = /^wsp:(gutenberg|standard-ebooks|wikisource-en)\/(.+)$/;
const BOOK_SITE_NAMES: Record<string, string> = {
  gutenberg: 'Project Gutenberg',
  'standard-ebooks': 'Standard Ebooks',
  'wikisource-en': 'Wikisource',
};

/**
 * A library catalog card (Open Library): it describes a book the library may not hold. Say where
 * the book is: in the library (a link the card carries resolves to a held book, or a book of the
 * same title turns up), elsewhere (the card names a Project Gutenberg copy the library has not
 * shelved), or nowhere free (published after the public-domain line), with the search as the
 * fallback. The owner asked for "go right to the book" or "why not" (2026-10-13).
 */
export function catalogNotice(
  doc: Pick<DocumentResponse, 'title' | 'text' | 'source_id'> &
    Partial<Pick<DocumentResponse, 'links'>>,
  source: Pick<SourceInfo, 'domain'> | undefined,
  help?: CatalogHelp,
): HTMLElement | null {
  if (!source?.domain.includes('catalog')) return null;
  const text = doc.text ?? '';
  const title = doc.title.replace(/\s*[:(].*$/, '').trim() || doc.title; // "Title : subtitle" -> "Title"
  const work = /^Work:\s*(works\/OL\d+W)\s*$/m.exec(text)?.[1];
  const workLink = work
    ? [' · ', wspLink(makeAddress(doc.source_id, work), 'All editions of this work')]
    : null;
  const searchLink = (label: string) => wspLink(searchAddress(title, { kind: 'books' }), label);
  const notice = (...body: Child[]) =>
    h(
      'div',
      { class: 'notice catalog-notice', role: 'note' },
      h('b', null, 'Catalog card. '),
      ...body,
    );
  if (/^Open Library catalog record: author\.$/m.test(text)) {
    return notice(
      'This page describes an author. ',
      searchLink(`Look for books by ${title} in the library`),
      '.',
    );
  }
  const links = doc.links ?? [];
  const held = links.find(
    (l) => l.target && help?.bookSources.has(l.target.replace(/^wsp:/, '').split('/')[0] ?? ''),
  );
  if (held?.target) {
    const m = BOOK_LINK_RE.exec(held.target);
    const where = m
      ? ` (${BOOK_SITE_NAMES[m[1]!] ?? m[1]} ${m[1] === 'gutenberg' ? '#' : ''}${m[2]})`
      : '';
    return notice(
      'The library holds this book: ',
      wspLink(docIdToAddress(held.target), h('b', null, `Read ${title}`)),
      where,
      '.',
      workLink,
    );
  }
  const promised = links
    .map((l) => BOOK_LINK_RE.exec(l.href))
    .find((m) => m && !links.find((l) => l.href === m[0])?.target);
  if (promised) {
    const site = BOOK_SITE_NAMES[promised[1]!] ?? promised[1];
    return notice(
      `${site} has this book (${promised[1] === 'gutenberg' ? '#' : ''}${promised[2]}); the library has not shelved it yet. `,
      searchLink('Look for another edition among the library’s books'),
      '.',
      workLink,
    );
  }
  const year = cardYear(text);
  const line = publicDomainLine(help?.now);
  if (year !== null && year > line) {
    return notice(
      `This page describes a book, not the book itself. Published ${year}, it is still in copyright in the United States, so no free text of it exists; the card is all the library can hold. `,
      searchLink('Look for an older edition among the library’s books'),
      '.',
      workLink,
    );
  }
  const status = h('span', { class: 'catalog-lookup', 'aria-live': 'polite' });
  const el = notice(
    'This page describes a book; it is not the book. ',
    year !== null ? `Published ${year}, it is in the public domain. ` : null,
    status,
    workLink,
  );
  const fallback = () =>
    status.replaceChildren(searchLink(`Look for “${title}” among the library’s books`), '.');
  if (!help?.lookup) {
    fallback();
    return el;
  }
  // The lookup is a search (a second or two): the reader can act at once instead of waiting on a
  // placeholder (field exam 2026-10-14).
  status.replaceChildren(
    'Checking the library’s books for a copy… ',
    searchLink('search them yourself'),
    '.',
  );
  const key = titleKey(title);
  help
    .lookup(title)
    .then((hits) => {
      const match = bestBookMatch(hits, key);
      if (match) {
        status.replaceChildren(
          'The library holds it: ',
          wspLink(match.address, h('b', null, `Read ${match.title}`)),
          ` (${match.source_name}).`,
        );
      } else {
        status.replaceChildren(
          year !== null
            ? 'No transcription of it is in the library yet. '
            : 'The library has no book by this title. ',
          searchLink('Search the library’s books'),
          '.',
        );
      }
    })
    .catch(fallback);
  return el;
}

/**
 * Page kinds rewritten before the sanitiser drops their classes: a question page (Stack Exchange) as
 * a thread, a numbered comic as a comic, a man page with a sections box. A cheap string test first: parsing every 1-2 MB article a
 * second time only to find it is neither cost a full parse per page (review 2026-10-14).
 */
function rewritePage(html: string, source: string): string {
  const thread = html.includes('data-questionid') || html.includes('id="question"');
  const comic = looksLikeComic(html);
  const man = looksLikeManPage(html);
  const planet = looksLikePlanetMath(html);
  if (!thread && !comic && !man && !planet) return html;
  const parsed = new DOMParser().parseFromString(html, 'text/html');
  let changed = false;
  if (thread && isThread(parsed.body)) {
    threadView(parsed.body, source);
    changed = true;
  }
  if (comic && isComic(parsed.body)) {
    comicView(parsed.body, source);
    changed = true;
  }
  if (man && isManPage(parsed.body) && manPageView(parsed.body) > 0) changed = true;
  if (planet && planetMathView(parsed.body) > 0) changed = true;
  return changed ? parsed.body.innerHTML : html;
}

/** Below this many characters a page's own text is a stub: its other copy is the place to read it. */
export const STUB_TEXT_CHARS = 1500;

/**
 * Other copies of the same work (the API's `copies`): the illustrated edition of a Gutenberg text, the
 * complete Wikisource rendering of an empty transclusion. A page with almost no text of its own says
 * so first (2026-10-14 field exam: readers landed on the stub and never saw the complete copy).
 */
export function copiesNotice(
  doc: Pick<DocumentResponse, 'copies' | 'text' | 'html'>,
  names: ReadonlyMap<string, string>,
): HTMLElement | null {
  const copies = doc.copies ?? [];
  if (copies.length === 0) return null;
  const stub = !doc.html && (doc.text ?? '').trim().length < STUB_TEXT_CHARS;
  const items = copies.map((c, i) => [
    i ? ' · ' : null,
    wspLink(c.address, h('b', null, names.get(c.source_id) ?? c.source_id)),
    ` (${c.why})`,
  ]);
  return h(
    'div',
    { class: 'notice copies-notice', role: 'note' },
    stub ? 'This page has little text here. Read it in ' : 'Also in the library: ',
    ...items,
    '.',
  );
}

/** A web archive's placeholder for an embedded player it did not capture (`youtube.fuzzy.replayweb.page/embed/…`): the page reads "YouTube" and nothing else (field exam 2026-10-14). Same rule as wsp_core.listing.REPLAY_STUB. */
export const REPLAY_STUB = /(?:^|\/)[^/]*\.fuzzy\.replayweb\.page\//;

export function replayStubNotice(doc: Pick<DocumentResponse, 'native_id'>): HTMLElement | null {
  if (!REPLAY_STUB.test(doc.native_id)) return null;
  return h(
    'div',
    { class: 'notice replay-stub-notice', role: 'note' },
    h('b', null, 'Not captured. '),
    'This was an embedded video player on the original site. The archive kept only its frame; the video itself is not in the library.',
  );
}

/** A question pool reads better as a drill: offer the Exam Practice Hall. */
export function practiceNotice(
  doc: Pick<DocumentResponse, 'source_id' | 'native_id' | 'title'>,
): HTMLElement | null {
  if (doc.source_id !== POOL_SOURCE || /diagrams?/i.test(doc.title)) return null;
  return h(
    'div',
    { class: 'notice practice-notice', role: 'note' },
    h('b', null, 'Question pool. '),
    wspLink(makeAddress('practice', doc.native_id), 'Practice it in the Exam Practice Hall'),
    ': one question at a time with the answer shown, or a mock exam drawn like the real one.',
  );
}

const MAX_LISTED_LINKS = 60;

/**
 * The outbound links of a plain-text document (a Gutenberg book's illustrated edition, a PubMed
 * record's DOI and MeSH links). HTML pages carry theirs in the page; text has nowhere else to show them.
 * Pages the library holds come first; the rest are shown as broken links (not fetched, offline).
 */
export function linksList(
  doc: Pick<DocumentResponse, 'format' | 'html' | 'links'>,
): HTMLElement | null {
  if ((doc.format === 'html' && doc.html) || doc.links.length === 0) return null;
  const held = doc.links.filter((l) => l.target);
  const elsewhere = doc.links.filter((l) => !l.target);
  const items = [...held, ...elsewhere].slice(0, MAX_LISTED_LINKS).map((l) => {
    if (l.target) {
      const addr = docIdToAddress(l.target);
      const m = /^wsp:([^/]+)\/(.+)$/.exec(l.target);
      const label = m ? m[2]!.replace(/_/g, ' ') : l.target;
      return h(
        'li',
        null,
        wspLink(addr, label),
        m ? h('span', { class: 'fineprint' }, ` (${m[1]})`) : null,
      );
    }
    const addr = makeAddress('offline', '', { url: l.href });
    return h(
      'li',
      null,
      h(
        'a',
        {
          href: `#${addr}`,
          dataset: { wsp: addr },
          class: 'broken-link',
          title: `Not in library: ${l.href}`,
        },
        l.href,
      ),
    );
  });
  const more = doc.links.length - items.length;
  return h(
    'section',
    { class: 'doc-links', 'aria-label': 'Links' },
    h('h2', null, held.length ? 'Also in the library' : 'Links'),
    h('ul', null, ...items),
    more > 0 ? h('p', { class: 'fineprint' }, `…and ${more} more.`) : null,
  );
}

function webring(source: SourceInfo | undefined, everything: SourceInfo[]): HTMLElement | null {
  // Only sites with something to read: a planned source is never a neighbour on the ring.
  const all = everything.filter((s) => s.readable || s.id === source?.id);
  if (!source || all.length === 0) return null;
  const i = all.findIndex((s) => s.id === source.id);
  const prev = all[(i - 1 + all.length) % all.length]!;
  const next = all[(i + 1) % all.length]!;
  const related = source.related
    .map((id) => all.find((s) => s.id === id))
    .filter((s): s is SourceInfo => !!s);
  return h(
    'nav',
    { class: 'webring', 'aria-label': 'Webring' },
    h('span', { class: 'webring-title' }, 'Whisipedia Ring:'),
    ' [ ',
    wspLink(makeAddress(prev.id), `« ${prev.name}`),
    ' | ',
    wspLink(makeAddress('random', '', { source: source.id }), 'Random page here'),
    ' | ',
    wspLink(makeAddress('random'), 'Random site'),
    ' | ',
    wspLink(makeAddress(next.id), `${next.name} »`),
    ' ]',
    related.length
      ? h(
          'span',
          { class: 'webring-related' },
          ' Related: ',
          ...related.flatMap((r, k) => [k ? ', ' : '', wspLink(makeAddress(r.id), r.name)]),
        )
      : null,
  );
}

/**
 * Reader warning for historical, superseded or rescinded editions (PLAN D13):
 * shown above the text, never instead of it. The server decides the wording.
 */
export function documentWarning(
  doc: Pick<DocumentResponse, 'banner' | 'currency'>,
): HTMLElement | null {
  if (!doc.banner) return null;
  return h(
    'div',
    { class: `doc-warning doc-warning-${doc.currency ?? 'note'}`, role: 'note' },
    h('span', { class: 'doc-warning-icon', 'aria-hidden': 'true' }, '⚠'),
    ' ',
    h('strong', null, 'Notice: '),
    doc.banner,
  );
}

/** "PDF document", "TIFF image"…: what the stored file is, in words. */
function fileKind(media: string, name: string): string {
  if (media === 'application/pdf') return 'PDF document';
  if (media.startsWith('image/')) return `${media.slice(6).toUpperCase()} image`;
  const ext = name.includes('.') ? name.slice(name.lastIndexOf('.') + 1).toUpperCase() : '';
  return ext ? `${ext} file` : 'file';
}

/**
 * The stored original a collection document was refined from (a PDF, a
 * scanned map, an archive): shown above the extracted text, so a map is a
 * picture and a handbook keeps its figures. PDFs open in the browser's own
 * viewer; map scans show a server-made JPEG preview.
 */
/** What a media original needs from the app: the viewer's skin and playback memory. */
export interface MediaContext {
  app: Pick<AppServices, 'storage' | 'theme'>;
  docId: string;
  address: string;
}

/**
 * A PDF in a maps source is a map sheet when it has almost no text of its own: a scanned USGS sheet
 * carries only the library's catalog line (about 180 characters), while the NOAA Coast Pilots in the
 * same kind of source run to two million (measured 2026-10-14). Sheets show as a picture; anything
 * with more text than this opens in the viewer like every other PDF.
 */
export const MAP_SHEET_MAX_TEXT = 2000;

export function originalPanel(
  doc: Pick<DocumentResponse, 'original' | 'title' | 'kind'> &
    Partial<Pick<DocumentResponse, 'text'>>,
  urlOf: (path: string) => string = (path) => path,
  media?: MediaContext,
): HTMLElement | null {
  const o = doc.original;
  if (!o) return null;
  const url = urlOf(o.url);
  const playable = mediaKindOf(o.media_type, o.name);
  if (playable && media) return mediaPanel(doc, o, url, playable, media);
  const kind = fileKind(o.media_type, o.name);
  const links = h(
    'p',
    { class: 'original-links' },
    h('b', null, 'Original file: '),
    h('code', null, o.name),
    ` (${kind}) · `,
    o.inline || o.preview_url
      ? [h('a', { href: url, target: '_blank', rel: 'noopener' }, 'Open in a new window'), ' · ']
      : null,
    h('a', { href: url, download: o.name }, 'Download'),
  );
  // A map is a picture: show the rendered sheet (it opens the full file); other PDFs read in the viewer.
  const pdf = o.media_type === 'application/pdf';
  const sheet = doc.kind === 'maps' && (doc.text ?? '').length <= MAP_SHEET_MAX_TEXT;
  const picture = o.preview_url && (!pdf || sheet);
  let view: HTMLElement | null = null;
  if (picture && o.preview_url) {
    view = h(
      'a',
      { class: 'original-preview', href: url, target: '_blank', rel: 'noopener' },
      h('img', { src: urlOf(o.preview_url), alt: `Preview of ${doc.title}`, loading: 'lazy' }),
    );
  } else if (o.media_type === 'application/pdf') {
    view = h('iframe', {
      class: 'original-frame',
      src: url,
      title: `${doc.title} (${kind})`,
      loading: 'lazy',
    });
  }
  return h(
    'section',
    { class: 'original', 'aria-label': 'Original file' },
    links,
    view,
    view
      ? null
      : h(
          'p',
          { class: 'fineprint' },
          'Your browser cannot show this kind of file here; download it to open it.',
        ),
    h('h2', { class: 'original-text-heading' }, 'Text extracted from the file'),
  );
}

/** A film or a recording: the Screening Room's player, then the download and the text as usual. */
function mediaPanel(
  doc: Pick<DocumentResponse, 'title'>,
  o: NonNullable<DocumentResponse['original']>,
  url: string,
  kind: 'video' | 'audio',
  media: MediaContext,
): HTMLElement {
  const { storage, theme } = media.app;
  const make = kind === 'video' ? videoPlayer : audioDeck;
  const player = make({
    src: url,
    title: doc.title,
    mediaType: o.media_type,
    fileName: o.name,
    startAt: resumeAt(storage, media.docId) ?? undefined,
    skin: readSkin(storage, theme.current.theme),
    onSkin: (skin) => writeSkin(storage, skin),
    onProgress: (at, duration) =>
      remember(storage, {
        docId: media.docId,
        address: media.address,
        title: doc.title,
        kind,
        at,
        duration,
      }),
    downloadHref: url,
  });
  return h(
    'section',
    { class: 'original original-media', 'aria-label': kind === 'video' ? 'Film' : 'Recording' },
    player.el,
    h(
      'p',
      { class: 'original-links' },
      h('b', null, 'Original file: '),
      h('code', null, o.name),
      ` (${formatLabel(o.media_type, o.name)} ${kind}) · `,
      h('a', { href: url, download: o.name }, 'Download'),
      ' · ',
      wspLink(makeAddress('screening-room'), 'Screening Room'),
    ),
    h('h2', { class: 'original-text-heading' }, 'Text that came with the file'),
  );
}

function siteBanner(doc: DocumentResponse): HTMLElement {
  return h(
    'div',
    { class: `site-banner site-${doc.kind}` },
    wspLink(makeAddress(doc.source_id), h('span', { class: 'site-name' }, doc.source_name)),
    h(
      'span',
      { class: 'site-meta' },
      badge(KIND_LABELS[doc.kind], 'badge-kind'),
      ' ',
      licenseBadge(doc.license),
      ' ',
      asOf(doc.timestamp),
    ),
  );
}

function timeMachine(ctx: PageContext, doc: DocumentResponse, route: DocRoute): HTMLElement {
  const list = h('div', { class: 'tm-list' }, 'Loading versions…');
  const details = h(
    'details',
    { class: 'time-machine', open: route.at !== undefined },
    h('summary', null, 'Time Machine: see this page as of an earlier date'),
    list,
  );
  let loaded = false;
  const load = () => {
    if (loaded) return;
    loaded = true;
    ctx.app.api
      .history(doc.doc_id, ctx.signal)
      .then((hist) => {
        const here = makeAddress(doc.source_id, doc.native_id);
        list.replaceChildren(
          h(
            'table',
            { class: 'tm-table' },
            h(
              'thead',
              null,
              h(
                'tr',
                null,
                h('th', null, 'As of'),
                h('th', null, 'Revision'),
                h('th', null, 'Change'),
                h('th', null, ''),
              ),
            ),
            h(
              'tbody',
              null,
              ...[...hist.items].reverse().map((v, i) => {
                const at = v.generation_id;
                const label = v.timestamp ? formatDate(v.timestamp) : at;
                return h(
                  'tr',
                  { class: v.content_hash === doc.content_hash ? 'tm-current' : '' },
                  h(
                    'td',
                    null,
                    i === 0
                      ? wspLink(here, label)
                      : wspLink(makeAddress(doc.source_id, doc.native_id, { at }), label),
                  ),
                  h('td', null, h('code', null, v.revision ?? at)),
                  h('td', null, v.kind === 'added' ? 'added' : 'modified'),
                  h(
                    'td',
                    null,
                    i === 0
                      ? '(current)'
                      : wspLink(
                          makeAddress(doc.source_id, doc.native_id, { at, diff: 1 }),
                          'diff vs current',
                        ),
                  ),
                );
              }),
            ),
          ),
        );
      })
      .catch((err: unknown) => {
        list.replaceChildren(
          err instanceof ApiRequestError && err.isBusy
            ? 'The library is busy; reload the page to see the versions.'
            : err instanceof ApiRequestError && err.isUnavailable
              ? 'This source keeps no history.'
              : `Could not load history: ${String(err)}`,
        );
      });
  };
  if (!doc.has_history && route.at === undefined) {
    list.replaceChildren('This page has a single version in the library.');
    loaded = true;
  }
  details.addEventListener('toggle', () => {
    if (details.open) load();
  });
  if (details.open) load();
  return details;
}

function renderDiff(oldDoc: DocumentResponse, cur: DocumentResponse): HTMLElement {
  const ops = diffLines(oldDoc.text ?? '', cur.text ?? '');
  if (!ops) return h('p', { class: 'notice' }, 'This page is too large to compare in the browser.');
  const st = diffStats(ops);
  return h(
    'div',
    { class: 'diff' },
    h(
      'p',
      { class: 'diff-stats' },
      `${st.added} line(s) added, ${st.removed} line(s) removed between ${asOfText(oldDoc)} and ${asOfText(cur)}.`,
    ),
    h(
      'pre',
      { class: 'diff-body' },
      ...ops.map((o) =>
        o.op === 'eq'
          ? h('span', { class: 'diff-eq' }, `  ${o.text}\n`)
          : o.op === 'add'
            ? h('ins', { class: 'diff-add' }, `+ ${o.text}\n`)
            : h('del', { class: 'diff-del' }, `- ${o.text}\n`),
      ),
    ),
  );
}

function footer(app: AppServices, doc: DocumentResponse, address: string): HTMLElement {
  const bookmark = h(
    'button',
    { type: 'button', class: 'btn btn-small' },
    app.hotlist.has(address) ? 'In Hotlist ✓' : 'Add to Hotlist',
  );
  bookmark.addEventListener('click', () => {
    app.hotlist.add(address, doc.title);
    bookmark.textContent = 'In Hotlist ✓';
    app.status(`Added “${doc.title}” to the Hotlist.`);
  });
  const vsd = h(
    'button',
    { type: 'button', class: 'btn btn-small', 'aria-keyshortcuts': 'Control+U' },
    'View Source Data',
  );
  vsd.addEventListener('click', () => app.openSourceData(doc));
  return h(
    'footer',
    { class: 'doc-footer' },
    h('hr'),
    h(
      'p',
      null,
      'From ',
      wspLink(makeAddress(doc.source_id), doc.source_name),
      ` · revision ${doc.revision ?? doc.generation_id ?? 'unknown'} · `,
      asOf(doc.timestamp),
      ' · license ',
      licenseBadge(doc.license),
    ),
    doc.url_native
      ? h(
          'p',
          { class: 'fineprint' },
          'Original address (not fetched, offline): ',
          h('code', null, doc.url_native),
        )
      : null,
    h('p', { class: 'doc-actions' }, vsd, ' ', bookmark),
  );
}

export async function render(ctx: PageContext): Promise<PageResult> {
  const route = ctx.route as DocRoute;
  let doc: DocumentResponse;
  try {
    doc = await loadDoc(ctx, route.docId, route.at, Boolean(route.at && route.diff));
  } catch (err) {
    if (err instanceof ApiRequestError && err.status === 404) {
      const words = route.nativeId.replace(/[_/]+/g, ' ');
      // A link led here but the library lacks the page: tell the gap miner (PLAN §5.3). Not for an
      // old version (`?at=`): the page exists, only that version does not.
      if (!route.at)
        void ctx.app.api.reportGap({ href: ctx.address, from_doc_id: ctx.app.lastDocId() });
      const site = (await ctx.app.sources()).find((s) => s.id === route.source);
      return {
        status: 'error',
        title: '404 Not Found',
        body: errorPage({
          title: 'Not Found',
          heading: 'Not Found',
          message: route.at
            ? `The library holds no version of this page as of ${route.at}.`
            : `The requested page was not found in the library. It may not have been mirrored yet, or it was removed upstream.`,
          address: ctx.address,
          suggestions: [
            route.at
              ? wspLink(makeAddress(route.source, route.nativeId), 'View the current page')
              : null,
            wspLink(searchAddress(words), `Search the library for “${words}”`),
            site ? wspLink(makeAddress(site.id), `Go to ${site.name}`) : null,
            wspLink(makeAddress('home'), 'Go to the Start Page'),
          ].filter((x): x is HTMLAnchorElement => x !== null),
        }),
      };
    }
    throw err;
  }
  ctx.app.setCurrentDocument(doc);
  const sources = await ctx.app.sources();
  const source = sources.find((s) => s.id === doc.source_id);

  let content: HTMLElement;
  let broken = 0;
  let tmBanner: HTMLElement | null = null;
  const here = makeAddress(doc.source_id, doc.native_id);
  if (route.at) {
    tmBanner = h(
      'p',
      { class: 'notice notice-tm', role: 'note' },
      h('b', null, 'Time Machine: '),
      `you are viewing this page as of ${asOfText(doc)} (generation ${doc.generation_id ?? route.at}). `,
      wspLink(here, 'View the current page'),
      route.diff
        ? null
        : [
            ' · ',
            wspLink(
              makeAddress(doc.source_id, doc.native_id, { at: route.at, diff: 1 }),
              'Show changes since',
            ),
          ],
    );
  }
  if (route.at && route.diff) {
    const current = await loadDoc(ctx, route.docId, undefined, true);
    content = renderDiff(doc, current);
  } else {
    const r = renderBody(doc, {
      app: ctx.app,
      chapter: route.chapter,
      address: here,
      signal: ctx.signal,
    });
    content = r.node;
    broken = r.broken;
  }

  const body = h(
    'article',
    { class: `page page-document doc-kind-${doc.kind}`, 'aria-labelledby': 'doc-title' },
    siteBanner(doc),
    webring(source, sources),
    tmBanner,
    documentWarning(doc),
    catalogNotice(doc, source, {
      bookSources: new Set(sources.filter((s) => s.kind === 'books').map((s) => s.id)),
      lookup: (q) => lookupBooks(ctx, q),
    }),
    practiceNotice(doc),
    replayStubNotice(doc),
    copiesNotice(doc, new Map(sources.map((s) => [s.id, s.name]))),
    doc.format === 'html' && doc.html && looksLikeComic(doc.html) && !route.at
      ? comicBar(doc.source_id, doc.native_id, (a) => ctx.app.navigate(a))
      : null,
    // Original pages (ZIM HTML) carry their own <h1>; don't print the title twice.
    content instanceof Element && content.querySelector('h1') ? null : pageHeading(doc.title),
    route.at
      ? null
      : originalPanel(doc, (path) => ctx.app.api.url(path), {
          app: ctx.app,
          docId: doc.doc_id,
          address: here,
        }),
    content,
    broken
      ? h(
          'p',
          { class: 'fineprint broken-note' },
          `${broken} link(s) on this page go to websites the library does not hold offline. They are marked, and opening one explains what is missing.`,
        )
      : null,
    route.diff ? null : linksList(doc),
    timeMachine(ctx, doc, route),
    footer(ctx.app, doc, here),
  );
  body.querySelector('h1')?.setAttribute('id', 'doc-title');
  return {
    title: route.at ? `${doc.title} (as of ${asOfText(doc)})` : doc.title,
    body,
    scrollTo: route.fragment
      ? `doc-${route.fragment}`
      : body.querySelector(`#${RESUME_ID}`)
        ? RESUME_ID
        : undefined,
  };
}
