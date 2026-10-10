/**
 * Framework-free mock of the Whisipedia API v1. Used by the Vite dev/preview
 * middleware (mock/vite-plugin.ts) and by tests. Deterministic except for
 * /v1/random and uptime.
 */
import type {
  ChangeEntry,
  ChangesResponse,
  DirectoryResponse,
  DocKind,
  DocumentResponse,
  HistoryResponse,
  InstantAnswer,
  PortalResponse,
  SearchHit,
  SearchResponse,
  SourcesResponse,
  Suggestion,
  SuggestResponse,
  SummarizeEvent,
  SummarizeRequest,
  SystemResponse,
} from '../src/api/types.ts';
import { DESCRIPTIONS, DIRECTORY, DOCS, type FixtureDoc, GENERATION, SOURCES } from './data.ts';

export type MockResult =
  | { status: number; json: unknown }
  | { status: 200; stream: Array<{ delayMs: number; event: SummarizeEvent }> };

const docId = (d: FixtureDoc) => `wsp:${d.source_id}/${d.native_id}`;
const byId = new Map(DOCS.map((d) => [docId(d), d]));
const sourceName = (id: string) => SOURCES.find((s) => s.id === id)?.name ?? id;

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

function terms(q: string): string[] {
  return q
    .toLowerCase()
    .split(/[^\p{L}\p{N}-]+/u)
    .filter((t) => t.length > 1 && !STOP.has(t));
}
const STOP = new Set(
  'a an and are as at be by for from how in is it of on or the to what with about does do define map books'.split(
    ' ',
  ),
);

function score(d: FixtureDoc, q: string, ts: string[]): number {
  const title = d.title.toLowerCase();
  const text = bodyText(d).toLowerCase();
  const aliases = (d.aliases ?? []).join(' | ');
  let s = 0;
  if (aliases.includes(q.toLowerCase())) s += 12;
  for (const t of ts) {
    if (title.includes(t)) s += 5;
    if (aliases.includes(t)) s += 3;
    const n = text.split(t).length - 1;
    s += Math.min(n, 5);
  }
  return s;
}

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
  const cps = Array.from(snip);
  const lowerCps = cps.map((c) => c.toLowerCase());
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
        { part_of_speech: 'adjective', definition: 'Given to using long words.' },
        {
          part_of_speech: 'adjective',
          definition: 'Of a word: long, having many syllables.',
          example: 'a sesquipedalian vocabulary',
        },
        { part_of_speech: 'noun', definition: 'A long word.' },
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
        { label: 'Born', value: '23 June 1912, Maida Vale, London' },
        { label: 'Died', value: '7 June 1954, Wilmslow' },
        { label: 'Known for', value: 'Turing machine, codebreaking, the imitation game' },
      ],
      doc_id: 'wsp:wikidata/Q7251',
      license: 'CC0-1.0',
    };
  }
  return null;
}

export function search(params: URLSearchParams): SearchResponse {
  const q = (params.get('q') ?? '').trim();
  const kind = params.get('kind') as DocKind | null;
  const source = params.get('source');
  const page = Math.max(1, Number(params.get('page') ?? 1) || 1);
  const pageSize = Math.min(50, Math.max(1, Number(params.get('page_size') ?? 10) || 10));
  const ts = terms(q);

  const scored = DOCS.filter((d) => d.native_id !== 'Main_Page')
    .map((d) => ({ d, s: score(d, q, ts) }))
    .filter((x) => x.s > 0 && (!source || x.d.source_id === source))
    .sort((a, b) => b.s - a.s || a.d.title.localeCompare(b.d.title));

  const facetsMap = new Map<DocKind, number>();
  for (const { d } of scored) facetsMap.set(d.kind, (facetsMap.get(d.kind) ?? 0) + 1);
  const filtered = kind ? scored.filter((x) => x.d.kind === kind) : scored;

  // Pad results with synthetic hits so paging is demoable for broad queries.
  const synthetic: SearchHit[] = [];
  if (filtered.length > 0 && !source && !kind) {
    for (let i = 1; i <= 25; i += 1) {
      const n = 2000 + i;
      synthetic.push({
        doc_id: `wsp:wikipedia-en/Fixture_${encodeURIComponent(q)}_${n}`,
        source_id: 'wikipedia-en',
        source_name: sourceName('wikipedia-en'),
        kind: 'encyclopedia',
        title: `${q.replace(/\b\w/g, (c) => c.toUpperCase())} (fixture result ${i})`,
        snippet: `A synthetic fixture page about ${q}. It exists so that paging, visited links and "not in library" pages can be tried in the mock.`,
        license: 'CC-BY-SA-4.0',
        as_of: '2026-10-01T00:00:00Z',
        lang: 'en',
        score: 0.1 / i,
      });
    }
  }

  const hits: SearchHit[] = filtered.map(({ d, s }) => {
    const sn = snippet(bodyText(d), ts.length ? ts : [q.toLowerCase()]);
    return {
      doc_id: docId(d),
      source_id: d.source_id,
      source_name: sourceName(d.source_id),
      kind: d.kind,
      title: d.title,
      snippet: sn.snippet,
      highlights: sn.highlights,
      license: d.license,
      as_of: d.versions[0]!.timestamp,
      lang: d.lang,
      score: s,
      url_native: d.url_native,
    };
  });
  const all = [...hits, ...synthetic];
  const start = (page - 1) * pageSize;

  const lq = q.toLowerCase();
  let navigate: string | null = null;
  const nav = /^(?:wikipedia|wiki)\s+(.+)$/.exec(lq);
  if (nav) {
    const d = DOCS.find((x) => x.source_id === 'wikipedia-en' && x.title.toLowerCase() === nav[1]);
    if (d) navigate = docId(d);
  }

  return {
    query: q,
    intent: /^define\b/.test(lq)
      ? 'define'
      : /\bmap\b/.test(lq)
        ? 'locate'
        : /^how\b/.test(lq)
          ? 'how-to'
          : navigate
            ? 'navigate'
            : 'lookup',
    total: all.length,
    page,
    page_size: pageSize,
    took_ms: 18 + (q.length % 23),
    generation_id: GENERATION,
    hits: all.slice(start, start + pageSize),
    facets: [...facetsMap.entries()].map(([k, count]) => ({ kind: k, count })),
    instant: page === 1 ? instantFor(q) : null,
    navigate_to: navigate,
    did_you_mean: /\bvacine/.test(lq) ? lq.replace(/vacine/g, 'vaccine') : null,
    low_confidence: hits.length === 0,
  };
}

export function suggest(params: URLSearchParams): SuggestResponse {
  const q = (params.get('q') ?? '').toLowerCase().trim();
  const limit = Number(params.get('limit') ?? 8) || 8;
  if (!q) return { query: q, suggestions: [] };
  const out: Suggestion[] = DOCS.filter(
    (d) =>
      d.native_id !== 'Main_Page' &&
      (d.title.toLowerCase().startsWith(q) ||
        d.title.toLowerCase().includes(` ${q}`) ||
        (d.aliases ?? []).some((a) => a.startsWith(q))),
  )
    .slice(0, limit)
    .map((d) => ({ text: d.title, doc_id: docId(d), kind: d.kind }));
  const phrases = DOCS.flatMap((d) => d.aliases ?? []).filter((a) => a.startsWith(q) && a !== q);
  for (const p of [...new Set(phrases)].slice(0, Math.max(0, limit - out.length)))
    out.push({ text: p });
  return { query: q, suggestions: out.slice(0, limit) };
}

function versionFor(d: FixtureDoc, at: string | null, version: string | null) {
  if (version) return d.versions.find((v) => v.revision === version) ?? null;
  if (at) return d.versions.find((v) => v.timestamp.slice(0, 10) <= at) ?? d.versions.at(-1)!;
  return d.versions[0]!;
}

export function document(id: string, params: URLSearchParams): DocumentResponse | null {
  const d = byId.get(id);
  if (!d) return null;
  const v = versionFor(d, params.get('at'), params.get('version'));
  if (!v) return null;
  const hash = Array.from(`${id}:${v.revision}`)
    .reduce((h, c) => (Math.imul(h, 31) + c.charCodeAt(0)) >>> 0, 7)
    .toString(16)
    .padStart(8, '0');
  return {
    doc_id: id,
    source_id: d.source_id,
    source_name: sourceName(d.source_id),
    native_id: d.native_id,
    kind: d.kind,
    title: d.title,
    lang: d.lang,
    url_native: d.url_native,
    license: d.license,
    revision: v.revision,
    timestamp: v.timestamp,
    content_hash: `b3:${hash.repeat(8)}`,
    generation_id: GENERATION,
    format: d.format,
    ...(v.html !== undefined ? { html: v.html, text: plain(v.html) } : {}),
    ...(v.text !== undefined ? { text: v.text } : {}),
    links: d.links,
    entities: d.entities,
    vault: {
      path: `vault/sources/${d.source_id}/${GENERATION}/${d.format === 'html' ? 'content.zim' : 'texts.parquet'}`,
      manifest: `vault/sources/${d.source_id}/${GENERATION}/MANIFEST.json`,
      sha256: `${hash}${'0'.repeat(56)}`,
      offset: (hash.charCodeAt(0) * 4096) % 1_000_000,
      length: (v.html ?? v.text ?? '').length,
    },
    has_history: d.versions.length > 1,
  };
}

export function history(id: string): HistoryResponse | null {
  const d = byId.get(id);
  if (!d) return null;
  return {
    doc_id: id,
    versions: d.versions.map((v, i) => ({
      content_hash: `b3:${v.revision}`,
      revision: v.revision,
      timestamp: v.timestamp,
      generation_id: `gen-${v.timestamp.slice(0, 10)}`,
      op: i === d.versions.length - 1 ? 'add' : 'modify',
    })),
  };
}

export function sources(): SourcesResponse {
  return { generation_id: GENERATION, sources: SOURCES };
}

const CHANGES: ChangeEntry[] = (() => {
  const base = Date.parse('2026-10-08T22:00:00Z');
  const out: ChangeEntry[] = [];
  let seq = 90_210;
  DOCS.forEach((d, i) => {
    out.push({
      seq: seq--,
      doc_id: docId(d),
      source_id: d.source_id,
      title: d.title,
      op: i % 4 === 0 ? 'add' : 'modify',
      content_hash: `b3:${d.versions[0]!.revision}`,
      timestamp: new Date(base - i * 5.5 * 3600_000).toISOString(),
      generation_id: GENERATION,
    });
  });
  out.splice(5, 0, {
    seq: 90_199,
    doc_id: 'wsp:wikipedia-en/Retracted_Hoax_Article',
    source_id: 'wikipedia-en',
    title: 'Retracted Hoax Article',
    op: 'tombstone',
    content_hash: null,
    timestamp: new Date(base - 26 * 3600_000).toISOString(),
    generation_id: GENERATION,
  });
  return out;
})();

export function changes(params: URLSearchParams): ChangesResponse {
  const limit = Math.min(200, Number(params.get('limit') ?? 50) || 50);
  const since = params.get('since');
  const startIdx = since ? CHANGES.findIndex((c) => String(c.seq) === since) + 1 : 0;
  const page = CHANGES.slice(startIdx, startIdx + limit);
  const last = page.at(-1);
  return {
    cursor: since,
    next_cursor: last && startIdx + limit < CHANGES.length ? String(last.seq) : null,
    changes: page,
  };
}

const started = Date.now() - 3 * 86400_000 - 4 * 3600_000;

export function system(): SystemResponse {
  const now = Date.now();
  const progress = ((now / 1000) % 600) / 600;
  return {
    version: '0.1.0-mock',
    generation_id: GENERATION,
    uptime_s: Math.floor((now - started) / 1000),
    freshness: SOURCES.map((s) => {
      const age = Math.max(
        0,
        Math.round((Date.parse('2026-10-09') - Date.parse(s.as_of)) / 86400_000),
      );
      return {
        source_id: s.id,
        as_of: s.as_of,
        slo_days: 35,
        age_days: age,
        status: s.status === 'offline' ? 'offline' : s.status,
      };
    }),
    scrub: {
      progress,
      bytes_verified: Math.floor(progress * 1.62e12),
      bytes_total: 1.62e12,
      last_completed: '2026-10-02T03:14:00Z',
      errors_found: 2,
      errors_repaired: 2,
    },
    degraded: [
      {
        path: 'vault/sources/us-code/gen-2026-08-20/usc42.xml',
        reason: 'Checksum mismatch on NAS copy; repair from parity scheduled',
        since: '2026-10-08T19:02:00Z',
      },
    ],
    disk: [
      { name: 'NAS cold vault', used_bytes: 1.62e12, total_bytes: 7.2e12 },
      { name: 'Local NVMe (indexes)', used_bytes: 0.31e12, total_bytes: 0.4e12 },
      { name: 'Scratch', used_bytes: 0.02e12, total_bytes: 0.1e12 },
    ],
    ledger_tail: [
      { ts: '2026-10-09T07:00:12Z', event: 'scrub.chunk', detail: 'osm-planet 412/980 OK' },
      {
        ts: '2026-10-09T06:45:03Z',
        event: 'index.promote',
        detail: `lexical ${GENERATION} nDCG@10 0.71 (+0.02)`,
      },
      { ts: '2026-10-08T22:00:00Z', event: 'acquire.done', detail: 'rfc +4 docs, 0 errors' },
      {
        ts: '2026-10-08T19:02:00Z',
        event: 'warden.degraded',
        detail: 'us-code usc42.xml checksum mismatch',
      },
      { ts: '2026-10-08T19:05:41Z', event: 'warden.repair', detail: 'parity rebuild queued' },
      {
        ts: '2026-10-07T04:10:00Z',
        event: 'acquire.done',
        detail: 'wikipedia-en incremental 11.0 GB',
      },
    ],
    latency_ms: { search_p50: 41, search_p95: 87 },
  };
}

export function portal(): PortalResponse {
  return {
    date: '2026-10-09',
    generation_id: GENERATION,
    featured: {
      doc_id: 'wsp:wikipedia-en/Alan_Turing',
      title: 'Alan Turing',
      blurb:
        'English mathematician and codebreaker whose 1936 paper described the machine that bears his name, and with it the limits of what can be computed.',
    },
    on_this_day: [
      {
        year: 1874,
        text: 'The General Postal Union (later the Universal Postal Union) is founded by the Treaty of Bern.',
      },
      { year: 1888, text: 'The Washington Monument opens to the public.' },
      { year: 1992, text: 'A meteorite lands on a parked car in Peekskill, New York.' },
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
  };
}

export function directory(params: URLSearchParams): DirectoryResponse | null {
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
      ...node.children.map((c) => ({ path: c, name: DIRECTORY[c]?.name ?? c, count: count(c) })),
      ...(node.crosslinks ?? []).map((c) => ({
        path: c,
        name: DIRECTORY[c]?.name ?? c,
        count: count(c),
        crosslink: true,
      })),
    ].sort((a, b) => a.name.localeCompare(b.name)),
    entries: node.entries.map((id) => {
      const d = byId.get(id);
      return {
        doc_id: id,
        title: d?.title ?? id,
        description: DESCRIPTIONS[id] ?? '',
        source_id: d?.source_id ?? id.slice(4).split('/')[0]!,
      };
    }),
  };
}

export function random(params: URLSearchParams): { doc_id: string; title: string } | null {
  const source = params.get('source');
  const pool = DOCS.filter(
    (d) => d.native_id !== 'Main_Page' && (!source || d.source_id === source),
  );
  const d = pool[Math.floor(Math.random() * pool.length)];
  return d ? { doc_id: docId(d), title: d.title } : null;
}

export function summarize(req: SummarizeRequest): MockResult {
  const ids = (req.doc_ids ?? []).slice(0, 5);
  if (ids.length === 0) {
    return { status: 400, json: { error: { code: 'bad_request', message: 'doc_ids is empty' } } };
  }
  const titles = ids.map((id) => byId.get(id)?.title ?? id);
  const events: SummarizeEvent[] = [];
  const say = (text: string) => {
    for (const w of text.split(/(?<= )/)) events.push({ type: 'token', text: w });
  };
  say(`Across the top results for “${req.query}”, the library's sources agree on the essentials. `);
  say(`${titles[0]} gives the most direct account `);
  events.push({ type: 'citation', index: 1, doc_id: ids[0]! });
  if (ids[1]) {
    say('. A second source adds supporting detail ');
    events.push({ type: 'citation', index: 2, doc_id: ids[1] });
  }
  if (ids[2]) {
    say(', and a third covers it from a different angle ');
    events.push({ type: 'citation', index: 3, doc_id: ids[2] });
  }
  say('. (This is the mock summarizer: it shows streaming and citations, not real synthesis.)');
  events.push({ type: 'done' });
  return {
    status: 200,
    stream: events.map((event, i) => ({ delayMs: i === 0 ? 300 : 35, event })),
  };
}

/** Route a request. `path` excludes the query string. */
export function handle(
  method: string,
  path: string,
  params: URLSearchParams,
  body?: unknown,
): MockResult {
  const nf = (msg: string): MockResult => ({
    status: 404,
    json: { error: { code: 'not_found', message: msg } },
  });
  if (method === 'POST' && path === '/v1/summarize') return summarize(body as SummarizeRequest);
  if (method !== 'GET')
    return { status: 405, json: { error: { code: 'method_not_allowed', message: method } } };

  const docMatch = /^\/v1\/documents\/([^/]+)(\/history)?$/.exec(path);
  if (docMatch) {
    let id: string;
    try {
      id = decodeURIComponent(docMatch[1]!);
    } catch {
      return nf('bad id');
    }
    const r = docMatch[2] ? history(id) : document(id, params);
    return r ? { status: 200, json: r } : nf(`No document ${id} in this library`);
  }
  switch (path) {
    case '/v1/search':
      return { status: 200, json: search(params) };
    case '/v1/suggest':
      return { status: 200, json: suggest(params) };
    case '/v1/sources':
      return { status: 200, json: sources() };
    case '/v1/changes':
      return { status: 200, json: changes(params) };
    case '/v1/system':
      return { status: 200, json: system() };
    case '/v1/portal':
      return { status: 200, json: portal() };
    case '/v1/directory': {
      const r = directory(params);
      return r ? { status: 200, json: r } : nf('No such category');
    }
    case '/v1/random': {
      const r = random(params);
      return r ? { status: 200, json: r } : nf('Empty source');
    }
    default:
      return nf(`No route ${path}`);
  }
}
