/**
 * wsp://whatsnew — "What's New!" fed by the change feed: the newest changes
 * from /v1/changes/recent, then live updates from /v1/changes/stream (SSE,
 * with polling as the fallback) while the page is open.
 */
import { watchChanges } from '../api/changes.ts';
import type { Change, SourceInfo } from '../api/types.ts';
import { makeAddress } from '../core/address.ts';
import { h } from '../core/dom.ts';
import { docLink, newBadge, pageHeading, wspLink } from '../ui/components.ts';
import type { PageContext, PageResult } from './types.ts';

const OP_LABEL = {
  added: 'New',
  modified: 'Updated',
  removed: 'Removed',
  renamed: 'Moved',
} as const;
const PAGE = 30;
const MAX = 500;

/** Groups by generation (the library's update cycles), keeping the input order. */
export function groupByGeneration(changes: Change[]): Map<string, Change[]> {
  const out = new Map<string, Change[]>();
  for (const c of changes) out.set(c.generation_id, [...(out.get(c.generation_id) ?? []), c]);
  return out;
}

function item(c: Change, name: (id: string) => string): HTMLLIElement {
  const title = c.title ?? c.doc_id;
  return h(
    'li',
    { class: `wn-${c.kind}` },
    c.kind === 'added'
      ? newBadge()
      : h('span', { class: `wn-op wn-op-${c.kind}` }, OP_LABEL[c.kind]),
    ' ',
    c.kind === 'removed'
      ? h('s', { title: 'Removed upstream; kept in older generations' }, title)
      : docLink(c.doc_id, title),
    ' — ',
    wspLink(makeAddress(c.source_id), name(c.source_id)),
  );
}

function renderGroups(target: HTMLElement, changes: Change[], sources: SourceInfo[]): void {
  const name = (id: string) => sources.find((s) => s.id === id)?.name ?? id;
  target.replaceChildren();
  for (const [gen, list] of groupByGeneration(changes)) {
    target.append(
      h('h2', { class: 'wn-day' }, `Update ${gen}`),
      h(
        'ul',
        { class: 'wn-list', dataset: { generation: gen } },
        ...list.map((c) => item(c, name)),
      ),
    );
  }
}

export async function render(ctx: PageContext): Promise<PageResult> {
  const [sources, first] = await Promise.all([
    ctx.app.sources(),
    ctx.app.api.recentChanges(PAGE, undefined, ctx.signal),
  ]);
  let changes = [...first.items];
  let limit = PAGE;
  const list = h('div', { class: 'wn-groups' });
  const empty = h('p', { hidden: changes.length > 0 }, 'Nothing has changed yet.');
  const live = h('p', { class: 'fineprint', 'aria-live': 'polite' });
  renderGroups(list, changes, sources);

  const more = h(
    'button',
    { type: 'button', class: 'btn', hidden: changes.length < limit },
    'More changes…',
  );
  more.addEventListener('click', () => {
    more.disabled = true;
    limit = Math.min(MAX, limit * 2);
    ctx.app.api
      .recentChanges(limit, undefined, ctx.signal)
      .then((r) => {
        changes = [...r.items];
        renderGroups(list, changes, sources);
        more.hidden = changes.length < limit || limit >= MAX;
      })
      .catch((err: unknown) => ctx.app.status(`Could not load more changes: ${String(err)}`))
      .finally(() => {
        more.disabled = false;
      });
  });

  const watcher = watchChanges(ctx.app.api, first.cursor, (c) => {
    changes = [
      c,
      ...changes.filter((x) => !(x.doc_id === c.doc_id && x.generation_id === c.generation_id)),
    ];
    renderGroups(list, changes, sources);
    empty.hidden = true;
    live.textContent = `Just in: ${c.title ?? c.doc_id}`;
  });
  ctx.signal.addEventListener('abort', () => watcher.close(), { once: true });

  const body = h(
    'div',
    { class: 'page page-whatsnew' },
    pageHeading("What's New!"),
    h(
      'p',
      { class: 'wn-intro' },
      'The latest additions and updates to the library, straight from the change feed. ',
      h('span', { class: 'under-construction' }, 'Updates live while you watch!'),
    ),
    live,
    list,
    empty,
    h('p', null, more),
  );
  return { title: "What's New!", body };
}
