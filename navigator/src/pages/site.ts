/** wsp://<source> — a source's own "site" front page. */
import { makeAddress, searchAddress } from '../core/address.ts';
import { h } from '../core/dom.ts';
import { formatBytes, formatNumber } from '../core/format.ts';
import {
  asOf,
  badge,
  docLink,
  errorPage,
  KIND_LABELS,
  licenseBadge,
  pageHeading,
  searchForm,
  statusLamp,
  wspLink,
} from '../ui/components.ts';
import type { PageContext, PageResult } from './types.ts';

export async function render(ctx: PageContext): Promise<PageResult> {
  const id = (ctx.route as { source: string }).source;
  const sources = await ctx.app.sources();
  const src = sources.find((s) => s.id === id);
  if (!src) {
    return {
      title: 'Unable to locate the server',
      body: errorPage({
        title: 'Unknown site',
        heading: 'Unable to locate the server',
        message: `The library has no site called “${id}”. The address may be mistyped, or that source has not been added to the library.`,
        address: ctx.address,
        suggestions: [
          wspLink(searchAddress(id), `Search the library for “${id}”`),
          wspLink(makeAddress('directory'), 'Browse the directory'),
          wspLink(makeAddress('home'), 'Go to the Start Page'),
        ],
      }),
    };
  }
  const { form } = searchForm({
    label: `Search ${src.name}:`,
    onSubmit: (q) => ctx.app.navigate(searchAddress(q, { source: src.id })),
  });
  const held = src.current_generation !== null;
  const related = src.related
    .map((r) => sources.find((s) => s.id === r))
    .filter((s) => s !== undefined);

  const body = h(
    'div',
    { class: `page page-site site-${src.kind}` },
    h(
      'div',
      { class: `site-banner site-${src.kind}` },
      h('span', { class: 'site-name' }, src.name),
      h(
        'span',
        { class: 'site-meta' },
        badge(KIND_LABELS[src.kind], 'badge-kind'),
        ' ',
        licenseBadge(src.license),
      ),
    ),
    pageHeading(`Welcome to ${src.name}`),
    src.description ? h('p', { class: 'site-desc' }, src.description) : null,
    held
      ? null
      : h(
          'p',
          { class: 'site-notice' },
          'This site is planned for the library but has not been downloaded yet, so there is nothing to read here for now.',
        ),
    held ? form : null,
    h(
      'table',
      { class: 'kv' },
      h(
        'tbody',
        null,
        h(
          'tr',
          null,
          h('th', { scope: 'row' }, 'Publisher'),
          h('td', null, src.publisher ?? 'unknown'),
        ),
        h(
          'tr',
          null,
          h('th', { scope: 'row' }, 'Documents'),
          h('td', null, src.doc_count === null ? 'unknown' : formatNumber(src.doc_count)),
        ),
        h(
          'tr',
          null,
          h('th', { scope: 'row' }, 'Size on disk'),
          h('td', null, src.bytes === null ? 'unknown' : formatBytes(src.bytes)),
        ),
        h('tr', null, h('th', { scope: 'row' }, 'Snapshot'), h('td', null, asOf(src.as_of))),
        h('tr', null, h('th', { scope: 'row' }, 'Tier'), h('td', null, src.tier)),
        h('tr', null, h('th', { scope: 'row' }, 'Health'), h('td', null, statusLamp(src.health))),
      ),
    ),
    h(
      'p',
      { class: 'site-actions' },
      src.home_doc_id
        ? [docLink(src.home_doc_id, h('span', { class: 'btn-like' }, 'Enter site')), ' ']
        : null,
      src.doc_count || src.home_doc_id
        ? wspLink(
            makeAddress('random', '', { source: src.id }),
            h('span', { class: 'btn-like' }, 'Random page'),
          )
        : null,
    ),
    related.length
      ? h(
          'p',
          null,
          'Related sites: ',
          ...related.flatMap((r, i) => [i ? ' · ' : '', wspLink(makeAddress(r.id), r.name)]),
        )
      : null,
  );
  return { title: src.name, body };
}
