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
const HEADING_LINE =
  /^(?:(?:chapter|book|part|section|canto|act|scene|letter|stave|lecture)\b.{0,60}|[IVXLC]{1,7}\.?)$/i;
const PAUSE = Number(process.env.WSP_AUDIT_PAUSE ?? 500);
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
  [/<\/?(p|div|span|table|tr|td|li|ul|ol|h[1-6])(\s[^>]*)?>/i, 'raw HTML tags'],
  [/\{\{[A-Za-z][^}]{0,60}(\||\}\})/, 'wiki template'],
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
async function waitForLibrary() {
  for (let i = 0; i < 180; i++) {
    try {
      const r = await fetch(`${BASE}/v1/health`, {
        headers: HEADERS,
        signal: AbortSignal.timeout(10_000),
      });
      if (r.ok) return true;
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
async function samplesOf(source) {
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
      timeout: 20_000,
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
    .map((h) => decodeURIComponent(h.slice(1)));
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
    frame: main?.querySelector('.original-frame')?.getAttribute('src') ?? null,
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
  list.push({ dim, code, detail: String(detail).slice(0, 200) });
}

async function audit(context, phoneContext, id) {
  const issues = [];
  const address = addressOf(id);
  const started = Date.now();
  // --- reach: the API ------------------------------------------------------------------------------
  let doc = null;
  try {
    const r = await fetch(`${BASE}${docPath(id)}`, {
      headers: HEADERS,
      signal: AbortSignal.timeout(90_000),
    });
    if (!r.ok) add(issues, 'reach', 'api-status', `${r.status}`);
    else doc = await r.json();
  } catch (e) {
    add(issues, 'reach', 'api-timeout', e);
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
    // Once more after a reset: the server closes an idle keep-alive connection after 5 s, and reusing
    // it as it closes reads ECONNRESET; a browser retries such a GET by itself (24 images, 2026-10-19).
    const get = () =>
      context.request.get(`${BASE}${img.src}`, { timeout: 60_000 }).catch((e) => {
        if (/ECONNRESET|socket hang up|EPIPE/.test(String(e))) return 'reset';
        return null;
      });
    let r = await get();
    if (r === 'reset') r = await get();
    if (r === 'reset') r = null;
    if (!r?.ok()) add(issues, 'render', 'image-missing', `${r?.status() ?? 'timeout'} ${img.src}`);
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
    // page's code in a code table) are its words, not markup left over.
    const tag = what === 'raw HTML tags' ? /<\/?([a-z0-9]+)/i.exec(m?.[0] ?? '')?.[1] : null;
    // A whole template a page writes in its own text is its words (Wikipedia's portal pages discuss
    // "the {{Random portal component}}"); the Navigator removes the leftovers it knows.
    const written =
      what === 'wiki template' &&
      /^\{\{[^{}]+\}\}$/.test(m?.[0] ?? '') &&
      (doc?.html ?? '').includes(m?.[0] ?? '\u0000');
    if (m && !written && !(tag && (doc?.html ?? '').includes(`&lt;${tag}`)))
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
    const here = (await page.evaluate(() => decodeURIComponent(location.hash.slice(1)))).replace(
      /(%2F|\/)+$/i,
      '',
    );
    const others = page.locator('main .doc-body a[data-wsp]:not([data-missing])');
    const n = await others.count();
    let link = others.first();
    for (let i = 0; i < Math.min(n, 50); i++) {
      const at = decodeURIComponent((await others.nth(i).getAttribute('data-wsp')) ?? '');
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
    await page.keyboard.press('Control+]');
    await page.keyboard.press('Control+]');
    // A page of 1.8 million characters takes longer than 300 ms to lay out again (NGA's Sailing
    // Directions, 2026-10-19): wait for the size to change, three seconds at most.
    let bigger = await page.evaluate(inspect);
    for (let t = 0; t < 10 && bigger.fontPx !== null && bigger.fontPx <= seen.fontPx; t++) {
      await page.waitForTimeout(300);
      bigger = await page.evaluate(inspect);
    }
    if (bigger.fontPx !== null && bigger.fontPx <= seen.fontPx)
      add(issues, 'use', 'larger-text-ignored', `${seen.fontPx}px stays ${bigger.fontPx}px`);
    await page.keyboard.press('Control+0');
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
      const r = await context.request
        .get(href.startsWith('http') ? href : `${BASE}${href}`, {
          timeout: 60_000,
          headers: { Range: 'bytes=0-1023' },
        })
        .catch(() => null);
      if (!r?.ok())
        add(issues, 'use', 'download-fails', `${r?.status() ?? 'timeout'} ${href.slice(0, 120)}`);
      else if (type && !(r.headers()['content-type'] ?? '').startsWith(type.split(';')[0]))
        add(issues, 'use', 'download-wrong-type', `${r.headers()['content-type']} for ${type}`);
    }
    if (
      type === 'application/pdf' &&
      doc?.url_native?.includes('#page=') &&
      seen.frame &&
      !seen.frame.includes('#page=')
    )
      add(issues, 'use', 'pdf-opens-at-page-1', doc.url_native);
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
        codes.set(k, { n: 0, sources: new Set(), example: `${r.address} — ${i.detail}` });
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
  let n = 0;
  for (const s of sources) {
    if (!s.readable) {
      skipped.push({ id: s.id, why: 'not readable as documents (data, tiles or a history feed)' });
      continue;
    }
    let ids = await samplesOf(s).catch(() => null);
    if (ids === null || ids.length === 0) {
      await waitForLibrary();
      ids = (await samplesOf(s).catch(() => [])) ?? [];
    }
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
    for (const id of ids) {
      if (seen.has(id)) continue;
      const once = () =>
        audit(context, phoneContext, id).catch((e) => ({
          id,
          address: addressOf(id),
          source: s.id,
          issues: [{ dim: 'reach', code: 'audit-error', detail: String(e).slice(0, 200) }],
        }));
      let r = await once();
      // The library went away under the page (a restart): wait for it and look again.
      if (r.issues.some((i) => RESTARTED.test(i.detail))) {
        log(`library not answering; waiting, then ${addressOf(id)} again`);
        await waitForLibrary();
        r = await once();
      }
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
