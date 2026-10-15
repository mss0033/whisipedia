/**
 * wsp://system — System Monitor: open alerts, what the trawler found, freshness per site, degraded
 * generations and the ledger tail with its integrity check. Refreshes every 5 s while open.
 */
import type { SystemStatus } from '../api/types.ts';

type Ops = NonNullable<SystemStatus['ops']>;
const RANK: Record<string, number> = { critical: 0, warning: 1, info: 2 };

import { makeAddress } from '../core/address.ts';
import { h } from '../core/dom.ts';
import { formatDateTime, formatDuration } from '../core/format.ts';
import { pageHeading, sectionBox, statusLamp, wspLink } from '../ui/components.ts';
import type { PageContext, PageResult } from './types.ts';

export const REFRESH_MS = 5000;

/**
 * Open alerts, worst first; more than three of one kind (36 "dormant upstream" after a discovery
 * pass) become one line, so the few that matter are not buried.
 */
export function alertsBox(ops: Ops | null | undefined): HTMLElement {
  if (!ops) return sectionBox('Alerts', h('p', null, 'No orchestrator data yet.'));
  if (!ops.alerts.length) return sectionBox('Alerts', h('p', null, 'None open.'));
  const sorted = [...ops.alerts].sort(
    (a, b) => (RANK[a.severity] ?? 3) - (RANK[b.severity] ?? 3) || a.key.localeCompare(b.key),
  );
  const groups = new Map<string, typeof sorted>();
  for (const a of sorted) {
    const kind = `${a.severity}|${a.key.split(':')[0]}`;
    groups.set(kind, [...(groups.get(kind) ?? []), a]);
  }
  const items: HTMLElement[] = [];
  for (const [kind, group] of groups) {
    const [severity, name] = kind.split('|') as [string, string];
    const lamp = statusLamp(
      severity === 'critical' ? 'degraded' : severity === 'warning' ? 'stale' : 'ok',
    );
    if (group.length > 3) {
      items.push(
        h(
          'li',
          null,
          lamp,
          ` ${group.length} ${severity} alerts of kind `,
          h('b', null, name),
          ': ',
          group.map((a) => a.key.split(':').slice(1).join(':')).join(', '),
        ),
      );
    } else {
      for (const a of group)
        items.push(
          h(
            'li',
            null,
            lamp,
            ` ${a.message} `,
            h('span', { class: 'fineprint' }, `(${formatDateTime(a.raised_at)})`),
          ),
        );
    }
  }
  return sectionBox(
    `Alerts (${ops.alerts.length} open)`,
    h('ul', { class: 'alert-list' }, ...items),
  );
}

/** The plan's phase-5 milestone: 30 days unattended, every SLO met, no manual repairs. */
export function autonomyBox(ops: Ops | null | undefined): HTMLElement | null {
  const a = ops?.autonomy;
  if (!a) return null;
  const pct = Math.min(100, Math.round((100 * a.days) / a.goal_days));
  return sectionBox(
    'Unattended',
    // The days first, then the state on its own line: "STALE 0.2 of 30 days" read as "0.2 stale"
    // (field exam 2026-10-14).
    h(
      'p',
      null,
      `${a.days.toFixed(1)} of ${a.goal_days} days without a critical alert or a manual repair. `,
      h('progress', { max: '100', value: String(pct), 'aria-label': `${pct}% of the goal` }),
    ),
    h(
      'p',
      null,
      'Library state: ',
      statusLamp(a.critical_open ? 'degraded' : a.slo_failing ? 'stale' : 'ok'),
    ),
    h(
      'p',
      { class: 'fineprint' },
      a.critical_open
        ? `${a.critical_open} critical alert(s) open: the count starts once they are resolved. `
        : a.since
          ? `Counting since ${formatDateTime(a.since)} (${a.reset_by}). `
          : `${a.reset_by}. `,
      a.slo_failing
        ? `${a.slo_failing} site(s) behind their freshness target.`
        : 'Every site within its freshness target.',
    ),
  );
}

/** What the trawler found opening pages nobody had opened yet. */
export function trawlerBox(ops: Ops | null | undefined): HTMLElement | null {
  const t = ops?.trawler;
  if (!t) return null;
  return sectionBox(
    'Trawler',
    h(
      'p',
      null,
      `${t.pages.toLocaleString('en-US')} pages checked in ${t.sources_checked} sites; `,
      `${t.failed_last_week} failed in the last week (${t.failed} in all); `,
      `${t.with_defects} work but have a missing image or a link into nothing. `,
      t.catalog_without_link
        ? `${t.catalog_without_link} catalog card(s) without a direct link to their book. `
        : null,
      t.updated
        ? h('span', { class: 'fineprint' }, `Last page ${formatDateTime(t.updated)}.`)
        : null,
    ),
    t.recent.length
      ? h(
          'ul',
          { class: 'trawl-list' },
          ...t.recent.map((r) =>
            h(
              'li',
              null,
              wspLink(r.address, r.address.replace(/^wsp:\/\//, '')),
              h('br'),
              h('span', { class: 'fineprint' }, `${r.problem} · ${formatDateTime(r.at)}`),
            ),
          ),
        )
      : h('p', null, 'No problems found yet.'),
  );
}

function age(seconds: number | null): string {
  return seconds === null ? '—' : `${Math.floor(seconds / 86400)} d`;
}

export function renderSystem(sys: SystemStatus, names: Map<string, string>): HTMLElement {
  return h(
    'div',
    { class: 'sys-grid' },
    autonomyBox(sys.ops),
    alertsBox(sys.ops),
    trawlerBox(sys.ops),
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
    `Updated ${formatDateTime(Date.now())} · refreshes every ${REFRESH_MS / 1000} s`,
  );

  const timer = setInterval(() => {
    if (document.hidden) return;
    ctx.app.api
      .system(ctx.signal)
      .then((next) => {
        holder.replaceChildren(renderSystem(next, names));
        stamp.textContent = `Updated ${formatDateTime(Date.now())} · refreshes every ${REFRESH_MS / 1000} s`;
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
