/** Shared page-building blocks with a late-90s look. */
import type { DocKind, SourceInfo } from '../api/types.ts';
import { docIdToAddress, makeAddress, searchAddress } from '../core/address.ts';
import { type Child, h, uid } from '../core/dom.ts';
import { formatDate } from '../core/format.ts';

export const KIND_LABELS: Record<DocKind, string> = {
  encyclopedia: 'Encyclopedia',
  books: 'Books',
  papers: 'Papers',
  law: 'Law',
  maps: 'Maps',
  qa: 'Q&A',
  dictionary: 'Dictionary',
  data: 'Data',
  medicine: 'Medicine',
  news: 'News',
  video: 'Video',
  audio: 'Audio',
  other: 'Other',
};

/** Order of kind filters on the results page (PLAN §10.3 first). */
export const KIND_ORDER: DocKind[] = [
  'encyclopedia',
  'books',
  'papers',
  'law',
  'maps',
  'qa',
  'dictionary',
  'medicine',
  'data',
  'news',
  'video',
  'audio',
  'other',
];

export const ENGINE_NAME = 'WhizBang!';

/** Internal link. `href` keeps open-in-new-tab working; clicks are intercepted. */
export function wspLink(address: string, ...children: Child[]): HTMLAnchorElement {
  const a = h('a', { href: `#${address}`, dataset: { wsp: address } }, ...children);
  if (children.length === 0) a.textContent = address;
  return a;
}

export function docLink(docId: string, ...children: Child[]): HTMLAnchorElement {
  return wspLink(docIdToAddress(docId), ...children);
}

export function badge(text: string, cls = ''): HTMLElement {
  return h('span', { class: `badge ${cls}`.trim() }, text);
}

export function licenseBadge(license: string): HTMLElement {
  return h('span', { class: 'badge badge-license', title: `License: ${license}` }, license);
}

export function asOf(ts: string | null): HTMLElement {
  if (!ts) return h('span', { class: 'as-of' }, 'date unknown');
  return h('span', { class: 'as-of' }, 'as of ', h('time', { datetime: ts }, formatDate(ts)));
}

/** How old a time is, in words: "12 min", "3 h", "2 days". */
export function age(iso: string | null | undefined, now = Date.now()): string {
  if (!iso) return '';
  const ms = now - Date.parse(iso);
  if (!Number.isFinite(ms)) return '';
  const min = Math.max(0, Math.round(ms / 60_000));
  if (min < 60) return `${min} min`;
  const hours = Math.round(min / 60);
  if (hours < 48) return `${hours} h`;
  return `${Math.round(hours / 24)} days`;
}

const AVAILABILITY_HELP: Record<string, string> = {
  offline: 'Held in the library: sealed on the NAS, works with no connection.',
  kept: 'A kept copy of something that changes: fetched while connected, works offline at the age shown.',
  live: 'Asks the outside world now: needs a connection.',
};

/**
 * The library's three kinds of availability (docs/components/weather.md §1): "In the library",
 * "Kept copy · 2 h old" (stale: "out of date"), "Needs a connection".
 */
export function availabilityBadge(av: {
  kind: string;
  label: string;
  as_of?: string | null;
  fetched_at?: string | null;
  stale?: boolean;
  note?: string;
}): HTMLElement {
  const old = av.kind === 'kept' ? age(av.fetched_at ?? av.as_of) : '';
  const text = av.kind === 'kept' && old ? `${av.label} · ${old} old` : av.label;
  const title = [AVAILABILITY_HELP[av.kind] ?? '', av.note ?? ''].filter(Boolean).join(' ');
  return h(
    'span',
    {
      class: `badge badge-avail badge-avail-${av.kind}${av.stale ? ' badge-avail-stale' : ''}`,
      title,
    },
    text,
    av.stale ? ' · out of date' : '',
  );
}

/** License badge, or nothing when the license is unknown. */
export function maybeLicenseBadge(license: string | null): HTMLElement | null {
  return license ? licenseBadge(license) : null;
}

export function newBadge(): HTMLElement {
  return h('span', { class: 'new-badge', 'aria-label': 'new' }, 'NEW!');
}

export function statusLamp(status: string): HTMLElement {
  return h(
    'span',
    { class: `lamp lamp-${status}`, role: 'img', 'aria-label': `status ${status}` },
    status.toUpperCase(),
  );
}

export function sectionBox(title: string, ...children: Child[]): HTMLElement {
  const id = uid('box');
  return h(
    'section',
    { class: 'box', 'aria-labelledby': id },
    h('h2', { class: 'box-title', id }, title),
    h('div', { class: 'box-body' }, ...children),
  );
}

export function pageHeading(text: string, ...extra: Child[]): HTMLHeadingElement {
  return h('h1', { tabindex: '-1', class: 'page-title' }, text, ...extra);
}

export function sourceSiteLink(s: SourceInfo): HTMLAnchorElement {
  return wspLink(makeAddress(s.id), s.name);
}

/** The engine logo: one span per letter so themes can colour them. */
export function engineLogo(size: 'big' | 'small' = 'big'): HTMLElement {
  const letters = Array.from(ENGINE_NAME).map((c, i) =>
    h('span', { class: `logo-l logo-l${i % 6}`, 'aria-hidden': 'true' }, c),
  );
  return h(
    'div',
    { class: `engine-logo engine-logo-${size}`, role: 'img', 'aria-label': ENGINE_NAME },
    ...letters,
    size === 'big' ? h('sup', { class: 'logo-tm', 'aria-hidden': 'true' }, 'beta') : null,
  );
}

export interface SearchFormOptions {
  query?: string;
  kind?: DocKind | undefined;
  source?: string | undefined;
  big?: boolean;
  lucky?: boolean;
  label?: string;
  onSubmit: (q: string, lucky: boolean) => void;
}

/** A search box + buttons. Returns the form and its input. */
export function searchForm(opts: SearchFormOptions): {
  form: HTMLFormElement;
  input: HTMLInputElement;
} {
  const id = uid('q');
  const input = h('input', {
    id,
    type: 'search',
    name: 'q',
    class: 'search-input',
    value: opts.query ?? '',
    autocomplete: 'off',
    spellcheck: 'false',
    size: opts.big ? 48 : 36,
    'data-search-box': 'true',
  });
  let lucky = false;
  const form = h(
    'form',
    { class: `search-form ${opts.big ? 'search-form-big' : ''}`, role: 'search' },
    h(
      'label',
      { for: id, class: opts.big ? 'visually-hidden' : 'search-label' },
      opts.label ?? 'Search:',
    ),
    ' ',
    input,
    ' ',
    h(
      'span',
      { class: 'search-buttons' },
      h('button', { type: 'submit', class: 'btn' }, opts.big ? `${ENGINE_NAME} Search` : 'Search'),
      opts.lucky ? ' ' : null,
      opts.lucky
        ? h(
            'button',
            {
              type: 'submit',
              class: 'btn',
              onclick: () => {
                lucky = true;
              },
            },
            "I'm Feeling Lucky",
          )
        : null,
    ),
  );
  form.addEventListener('submit', (ev) => {
    ev.preventDefault();
    const q = input.value.trim();
    const wasLucky = lucky;
    lucky = false;
    if (q) opts.onSubmit(q, wasLucky);
    else input.focus();
  });
  return { form, input };
}

export function searchLink(q: string, text?: string): HTMLAnchorElement {
  return wspLink(searchAddress(q), text ?? q);
}

/** Period-style error page ("Not Found", "unable to locate the server"). */
export function errorPage(opts: {
  title: string;
  heading: string;
  message: Child;
  address?: string | undefined;
  suggestions?: Child[] | undefined;
}): HTMLElement {
  return h(
    'div',
    { class: 'page page-error' },
    h('h1', { tabindex: '-1', class: 'page-title' }, opts.heading),
    h('p', null, opts.message),
    opts.address ? h('p', null, h('code', null, opts.address)) : null,
    opts.suggestions?.length
      ? h('ul', null, ...opts.suggestions.map((s) => h('li', null, s)))
      : null,
    h('hr'),
    h('address', null, 'Whisipedia Navigator / 127.0.0.1'),
  );
}

export function hr(): HTMLHRElement {
  return h('hr');
}

/** Simple horizontal bar (meter) with a text equivalent. */
export function meter(value: number, max: number, label: string): HTMLElement {
  const pct = max > 0 ? Math.round((value / max) * 100) : 0;
  return h(
    'span',
    { class: 'meter-wrap' },
    h('meter', {
      min: 0,
      max,
      value,
      low: max * 0.7,
      high: max * 0.9,
      optimum: 0,
      'aria-label': label,
    }),
    ` ${pct}%`,
  );
}
