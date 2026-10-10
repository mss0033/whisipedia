/**
 * wsp://directory/<path> — Yahoo!-'97-style hand-curated directory. Uses the
 * (proposed) /v1/directory endpoint; falls back to a tree built from
 * /v1/sources so the page always works.
 */
import { ApiRequestError } from '../api/client.ts';
import type { DirectoryResponse, SourceInfo } from '../api/types.ts';
import { makeAddress, searchAddress } from '../core/address.ts';
import { h } from '../core/dom.ts';
import { formatNumber } from '../core/format.ts';
import { badge, docLink, errorPage, KIND_LABELS, searchForm, wspLink } from '../ui/components.ts';
import type { PageContext, PageResult } from './types.ts';

/** Fallback directory: Top › <kind> › sources. */
export function directoryFromSources(
  path: string,
  sources: SourceInfo[],
): DirectoryResponse | null {
  const kinds = [...new Set(sources.map((s) => s.kind))];
  if (!path) {
    return {
      path: '',
      name: 'Top',
      trail: [],
      children: kinds
        .map((k) => ({
          path: k,
          name: KIND_LABELS[k],
          count: sources.filter((s) => s.kind === k).length,
        }))
        .sort((a, b) => a.name.localeCompare(b.name)),
      entries: [],
    };
  }
  const kind = kinds.find((k) => k === path);
  if (!kind) return null;
  return {
    path,
    name: KIND_LABELS[kind],
    trail: [{ path, name: KIND_LABELS[kind] }],
    children: [],
    entries: sources
      .filter((s) => s.kind === kind)
      .map((s) => ({
        doc_id: s.home_doc_id ?? `wsp:${s.id}/`,
        title: s.name,
        description: s.description,
        source_id: s.id,
      })),
  };
}

export async function render(ctx: PageContext): Promise<PageResult> {
  const path = (ctx.route as { path: string }).path.replace(/^\/+|\/+$/g, '');
  const sources = await ctx.app.sources();
  let dir: DirectoryResponse | null;
  try {
    dir = await ctx.app.api.directory(path, ctx.signal);
  } catch (err) {
    // 404 is either "no such category" or "endpoint not implemented"; the
    // fallback tree answers both (unknown paths resolve to null).
    if (!(err instanceof ApiRequestError) || !err.isUnavailable) throw err;
    dir = directoryFromSources(path, sources);
  }
  if (!dir) {
    return {
      title: 'Category not found',
      body: errorPage({
        title: 'Not Found',
        heading: 'Category Not Found',
        message: 'There is no such category in the directory.',
        address: ctx.address,
        suggestions: [wspLink(makeAddress('directory'), 'Back to the top of the directory')],
      }),
    };
  }

  const { form } = searchForm({
    label: 'Search:',
    onSubmit: (q) => ctx.app.navigate(searchAddress(q)),
  });

  const half = Math.ceil(dir.children.length / 2);
  const col = (items: DirectoryResponse['children']) =>
    h(
      'ul',
      { class: 'dir-col' },
      ...items.map((c) =>
        h(
          'li',
          null,
          wspLink(makeAddress('directory', c.path), h('b', null, c.name)),
          c.crosslink
            ? h('span', { class: 'crosslink', title: 'Listed in another category' }, '@')
            : null,
          ` (${formatNumber(c.count)})`,
        ),
      ),
    );

  const sourceName = (id: string) => sources.find((s) => s.id === id)?.name ?? id;

  const body = h(
    'div',
    { class: 'page page-directory' },
    h(
      'div',
      { class: 'dir-header' },
      h(
        'h1',
        { tabindex: '-1', class: 'dir-logo' },
        'Whisi',
        h('span', { class: 'dir-logo-bang' }, 'Dir!'),
      ),
      form,
    ),
    h(
      'nav',
      { class: 'breadcrumbs', 'aria-label': 'Category path' },
      dir.path ? wspLink(makeAddress('directory'), 'Top') : h('b', null, 'Top'),
      ...dir.trail.flatMap((t, i) => [
        ' › ',
        i === dir.trail.length - 1
          ? h('b', { 'aria-current': 'page' }, t.name)
          : wspLink(makeAddress('directory', t.path), t.name),
      ]),
    ),
    h('hr'),
    dir.children.length
      ? h(
          'div',
          { class: 'dir-children' },
          col(dir.children.slice(0, half)),
          col(dir.children.slice(half)),
        )
      : null,
    dir.entries.length
      ? [
          dir.children.length ? h('hr') : null,
          h(
            'ul',
            { class: 'dir-entries' },
            ...dir.entries.map((e) =>
              h(
                'li',
                null,
                e.doc_id.endsWith('/')
                  ? wspLink(makeAddress(e.source_id), e.title)
                  : docLink(e.doc_id, e.title),
                e.description ? ` - ${e.description}` : '',
                ' ',
                badge(sourceName(e.source_id), 'badge-source'),
              ),
            ),
          ),
        ]
      : null,
    !dir.children.length && !dir.entries.length
      ? h('p', null, h('i', null, 'This category is under construction.'))
      : null,
    h('hr'),
    h(
      'p',
      { class: 'fineprint' },
      'Categories built from Wikidata classes, Library of Congress subjects and Gutenberg LCC. ',
      h('span', { class: 'crosslink' }, '@'),
      ' = listed elsewhere too.',
    ),
  );
  return { title: dir.path ? `Directory: ${dir.name}` : 'Whisipedia Directory', body };
}
