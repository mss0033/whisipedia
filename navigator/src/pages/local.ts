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
            ...list.map((e) => {
              const del = h(
                'button',
                {
                  type: 'button',
                  class: 'btn btn-small',
                  'aria-label': `Remove ${e.title} from history`,
                },
                'Remove',
              );
              del.addEventListener('click', () => {
                history.remove(e.address);
                draw();
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
              disabled: i === items.length - 1,
              'aria-label': `Move ${b.title} down`,
            },
            '▼',
          );
          const del = h(
            'button',
            { type: 'button', class: 'btn btn-small', 'aria-label': `Delete ${b.title}` },
            'Delete',
          );
          up.addEventListener('click', () => {
            hotlist.move(b.address, -1);
            draw();
          });
          down.addEventListener('click', () => {
            hotlist.move(b.address, 1);
            draw();
          });
          del.addEventListener('click', () => {
            hotlist.remove(b.address);
            draw();
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
