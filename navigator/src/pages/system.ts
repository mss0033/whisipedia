/**
 * wsp://system — System Monitor: freshness, scrub progress, degraded items,
 * disk budget and the ledger tail. Refreshes every 5 s while open.
 */
import type { SystemResponse } from '../api/types.ts';
import { makeAddress } from '../core/address.ts';
import { h } from '../core/dom.ts';
import { formatBytes, formatDate, formatDateTime, formatDuration } from '../core/format.ts';
import { meter, pageHeading, sectionBox, statusLamp, wspLink } from '../ui/components.ts';
import type { PageContext, PageResult } from './types.ts';

export const REFRESH_MS = 5000;

export function renderSystem(sys: SystemResponse, names: Map<string, string>): HTMLElement {
  const pct = Math.round(sys.scrub.progress * 100);
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
          h(
            'tr',
            null,
            h('th', { scope: 'row' }, 'Generation'),
            h('td', null, h('code', null, sys.generation_id)),
          ),
          h('tr', null, h('th', { scope: 'row' }, 'Daemon version'), h('td', null, sys.version)),
          h(
            'tr',
            null,
            h('th', { scope: 'row' }, 'Uptime'),
            h('td', null, formatDuration(sys.uptime_s)),
          ),
          sys.latency_ms
            ? h(
                'tr',
                null,
                h('th', { scope: 'row' }, 'Search latency'),
                h(
                  'td',
                  null,
                  `p50 ${sys.latency_ms.search_p50} ms · p95 ${sys.latency_ms.search_p95} ms`,
                ),
              )
            : null,
        ),
      ),
    ),
    sectionBox(
      'Scrub (integrity check)',
      h(
        'p',
        null,
        h('progress', { max: 100, value: pct, 'aria-label': 'Scrub progress' }),
        ` ${pct}%`,
      ),
      h(
        'p',
        null,
        `${formatBytes(sys.scrub.bytes_verified)} of ${formatBytes(sys.scrub.bytes_total)} verified this pass.`,
      ),
      h(
        'p',
        null,
        `Last full pass: ${sys.scrub.last_completed ? formatDateTime(sys.scrub.last_completed) : 'never'}. `,
        `Errors found ${sys.scrub.errors_found}, repaired ${sys.scrub.errors_repaired}.`,
      ),
    ),
    sectionBox(
      'Disk Budget',
      h(
        'table',
        { class: 'kv' },
        h(
          'tbody',
          null,
          ...sys.disk.map((d) =>
            h(
              'tr',
              null,
              h('th', { scope: 'row' }, d.name),
              h(
                'td',
                null,
                meter(d.used_bytes, d.total_bytes, `${d.name} usage`),
                ` ${formatBytes(d.used_bytes)} / ${formatBytes(d.total_bytes)}`,
              ),
            ),
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
            h('th', null, 'As of'),
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
              h(
                'td',
                null,
                wspLink(makeAddress(f.source_id), names.get(f.source_id) ?? f.source_id),
              ),
              h('td', null, formatDate(f.as_of)),
              h('td', null, `${f.age_days} d / ${f.slo_days} d SLO`),
              h('td', null, statusLamp(f.status)),
            ),
          ),
        ),
      ),
    ),
    sectionBox(
      'Degraded Items',
      sys.degraded.length
        ? h(
            'ul',
            null,
            ...sys.degraded.map((d) =>
              h(
                'li',
                null,
                h('code', null, d.path),
                h('br'),
                `${d.reason} (since ${formatDateTime(d.since)})`,
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
        sys.ledger_tail.map((l) => `${l.ts}  ${l.event.padEnd(16)} ${l.detail}`).join('\n'),
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
