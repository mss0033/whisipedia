/**
 * wsp://home — the Start Page, a late-90s portal: featured article, On this
 * day, What's New, random-page buttons and a link to every site.
 */
import { ApiRequestError } from '../api/client.ts';
import type { Change, Portal, SourceInfo } from '../api/types.ts';
import { attachSuggest } from '../chrome/suggest.ts';
import { makeAddress, searchAddress } from '../core/address.ts';
import { unfinishedBooks } from '../core/book.ts';
import { h } from '../core/dom.ts';
import { formatDate, formatNumber, odometer } from '../core/format.ts';
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
  const portal = await optional<Portal>(app.api.portal(ctx.signal));
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

  const randomButtons = h(
    'p',
    { class: 'random-buttons' },
    wspLink(makeAddress('random'), h('span', { class: 'btn-like' }, 'Random page!')),
    ' ',
    ...sources
      .filter((s) => (portal ? portal.random_sources.includes(s.id) : (s.doc_count ?? 0) > 0))
      .slice(0, 4)
      .flatMap((s) => [
        wspLink(
          makeAddress('random', '', { source: s.id }),
          h('span', { class: 'btn-like' }, `Random ${s.name.split(' ')[0]}`),
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
      wspLink(makeAddress('practice'), 'Practice Hall'),
      ' | ',
      wspLink(makeAddress('screening-room'), 'Screening Room'),
      ' | ',
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
        h('li', null, wspLink(b.address, b.title), ` · chapter ${b.chapter + 1} of ${b.chapters}`),
      ),
    ),
  );
}
