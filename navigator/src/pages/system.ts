/**
 * wsp://system — System Monitor: freshness per site, degraded generations and
 * the ledger tail with its integrity check. Refreshes every 5 s while open.
 */
import type { SystemStatus } from '../api/types.ts';
import { makeAddress } from '../core/address.ts';
import { h } from '../core/dom.ts';
import { formatDateTime, formatDuration } from '../core/format.ts';
import { pageHeading, sectionBox, statusLamp, wspLink } from '../ui/components.ts';
import type { PageContext, PageResult } from './types.ts';

export const REFRESH_MS = 5000;

function age(seconds: number | null): string {
  return seconds === null ? '—' : `${Math.floor(seconds / 86400)} d`;
}

export function renderSystem(sys: SystemStatus, names: Map<string, string>): HTMLElement {
  return h(
    'div',
    { class: 'sys-grid' },
    sectionBox(
      'Library',
      h(
        'table',
        { class: 'kv' },
        h(
          'tbody',
          null,
          h('tr', null, h('th', { scope: 'row' }, 'API version'), h('td', null, sys.api_version)),
          h(
            'tr',
            null,
            h('th', { scope: 'row' }, 'Uptime'),
            h('td', null, formatDuration(sys.uptime_seconds)),
          ),
          h(
            'tr',
            null,
            h('th', { scope: 'row' }, 'Ledger'),
            h(
              'td',
              null,
              statusLamp(sys.ledger_ok === null ? 'stale' : sys.ledger_ok ? 'ok' : 'degraded'),
              ' hash chain ',
              sys.ledger_ok === null
                ? 'is being verified (check back shortly)'
                : sys.ledger_ok
                  ? 'verifies'
                  : 'is BROKEN',
            ),
          ),
          h(
            'tr',
            null,
            h('th', { scope: 'row' }, 'Checked'),
            h('td', null, formatDateTime(sys.generated_at)),
          ),
        ),
      ),
    ),
    sectionBox(
      'Freshness',
      h(
        'table',
        { class: 'grid-table' },
        h(
          'thead',
          null,
          h(
            'tr',
            null,
            h('th', null, 'Site'),
            h('th', null, 'Generation'),
            h('th', null, 'Age'),
            h('th', null, 'Status'),
          ),
        ),
        h(
          'tbody',
          null,
          ...sys.freshness.map((f) =>
            h(
              'tr',
              null,
              h('td', null, wspLink(makeAddress(f.source_id), names.get(f.source_id) ?? f.name)),
              h('td', null, f.current_generation ? h('code', null, f.current_generation) : '—'),
              h('td', null, `${age(f.age_seconds)}${f.cadence ? ` / ${f.cadence} cadence` : ''}`),
              h('td', null, statusLamp(f.health)),
            ),
          ),
        ),
      ),
    ),
    sectionBox(
      'Degraded Generations',
      sys.degraded.length
        ? h(
            'ul',
            null,
            ...sys.degraded.map((d) =>
              h(
                'li',
                null,
                h('code', null, `${d.source_id}/${d.generation_id}`),
                d.current ? ' (current)' : '',
                h('br'),
                `Failed scrub and could not be repaired${d.sealed_at ? `; sealed ${formatDateTime(d.sealed_at)}` : ''}.`,
              ),
            ),
          )
        : h('p', null, 'None. Everything verifies.'),
    ),
    sectionBox(
      'Ledger (latest entries)',
      h(
        'pre',
        { class: 'ledger' },
        sys.ledger_tail
          .map((l) => `${l.at}  ${l.kind.padEnd(16)} ${l.subject} ${JSON.stringify(l.data)}`)
          .join('\n'),
      ),
    ),
  );
}

export async function render(ctx: PageContext): Promise<PageResult> {
  const [sys, sources] = await Promise.all([ctx.app.api.system(ctx.signal), ctx.app.sources()]);
  const names = new Map(sources.map((s) => [s.id, s.name]));
  const holder = h('div', { class: 'sys-holder' }, renderSystem(sys, names));
  const stamp = h(
    'p',
    { class: 'fineprint', 'aria-live': 'off' },
    `Updated ${new Date().toLocaleTimeString()} · refreshes every ${REFRESH_MS / 1000} s`,
  );

  const timer = setInterval(() => {
    if (document.hidden) return;
    ctx.app.api
      .system(ctx.signal)
      .then((next) => {
        holder.replaceChildren(renderSystem(next, names));
        stamp.textContent = `Updated ${new Date().toLocaleTimeString()} · refreshes every ${REFRESH_MS / 1000} s`;
      })
      .catch(() => {
        stamp.textContent = 'Lost contact with the daemon; retrying…';
      });
  }, REFRESH_MS);
  ctx.signal.addEventListener('abort', () => clearInterval(timer), { once: true });

  const body = h(
    'div',
    { class: 'page page-system' },
    pageHeading('System Monitor'),
    stamp,
    holder,
  );
  return { title: 'System Monitor', body };
}
