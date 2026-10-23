#!/usr/bin/env node
/**
 * The source audit: a sample of every source's documents, opened through the real Navigator the way a
 * reader would, and checked on five counts (owner, 2026-10-18: "reachable, renderable, navigable,
 * readable, and the UI offers all the functionality to manipulate the document for consumption").
 *
 *   reach     the document answers (API and page), within the page timeout, without an error page
 *   render    no script error or failed API call, images there, nothing wider than the window
 *             (desktop and phone), something to read or a viewer for the original
 *   read      the text is whole (not cut short against the API's text), not mojibake, no markup
 *             leaking through as text, legible (contrast) in the light and the dark theme
 *   navigate  a title, a way to the source's home, links that lead somewhere, anchors that exist, a
 *             book's next chapter and contents, a chapter's way to its whole book, Back that returns
 *   use       larger and smaller text reach the document's text, the dark theme reaches it, printing
 *             shows the document without the browser chrome, the original opens and downloads,
 *             a film or recording plays, and the browser's own Find is not taken away
 *
 * Unlike the trawler (scripts/trawl.mjs: one fresh page a minute, forever) this is a sweep: every
 * readable source, WSP_AUDIT_SAMPLES pages each (the source's home page first, then random ones), in
 * one run. Every request carries `X-Whisipedia-Probe: check`, so it never counts as the owner's use.
 *
 * Results: <out>/results.jsonl (a line a page) and <out>/report.md (per source and per check).
 * Environment:
 *   WSP_AUDIT_URL      the library (default http://127.0.0.1:8737)
 *   WSP_AUDIT_OUT      where results go (default ./audit-<date>)
 *   WSP_AUDIT_SAMPLES  pages per source (default 5)
 *   WSP_AUDIT_SOURCE   only these sources (comma-separated)
 *   WSP_AUDIT_PAUSE    milliseconds between pages (default 500)
 *   WSP_E2E_CHROMIUM   the Chromium to run (default: as the e2e tests pick it)
 */
import { existsSync, mkdirSync, readdirSync, readFileSync } from 'node:fs';
import { appendFile, writeFile } from 'node:fs/promises';
import { homedir } from 'node:os';
import { join } from 'node:path';
import { chromium } from '@playwright/test';

const BASE = (process.env.WSP_AUDIT_URL ?? 'http://127.0.0.1:8737').replace(/\/+$/, '');
const OUT = process.env.WSP_AUDIT_OUT ?? `audit-${new Date().toISOString().slice(0, 10)}`;
const SAMPLES = Number(process.env.WSP_AUDIT_SAMPLES ?? 5);
const ONLY = new Set(
  (process.env.WSP_AUDIT_SOURCE ?? '')
    .split(',')
    .map((s) => s.trim())
    .filter(Boolean),
);
// A line that heads a chapter of a book: "CHAPTER IV", "Book 2", "ACT III.", "XII.", "Canto 5".
// A Roman numeral heading is in capitals: "Cl." is a speaker (Cleinias) in Plato's dialogues, and a
// dialogue has no chapters to list (Epinomis, 2026-10-22).
const HEADING_LINE = {
  test: (line) =>
    /^(?:chapter|book|part|section|canto|act|scene|letter|stave|lecture)\b.{0,60}$/i.test(line) ||
    ROMAN.test(line),
};
// A well-formed numeral (not "VCC", a supply rail), and a single letter only with its full stop: a
// circuit's labels ("C", "V", "I") are lines of their own in Kuphaldt's exams (2026-10-23).
const ROMAN = /^(?=[IVXLC]{2,}\.?$|[IVXLC]\.$)C{0,3}(?:XC|XL|L?X{0,3})(?:IX|IV|V?I{0,3})\.?$/;
const PAUSE = Number(process.env.WSP_AUDIT_PAUSE ?? 500);
const BUSY_TRIES = 4; // = IMAGE_BUSY_TRIES in src/core/links.ts
const HEADERS = { 'X-Whisipedia-Probe': 'check' };
const PAGE_TIMEOUT = 120_000;
const DESKTOP = { width: 1280, height: 900 };
const PHONE = { width: 390, height: 844 };
const LINKS_CHECKED = 5;
const IMAGES_CHECKED = 6;
const RANDOM_TRIES = 8;
const MIN_CONTRAST = 4.5;
const BUILTIN = new Set([
  'home',
  'directory',
  'search',
  'whatsnew',
  'system',
  'history',
  'hotlist',
  'about',
  'help',
  'random',
  'offline',
  'practice',
  'screening-room',
  'define',
  'atlas',
]);
// Text that is the encoding gone wrong (UTF-8 read as Latin-1 or cp1252), not the language.
const MOJIBAKE = /Ã[\u0080-¿]|â€[\u0080-¿™œ"]|Â[ -¿]|�{2,}/;
// Markup a reader should never see as text.
const MARKUP = [
  [/&lt;\/?(p|div|span|br|a|b|i|em|strong|table|tr|td|li|ul|ol|h[1-6])\b/i, 'escaped HTML tags'],
  [/<\/?(p|div|span|table|tr|td|li|ul|ol|h[1-6])(\s[^>\n]*)?>/i, 'raw HTML tags'],
  // A template name starts with a capital: {{ca:a∈A}} is a set in a logic text (IEP, 2026-10-20).
  [/\{\{[A-Z][A-Za-z ]{0,60}(\||\}\})/, 'wiki template'],
  // A wiki title: a capital first, no comma (a chemical name in brackets is not one, eCFR 2026-10-19).
  [/\[\[(?:[A-Z][^\]|,]{0,80})(?:\|[^\]]{0,80})?\]\]/, 'wiki link'],
  [/&(?:amp;)?(?:nbsp|quot|#\d{2,5});/, 'escaped entities'],
];

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const log = (...a) => console.log(new Date().toISOString(), ...a);

function chromiumExecutable() {
  if (process.env.WSP_E2E_CHROMIUM) return process.env.WSP_E2E_CHROMIUM;
  try {
    if (existsSync(chromium.executablePath())) return undefined;
  } catch {
    /* fall through to the cache */
  }
  const cache = process.env.PLAYWRIGHT_BROWSERS_PATH || join(homedir(), '.cache', 'ms-playwright');
  if (!existsSync(cache)) return undefined;
  return readdirSync(cache)
    .map((d) => /^chromium-(\d+)$/.exec(d))
    .filter((m) => m !== null)
    .sort((a, b) => Number(b[1]) - Number(a[1]))
    .map((m) => join(cache, m[0], 'chrome-linux', 'chrome'))
    .find((p) => existsSync(p));
}

async function api(path) {
  const r = await fetch(`${BASE}${path}`, {
    headers: HEADERS,
    signal: AbortSignal.timeout(90_000),
  });
  if (!r.ok) throw new Error(`${path}: HTTP ${r.status}`);
  return r.json();
}

/** Wait (up to 15 minutes) for the library to answer again: a deploy restarts the API mid-audit, and
 * every page in that minute would otherwise count as unreachable (the first run lost 135 sources so). */
// Stopped (a shutdown, the memory guard): the page in hand is not recorded, its errors are the stop's
// (round 21i: an American Stories page "browser has been closed", a US Code page "connection
// refused"), and a rerun into the same round does it again (2026-10-23).
let stopping = false;
for (const sig of ['SIGTERM', 'SIGINT'])
  process.on(sig, () => {
    stopping = true;
    setTimeout(() => process.exit(143), 20_000).unref();
  });

async function waitForLibrary() {
  for (let i = 0; i < 180; i++) {
    try {
      const r = await fetch(`${BASE}/v1/health`, {
        headers: HEADERS,
        signal: AbortSignal.timeout(10_000),
      });
      // Up is not yet ready: a library just started answers health at once and pages "warming up"
      // (503) until its snapshot is built (round 21j: two pages of one book during a restart).
      const ready =
        r.ok &&
        (await fetch(`${BASE}/v1/sources?limit=1`, {
          headers: HEADERS,
          signal: AbortSignal.timeout(30_000),
        }).then((s) => s.ok));
      if (ready) return true;
    } catch {
      /* not up yet */
    }
    await sleep(5_000);
  }
  return false;
}

const RESTARTED = /ERR_CONNECTION_REFUSED|ECONNREFUSED|fetch failed|socket hang up/;

async function allSources() {
  const out = [];
  let cursor = '';
  for (;;) {
    const page = await api(
      `/v1/sources?limit=100${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ''}`,
    );
    out.push(...page.items);
    if (!page.next_cursor) return out;
    cursor = page.next_cursor;
  }
}

const docPath = (id) => `/v1/documents/${encodeURIComponent(id)}`;
/** A document's address, encoded as the library encodes it (wsp_api.addresses.encode_native): `%`, `?`,
 * `#` and spaces escaped, a trailing `/` as %2F. Unencoded, a native id with a query
 * (`viewcontent.cgi?article=1018`) opened another address, Not Found (2026-10-19). */
const addressOf = (id) => {
  const [source, ...rest] = id.slice('wsp:'.length).split('/');
  if (!rest.length) return `wsp://${source}`;
  const body = [...rest.join('/')]
    .map((c) => (/[%?#]|\s/.test(c) ? encodeURIComponent(c) : c))
    .join('');
  const stripped = body.replace(/\/+$/, '');
  return `wsp://${source}/${stripped}${'%2F'.repeat(body.length - stripped.length)}`;
};

/** The pages to audit for a source: its home page, then random ones (distinct). */
// Given pages instead of samples (doc ids, comma-separated), for going over a finding again with
// everything the audit does before its check: WSP_AUDIT_IDS=wsp:nga-nautical-pubs/... (with
// WSP_AUDIT_SOURCE naming their sources).
const IDS = (process.env.WSP_AUDIT_IDS ?? '')
  .split(',')
  .map((s) => s.trim())
  .filter(Boolean);
async function samplesOf(source) {
  if (IDS.length) return IDS.filter((id) => id.startsWith(`wsp:${source.id}/`));
  const ids = [];
  if (source.home_doc_id) ids.push(source.home_doc_id);
  for (let i = 0; ids.length < SAMPLES && i < SAMPLES * RANDOM_TRIES; i++) {
    const r = await api(`/v1/random?source=${encodeURIComponent(source.id)}`).catch(() => null);
    if (!r) break;
    if (!ids.includes(r.doc_id)) ids.push(r.doc_id);
  }
  return ids;
}

async function waitForPage(page) {
  await page.waitForFunction(
    () => {
      const root = document.getElementById('app');
      const main = document.querySelector('main');
      const retrying = /still loading/i.test(
        main?.querySelector('.page-error h1, .page-error h2')?.textContent ?? '',
      );
      return (
        root &&
        !root.classList.contains('is-loading') &&
        !retrying &&
        main?.querySelector('h1, .page-error')
      );
    },
    null,
    { timeout: PAGE_TIMEOUT, polling: 500 },
  );
  // A document's links that lead nowhere are marked once the library has said which (they are not
  // counted as broken: the reader is told before following them).
  await page
    .waitForSelector('[data-links-checked]:not([data-typesetting]), .page-error', {
      state: 'attached',
      // The reader asks again about links a busy library left unchecked, up to six rounds with
      // pauses of 3 to 15 s (2026-10-22): 20 s was shorter than that under load.
      timeout: 90_000,
    })
    .catch(() => {});
}

/** Everything about the page as the reader sees it (run in the page). */
function inspect() {
  const main = document.querySelector('main');
  const body = main?.querySelector('.doc-body');
  const err = main?.querySelector('.page-error');
  const parse = (c) => {
    const m = /rgba?\(([\d.]+),\s*([\d.]+),\s*([\d.]+)(?:,\s*([\d.]+))?\)/.exec(c ?? '');
    return m
      ? [Number(m[1]), Number(m[2]), Number(m[3]), m[4] === undefined ? 1 : Number(m[4])]
      : null;
  };
  const lum = ([r, g, b]) => {
    const f = (v) => {
      const s = v / 255;
      return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
    };
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b);
  };
  const backgroundOf = (el) => {
    for (let e = el; e; e = e.parentElement) {
      const c = parse(getComputedStyle(e).backgroundColor);
      if (c && c[3] > 0.5) return c;
    }
    return [255, 255, 255, 1];
  };
  // The first stretch of real reading text: a paragraph (or list item, cell) with words in it.
  const textEl =
    [...(body?.querySelectorAll('p, li, td, dd, pre, blockquote, div') ?? [])].find(
      (e) =>
        e.childElementCount < 6 &&
        (e.textContent ?? '').trim().length > 40 &&
        e.getClientRects().length > 0 &&
        getComputedStyle(e).visibility !== 'hidden',
    ) ?? null;
  let contrast = null;
  let fontPx = null;
  if (textEl) {
    const fg = parse(getComputedStyle(textEl).color);
    const bg = backgroundOf(textEl);
    if (fg) {
      const a = lum(fg) + 0.05;
      const b = lum(bg) + 0.05;
      contrast = Math.round((Math.max(a, b) / Math.min(a, b)) * 100) / 100;
    }
    fontPx = Number.parseFloat(getComputedStyle(textEl).fontSize);
  }
  const ids = new Set(
    [...document.querySelectorAll('[id], a[name]')].map((e) => e.id || e.getAttribute('name')),
  );
  const fragments = [...(body?.querySelectorAll('a[href^="#"]') ?? [])]
    .map((a) => a.getAttribute('href') ?? '')
    // The Navigator's own addresses are #wsp://...; a page anchor is #name.
    .filter((h) => h.length > 1 && !h.startsWith('#wsp:') && !h.startsWith('#/'))
    .map((h) => {
      // A page's anchor may carry a malformed escape (Wikipedia's Nintendo Australia, 2026-10-22):
      // compared as written then, as a browser would.
      try {
        return decodeURIComponent(h.slice(1));
      } catch {
        return h.slice(1);
      }
    });
  const banner = main?.querySelector('.site-banner a');
  return {
    error: err ? (err.querySelector('h1, h2')?.textContent ?? 'error page').trim() : null,
    // The page's title heading (an archived page can carry an empty logo <h1> above it).
    title: (
      (main?.querySelector('#doc-title') ?? main?.querySelector('h1'))?.textContent ?? ''
    ).trim(),
    text: (body?.innerText ?? '').trim(),
    emptyNotice: !!main?.querySelector('.empty-page-notice'),
    // The text outside code: a code sample showing HTML or [[...]] is the page's content, not markup
    // left over (Bootstrap's examples, a Python list comprehension).
    prose: (() => {
      const copy = body?.cloneNode(true);
      if (!copy) return '';
      for (const c of copy.querySelectorAll('pre, code, kbd, samp, textarea')) c.remove();
      return (copy.textContent ?? '').replace(/[ \t]+/g, ' ').trim();
    })(),
    images: [...(body?.querySelectorAll('img') ?? [])].map((i) => ({
      src: i.getAttribute('src'),
      broken: i.complete && i.naturalWidth === 0 && !!i.getAttribute('src'),
    })),
    links: [...(main?.querySelectorAll('.doc-body a[data-wsp]:not([data-missing])') ?? [])]
      .map((a) => a.getAttribute('data-wsp') ?? '')
      .filter((x) => x.startsWith('wsp://')),
    missingAnchors: [...new Set(fragments.filter((f) => !ids.has(f)))].slice(0, 5),
    anchors: fragments.length,
    home: banner ? (banner.getAttribute('data-wsp') ?? banner.getAttribute('href') ?? '') : null,
    reader: !!main?.querySelector('.reader'),
    toc: main?.querySelectorAll('.reader-toc li').length ?? 0,
    readerNext: [...(main?.querySelectorAll('.reader-nav a, .reader-nav button') ?? [])]
      .map((a) => (a.textContent ?? '').trim())
      .filter((t) => /next|›|»|→/i.test(t)),
    player: !!main?.querySelector('.original-media .player'),
    fileLinks: [...(main?.querySelectorAll('.original-links a') ?? [])].map((a) => ({
      text: (a.textContent ?? '').trim(),
      href: a.getAttribute('href') ?? '',
      download: a.hasAttribute('download'),
    })),
    // Where the PDF opens: the frame once shown, else the link that opens it in a new window (the
    // frame waits for the reader's click, 2026-10-22).
    frame:
      main?.querySelector('.original-frame')?.getAttribute('src') ??
      main?.querySelector('.original-links a[target="_blank"]')?.getAttribute('href') ??
      null,
    showOriginal: !!main?.querySelector('button.original-show'),
    media:
      main?.querySelectorAll('.doc-body video, .doc-body audio, .doc-body iframe, .doc-body pre')
        .length ?? 0,
    allLinks: [...(main?.querySelectorAll('a') ?? [])].map((a) => ({
      text: (a.textContent ?? '').trim().slice(0, 80),
      wsp: a.getAttribute('data-wsp') ?? '',
      href: a.getAttribute('href') ?? '',
    })),
    overflow: document.documentElement.scrollWidth - window.innerWidth,
    contrast,
    fontPx,
    hasText: !!textEl,
  };
}

function add(list, dim, code, detail = '') {
  // One line: a detail quoting a page's text keeps its line breaks, and they broke the report's
  // table (Wiktionary's character box, 2026-10-22).
  list.push({ dim, code, detail: String(detail).replace(/\s+/g, ' ').trim().slice(0, 200) });
}

// What a failed fetch() met: Node's "fetch failed" keeps the socket's error in `cause`.
const causeOf = (e) => {
  const c = e?.cause;
  return c ? `${c.code ?? c.name ?? ''} ${c.message ?? ''}`.trim() : String(e?.name ?? '');
};

// A GET as a browser makes it: once more after a reset. The server closes an idle keep-alive
// connection after 5 s, and reusing it as it closes reads ECONNRESET; a browser retries such a GET by
// itself (24 images on 2026-10-19, 2 CIA downloads reported as "timeout" on 2026-10-20). `status` is
// the HTTP status, or what went wrong when no answer came.
async function getAsBrowser(context, url, headers = {}) {
  let failure = '';
  let busy = 0;
  for (let attempt = 0; attempt < 2; attempt++) {
    try {
      // As long as a browser waits for a file to start: the library holds a request up to 120 s for
      // a transfer slot before it answers busy (wsp_api.files.TRANSFER_WAIT).
      const r = await context.request.get(url, { timeout: 150_000, headers });
      // Busy (503) is not an answer: asked again after its Retry-After, as the reader asks for a
      // busy picture again (IMAGE_BUSY_TRIES in src/core/links.ts). A file and a picture came 503
      // while a 44 GB archive was being profiled (2026-10-23).
      if (r.status() === 503 && busy < BUSY_TRIES) {
        busy += 1;
        attempt -= 1;
        await sleep(1000 * Math.min(30, Number(r.headers()['retry-after']) || 5));
        continue;
      }
      return { ok: r.ok(), status: r.status(), headers: r.headers() };
    } catch (e) {
      failure = String(e).split('\n')[0].slice(0, 80);
      if (!/ECONNRESET|socket hang up|EPIPE/.test(failure)) break;
    }
  }
  return { ok: false, status: /timeout/i.test(failure) ? 'timeout' : failure, headers: {} };
}

async function audit(context, phoneContext, id) {
  const issues = [];
  const address = addressOf(id);
  const started = Date.now();
  // --- reach: the API ------------------------------------------------------------------------------
  let doc = null;
  try {
    // A 503 (an archive still opening, the storage slow for a moment) is retried as the reader
    // retries it, after its Retry-After; only a page that stays busy is reported.
    let r = null;
    let resets = 0;
    for (let attempt = 0; attempt < 4; attempt++) {
      try {
        r = await fetch(`${BASE}${docPath(id)}`, {
          headers: HEADERS,
          signal: AbortSignal.timeout(90_000),
        });
      } catch (e) {
        // A kept-alive connection the server closed as this reused it (it closes an idle one after
        // 5 s): a browser sends such a GET again by itself, and so does this, once (2026-10-22:
        // three "fetch failed" in 500 pages, the library up throughout).
        if (resets++ === 0 && /ECONNRESET|UND_ERR_SOCKET|EPIPE|other side closed/.test(causeOf(e)))
          continue;
        throw e;
      }
      if (r.status !== 503 || attempt === 3) break;
      await sleep(1000 * Math.min(30, Number(r.headers.get('retry-after')) || 5));
    }
    if (!r.ok) add(issues, 'reach', 'api-status', `${r.status}`);
    else doc = await r.json();
  } catch (e) {
    add(issues, 'reach', 'api-timeout', `${e} (${causeOf(e)})`);
  }
  const page = await context.newPage();
  const scriptErrors = [];
  const failedApi = [];
  page.on('pageerror', (e) => scriptErrors.push(String(e.message ?? e)));
  page.on('response', (resp) => {
    const url = resp.url();
    if (url.startsWith(`${BASE}/v1/`) && resp.status() >= 500 && resp.status() !== 503)
      failedApi.push(`${resp.status()} ${url.slice(BASE.length, BASE.length + 140)}`);
  });
  let seen = null;
  try {
    await page.goto(`${BASE}/#${address}`, { timeout: PAGE_TIMEOUT });
    await waitForPage(page);
    await page.waitForTimeout(800); // images, the reader, a late script
    seen = await page.evaluate(inspect);
  } catch (e) {
    add(issues, 'reach', 'page-timeout', e.message ?? e);
  }
  const result = {
    id,
    address,
    source: id.slice(4).split('/')[0],
    kind: doc?.kind ?? null,
    format: doc?.format ?? null,
  };
  if (!seen) {
    await page.close().catch(() => undefined);
    return { ...result, issues, ms: Date.now() - started };
  }
  if (seen.error) add(issues, 'reach', 'error-page', seen.error);
  const original = doc?.original ?? null;
  const type = original?.media_type ?? '';
  const isMedia = /^(video|audio)\//.test(type);
  // --- render ---------------------------------------------------------------------------------------
  for (const e of [...new Set(scriptErrors)].slice(0, 3)) add(issues, 'render', 'script-error', e);
  for (const f of [...new Set(failedApi)].slice(0, 3)) add(issues, 'render', 'api-5xx', f);
  if (
    !seen.error &&
    !seen.emptyNotice && // the reader says the archive holds no text for it (an upstream gap)
    // A two-word dictionary entry is all there is (an HTML page's emptiness the reader says itself).
    (doc?.format === 'html' || (doc?.text ?? '').trim().length >= 40) &&
    seen.text.length < 20 &&
    seen.images.length === 0 &&
    seen.media === 0 &&
    !original
  )
    add(issues, 'render', 'empty');
  if (seen.overflow > 2)
    add(issues, 'render', 'overflow-desktop', `${seen.overflow}px wider than the window`);
  const broken = seen.images.filter((i) => i.broken).map((i) => i.src);
  if (broken.length) add(issues, 'render', 'image-broken', `${broken.length}: ${broken[0]}`);
  for (const img of seen.images.slice(0, IMAGES_CHECKED)) {
    if (!img.src?.startsWith('/v1/') || img.broken) continue;
    const r = await getAsBrowser(context, `${BASE}${img.src}`);
    // A picture the archive does not hold is the reader's placeholder ("[IMAGE: caption]") once it
    // has asked: only one still shown as a broken picture counts (2026-10-21: LibreTexts' figures
    // saved as error pages, now a 404 by design).
    const shown = async () =>
      page
        .evaluate((src) => !!document.querySelector(`main img[src="${CSS.escape(src)}"]`), img.src)
        .catch(() => false);
    // A lazy picture below the fold is asked for only when it is scrolled to (LibreTexts' figures,
    // 2026-10-22): bring it into view, as a reader would.
    if (r.status === 404)
      await page
        .evaluate(
          (src) => document.querySelector(`main img[src="${CSS.escape(src)}"]`)?.scrollIntoView(),
          img.src,
        )
        .catch(() => undefined);
    let still = r.status === 404 ? await shown() : true;
    for (let t = 0; t < 10 && still && r.status === 404; t++) {
      await page.waitForTimeout(500);
      still = await shown();
    }
    if (!r.ok && still) add(issues, 'render', 'image-missing', `${r.status} ${img.src}`);
  }
  // --- read -----------------------------------------------------------------------------------------
  const apiText = (doc?.text ?? '').trim();
  if (
    !seen.reader &&
    apiText.length > 2000 &&
    seen.text.length < apiText.length * 0.5 &&
    doc?.format !== 'html'
  )
    add(issues, 'read', 'cut-short', `${seen.text.length} of ${apiText.length} characters shown`);
  const sample = seen.text.slice(0, 60_000);
  const moji = MOJIBAKE.exec(sample);
  // Characters the archived page itself holds are its own (a pasted systeminfo listing with
  // U+FFFD in it, the OPSEC Bible, 2026-10-19): only what the reader garbled counts.
  if (moji && !(doc?.html ?? doc?.text ?? '').includes(moji[0]))
    add(issues, 'read', 'mojibake', sample.slice(Math.max(0, moji.index - 30), moji.index + 30));
  const prose = (seen.prose ?? seen.text).slice(0, 60_000);
  for (const [re, what] of MARKUP) {
    const m = re.exec(prose);
    // Tags the archived page writes as text (&lt;h1&gt;: How to Design Programs shows an example
    // page's code in a code table; Energypedia's &lt;/pre&gt; an editor left) are its words, not
    // markup the reader failed to render.
    const tag = what === 'raw HTML tags' ? /<\/?([a-z0-9]+)/i.exec(m?.[0] ?? '')?.[1] : null;
    // A whole template a page writes in its own text is its words (Wikipedia's portal pages discuss
    // "the {{Random portal component}}"); the Navigator removes the leftovers it knows.
    const written =
      what === 'wiki template' &&
      /^\{\{[^{}]+\}\}$/.test(m?.[0] ?? '') &&
      (doc?.html ?? '').includes(m?.[0] ?? '\u0000');
    // A plain-text document's text is all its own words (OCR of a CIA page reads "<li ee g a!").
    const escaped =
      tag &&
      (doc?.html
        ? new RegExp(`&lt;/?${tag}\\b`, 'i').test(doc.html)
        : (doc?.text ?? '').includes(m?.[0] ?? '\u0000'));
    // Any markup a plain-text document holds verbatim is its words: the KiCad manual prints a sample
    // info.html, `0.1&quot;` and all (2026-10-21).
    // Compared with runs of white space as one: the shown text joins the document's \r\n (CIA OCR,
    // "<p aneetmeeren…", 2026-10-21).
    const flat = (t) => t.replace(/\s+/g, ' ');
    const own = !doc?.html && m && flat(doc?.text ?? '').includes(flat(m[0]).trim());
    // An entity the archived page writes as text is its words: Wiktionary's character box shows a
    // character's code, "&#23553;", from &amp;#23553; in its HTML (封, 2026-10-22).
    const entity =
      what === 'escaped entities' &&
      (doc?.html ?? '').includes((m?.[0] ?? '\u0000').replace(/^&/, '&amp;'));
    if (m && !written && !escaped && !own && !entity)
      add(
        issues,
        'read',
        'markup-shown',
        `${what}: ${prose.slice(Math.max(0, m.index - 20), m.index + 50)}`,
      );
  }
  if (seen.contrast !== null && seen.contrast < MIN_CONTRAST)
    add(issues, 'read', 'low-contrast-light', `${seen.contrast}:1`);
  // --- navigate -------------------------------------------------------------------------------------
  if (!seen.error && !seen.title) add(issues, 'navigate', 'no-title');
  if (!seen.error && !seen.home) add(issues, 'navigate', 'no-source-link');
  if (seen.missingAnchors.length)
    add(
      issues,
      'navigate',
      'anchor-missing',
      `${seen.missingAnchors.length}: #${seen.missingAnchors[0]}`,
    );
  const links = [...new Set(seen.links)].filter(
    (a) => !BUILTIN.has(a.slice('wsp://'.length).split(/[/?#]/)[0] ?? ''),
  );
  let linksBroken = 0;
  for (const a of links.sort(() => Math.random() - 0.5).slice(0, LINKS_CHECKED)) {
    const go = () =>
      context.request
        .get(`${BASE}/go/${a.slice('wsp://'.length)}`, { timeout: 60_000, maxRedirects: 0 })
        .catch(() => null);
    const r = (await go()) ?? (await go()); // once more: a cold archive can miss the first minute
    if (!r) add(issues, 'navigate', 'link-timeout', a);
    else if (r.status() === 404) {
      linksBroken += 1;
      if (linksBroken === 1) add(issues, 'navigate', 'link-broken', a);
    } else if (r.status() >= 500) add(issues, 'navigate', 'link-error', `${r.status()} ${a}`);
  }
  // A chapter of a longer document leads to the whole of it and to its neighbours (the API's parts).
  const parts = doc?.parts ?? null;
  if (parts && parts.index !== null) {
    const shown = new Set(seen.allLinks.map((l) => l.wsp));
    if (!shown.has(parts.whole.address))
      add(issues, 'navigate', 'no-whole-document-link', `part of ${parts.whole.address}`);
    const neighbours = [parts.parts[parts.index - 1], parts.parts[parts.index + 1]].filter(Boolean);
    if (neighbours.some((n) => !shown.has(n.address)))
      add(issues, 'navigate', 'no-chapter-neighbours', 'no previous/next part link');
  } else if (parts && !parts.parts.every((p) => seen.allLinks.some((l) => l.wsp === p.address)))
    add(issues, 'navigate', 'parts-not-listed', `${parts.parts.length} parts`);
  // A book in the reader: contents and a next chapter that works.
  if (seen.reader && doc?.kind === 'books' && apiText.length > 20000) {
    // Contents are owed to a text with chapters: a single speech of 40,000 characters has none to
    // list (2026-10-19: Perseus's Cicero speeches).
    const headings = apiText.split('\n').filter((l) => HEADING_LINE.test(l.trim())).length;
    if (seen.toc === 0 && headings >= 3)
      add(issues, 'navigate', 'reader-no-contents', `${headings} heading lines`);
    if (seen.toc > 1) {
      const chapterText = () =>
        page.evaluate(() =>
          (document.querySelector('main .reader-chapter')?.textContent ?? '').trim().slice(0, 300),
        );
      const before = await chapterText();
      const next = page
        .locator('.reader-nav a, .reader-nav button')
        .filter({ hasText: /next|›|»|→/i })
        .first();
      const lastPart = !!parts && parts.index === parts.parts.length - 1; // nothing follows it
      if ((await next.count()) === 0) {
        if (!lastPart) add(issues, 'navigate', 'reader-no-next');
      } else {
        const was = await page.evaluate(() => location.hash);
        // The reader scrolls inside its own pane; Playwright's scroll into view missed a link four
        // screens down there, so the click landed on nothing (2026-10-19). A reader scrolls to it.
        await next.scrollIntoViewIfNeeded({ timeout: 10_000 }).catch(() => undefined);
        await next.click({ timeout: 10_000 }).catch(() => undefined);
        // The next chapter can be the next part, a document of its own: wait for it, not 600 ms
        // (2026-10-19: ten "Next does nothing" findings were the next part still loading).
        await page
          .waitForFunction((h) => location.hash !== h, was, { timeout: 10_000 })
          .catch(() => undefined);
        await waitForPage(page).catch(() => undefined);
        const after = await chapterText();
        // A part of a long document is not shown in a .reader-chapter: Next leading to the next part
        // (a new address) has done something.
        const moved = (await page.evaluate(() => location.hash)) !== was;
        if (after === before && !moved)
          add(
            issues,
            'navigate',
            'reader-next-does-nothing',
            `${was} ${(await next.textContent()) ?? ''}`,
          );
        await page.goBack({ timeout: 30_000 }).catch(() => undefined);
      }
    }
  }
  // Back returns: follow one in-library link, then Back.
  if (links.length && !seen.error) {
    const title = seen.title;
    // A link that leads elsewhere: a site's logo links its own front page, and following that adds no
    // history entry, so Back left the app (2026-10-19: 9 front pages reported an empty title).
    const here = (
      await page.evaluate(() => {
        try {
          return decodeURIComponent(location.hash.slice(1));
        } catch {
          return location.hash.slice(1);
        }
      })
    ).replace(/(%2F|\/)+$/i, '');
    const others = page.locator('main .doc-body a[data-wsp]:not([data-missing])');
    const n = await others.count();
    let link = others.first();
    for (let i = 0; i < Math.min(n, 50); i++) {
      const raw = (await others.nth(i).getAttribute('data-wsp')) ?? '';
      let at = raw;
      try {
        at = decodeURIComponent(raw);
      } catch {
        // a malformed escape: compared as written
      }
      if (
        at.replace(/(%2F|\/)+$/i, '') !== here &&
        (await others
          .nth(i)
          .isVisible()
          .catch(() => false))
      ) {
        link = others.nth(i);
        break;
      }
    }
    if ((await link.count()) > 0 && (await link.isVisible().catch(() => false))) {
      const from = await page.evaluate(() => location.hash);
      await link.click({ timeout: 10_000 }).catch(() => undefined);
      // As in any browser, a page still loading has no history entry yet: Back then leaves the page
      // before. Back is judged only once the link's page is in (2026-10-19: a slow Wikipedia page).
      const arrived = await page
        .waitForFunction((h) => location.hash !== h, from, { timeout: PAGE_TIMEOUT })
        .then(() => true)
        .catch(() => false);
      if (arrived) {
        await waitForPage(page).catch(() => undefined);
        await page.goBack({ timeout: 30_000 }).catch(() => undefined);
        await waitForPage(page).catch(() => undefined);
        const t = await page
          .evaluate(() =>
            (
              (document.querySelector('main #doc-title') ?? document.querySelector('main h1'))
                ?.textContent ?? ''
            ).trim(),
          )
          .catch(() => '');
        if (t !== title)
          add(
            issues,
            'navigate',
            'back-does-not-return',
            `"${t.slice(0, 60)}" after Back, was "${title.slice(0, 60)}"`,
          );
      } else {
        await page.goto(`${BASE}/${from}`).catch(() => undefined); // back to the page under audit
        await waitForPage(page).catch(() => undefined);
      }
    }
  }
  // --- use ------------------------------------------------------------------------------------------
  if (seen.hasText && seen.fontPx) {
    // Measured again here, after the Back above, from the page with nothing focused: twice in 2,000
    // pages the size "stayed" while the shortcut worked in every try by hand (NGA, 2026-10-21). A
    // second try before it counts.
    const larger = async () => {
      await page.evaluate(() =>
        document.activeElement instanceof HTMLElement ? document.activeElement.blur() : undefined,
      );
      const before = (await page.evaluate(inspect)).fontPx ?? seen.fontPx;
      await page.keyboard.press('Control+]');
      await page.keyboard.press('Control+]');
      // A page of 1.8 million characters takes longer than 300 ms to lay out again (NGA's Sailing
      // Directions, 2026-10-19): wait for the size to change, three seconds at most.
      let after = await page.evaluate(inspect);
      for (let t = 0; t < 10 && after.fontPx !== null && after.fontPx <= before; t++) {
        await page.waitForTimeout(300);
        after = await page.evaluate(inspect);
      }
      // What a failure needs to be told apart: the keys not arriving (focus, scale unchanged) or the
      // scale changing and the measured text not following it.
      const state = await page.evaluate(
        () =>
          `focus ${document.activeElement?.tagName ?? 'none'}, scale ${document.documentElement.style.getPropertyValue('--font-scale') || '1'}`,
      );
      await page.keyboard.press('Control+0');
      return { before, after: after.fontPx, state };
    };
    let size = await larger();
    if (size.after !== null && size.after <= size.before) size = await larger();
    if (size.after !== null && size.after <= size.before)
      add(
        issues,
        'use',
        'larger-text-ignored',
        `${size.before}px stays ${size.after}px (${size.state})`,
      );
  }
  const findBlocked = await page.evaluate(() => {
    const e = new KeyboardEvent('keydown', {
      key: 'f',
      ctrlKey: true,
      bubbles: true,
      cancelable: true,
    });
    document.activeElement?.dispatchEvent(e) ?? document.dispatchEvent(e);
    return e.defaultPrevented;
  });
  if (findBlocked) add(issues, 'use', 'find-blocked', 'Ctrl+F is taken from the browser');
  // Print: the document without the browser chrome.
  await page.emulateMedia({ media: 'print' });
  const printed = await page.evaluate(() => {
    const vis = (sel) => {
      const e = document.querySelector(sel);
      if (!e) return null;
      const s = getComputedStyle(e);
      return s.display !== 'none' && s.visibility !== 'hidden' && e.getClientRects().length > 0;
    };
    return {
      chrome: vis('.toolbar') || vis('.menubar') || vis('nav.menubar'),
      page: vis('main article') ?? vis('main'),
    };
  });
  await page.emulateMedia({ media: 'screen' });
  if (printed.chrome) add(issues, 'use', 'print-shows-chrome');
  if (printed.page === false) add(issues, 'use', 'print-hides-document');
  // The original: opens and downloads.
  if (original && !seen.error) {
    if (isMedia && !seen.player) add(issues, 'use', 'no-player', type);
    if (!isMedia && seen.fileLinks.length === 0) add(issues, 'use', 'no-file-panel');
    const dl = seen.fileLinks.find((l) => l.download);
    if (!isMedia && !dl) add(issues, 'use', 'no-download');
    const href = dl?.href || (original.url ? `${BASE}${original.url}` : null);
    if (href) {
      // The first bytes only, as a browser's download or viewer starts it (a range GET, not HEAD).
      const r = await getAsBrowser(context, href.startsWith('http') ? href : `${BASE}${href}`, {
        Range: 'bytes=0-1023',
      });
      if (!r.ok) add(issues, 'use', 'download-fails', `${r.status} ${href.slice(0, 120)}`);
      else if (type && !(r.headers['content-type'] ?? '').startsWith(type.split(';')[0]))
        add(issues, 'use', 'download-wrong-type', `${r.headers['content-type']} for ${type}`);
    }
    if (
      type === 'application/pdf' &&
      doc?.url_native?.includes('#page=') &&
      seen.frame &&
      !seen.frame.includes('#page=')
    )
      add(issues, 'use', 'pdf-opens-at-page-1', doc.url_native);
    // The viewer the reader asks for comes up, at the same page (after every keyboard check above:
    // Chrome's PDF viewer may take the keyboard).
    if (seen.showOriginal) {
      await page
        .locator('main button.original-show')
        .click({ timeout: 10_000 })
        .catch(() => undefined);
      const shown = await page
        .locator('main .original-frame')
        .first()
        .getAttribute('src', { timeout: 10_000 })
        .catch(() => null);
      if (!shown) add(issues, 'use', 'original-not-shown');
      else if (seen.frame && shown !== seen.frame)
        add(issues, 'use', 'original-shown-elsewhere', `${shown} (link: ${seen.frame})`);
    }
  }
  // Dark theme: the document's own text follows it and stays legible.
  if (seen.hasText) {
    await page.emulateMedia({ colorScheme: 'dark' });
    await page.waitForTimeout(300);
    const dark = await page.evaluate(inspect);
    if (dark.contrast !== null && dark.contrast < MIN_CONTRAST)
      add(issues, 'read', 'low-contrast-dark', `${dark.contrast}:1`);
    await page.emulateMedia({ colorScheme: 'light' });
  }
  await page.close().catch(() => undefined);
  // Phone width: nothing wider than the screen.
  const phone = await phoneContext.newPage();
  try {
    await phone.goto(`${BASE}/#${address}`, { timeout: PAGE_TIMEOUT });
    await waitForPage(phone);
    await phone.waitForTimeout(600);
    const over = await phone.evaluate(
      () => document.documentElement.scrollWidth - window.innerWidth,
    );
    if (over > 2) add(issues, 'render', 'overflow-phone', `${over}px wider than a 390px screen`);
  } catch {
    add(issues, 'reach', 'phone-timeout');
  }
  await phone.close().catch(() => undefined);
  return {
    ...result,
    issues,
    ms: Date.now() - started,
    text_chars: seen.text.length,
    api_chars: apiText.length,
    images: seen.images.length,
    links: links.length,
    reader: seen.reader,
    original: type || null,
  };
}

const DIMS = ['reach', 'render', 'read', 'navigate', 'use'];

function report(sources, results, skipped) {
  const by = new Map();
  for (const r of results) {
    if (!by.has(r.source)) by.set(r.source, []);
    by.get(r.source).push(r);
  }
  const codes = new Map();
  for (const r of results)
    for (const i of r.issues) {
      const k = `${i.dim}:${i.code}`;
      if (!codes.has(k))
        codes.set(k, {
          n: 0,
          sources: new Set(),
          example: `${r.address} — ${i.detail}`.replaceAll('|', '\\|'),
        });
      const c = codes.get(k);
      c.n += 1;
      c.sources.add(r.source);
    }
  const clean = results.filter((r) => r.issues.length === 0).length;
  const lines = [
    `# Source audit — ${new Date().toISOString().slice(0, 16).replace('T', ' ')} UTC`,
    '',
    `${results.length} pages from ${by.size} sources; ${clean} with no issue. Samples per source: ${SAMPLES}.`,
    '',
    '## Issues by check',
    '',
    '| Check | Pages | Sources | Example |',
    '|---|--:|--:|---|',
    ...[...codes.entries()]
      .sort(
        (a, b) =>
          DIMS.indexOf(a[0].split(':')[0]) - DIMS.indexOf(b[0].split(':')[0]) || b[1].n - a[1].n,
      )
      .map(
        ([k, c]) => `| ${k} | ${c.n} | ${c.sources.size} | ${c.example.replace(/\|/g, '\\|')} |`,
      ),
    '',
    '## Per source',
    '',
    `| Source | Pages | ${DIMS.join(' | ')} |`,
    `|---|--:|${DIMS.map(() => ':-:').join('|')}|`,
  ];
  for (const s of sources) {
    const rs = by.get(s.id);
    if (!rs) continue;
    const cell = (d) => {
      const n = rs.filter((r) => r.issues.some((i) => i.dim === d)).length;
      return n === 0 ? 'ok' : `**${n}**`;
    };
    lines.push(`| ${s.id} | ${rs.length} | ${DIMS.map(cell).join(' | ')} |`);
  }
  if (skipped.length) {
    lines.push('', '## Not audited as documents', '');
    for (const s of skipped) lines.push(`- ${s.id}: ${s.why}`);
  }
  lines.push('', '## Every issue', '');
  for (const r of results)
    for (const i of r.issues) lines.push(`- ${r.address} — **${i.dim}:${i.code}** ${i.detail}`);
  return `${lines.join('\n')}\n`;
}

async function main() {
  mkdirSync(OUT, { recursive: true });
  const executablePath = chromiumExecutable();
  const browser = await chromium.launch({
    headless: true,
    ...(executablePath ? { executablePath } : {}),
    args: ['--disable-gpu', '--disable-dev-shm-usage', '--renderer-process-limit=2'],
  });
  const make = (viewport) =>
    browser.newContext({ extraHTTPHeaders: HEADERS, serviceWorkers: 'block', viewport });
  let context = await make(DESKTOP);
  let phoneContext = await make(PHONE);
  const sources = (await allSources()).filter(
    (s) => s.current_generation && (!ONLY.size || ONLY.has(s.id)),
  );
  // A rerun into the same directory continues: pages already in results.jsonl are kept, not redone.
  const results = [];
  const seen = new Set();
  try {
    for (const line of readFileSync(join(OUT, 'results.jsonl'), 'utf8').split('\n')) {
      if (!line.trim()) continue;
      const r = JSON.parse(line);
      results.push(r);
      seen.add(r.id);
    }
  } catch {
    /* a first run */
  }
  const skipped = [];
  // Samples are drawn at random: a rerun (the memory guard stopped the round and started it again,
  // 2026-10-23) takes only what each source still lacks, not a second full sample.
  const have = new Map();
  for (const r of results) have.set(r.source, (have.get(r.source) ?? 0) + 1);
  let n = 0;
  for (const s of sources) {
    if (stopping) break;
    const owed = SAMPLES - (have.get(s.id) ?? 0);
    if (owed <= 0) continue;
    if (!s.readable) {
      skipped.push({ id: s.id, why: 'not readable as documents (data, tiles or a history feed)' });
      continue;
    }
    let ids = await samplesOf(s).catch(() => null);
    // A library just started opens its archives cold, for minutes: ask again, a few times, before
    // saying a source has no page (English Wikipedia "found nothing" after a reboot, 2026-10-23).
    for (let t = 0; t < 3 && (ids === null || ids.length === 0) && !stopping; t++) {
      await waitForLibrary();
      await sleep(60_000);
      ids = (await samplesOf(s).catch(() => [])) ?? [];
    }
    if (stopping) break;
    if (ids.length === 0 && have.get(s.id)) continue; // it gave pages before: this is the moment
    if (ids.length === 0) {
      skipped.push({ id: s.id, why: 'no page to draw (random and home both empty)' });
      results.push({
        id: `wsp:${s.id}`,
        address: `wsp://${s.id}`,
        source: s.id,
        issues: [{ dim: 'reach', code: 'no-pages', detail: '/v1/random found nothing' }],
      });
      continue;
    }
    for (const id of ids.filter((i) => !seen.has(i)).slice(0, owed)) {
      const once = () =>
        audit(context, phoneContext, id).catch((e) => ({
          id,
          address: addressOf(id),
          source: s.id,
          issues: [{ dim: 'reach', code: 'audit-error', detail: String(e).slice(0, 200) }],
        }));
      let r = await once();
      if (stopping) break; // the browser is going too: no retries, no fresh contexts
      // The library went away under the page (a restart): wait for it and look again.
      // Gone, or still warming up after its retries (a library restarted under the page).
      const gone = r.issues.find(
        (i) => RESTARTED.test(i.detail) || (i.code === 'api-status' && i.detail === '503'),
      );
      if (gone) {
        // The detail says which request failed and how (a restart, a reset, a refused connection).
        log(
          `library not answering (${gone.dim}:${gone.code} ${gone.detail}); waiting, then ${addressOf(id)} again`,
        );
        await waitForLibrary();
        r = await once();
      }
      // The browser's tab crashed (the audit's own browser short of memory, 2026-10-20): fresh
      // contexts and the page once more; a page that crashes a tab twice is reported.
      if (
        r.issues.some((i) =>
          /Target crashed|Page crashed|Target page, context or browser has been closed/.test(
            i.detail,
          ),
        )
      ) {
        log(`browser tab crashed; ${addressOf(id)} again in fresh contexts`);
        await context.close().catch(() => undefined);
        await phoneContext.close().catch(() => undefined);
        context = await make(DESKTOP);
        phoneContext = await make(PHONE);
        r = await once();
      }
      // A timeout once is not yet the library's: the audit runs in the background slice, which
      // tools/wsp-yield freezes while the owner reads, and the browser's clock runs on through a
      // freeze (earth2studio's intro, 5 frozen minutes, a phone-timeout; 2026-10-22). Once more,
      // and a page that times out twice is reported.
      const slow = r.issues.find((i) => /-timeout$/.test(i.code));
      if (slow) {
        log(`${slow.dim}:${slow.code}; ${addressOf(id)} once more`);
        r = await once();
      }
      if (stopping) break; // the page in hand is the stop's, not the library's
      r.source = s.id;
      results.push(r);
      await appendFile(join(OUT, 'results.jsonl'), `${JSON.stringify(r)}\n`);
      log(
        `${r.issues.length ? 'ISSUES' : 'ok    '} ${r.address} ${r.issues.map((i) => `${i.dim}:${i.code}`).join(' ')}`,
      );
      n += 1;
      if (n % 60 === 0) {
        await context.close().catch(() => undefined);
        await phoneContext.close().catch(() => undefined);
        context = await make(DESKTOP);
        phoneContext = await make(PHONE);
        await writeFile(join(OUT, 'report.md'), report(sources, results, skipped));
      }
      await sleep(PAUSE);
    }
  }
  await writeFile(join(OUT, 'report.md'), report(sources, results, skipped));
  await browser.close();
  log(`audit: ${results.length} pages; report ${join(OUT, 'report.md')}`);
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
