/**
 * wsp://<source>/<native_id> — a document rendered inside the period page
 * frame, with webring bar, provenance footer, time machine and View Source
 * Data.
 */
import { ApiRequestError } from '../api/client.ts';
import type { DocumentResponse, SourceInfo } from '../api/types.ts';
import { makeAddress, searchAddress } from '../core/address.ts';
import { diffLines, diffStats } from '../core/diff.ts';
import { h } from '../core/dom.ts';
import { formatDate } from '../core/format.ts';
import { sanitizeDocumentHtml } from '../core/links.ts';
import type { Route } from '../core/router.ts';
import { renderPlainText } from '../core/text.ts';
import {
  asOf,
  badge,
  errorPage,
  KIND_LABELS,
  licenseBadge,
  pageHeading,
  wspLink,
} from '../ui/components.ts';
import type { AppServices, PageContext, PageResult } from './types.ts';

type DocRoute = Extract<Route, { page: 'document' }>;

/** Small LRU so Back/Forward are instant. */
class Lru<V> {
  private readonly map = new Map<string, V>();
  constructor(private readonly max: number) {}
  get(k: string): V | undefined {
    const v = this.map.get(k);
    if (v !== undefined) {
      this.map.delete(k);
      this.map.set(k, v);
    }
    return v;
  }
  set(k: string, v: V): void {
    this.map.delete(k);
    this.map.set(k, v);
    if (this.map.size > this.max) this.map.delete(this.map.keys().next().value!);
  }
}

const cache = new Lru<DocumentResponse>(40);

async function loadDoc(ctx: PageContext, id: string, at?: string): Promise<DocumentResponse> {
  const key = `${id}@${at ?? ''}`;
  if (!ctx.opts.reload) {
    const hit = cache.get(key);
    if (hit) return hit;
  }
  const doc = await ctx.app.api.document(id, { at }, ctx.signal);
  cache.set(key, doc);
  return doc;
}

function asOfText(doc: DocumentResponse): string {
  return doc.timestamp ? formatDate(doc.timestamp) : (doc.generation_id ?? 'an unknown date');
}

export function isPreformatted(doc: Pick<DocumentResponse, 'source_id' | 'kind'>): boolean {
  return doc.source_id === 'rfc' || doc.kind === 'data' || doc.kind === 'law';
}

export function renderBody(doc: DocumentResponse): { node: HTMLElement; broken: number } {
  const wrap = h('div', { class: 'doc-body', lang: doc.lang });
  if (doc.format === 'html' && doc.html) {
    const { fragment, stats } = sanitizeDocumentHtml(doc.html, {
      sourceId: doc.source_id,
      nativeId: doc.native_id,
      docId: doc.doc_id,
      links: doc.links,
    });
    wrap.appendChild(fragment);
    return { node: wrap, broken: stats.broken };
  }
  wrap.appendChild(renderPlainText(doc.text, isPreformatted(doc)));
  return { node: wrap, broken: wrap.querySelectorAll('.broken-link').length };
}

function webring(source: SourceInfo | undefined, all: SourceInfo[]): HTMLElement | null {
  if (!source || all.length === 0) return null;
  const i = all.findIndex((s) => s.id === source.id);
  const prev = all[(i - 1 + all.length) % all.length]!;
  const next = all[(i + 1) % all.length]!;
  const related = source.related
    .map((id) => all.find((s) => s.id === id))
    .filter((s): s is SourceInfo => !!s);
  return h(
    'nav',
    { class: 'webring', 'aria-label': 'Webring' },
    h('span', { class: 'webring-title' }, 'Whisipedia Ring:'),
    ' [ ',
    wspLink(makeAddress(prev.id), `« ${prev.name}`),
    ' | ',
    wspLink(makeAddress('random', '', { source: source.id }), 'Random page here'),
    ' | ',
    wspLink(makeAddress('random'), 'Random site'),
    ' | ',
    wspLink(makeAddress(next.id), `${next.name} »`),
    ' ]',
    related.length
      ? h(
          'span',
          { class: 'webring-related' },
          ' Related: ',
          ...related.flatMap((r, k) => [k ? ', ' : '', wspLink(makeAddress(r.id), r.name)]),
        )
      : null,
  );
}

function siteBanner(doc: DocumentResponse): HTMLElement {
  return h(
    'div',
    { class: `site-banner site-${doc.kind}` },
    wspLink(makeAddress(doc.source_id), h('span', { class: 'site-name' }, doc.source_name)),
    h(
      'span',
      { class: 'site-meta' },
      badge(KIND_LABELS[doc.kind], 'badge-kind'),
      ' ',
      licenseBadge(doc.license),
      ' ',
      asOf(doc.timestamp),
    ),
  );
}

function timeMachine(ctx: PageContext, doc: DocumentResponse, route: DocRoute): HTMLElement {
  const list = h('div', { class: 'tm-list' }, 'Loading versions…');
  const details = h(
    'details',
    { class: 'time-machine', open: route.at !== undefined },
    h('summary', null, 'Time Machine: see this page as of an earlier date'),
    list,
  );
  let loaded = false;
  const load = () => {
    if (loaded) return;
    loaded = true;
    ctx.app.api
      .history(doc.doc_id, ctx.signal)
      .then((hist) => {
        const here = makeAddress(doc.source_id, doc.native_id);
        list.replaceChildren(
          h(
            'table',
            { class: 'tm-table' },
            h(
              'thead',
              null,
              h(
                'tr',
                null,
                h('th', null, 'As of'),
                h('th', null, 'Revision'),
                h('th', null, 'Change'),
                h('th', null, ''),
              ),
            ),
            h(
              'tbody',
              null,
              ...[...hist.items].reverse().map((v, i) => {
                const at = v.generation_id;
                const label = v.timestamp ? formatDate(v.timestamp) : at;
                return h(
                  'tr',
                  { class: v.content_hash === doc.content_hash ? 'tm-current' : '' },
                  h(
                    'td',
                    null,
                    i === 0
                      ? wspLink(here, label)
                      : wspLink(makeAddress(doc.source_id, doc.native_id, { at }), label),
                  ),
                  h('td', null, h('code', null, v.revision ?? at)),
                  h('td', null, v.kind === 'added' ? 'added' : 'modified'),
                  h(
                    'td',
                    null,
                    i === 0
                      ? '(current)'
                      : wspLink(
                          makeAddress(doc.source_id, doc.native_id, { at, diff: 1 }),
                          'diff vs current',
                        ),
                  ),
                );
              }),
            ),
          ),
        );
      })
      .catch((err: unknown) => {
        list.replaceChildren(
          err instanceof ApiRequestError && err.isUnavailable
            ? 'This source keeps no history.'
            : `Could not load history: ${String(err)}`,
        );
      });
  };
  if (!doc.has_history && route.at === undefined) {
    list.replaceChildren('This page has a single version in the library.');
    loaded = true;
  }
  details.addEventListener('toggle', () => {
    if (details.open) load();
  });
  if (details.open) load();
  return details;
}

function renderDiff(oldDoc: DocumentResponse, cur: DocumentResponse): HTMLElement {
  const ops = diffLines(oldDoc.text, cur.text);
  if (!ops) return h('p', { class: 'notice' }, 'This page is too large to compare in the browser.');
  const st = diffStats(ops);
  return h(
    'div',
    { class: 'diff' },
    h(
      'p',
      { class: 'diff-stats' },
      `${st.added} line(s) added, ${st.removed} line(s) removed between ${asOfText(oldDoc)} and ${asOfText(cur)}.`,
    ),
    h(
      'pre',
      { class: 'diff-body' },
      ...ops.map((o) =>
        o.op === 'eq'
          ? h('span', { class: 'diff-eq' }, `  ${o.text}\n`)
          : o.op === 'add'
            ? h('ins', { class: 'diff-add' }, `+ ${o.text}\n`)
            : h('del', { class: 'diff-del' }, `- ${o.text}\n`),
      ),
    ),
  );
}

function footer(app: AppServices, doc: DocumentResponse, address: string): HTMLElement {
  const bookmark = h(
    'button',
    { type: 'button', class: 'btn btn-small' },
    app.hotlist.has(address) ? 'In Hotlist ✓' : 'Add to Hotlist',
  );
  bookmark.addEventListener('click', () => {
    app.hotlist.add(address, doc.title);
    bookmark.textContent = 'In Hotlist ✓';
    app.status(`Added “${doc.title}” to the Hotlist.`);
  });
  const vsd = h(
    'button',
    { type: 'button', class: 'btn btn-small', 'aria-keyshortcuts': 'Control+U' },
    'View Source Data',
  );
  vsd.addEventListener('click', () => app.openSourceData(doc));
  return h(
    'footer',
    { class: 'doc-footer' },
    h('hr'),
    h(
      'p',
      null,
      'From ',
      wspLink(makeAddress(doc.source_id), doc.source_name),
      ` · revision ${doc.revision ?? doc.generation_id ?? 'unknown'} · `,
      asOf(doc.timestamp),
      ' · license ',
      licenseBadge(doc.license),
    ),
    doc.url_native
      ? h(
          'p',
          { class: 'fineprint' },
          'Original address (not fetched, offline): ',
          h('code', null, doc.url_native),
        )
      : null,
    h('p', { class: 'doc-actions' }, vsd, ' ', bookmark),
  );
}

export async function render(ctx: PageContext): Promise<PageResult> {
  const route = ctx.route as DocRoute;
  let doc: DocumentResponse;
  try {
    doc = await loadDoc(ctx, route.docId, route.at);
  } catch (err) {
    if (err instanceof ApiRequestError && err.status === 404) {
      const words = route.nativeId.replace(/[_/]+/g, ' ');
      // A link led here but the library lacks the page: tell the gap miner (PLAN §5.3).
      void ctx.app.api.reportGap({ href: ctx.address, from_doc_id: ctx.app.lastDocId() });
      return {
        title: '404 Not Found',
        body: errorPage({
          title: 'Not Found',
          heading: 'Not Found',
          message: `The requested page was not found in the library. It may not have been mirrored yet, or it was removed upstream.`,
          address: ctx.address,
          suggestions: [
            wspLink(searchAddress(words), `Search the library for “${words}”`),
            wspLink(makeAddress(route.source), `Go to the ${route.source} site`),
            wspLink(makeAddress('home'), 'Go to the Start Page'),
          ],
        }),
      };
    }
    throw err;
  }
  ctx.app.setCurrentDocument(doc);
  const sources = await ctx.app.sources();
  const source = sources.find((s) => s.id === doc.source_id);

  let content: HTMLElement;
  let broken = 0;
  let tmBanner: HTMLElement | null = null;
  const here = makeAddress(doc.source_id, doc.native_id);
  if (route.at) {
    tmBanner = h(
      'p',
      { class: 'notice notice-tm', role: 'note' },
      h('b', null, 'Time Machine: '),
      `you are viewing this page as of ${asOfText(doc)} (generation ${doc.generation_id ?? route.at}). `,
      wspLink(here, 'View the current page'),
      route.diff
        ? null
        : [
            ' · ',
            wspLink(
              makeAddress(doc.source_id, doc.native_id, { at: route.at, diff: 1 }),
              'Show changes since',
            ),
          ],
    );
  }
  if (route.at && route.diff) {
    const current = await loadDoc(ctx, route.docId);
    content = renderDiff(doc, current);
  } else {
    const r = renderBody(doc);
    content = r.node;
    broken = r.broken;
  }

  const body = h(
    'article',
    { class: `page page-document doc-kind-${doc.kind}`, 'aria-labelledby': 'doc-title' },
    siteBanner(doc),
    webring(source, sources),
    tmBanner,
    // Original pages (ZIM HTML) carry their own <h1>; don't print the title twice.
    content instanceof Element && content.querySelector('h1') ? null : pageHeading(doc.title),
    content,
    broken
      ? h(
          'p',
          { class: 'fineprint broken-note' },
          `${broken} link(s) on this page point outside the library and are shown as broken links.`,
        )
      : null,
    timeMachine(ctx, doc, route),
    footer(ctx.app, doc, here),
  );
  body.querySelector('h1')?.setAttribute('id', 'doc-title');
  return {
    title: route.at ? `${doc.title} (as of ${asOfText(doc)})` : doc.title,
    body,
    scrollTo: route.fragment ? `doc-${route.fragment}` : undefined,
  };
}
