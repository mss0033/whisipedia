/**
 * wsp://home — the Start Page, a late-90s portal: featured article, On this
 * day, What's New, random-page buttons and a link to every site.
 */
import { ApiRequestError } from '../api/client.ts';
import type { Change, Portal, SourceInfo } from '../api/types.ts';
import { attachSuggest } from '../chrome/suggest.ts';
import { makeAddress, searchAddress } from '../core/address.ts';
import { unfinishedBooks } from '../core/book.ts';
import { h, uiLanguage } from '../core/dom.ts';
import { formatDate, formatNumber, localIsoDay, odometer } from '../core/format.ts';
import { PLACES } from '../core/places.ts';
import {
  docLink,
  ENGINE_NAME,
  engineLogo,
  KIND_LABELS,
  newBadge,
  searchForm,
  sectionBox,
  wspLink,
} from '../ui/components.ts';
import type { AppServices, PageContext, PageResult } from './types.ts';

async function optional<T>(p: Promise<T>): Promise<T | null> {
  try {
    return await p;
  } catch (err) {
    if (err instanceof ApiRequestError) return null;
    throw err;
  }
}

function sitesList(sources: SourceInfo[]): HTMLElement {
  const byKind = new Map<string, SourceInfo[]>();
  for (const s of sources) {
    const k = KIND_LABELS[s.kind] ?? s.kind;
    byKind.set(k, [...(byKind.get(k) ?? []), s]);
  }
  return h(
    'div',
    { class: 'sites-list' },
    h(
      'div',
      { class: 'sites-group sites-places' },
      h('h3', null, 'Places'),
      h('ul', null, ...PLACES.map((p) => h('li', null, wspLink(p.address, p.label)))),
    ),
    // Alphabetical by kind, with the catch-all "Other" last.
    ...[...byKind.entries()]
      .sort(
        ([a], [b]) =>
          Number(a === KIND_LABELS.other) - Number(b === KIND_LABELS.other) || a.localeCompare(b),
      )
      .map(([kind, list]) =>
        h(
          'div',
          { class: 'sites-group' },
          h('h3', null, kind),
          h(
            'ul',
            null,
            ...list.map((s) =>
              h(
                'li',
                null,
                wspLink(makeAddress(s.id), s.name),
                s.health !== 'ok'
                  ? h('span', { class: `lamp-inline lamp-${s.health}` }, ` (${s.health})`)
                  : null,
              ),
            ),
          ),
        ),
      ),
  );
}

/** Kinds of site worth a random page: reading matter, not data files, map tiles or media. */
const RANDOM_KINDS: ReadonlySet<string> = new Set([
  'encyclopedia',
  'books',
  'medicine',
  'news',
  'dictionary',
  'law',
  'qa',
  'papers',
]);
const RANDOM_MIN_DOCS = 50; // fewer is a handful of files (a model's code and papers), not a site
export const RANDOM_BUTTONS = 4;

/** A seeded PRNG in [0, 1): the same picks all day (mulberry32 over an FNV-1a hash of the seed). */
function seeded(seed: string): () => number {
  let a = 0x811c9dc5;
  for (let i = 0; i < seed.length; i++) a = Math.imul(a ^ seed.charCodeAt(i), 0x01000193);
  return () => {
    a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function shuffled<T>(items: readonly T[], rng: () => number): T[] {
  const out = [...items];
  for (let i = out.length - 1; i > 0; i--) {
    const j = Math.floor(rng() * (i + 1));
    [out[i], out[j]] = [out[j]!, out[i]!];
  }
  return out;
}

/**
 * The start page's random-page sites for `day`: reading sites with more than a handful of pages, one
 * of each kind before a second of any, in an order that changes daily. The buttons were always the
 * first four sites by id ("Random American", "Random US", "Random Army", "Random Aurora": a weather
 * model's files) (2026-11-05 deep dive).
 */
export function pickRandomSites(
  sources: readonly SourceInfo[],
  day: string,
  n = RANDOM_BUTTONS,
): SourceInfo[] {
  const reading = sources.filter(
    (s) => RANDOM_KINDS.has(s.kind) && (s.doc_count ?? 0) >= RANDOM_MIN_DOCS,
  );
  const rng = seeded(day);
  const byKind = new Map<string, SourceInfo[]>();
  for (const s of shuffled(reading.length ? reading : sources, rng)) {
    const list = byKind.get(s.kind);
    if (list) list.push(s);
    else byKind.set(s.kind, [s]);
  }
  const queues = shuffled([...byKind.values()], rng);
  const out: SourceInfo[] = [];
  for (let round = 0; out.length < n && queues.some((q) => q.length > round); round++) {
    for (const q of queues) {
      const s = q[round];
      if (s && out.length < n) out.push(s);
    }
  }
  return out;
}

/**
 * A site's name short enough for a button: before any " (", " - ", ":" or ",", and at most three
 * words ("Project Gutenberg", "MedlinePlus", "Wikipedia"); `name.split(' ')[0]` gave "Random US"
 * for the US Army Center of Military History.
 */
export function shortSiteName(name: string): string {
  const head = (name.replace(/^"+|"+$/g, '').split(/\s+\(|\s+[-–—]\s+|[:,;]\s/)[0] ?? '').trim();
  const words = head.split(/\s+/).filter(Boolean);
  if (words.length === 0) return name.trim();
  return words.length > 3 ? `${words.slice(0, 3).join(' ')}…` : words.join(' ');
}

function whatsNewList(changes: Change[]): HTMLElement {
  return h(
    'ul',
    { class: 'whatsnew-mini' },
    ...changes.slice(0, 8).map((c) =>
      h(
        'li',
        null,
        c.kind === 'removed'
          ? h('s', null, c.title ?? c.doc_id)
          : // A site entry has no doc_id (doc-collection sites have no landing page): follow its address.
            c.address
            ? wspLink(c.address, c.title ?? c.address)
            : docLink(c.doc_id, c.title ?? c.doc_id),
        ' ',
        c.kind === 'added'
          ? newBadge()
          : h('span', { class: 'updated' }, c.kind === 'removed' ? 'removed' : 'updated'),
      ),
    ),
  );
}

export async function render(ctx: PageContext): Promise<PageResult> {
  const { app } = ctx;
  // The reader's language and their own day: the featured article was Arabic one day in two and
  // "On This Day" was tomorrow's from 7 pm in New York (2026-11-05 deep dive).
  const today = localIsoDay();
  const portal = await optional<Portal>(
    app.api.portal(ctx.signal, { lang: uiLanguage(), date: today }),
  );
  // Older daemons have no /v1/portal: build the page from the sources and the change feed.
  const sources = portal?.sites ?? (await app.sources());
  const newest =
    portal?.newest ??
    (await optional(app.api.recentChanges(8, undefined, ctx.signal)))?.items ??
    [];

  const { form, input } = searchForm({
    big: false,
    lucky: true,
    label: `${ENGINE_NAME} the library:`,
    onSubmit: (q, lucky) => app.navigate(searchAddress(q, { lucky: lucky ? 1 : undefined })),
  });
  attachSuggest(input, app, {
    onPick: (s) => app.navigate(s.address ?? searchAddress(s.text)),
  });

  const totalDocs = sources.reduce((n, s) => n + (s.doc_count ?? 0), 0);
  const visits = app.history.list().find((e) => e.address === 'wsp://home')?.visits ?? 0;

  const randomable = new Set(portal?.random_sources ?? []);
  const randomButtons = h(
    'p',
    { class: 'random-buttons' },
    wspLink(makeAddress('random'), h('span', { class: 'btn-like' }, 'Random page!')),
    ' ',
    ...pickRandomSites(
      sources.filter((s) => (portal ? randomable.has(s.id) : (s.doc_count ?? 0) > 0)),
      portal?.date ?? today,
    ).flatMap((s) => [
      wspLink(
        makeAddress('random', '', { source: s.id }),
        h('span', { class: 'btn-like', title: s.name }, `Random ${shortSiteName(s.name)}`),
      ),
      ' ',
    ]),
  );

  const center = h(
    'div',
    { class: 'portal-main' },
    portal?.featured
      ? sectionBox(
          "Today's Featured Article",
          h(
            'p',
            null,
            h('b', null, docLink(portal.featured.doc_id, portal.featured.title)),
            ' — ',
            portal.featured.blurb,
          ),
          h('p', { class: 'more' }, docLink(portal.featured.doc_id, 'Read more…')),
        )
      : null,
    portal?.on_this_day.length
      ? sectionBox(
          `On This Day: ${formatDate(`${portal.date}T00:00:00Z`)}`,
          h(
            'ul',
            null,
            ...portal.on_this_day.map((e) =>
              h(
                'li',
                null,
                h('b', null, String(e.year)),
                ' — ',
                e.doc_id ? docLink(e.doc_id, e.text) : e.text,
              ),
            ),
          ),
        )
      : null,
    portal?.did_you_know.length
      ? sectionBox(
          'Did You Know…',
          h(
            'ul',
            null,
            ...portal.did_you_know.map((e) =>
              h('li', null, '… that ', e.doc_id ? docLink(e.doc_id, e.text) : e.text),
            ),
          ),
        )
      : null,
    sectionBox('Feeling Adventurous?', randomButtons),
    continueReading(app),
  );

  const body = h(
    'div',
    { class: 'page page-home' },
    h(
      'header',
      { class: 'portal-masthead' },
      h(
        'h1',
        { class: 'portal-title', tabindex: '-1' },
        'Whisipedia',
        h('span', { class: 'portal-sub' }, ' Start Page'),
      ),
      h(
        'p',
        { class: 'portal-tagline' },
        'Your library of the world’s open knowledge. Offline. Verified. Yours.',
      ),
    ),
    h('div', { class: 'portal-search' }, engineLogo('small'), form),
    h(
      'nav',
      { class: 'portal-quicklinks', 'aria-label': 'Quick links' },
      '[ ',
      wspLink(makeAddress('directory'), 'Directory'),
      ' | ',
      wspLink(makeAddress('search'), 'Search'),
      ' | ',
      wspLink(makeAddress('whatsnew'), "What's New!"),
      ' | ',
      wspLink(makeAddress('hotlist'), 'Hotlist'),
      ' | ',
      ...PLACES.flatMap((p) => [wspLink(p.address, p.label), ' | ']),
      wspLink(makeAddress('system'), 'System Monitor'),
      ' | ',
      wspLink(makeAddress('help'), 'Help'),
      ' ]',
    ),
    h(
      'div',
      { class: 'portal-grid' },
      h(
        'aside',
        { class: 'portal-left', 'aria-label': 'Sites in the library' },
        sectionBox('Sites in the Library', sitesList(sources)),
      ),
      center,
      h(
        'aside',
        { class: 'portal-right', 'aria-label': "What's New" },
        sectionBox(
          "What's New!",
          newest.length ? whatsNewList(newest) : h('p', null, 'No recent changes.'),
          h('p', { class: 'more' }, wspLink(makeAddress('whatsnew'), 'More new stuff…')),
        ),
        sectionBox(
          'Library Stats',
          h(
            'p',
            null,
            h('b', null, formatNumber(totalDocs)),
            ' documents in ',
            h('b', null, String(sources.length)),
            ' sites.',
          ),
          portal?.random
            ? h('p', null, 'Try: ', docLink(portal.random.doc_id, portal.random.title))
            : null,
        ),
      ),
    ),
    h('hr'),
    h(
      'footer',
      { class: 'portal-footer' },
      h(
        'p',
        null,
        'You are visitor number ',
        h('span', { class: 'odometer' }, odometer(visits)),
        ' to this page (on this computer).',
      ),
      h(
        'p',
        { class: 'fineprint' },
        'Best viewed with Whisipedia Navigator at 800×600 or larger. This page was not downloaded from anywhere.',
      ),
    ),
  );
  return { title: 'Whisipedia Start Page', body };
}

/** Books the viewer started and has not finished (the reader remembers the place in this browser). */
export function continueReading(app: Pick<AppServices, 'storage'>): HTMLElement | null {
  const books = unfinishedBooks(app.storage).slice(0, 4);
  if (books.length === 0) return null;
  return sectionBox(
    'Continue Reading',
    h(
      'ul',
      { class: 'continue-reading' },
      books.map((b) =>
        h(
          'li',
          null,
          wspLink(b.address, b.title),
          ` · ${b.where ? b.where[0]!.toLowerCase() + b.where.slice(1) : `chapter ${b.chapter + 1} of ${b.chapters}`}`,
        ),
      ),
    ),
  );
}
