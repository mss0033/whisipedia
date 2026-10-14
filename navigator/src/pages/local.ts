/** wsp://history and wsp://hotlist — local, per-browser pages. */
import { h } from '../core/dom.ts';
import { formatLocalDate, formatLocalDateTime } from '../core/format.ts';
import { pageHeading, wspLink } from '../ui/components.ts';
import type { PageContext, PageResult } from './types.ts';

export async function renderHistory(ctx: PageContext): Promise<PageResult> {
  const { history } = ctx.app;
  const filter = h('input', {
    type: 'search',
    id: 'history-filter',
    class: 'search-input',
    placeholder: 'Filter…',
    size: 30,
  });
  const table = h('div', { class: 'history-table' });

  const draw = () => {
    const f = filter.value.trim().toLowerCase();
    const rows = history
      .list()
      .filter((e) => !f || e.title.toLowerCase().includes(f) || e.address.includes(f));
    if (rows.length === 0) {
      table.replaceChildren(
        h('p', null, f ? 'No matching pages.' : 'You have not visited any pages yet.'),
      );
      return;
    }
    const groups = new Map<string, typeof rows>();
    for (const e of rows) {
      const day = formatLocalDate(e.lastVisit);
      groups.set(day, [...(groups.get(day) ?? []), e]);
    }
    table.replaceChildren(
      ...[...groups.entries()].flatMap(([day, list]) => [
        h('h2', null, day),
        h(
          'table',
          { class: 'grid-table' },
          h(
            'thead',
            null,
            h(
              'tr',
              null,
              h('th', null, 'Title'),
              h('th', null, 'Location'),
              h('th', null, 'Last visited'),
              h('th', null, 'Visits'),
              h('th', null, h('span', { class: 'visually-hidden' }, 'Actions')),
            ),
          ),
          h(
            'tbody',
            null,
            ...list.map((e, i) => {
              const del = h(
                'button',
                {
                  type: 'button',
                  class: 'btn btn-small',
                  'data-action': 'remove',
                  'aria-label': `Remove ${e.title} from history`,
                },
                'Remove',
              );
              del.addEventListener('click', () => {
                history.remove(e.address);
                draw();
                // Keep the keyboard where it was: the next row's Remove, else the filter.
                refocus(table, 'button[data-action="remove"]', i, filter);
              });
              return h(
                'tr',
                null,
                h('td', null, wspLink(e.address, e.title || e.address)),
                h('td', null, h('code', null, e.address)),
                h('td', null, formatLocalDateTime(e.lastVisit)),
                h('td', null, String(e.visits)),
                h('td', null, del),
              );
            }),
          ),
        ),
      ]),
    );
  };
  filter.addEventListener('input', draw);
  draw();

  const clearBtn = h('button', { type: 'button', class: 'btn' }, 'Clear History…');
  clearBtn.addEventListener('click', () => {
    if (globalThis.confirm('Clear all history on this computer?')) {
      history.clear();
      draw();
      ctx.app.status('History cleared.');
    }
  });

  const body = h(
    'div',
    { class: 'page page-history' },
    pageHeading('History'),
    h('p', null, 'Pages you have visited with this browser. Stored only on this computer.'),
    h('p', null, h('label', { for: 'history-filter' }, 'Find: '), filter, ' ', clearBtn),
    table,
  );
  return { title: 'History', body };
}

export async function renderHotlist(ctx: PageContext): Promise<PageResult> {
  const { hotlist } = ctx.app;
  const list = h('div');
  const draw = () => {
    const items = hotlist.list();
    if (items.length === 0) {
      list.replaceChildren(
        h(
          'p',
          null,
          'Your Hotlist is empty. Press ',
          h('kbd', null, 'Ctrl+D'),
          ' on any page to add it.',
        ),
      );
      return;
    }
    list.replaceChildren(
      h(
        'ul',
        { class: 'hotlist' },
        ...items.map((b, i) => {
          const up = h(
            'button',
            {
              type: 'button',
              class: 'btn btn-small',
              'data-action': 'up',
              disabled: i === 0,
              'aria-label': `Move ${b.title} up`,
            },
            '▲',
          );
          const down = h(
            'button',
            {
              type: 'button',
              class: 'btn btn-small',
              'data-action': 'down',
              disabled: i === items.length - 1,
              'aria-label': `Move ${b.title} down`,
            },
            '▼',
          );
          const del = h(
            'button',
            {
              type: 'button',
              class: 'btn btn-small',
              'data-action': 'delete',
              'aria-label': `Delete ${b.title}`,
            },
            'Delete',
          );
          // After a redraw, focus follows the item (moved) or its place (deleted).
          up.addEventListener('click', () => {
            hotlist.move(b.address, -1);
            draw();
            refocusMoved(list, i - 1, 'up');
          });
          down.addEventListener('click', () => {
            hotlist.move(b.address, 1);
            draw();
            refocusMoved(list, i + 1, 'down');
          });
          del.addEventListener('click', () => {
            hotlist.remove(b.address);
            draw();
            refocus(list, 'button[data-action="delete"]', i, null);
          });
          return h(
            'li',
            null,
            h('span', { class: 'bm-icon', 'aria-hidden': 'true' }, '◆'),
            ' ',
            wspLink(b.address, b.title),
            h('span', { class: 'fineprint' }, ` added ${formatLocalDate(b.added)} `),
            up,
            down,
            del,
          );
        }),
      ),
    );
  };
  draw();
  const body = h(
    'div',
    { class: 'page page-hotlist' },
    pageHeading('Hotlist'),
    h('p', null, 'Your favourite places in the offline web. Stored only on this computer.'),
    list,
  );
  return { title: 'Hotlist', body };
}

/**
 * After a redraw, put the keyboard focus on the button now at ``index`` among ``selector`` (the
 * next row, or the last one), else on ``fallback``. Redrawing dropped focus on the page body, so
 * removing items from the keyboard meant tabbing back through the whole window each time.
 */
function refocus(
  root: ParentNode,
  selector: string,
  index: number,
  fallback: HTMLElement | null,
): void {
  const buttons = [...root.querySelectorAll<HTMLButtonElement>(selector)];
  const target = buttons[Math.min(index, buttons.length - 1)] ?? fallback;
  target?.focus();
}

/** Focus the moved item's same arrow, or its other arrow at the top or bottom of the list. */
function refocusMoved(root: ParentNode, index: number, action: 'up' | 'down'): void {
  const items = [...root.querySelectorAll('li')];
  const li = items[index];
  if (!li) return;
  const same = li.querySelector<HTMLButtonElement>(`button[data-action="${action}"]`);
  const other = li.querySelector<HTMLButtonElement>(
    `button[data-action="${action === 'up' ? 'down' : 'up'}"]`,
  );
  (same && !same.disabled ? same : other)?.focus();
}
