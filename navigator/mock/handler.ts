/**
 * Framework-free mock of the Whisipedia API v1, typed by the types generated
 * from docs/api/openapi.json: every response is a `Routes[...]['response']`,
 * so the mock cannot drift from the spec without failing `npm run typecheck`.
 * Used by the Vite dev/preview middleware (mock/vite-plugin.ts) and by tests.
 * Deterministic except for /v1/random (without `seed`) and uptime.
 */
import type {
  Change,
  DocKind,
  Hit,
  InstantAnswer,
  Routes,
  SourceInfo,
  SummaryEvent,
} from '../src/api/types.ts';
import { DESCRIPTIONS, DIRECTORY, DOCS, type FixtureDoc, GENERATION, SOURCES } from './data.ts';

type Response<K extends keyof Routes> = Routes[K]['response'];

export type MockResult =
  | { status: number; json: unknown }
  | { status: 204 }
  | { status: 200; stream: Array<{ delayMs: number; event: SummaryEvent }> }
  /** Server-sent events that stay open until the client leaves (the change feed). */
  | { status: 200; hold: true; retryMs: number };

const API_VERSION = '1.1.0';
const docId = (d: FixtureDoc) => `wsp:${d.source_id}/${d.native_id}`;
const address = (d: FixtureDoc) => `wsp://${d.source_id}/${d.native_id}`;
const byId = new Map(DOCS.map((d) => [docId(d), d]));
const sourceName = (id: string) => SOURCES.find((s) => s.id === id)?.name ?? id;
const err = (status: number, code: string, message: string): MockResult => ({
  status,
  json: { error: { code, message } },
});

/** Gap reports received (inspected by tests). */
export const GAPS: Array<Routes['POST /v1/gaps']['body']> = [];

function sourceInfo(id: string): SourceInfo | null {
  const s = SOURCES.find((x) => x.id === id);
  if (!s) return null;
  return {
    id: s.id,
    name: s.name,
    tier: s.tier,
    status: 'active',
    license: s.license,
    legal_basis: 'open-license',
    domain: s.domain,
    adapter: null,
    cadence: 'P1M',
    strategy: null,
    irreplaceable: false,
    address: `wsp://${s.id}`,
    current_generation: s.health === 'offline' ? null : GENERATION,
    publisher: s.publisher,
    description: s.description,
    kind: s.kind,
    health: s.health,
    doc_count: s.doc_count,
    bytes: s.bytes,
    as_of: s.as_of,
    home_doc_id: s.home_doc_id ?? null,
    related: s.related,
  };
}

const allSources = (): SourceInfo[] => SOURCES.map((s) => sourceInfo(s.id)!);

function plain(html: string): string {
  return html
    .replace(/<script[\s\S]*?<\/script>/gi, ' ')
    .replace(/<[^>]+>/g, ' ')
    .replace(/&ndash;/g, '–')
    .replace(/&ldquo;|&rdquo;/g, '"')
    .replace(/&sect;/g, '§')
    .replace(/&[a-z]+;/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();
}

function bodyText(d: FixtureDoc): string {
  const v = d.versions[0]!;
  return v.text ? v.text.replace(/\s+/g, ' ').trim() : plain(v.html ?? '');
}

const STOP = new Set(
  'a an and are as at be by for from how in is it of on or the to what with about does do define map books'.split(
    ' ',
  ),
);

function terms(q: string): string[] {
  return q
    .toLowerCase()
    .split(/[^\p{L}\p{N}-]+/u)
    .filter((t) => t.length > 1 && !STOP.has(t));
}

function score(d: FixtureDoc, q: string, ts: string[]): number {
  const title = d.title.toLowerCase();
  const text = bodyText(d).toLowerCase();
  const aliases = (d.aliases ?? []).join(' | ');
  let s = 0;
  if (aliases.includes(q.toLowerCase())) s += 12;
  for (const t of ts) {
    if (title.includes(t)) s += 5;
    if (aliases.includes(t)) s += 3;
    s += Math.min(text.split(t).length - 1, 5);
  }
  return s;
}

/** Snippet plus highlight ranges in Unicode code points, as the API sends them. */
function snippet(
  text: string,
  ts: string[],
): { snippet: string; highlights: Array<[number, number]> } {
  const lower = text.toLowerCase();
  let at = -1;
  for (const t of ts) {
    const i = lower.indexOf(t);
    if (i !== -1 && (at === -1 || i < at)) at = i;
  }
  const start = Math.max(0, at - 60);
  let snip = text.slice(start, start + 220);
  if (start > 0) snip = `…${snip.replace(/^\S*\s/, '')}`;
  if (start + 220 < text.length) snip = `${snip.replace(/\s\S*$/, '')} …`;
  const highlights: Array<[number, number]> = [];
  const lowerCps = Array.from(snip).map((c) => c.toLowerCase());
  for (const t of ts) {
    const tc = Array.from(t);
    for (let i = 0; i + tc.length <= lowerCps.length; i += 1) {
      if (tc.every((c, k) => lowerCps[i + k] === c)) highlights.push([i, i + tc.length]);
    }
  }
  return { snippet: snip, highlights };
}

function instantFor(q: string): InstantAnswer | null {
  const lq = q.toLowerCase().trim();
  const def = /^(?:define|definition of|what does)\s+(\w+)/.exec(lq);
  if (def?.[1] === 'sesquipedalian') {
    return {
      type: 'definition',
      term: 'sesquipedalian',
      pronunciation: '/ˌsɛskwɪpɪˈdeɪliən/',
      senses: [
        { part_of_speech: 'adjective', definition: 'Given to using long words.', example: null },
        {
          part_of_speech: 'adjective',
          definition: 'Of a word: long, having many syllables.',
          example: 'a sesquipedalian vocabulary',
        },
        { part_of_speech: 'noun', definition: 'A long word.', example: null },
      ],
      doc_id: 'wsp:wiktionary-en/sesquipedalian',
      license: 'CC-BY-SA-4.0',
    };
  }
  if (/\blisbon\b/.test(lq) && /\b(map|where)\b/.test(lq)) {
    return {
      type: 'map',
      place: 'Lisbon, Portugal',
      lat: 38.7223,
      lon: -9.1393,
      zoom: 12,
      description: 'Capital of Portugal, on the Tagus estuary.',
      doc_id: 'wsp:osm-planet/relation/5400890',
      license: 'ODbL-1.0',
    };
  }
  if (/\b(alan )?turing\b/.test(lq)) {
    return {
      type: 'fact',
      qid: 'Q7251',
      label: 'Alan Turing',
      description: 'English computer scientist (1912–1954)',
      facts: [
        { label: 'Born', value: '23 June 1912, Maida Vale, London', doc_id: null },
        { label: 'Died', value: '7 June 1954, Wilmslow', doc_id: null },
        {
          label: 'Known for',
          value: 'Turing machine',
          doc_id: 'wsp:wikipedia-en-zim/Turing_machine',
        },
      ],
      doc_id: 'wsp:wikidata/Q7251',
      license: 'CC0-1.0',
    };
  }
  return null;
}

/** Same rules as the API's navigational resolver, for the fixtures. */
function navigateTo(q: string): string | null {
  const lq = q.toLowerCase().trim();
  const site = /^(?:wikipedia|wiki)\s+(.+)$/.exec(lq);
  if (site) {
    const d = DOCS.find(
      (x) => x.source_id === 'wikipedia-en-zim' && x.title.toLowerCase() === site[1],
    );
    return d ? docId(d) : null;
  }
  const num = /^(rfc|gutenberg)\s*#?\s*(\d+)$/.exec(lq);
  if (num) {
    const id = `wsp:${num[1]}/${Number(num[2])}`;
    return byId.has(id) ? id : null;
  }
  return null;
}

export function search(params: URLSearchParams): Response<'GET /v1/search'> {
  const t0 = performance.now();
  const q = (params.get('q') ?? '').trim();
  const kind = params.get('kind') as DocKind | null;
  const wanted = params
    .getAll('sources')
    .flatMap((s) => s.split(','))
    .filter(Boolean);
  const limit = Math.min(100, Math.max(1, Number(params.get('limit') ?? 10) || 10));
  const offset = Math.max(0, Number(params.get('offset') ?? 0) || 0);
  const ts = terms(q);

  const scored = DOCS.filter((d) => d.native_id !== 'Main_Page')
    .map((d) => ({ d, s: score(d, q, ts) }))
    .filter((x) => x.s > 0 && (wanted.length === 0 || wanted.includes(x.d.source_id)))
    .sort((a, b) => b.s - a.s || a.d.title.localeCompare(b.d.title));

  const facets = new Map<DocKind, number>();
  for (const { d } of scored) facets.set(d.kind, (facets.get(d.kind) ?? 0) + 1);
  const filtered = kind ? scored.filter((x) => x.d.kind === kind) : scored;

  const hits: Hit[] = filtered.map(({ d, s }) => {
    const sn = snippet(bodyText(d), ts.length ? ts : [q.toLowerCase()]);
    return {
      doc_id: docId(d),
      score: s,
      title: d.title,
      snippet: sn.snippet,
      source_id: d.source_id,
      address: address(d),
      source_name: sourceName(d.source_id),
      kind: d.kind,
      license: d.license,
      as_of: d.versions[0]!.timestamp,
      lang: d.lang,
      url_native: d.url_native,
      highlights: sn.highlights,
    };
  });
  // Pad broad queries with synthetic hits so paging is demoable.
  if (hits.length > 0 && wanted.length === 0 && !kind) {
    for (let i = 1; i <= 25; i += 1) {
      const native = `Fixture_${encodeURIComponent(q)}_${2000 + i}`;
      hits.push({
        doc_id: `wsp:wikipedia-en-zim/${native}`,
        score: 0.1 / i,
        title: `${q.replace(/\b\w/g, (c) => c.toUpperCase())} (fixture result ${i})`,
        snippet: `A synthetic fixture page about ${q}. It exists so that paging, visited links and "not in library" pages can be tried in the mock.`,
        source_id: 'wikipedia-en-zim',
        address: `wsp://wikipedia-en-zim/${native}`,
        source_name: sourceName('wikipedia-en-zim'),
        kind: 'encyclopedia',
        license: 'CC-BY-SA-4.0',
        as_of: '2026-10-01T00:00:00Z',
        lang: 'en',
        url_native: null,
        highlights: [],
      });
    }
  }
  const nav = offset === 0 ? navigateTo(q) : null;
  const lq = q.toLowerCase();
  const page = hits.slice(offset, offset + limit);
  return {
    query: q,
    offset,
    limit,
    hits: page,
    next_offset: offset + limit < hits.length ? offset + limit : null,
    kind,
    total: hits.length,
    total_is_estimate: false,
    took_ms: Math.round((performance.now() - t0) * 100) / 100 + 18,
    intent: nav
      ? 'navigate'
      : /^define\b/.test(lq)
        ? 'define'
        : /\bmap\b/.test(lq)
          ? 'locate'
          : /^how\b/.test(lq)
            ? 'how-to'
            : 'lookup',
    facets: [...facets.entries()].map(([k, count]) => ({ kind: k, count })),
    navigate_to: nav,
    instant: offset === 0 ? instantFor(q) : null,
    did_you_mean: /\bvacine/.test(lq) ? lq.replace(/vacine/g, 'vaccine') : null,
    low_confidence: filtered.length === 0,
  };
}

export function suggest(params: URLSearchParams): Response<'GET /v1/suggest'> {
  const q = (params.get('q') ?? '').toLowerCase().trim();
  const limit = Number(params.get('limit') ?? 8) || 8;
  if (!q) return { query: q, suggestions: [] };
  const titles = DOCS.filter(
    (d) =>
      d.native_id !== 'Main_Page' &&
      (d.title.toLowerCase().startsWith(q) || d.title.toLowerCase().includes(` ${q}`)),
  ).map((d) => d.title);
  const phrases = DOCS.flatMap((d) => d.aliases ?? []).filter((a) => a.startsWith(q) && a !== q);
  return { query: q, suggestions: [...new Set([...titles, ...phrases])].slice(0, limit) };
}

/** Versions are newest first; generation ids are the version dates. */
const genOf = (timestamp: string) => timestamp.slice(0, 10);

function versionFor(d: FixtureDoc, at: string | null) {
  if (at) return d.versions.find((v) => genOf(v.timestamp) <= at) ?? null;
  return d.versions[0]!;
}

function hashOf(id: string, revision: string): string {
  return Array.from(`${id}:${revision}`)
    .reduce((h, c) => (Math.imul(h, 31) + c.charCodeAt(0)) >>> 0, 7)
    .toString(16)
    .padStart(8, '0')
    .repeat(8);
}

export function document(
  id: string,
  params: URLSearchParams,
): Response<'GET /v1/documents/{doc_id}'> | null {
  const d = byId.get(id);
  if (!d) return null;
  const v = versionFor(d, params.get('at'));
  if (!v) return null;
  const gen = genOf(v.timestamp);
  const domain = SOURCES.find((s) => s.id === d.source_id)?.domain[0] ?? 'other';
  const base = `library/${domain}/${d.source_id}/${gen}`;
  const file =
    d.format === 'html' ? `upstream/${d.source_id}_${gen.slice(0, 7)}.zim` : 'upstream/texts.tar';
  return {
    doc_id: id,
    source_id: d.source_id,
    native_id: d.native_id,
    title: d.title,
    lang: d.lang,
    license: d.license,
    text: v.text ?? plain(v.html ?? ''),
    content_hash: hashOf(id, v.revision),
    revision: v.revision,
    timestamp: v.timestamp,
    url_native: d.url_native,
    html_ref: d.format === 'html' ? `zim:${d.native_id}` : null,
    entities: d.entities,
    links: d.links,
    address: address(d),
    source_name: sourceName(d.source_id),
    kind: d.kind,
    generation_id: gen,
    format: d.format,
    html: d.format === 'html' ? (v.html ?? null) : null,
    vault: {
      generation_id: gen,
      path: `${base}/${file}`,
      manifest: `${base}/MANIFEST.json`,
      entry: d.format === 'html' ? d.native_id : null,
      size: (v.html ?? v.text ?? '').length * 1000,
      sha256: hashOf(file, 'sha256'),
      blake3: hashOf(file, 'blake3'),
    },
    has_history: d.versions.length > 1,
    currency: d.currency ?? null,
    banner: d.banner ?? null,
  };
}

export function history(id: string): Response<'GET /v1/documents/{doc_id}/history'> | null {
  const d = byId.get(id);
  if (!d) return null;
  const oldestFirst = [...d.versions].reverse();
  return {
    doc_id: id,
    items: oldestFirst.map((v, i) => ({
      generation_id: genOf(v.timestamp),
      content_hash: hashOf(id, v.revision),
      revision: v.revision,
      timestamp: v.timestamp,
      kind: i === 0 ? 'added' : 'modified',
    })),
    next_cursor: null,
  };
}

/** The feed, oldest first. */
const CHANGES: Change[] = (() => {
  const out: Change[] = DOCS.map((d, i) => ({
    doc_id: docId(d),
    kind: i % 4 === 0 ? 'added' : 'modified',
    generation_id: GENERATION,
    old_hash: i % 4 === 0 ? null : hashOf(docId(d), 'old'),
    new_hash: hashOf(docId(d), d.versions[0]!.revision),
    renamed_from: null,
    source_id: d.source_id,
    title: d.title,
    address: address(d),
  }));
  out.splice(5, 0, {
    doc_id: 'wsp:wikipedia-en-zim/Retracted_Hoax_Article',
    kind: 'removed',
    generation_id: GENERATION,
    old_hash: hashOf('hoax', '1'),
    new_hash: null,
    renamed_from: null,
    source_id: 'wikipedia-en-zim',
    title: 'Retracted Hoax Article',
    address: 'wsp://wikipedia-en-zim/Retracted_Hoax_Article',
  });
  return out.reverse(); // built newest first; the feed is oldest first
})();

export function changes(params: URLSearchParams): Response<'GET /v1/changes'> | MockResult {
  const limit = Math.min(5000, Number(params.get('limit') ?? 1000) || 1000);
  const since = params.get('since');
  const start = since === null ? 0 : Number(since);
  if (!Number.isInteger(start) || start < 0 || start > CHANGES.length) {
    return err(400, 'invalid_cursor', `unknown cursor: ${since}`);
  }
  const items = CHANGES.slice(start, start + limit);
  return { items, cursor: String(start + items.length) };
}

export function recentChanges(params: URLSearchParams): Response<'GET /v1/changes/recent'> {
  const limit = Math.min(500, Number(params.get('limit') ?? 50) || 50);
  const source = params.get('source');
  const items = [...CHANGES]
    .reverse()
    .filter((c) => !source || c.source_id === source)
    .slice(0, limit);
  return { items, cursor: String(CHANGES.length) };
}

const started = Date.now() - 3 * 86400_000 - 4 * 3600_000;

export function system(): Response<'GET /v1/system'> {
  const now = Date.parse('2026-10-09T12:00:00Z');
  return {
    generated_at: new Date(now).toISOString(),
    freshness: SOURCES.map((s) => ({
      source_id: s.id,
      name: s.name,
      status: 'active',
      cadence: 'P1M',
      current_generation: s.health === 'offline' ? null : GENERATION,
      sealed_at: s.as_of,
      age_seconds: Math.max(0, (now - Date.parse(s.as_of)) / 1000),
      stale: s.health === 'stale',
      health: s.health,
    })),
    degraded: SOURCES.filter((s) => s.health === 'degraded').map((s) => ({
      source_id: s.id,
      generation_id: genOf(s.as_of),
      status: 'degraded',
      sealed_at: s.as_of,
      artifact_count: 3,
      total_bytes: s.bytes,
      current: true,
    })),
    ledger_tail: [
      ['scrub.chunk', 'osm-planet/2026-10-01', { verified: '412/980' }],
      ['release.state', 'rfc/2026-10-07', { state: 'promoted' }],
      ['scrub.mismatch', 'us-code/2026-08-20', { relpath: 'upstream/usc42.xml' }],
      ['repair.par2', 'us-code/2026-08-20', { result: 'queued' }],
    ].map(([kind, subject, data], i) => ({
      seq: 9000 + i,
      at: new Date(Date.parse('2026-10-08T19:00:00Z') + i * 600_000).toISOString(),
      kind: kind as string,
      subject: subject as string,
      data: data as Record<string, string>,
      hash: hashOf(String(i), 'ledger'),
    })),
    ledger_ok: true,
    api_version: `${API_VERSION}-mock`,
    uptime_seconds: Math.floor((Date.now() - started) / 1000),
    ops: null,
  };
}

export function portal(): Response<'GET /v1/portal'> {
  const turing = byId.get('wsp:wikipedia-en-zim/Alan_Turing')!;
  const frank = byId.get('wsp:gutenberg/84')!;
  return {
    date: '2026-10-09',
    featured: {
      doc_id: docId(turing),
      title: turing.title,
      address: address(turing),
      blurb:
        'English mathematician and codebreaker whose 1936 paper described the machine that bears his name, and with it the limits of what can be computed.',
    },
    on_this_day: [
      {
        year: 1874,
        text: 'The General Postal Union (later the Universal Postal Union) is founded by the Treaty of Bern.',
        doc_id: null,
      },
      { year: 1888, text: 'The Washington Monument opens to the public.', doc_id: null },
      {
        year: 1992,
        text: 'A meteorite lands on a parked car in Peekskill, New York.',
        doc_id: null,
      },
    ],
    did_you_know: [
      {
        text: 'there is an Internet standard for sending IP packets by carrier pigeon?',
        doc_id: 'wsp:rfc/1149',
      },
      { text: 'Frankenstein was first published anonymously in 1818?', doc_id: 'wsp:gutenberg/84' },
      {
        text: 'the word for "given to using long words" is itself a long word?',
        doc_id: 'wsp:wiktionary-en/sesquipedalian',
      },
    ],
    newest: [...CHANGES].reverse().slice(0, 8),
    random: {
      doc_id: docId(frank),
      source_id: frank.source_id,
      title: frank.title,
      address: address(frank),
    },
    random_sources: ['wikipedia-en-zim', 'gutenberg', 'rfc', 'stackexchange'],
    sites: allSources(),
  };
}

export function directory(params: URLSearchParams): Response<'GET /v1/directory'> | null {
  const path = (params.get('path') ?? '').replace(/^\/+|\/+$/g, '');
  const node = DIRECTORY[path];
  if (!node) return null;
  const parts = path ? path.split('/') : [];
  const count = (p: string): number => {
    const n = DIRECTORY[p];
    return n ? n.entries.length + n.children.reduce((a, c) => a + count(c), 0) : 0;
  };
  return {
    path,
    name: node.name,
    trail: parts.map((_, i) => {
      const p = parts.slice(0, i + 1).join('/');
      return { path: p, name: DIRECTORY[p]?.name ?? p };
    }),
    children: [
      ...node.children.map((c) => ({
        path: c,
        name: DIRECTORY[c]?.name ?? c,
        count: count(c),
        crosslink: false,
      })),
      ...(node.crosslinks ?? []).map((c) => ({
        path: c,
        name: DIRECTORY[c]?.name ?? c,
        count: count(c),
        crosslink: true,
      })),
    ].sort((a, b) => a.name.localeCompare(b.name)),
    entries: node.entries.map((id) => {
      const d = byId.get(id);
      const source = d?.source_id ?? id.slice(4).split('/')[0]!;
      return {
        title: d?.title ?? id,
        address: d ? address(d) : `wsp://${id.slice(4)}`,
        source_id: source,
        doc_id: id,
        description: DESCRIPTIONS[id] ?? null,
      };
    }),
  };
}

export function random(params: URLSearchParams): Response<'GET /v1/random'> | null {
  const source = params.get('source');
  const seed = params.get('seed');
  const pool = DOCS.filter(
    (d) => d.native_id !== 'Main_Page' && (!source || d.source_id === source),
  );
  const i = seed === null ? Math.floor(Math.random() * pool.length) : Number(seed) % pool.length;
  const d = pool[i];
  return d
    ? { doc_id: docId(d), source_id: d.source_id, title: d.title, address: address(d) }
    : null;
}

export function summarize(req: Routes['POST /v1/summarize']['body']): MockResult {
  const ids = (req.doc_ids ?? []).slice(0, 5);
  if (ids.length === 0)
    return err(422, 'invalid_request', 'doc_ids: List should have at least 1 item');
  const events: SummaryEvent[] = [];
  const say = (text: string) => {
    for (const w of text.split(/(?<= )/)) events.push({ type: 'token', text: w });
  };
  const cite = (index: number) => {
    const id = ids[index - 1]!;
    const d = byId.get(id);
    events.push({
      type: 'citation',
      index,
      doc_id: id,
      title: d?.title ?? id,
      address: d ? address(d) : `wsp://${id.slice(4)}`,
    });
  };
  const titles = ids.map((id) => byId.get(id)?.title ?? id);
  say(`Across the top results for “${req.query}”, the library's sources agree on the essentials. `);
  say(`${titles[0]} gives the most direct account `);
  cite(1);
  if (ids[1]) {
    say('. A second source adds supporting detail ');
    cite(2);
  }
  if (ids[2]) {
    say(', and a third covers it from a different angle ');
    cite(3);
  }
  say('. (This is the mock summarizer: it shows streaming and citations, not real synthesis.)');
  events.push({ type: 'done' });
  return {
    status: 200,
    stream: events.map((event, i) => ({ delayMs: i === 0 ? 300 : 35, event })),
  };
}

/** Route a request. `path` is the raw path (percent-encoding intact), without the query. */
export function handle(
  method: string,
  path: string,
  params: URLSearchParams,
  body?: unknown,
): MockResult {
  if (method === 'POST' && path === '/v1/summarize') {
    return summarize(body as Routes['POST /v1/summarize']['body']);
  }
  if (method === 'POST' && path === '/v1/gaps') {
    const gap = body as Routes['POST /v1/gaps']['body'] | undefined;
    if (!gap?.href) return err(422, 'invalid_request', 'href: Field required');
    GAPS.push(gap);
    return { status: 204 };
  }
  if (method !== 'GET') return err(405, 'method_not_allowed', method);

  const docMatch = /^\/v1\/documents\/([^/]+)(\/history)?$/.exec(path);
  if (docMatch) {
    let id: string;
    try {
      id = decodeURIComponent(docMatch[1]!);
    } catch {
      return err(400, 'invalid_doc_id', 'bad percent-encoding');
    }
    const r = docMatch[2] ? history(id) : document(id, params);
    return r ? { status: 200, json: r } : err(404, 'document_not_found', `no such document: ${id}`);
  }
  if (path.startsWith('/v1/documents/')) {
    return err(404, 'doc_id_not_encoded', 'put the doc id in ONE path segment (`/` as %2F)');
  }
  if (path.startsWith('/v1/assets/'))
    return err(404, 'asset_not_found', 'the mock holds no images');
  const sourceMatch = /^\/v1\/sources\/([^/]+)$/.exec(path);
  if (sourceMatch) {
    const s = sourceInfo(decodeURIComponent(sourceMatch[1]!));
    return s ? { status: 200, json: s } : err(404, 'source_not_found', 'unknown source');
  }
  const ok = (json: unknown): MockResult => ({ status: 200, json });
  switch (path) {
    case '/v1/health':
      return ok({ status: 'ok', api_version: API_VERSION, sources: SOURCES.length });
    case '/v1/search':
      return ok(search(params));
    case '/v1/suggest':
      return ok(suggest(params));
    case '/v1/sources': {
      const page: Response<'GET /v1/sources'> = { items: allSources(), next_cursor: null };
      return ok(page);
    }
    case '/v1/changes': {
      const r = changes(params);
      return 'status' in r ? r : ok(r);
    }
    case '/v1/changes/recent':
      return ok(recentChanges(params));
    case '/v1/changes/stream':
      return { status: 200, hold: true, retryMs: 60_000 };
    case '/v1/system':
      return ok(system());
    case '/v1/proposals':
      return ok({ items: [] });
    case '/v1/portal':
      return ok(portal());
    case '/v1/directory': {
      const r = directory(params);
      return r ? ok(r) : err(404, 'category_not_found', 'no such category');
    }
    case '/v1/random': {
      const r = random(params);
      return r ? ok(r) : err(404, 'empty', 'no pages to pick from');
    }
    default:
      return err(404, 'not_found', `No route ${path}`);
  }
}
