/** wsp://whatsnew — "What's New!" fed by the change feed (/v1/changes). */
import type { ChangeEntry, SourceInfo } from '../api/types.ts';
import { makeAddress } from '../core/address.ts';
import { h } from '../core/dom.ts';
import { formatDate, formatDateTime } from '../core/format.ts';
import { docLink, newBadge, pageHeading, wspLink } from '../ui/components.ts';
import type { PageContext, PageResult } from './types.ts';

const OP_LABEL = { add: 'New', modify: 'Updated', tombstone: 'Removed' } as const;

export function groupByDay(changes: ChangeEntry[]): Map<string, ChangeEntry[]> {
  const out = new Map<string, ChangeEntry[]>();
  for (const c of changes) {
    const day = c.timestamp.slice(0, 10);
    out.set(day, [...(out.get(day) ?? []), c]);
  }
  return out;
}

function renderGroups(target: HTMLElement, changes: ChangeEntry[], sources: SourceInfo[]): void {
  const name = (id: string) => sources.find((s) => s.id === id)?.name ?? id;
  for (const [day, list] of groupByDay(changes)) {
    let ul = target.querySelector<HTMLUListElement>(`ul[data-day="${day}"]`);
    if (!ul) {
      target.append(h('h2', { class: 'wn-day' }, formatDate(`${day}T00:00:00Z`)));
      ul = h('ul', { class: 'wn-list', dataset: { day } });
      target.append(ul);
    }
    for (const c of list) {
      ul.append(
        h(
          'li',
          { class: `wn-${c.op}` },
          c.op === 'add' ? newBadge() : h('span', { class: `wn-op wn-op-${c.op}` }, OP_LABEL[c.op]),
          ' ',
          c.op === 'tombstone'
            ? h('s', { title: 'Removed upstream; kept in older generations' }, c.title)
            : docLink(c.doc_id, c.title),
          ' — ',
          wspLink(makeAddress(c.source_id), name(c.source_id)),
          h('span', { class: 'fineprint' }, ` ${formatDateTime(c.timestamp)}`),
        ),
      );
    }
  }
}

export async function render(ctx: PageContext): Promise<PageResult> {
  const [sources, first] = await Promise.all([
    ctx.app.sources(),
    ctx.app.api.changes(undefined, 30, ctx.signal),
  ]);
  const list = h('div', { class: 'wn-groups' });
  renderGroups(list, first.changes, sources);

  let cursor = first.next_cursor;
  const more = h('button', { type: 'button', class: 'btn', hidden: !cursor }, 'More changes…');
  more.addEventListener('click', () => {
    if (!cursor) return;
    more.disabled = true;
    ctx.app.api
      .changes(cursor, 30, ctx.signal)
      .then((r) => {
        renderGroups(list, r.changes, sources);
        cursor = r.next_cursor;
        more.hidden = !cursor;
      })
      .catch((err: unknown) => ctx.app.status(`Could not load more changes: ${String(err)}`))
      .finally(() => {
        more.disabled = false;
      });
  });

  const body = h(
    'div',
    { class: 'page page-whatsnew' },
    pageHeading("What's New!"),
    h(
      'p',
      { class: 'wn-intro' },
      'The latest additions and updates to the library, straight from the change feed. ',
      h('span', { class: 'under-construction' }, 'Updated every cycle!'),
    ),
    first.changes.length ? list : h('p', null, 'Nothing has changed yet.'),
    h('p', null, more),
  );
  return { title: "What's New!", body };
}
