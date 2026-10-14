#!/usr/bin/env node
/**
 * The trawler: opens library pages nobody has opened yet, the way a reader would, and records what
 * is wrong with them. Most of the library is never visited by its two readers; a page found broken
 * by hand, when it is finally needed, is the worst time to find it (owner, 2026-10-14).
 *
 * One page at a time in a headless Chromium through the real Navigator (the same bundle the API
 * serves), so it checks what a reader sees: the page loads without an error page or a script error,
 * it has content, its images resolve, the right viewer is there (the reader for a book, the player
 * for a film or a recording, the file panel for an original), and a catalog card links its book
 * directly, and a few of its links lead to pages the library holds. Pages are drawn at random from the
 * least-checked source first; each is checked once.
 *
 * Low impact by construction: every request carries `X-Whisipedia-Probe: check` (background, never
 * counted as the owner's use), it waits WSP_TRAWL_INTERVAL seconds between pages, and it pauses
 * entirely while the owner has used the library in the last WSP_TRAWL_YIELD seconds.
 *
 * Results: <state>/trawl/results.jsonl (one line a page) and summary.json (per source, and the
 * latest problems), read by `wsp trawl report`. Environment:
 *   WSP_TRAWL_URL       the library (default http://127.0.0.1:8737)
 *   WSP_TRAWL_STATE     Whisipedia's state dir (default ~/.local/share/whisipedia/state)
 *   WSP_TRAWL_OUT       where results go (default <state>/trawl)
 *   WSP_TRAWL_INTERVAL  seconds between pages (default 60)
 *   WSP_TRAWL_YIELD     seconds after the owner's last use before resuming (default 300)
 *   WSP_TRAWL_PAGES     stop after this many pages (default 0: run until stopped)
 *   WSP_TRAWL_SOURCE    only this source
 *   WSP_TRAWL_REPORT    a command run every hour (the unit: `wsp trawl report --alerts --quiet`)
 *   WSP_E2E_CHROMIUM    the Chromium to run (default: as the e2e tests pick it)
 */
import { execFile } from 'node:child_process';
import { createReadStream, existsSync, mkdirSync, readdirSync, statSync } from 'node:fs';
import { appendFile, rename, writeFile } from 'node:fs/promises';
import { homedir } from 'node:os';
import { join } from 'node:path';
import { createInterface } from 'node:readline';
import { chromium } from '@playwright/test';

const BASE = (process.env.WSP_TRAWL_URL ?? 'http://127.0.0.1:8737').replace(/\/+$/, '');
const STATE = process.env.WSP_TRAWL_STATE ?? join(homedir(), '.local/share/whisipedia/state');
const OUT = process.env.WSP_TRAWL_OUT ?? join(STATE, 'trawl');
const INTERVAL = Number(process.env.WSP_TRAWL_INTERVAL ?? 60) * 1000;
const YIELD = Number(process.env.WSP_TRAWL_YIELD ?? 300) * 1000;
const PAGES = Number(process.env.WSP_TRAWL_PAGES ?? 0);
const ONLY = process.env.WSP_TRAWL_SOURCE ?? '';
const HEADERS = { 'X-Whisipedia-Probe': 'check' };
const PAGE_TIMEOUT = 120_000; // a cold NAS page can take a minute
const IMAGES_CHECKED = 8;
const LINKS_CHECKED = 3; // in-library links followed per page (an existence check, not a visit)
// Pages drawn by the Navigator itself: their links are never documents.
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
]);
const CONTEXT_PAGES = 100; // a fresh browser context every so often bounds its memory
const RECENT_PROBLEMS = 200;
const REPORT = (process.env.WSP_TRAWL_REPORT ?? '').trim();
const REPORT_EVERY = 3_600_000;
const RANDOM_MISSES = 5; // draws that only hit checked pages before a source counts as done for now

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
    signal: AbortSignal.timeout(60_000),
  });
  if (!r.ok) throw new Error(`${path}: HTTP ${r.status}`);
  return r.json();
}

async function readableSources() {
  const out = [];
  let cursor = '';
  for (;;) {
    const page = await api(
      `/v1/sources?limit=200${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ''}`,
    );
    out.push(...page.items.filter((s) => s.readable && (!ONLY || s.id === ONLY)).map((s) => s.id));
    if (!page.next_cursor) return out;
    cursor = page.next_cursor;
  }
}

/** The doc ids checked before, and the summary, rebuilt from results.jsonl. */
async function load() {
  const seen = new Set();
  const summary = { updated: null, pages: 0, failed: 0, with_defects: 0, sources: {}, recent: [] };
  const path = join(OUT, 'results.jsonl');
  if (!existsSync(path)) return { seen, summary };
  const lines = createInterface({ input: createReadStream(path), crlfDelay: Infinity });
  for await (const line of lines) {
    try {
      const r = JSON.parse(line);
      seen.add(r.doc_id);
      tally(summary, r);
    } catch {
      /* a torn last line after a crash */
    }
  }
  return { seen, summary };
}

function tally(summary, r) {
  summary.pages += 1;
  summary.updated = r.ts;
  summary.sources[r.source] ??= { checked: 0, failed: 0, with_defects: 0, catalog_without_link: 0 };
  const s = summary.sources[r.source];
  s.checked += 1;
  if (!r.ok) {
    s.failed += 1;
    summary.failed += 1;
    summary.recent.push({ ts: r.ts, address: r.address, source: r.source, problems: r.problems });
    if (summary.recent.length > RECENT_PROBLEMS) summary.recent.shift();
  }
  if (r.defects?.length) {
    s.with_defects = (s.with_defects ?? 0) + 1;
    summary.with_defects = (summary.with_defects ?? 0) + 1;
  }
  if (r.catalog && !r.catalog.direct) s.catalog_without_link += 1;
}

/** Seconds since the owner last used the library (the API touches this marker). */
function ownerIdleMs() {
  try {
    return Date.now() - statSync(join(STATE, 'last-interactive')).mtimeMs;
  } catch {
    return Infinity;
  }
}

/** Open one page and say what is wrong with it. */
async function check(context, address) {
  const page = await context.newPage();
  const problems = []; // the page does not work: error page, empty, wrong viewer, script error
  const defects = []; // it works, with a missing image or a link into nothing
  const failedApi = [];
  const scriptErrors = [];
  let doc = null;
  page.on('pageerror', (e) => scriptErrors.push(String(e.message ?? e).slice(0, 200)));
  page.on('response', async (resp) => {
    const url = resp.url();
    if (!url.startsWith(`${BASE}/v1/`)) return;
    if (resp.status() >= 500 || (resp.status() === 404 && url.includes('/v1/assets/')))
      failedApi.push(`${resp.status()} ${url.slice(BASE.length, BASE.length + 160)}`);
    if (doc === null && url.includes('/v1/documents/') && resp.ok() && !url.includes('/history')) {
      try {
        doc = await resp.json();
      } catch {
        /* not JSON: the page check will say so */
      }
    }
  });
  const started = Date.now();
  try {
    await page.goto(`${BASE}/#${address}`, { timeout: PAGE_TIMEOUT });
    await page.waitForFunction(
      () => {
        const root = document.getElementById('app');
        const main = document.querySelector('main');
        // The "still loading" page retries by itself (a large archive opening, a restart): wait it out.
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
  } catch {
    problems.push('timeout');
  }
  const seen = await page
    .evaluate(() => {
      const main = document.querySelector('main');
      const body = main?.querySelector('.doc-body');
      const err = main?.querySelector('.page-error');
      const catalog = main?.querySelector('.catalog-notice');
      return {
        error: err ? (err.querySelector('h1, h2')?.textContent ?? 'error page').trim() : null,
        kind:
          /doc-kind-(\S+)/.exec(
            main?.querySelector('article.page-document')?.className ?? '',
          )?.[1] ?? null,
        text: (body?.textContent ?? '').trim().length,
        images: [...(body?.querySelectorAll('img') ?? [])].map((i) => i.getAttribute('src')),
        notInLibrary: main?.querySelectorAll('.broken-image').length ?? 0,
        reader: !!main?.querySelector('.reader'),
        player: !!main?.querySelector('.original-media .player'),
        fileLinks: !!main?.querySelector('.original-links'),
        media:
          main?.querySelectorAll(
            '.doc-body video, .doc-body audio, .doc-body iframe, .doc-body pre',
          ).length ?? 0,
        links: [...(main?.querySelectorAll('.doc-body a[data-wsp]:not([data-missing])') ?? [])]
          .map((a) => a.getAttribute('data-wsp') ?? '')
          .filter((x) => x.startsWith('wsp://')),
        brokenLinks: Number(
          /^(\d+)/.exec(main?.querySelector('.broken-note')?.textContent ?? '')?.[1] ?? 0,
        ),
        catalog: catalog
          ? {
              direct: [...catalog.querySelectorAll('a')].some((a) =>
                /^Read /.test(a.textContent ?? ''),
              ),
              says: (catalog.textContent ?? '').replace(/\s+/g, ' ').trim().slice(0, 160),
            }
          : null,
      };
    })
    .catch(() => null);
  if (!seen) problems.push('page-unreadable');
  else {
    if (seen.error) problems.push(`error-page: ${seen.error}`);
    const hasOriginal = !!doc?.original;
    if (
      !seen.error &&
      seen.text < 20 &&
      seen.images.length === 0 &&
      seen.media === 0 &&
      !hasOriginal
    )
      problems.push('empty');
    // The right viewer for what the document is.
    const type = doc?.original?.media_type ?? '';
    if (/^(video|audio)\//.test(type) && !seen.player) problems.push(`no-player (${type})`);
    else if (hasOriginal && !/^(video|audio)\//.test(type) && !seen.fileLinks && !seen.player)
      problems.push('no-file-panel');
    if (
      doc?.kind === 'books' &&
      doc?.format !== 'html' &&
      (doc?.text ?? '').length > 20000 &&
      !seen.reader
    )
      problems.push('no-reader');
    // Its images, a few of them, really there.
    for (const src of seen.images.slice(0, IMAGES_CHECKED)) {
      if (!src?.startsWith('/v1/')) continue;
      try {
        const r = await context.request.get(`${BASE}${src}`, { headers: HEADERS, timeout: 60_000 });
        const type = r.headers()['content-type'] ?? '';
        if (!r.ok() || !type.startsWith('image/'))
          defects.push(`image ${r.status()} ${src.slice(0, 120)}`);
      } catch {
        defects.push(`image-timeout ${src.slice(0, 120)}`);
      }
    }
  }
  // A few of its links into the library, at random: does the page they name exist? The server
  // turns every relative link of a ZIM page into a library address without checking it.
  const links = [...new Set(seen?.links ?? [])].filter(
    (a) => !BUILTIN.has(a.slice('wsp://'.length).split(/[/?#]/)[0] ?? ''),
  );
  let linksChecked = 0;
  for (const address of shuffle(links).slice(0, LINKS_CHECKED)) {
    linksChecked += 1;
    try {
      const r = await context.request.get(`${BASE}/go/${address.slice('wsp://'.length)}`, {
        headers: HEADERS,
        timeout: 60_000,
      });
      if (r.status() === 404) defects.push(`broken-link: ${address.slice(0, 160)}`);
      else if (!r.ok()) defects.push(`link ${r.status()} ${address.slice(0, 160)}`);
    } catch {
      defects.push(`link-timeout ${address.slice(0, 160)}`);
    }
  }
  for (const f of failedApi.slice(0, 5)) problems.push(`api ${f}`);
  for (const e of scriptErrors.slice(0, 3)) problems.push(`script-error: ${e}`);
  await page.close().catch(() => undefined);
  return {
    problems,
    defects,
    ms: Date.now() - started,
    kind: doc?.kind ?? seen?.kind ?? null,
    images: seen?.images.length ?? 0,
    not_in_library: seen?.notInLibrary ?? 0,
    broken_links: seen?.brokenLinks ?? 0,
    links_checked: linksChecked,
    catalog: seen?.catalog ?? null,
  };
}

function shuffle(items) {
  const a = [...items];
  for (let i = a.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1));
    [a[i], a[j]] = [a[j], a[i]];
  }
  return a;
}

/**
 * The next source to visit: the one with the fewest checked pages (ties at random), skipping those
 * that drew only checked pages today. Round robin spent as much on a 40-page site as on Wikipedia;
 * this spreads the first pages over every site, then keeps them level.
 */
function nextSource(sources, summary, exhausted) {
  const today = Date.now() - 86_400_000;
  const open = sources.filter((s) => (exhausted.get(s) ?? 0) <= today);
  if (open.length === 0) return null;
  const count = (s) => summary.sources[s]?.checked ?? 0;
  const least = Math.min(...open.map(count));
  const pool = open.filter((s) => count(s) === least);
  return pool[Math.floor(Math.random() * pool.length)];
}

/** The hourly report (alerts): a separate process, so a slow or failing one never stops the trawl. */
function report() {
  if (!REPORT) return;
  const [cmd, ...args] = REPORT.split(/\s+/);
  execFile(cmd, args, { timeout: 300_000 }, (err, stdout, stderr) => {
    const said = `${stdout}${stderr}`.trim();
    if (err) log(`trawl: report failed: ${String(err.message ?? err).slice(0, 200)}`);
    else if (said) log(`trawl: ${said}`);
  });
}

async function main() {
  mkdirSync(OUT, { recursive: true });
  const { seen, summary } = await load();
  log(`trawl: ${seen.size} pages checked before; ${BASE}; one page every ${INTERVAL / 1000}s`);
  const executablePath = chromiumExecutable();
  const browser = await chromium.launch({
    headless: true,
    ...(executablePath ? { executablePath } : {}),
    args: ['--disable-gpu', '--disable-dev-shm-usage', '--renderer-process-limit=1'],
  });
  const fresh = () =>
    browser.newContext({
      extraHTTPHeaders: HEADERS,
      serviceWorkers: 'block',
      viewport: { width: 1280, height: 900 },
    });
  let context = await fresh();
  let sources = await readableSources();
  let done = 0;
  const exhausted = new Map(); // source -> when it last only drew checked pages
  let stop = false;
  let reported = 0;
  for (const sig of ['SIGTERM', 'SIGINT']) process.on(sig, () => (stop = true));
  while (!stop && (PAGES === 0 || done < PAGES)) {
    if (ownerIdleMs() < YIELD) {
      await sleep(30_000); // the owner is using the library: wait
      continue;
    }
    if (sources.length === 0) {
      log('trawl: no readable sources');
      await sleep(600_000);
      sources = await readableSources().catch(() => []);
      continue;
    }
    if (done > 0 && done % 200 === 0) sources = await readableSources().catch(() => sources);
    const source = nextSource(sources, summary, exhausted);
    if (source === null) {
      log('trawl: every source drew only checked pages today; resting an hour');
      await sleep(3_600_000);
      continue;
    }
    let pick = null;
    for (let i = 0; i < RANDOM_MISSES && !pick; i++) {
      const r = await api(`/v1/random?source=${encodeURIComponent(source)}`).catch(() => null);
      if (r && !seen.has(r.doc_id)) pick = r;
      else if (!r) break;
    }
    if (!pick) {
      exhausted.set(source, Date.now());
      continue;
    }
    const result = await check(context, pick.address).catch((e) => ({
      problems: [`trawler-error: ${String(e).slice(0, 200)}`],
      ms: 0,
    }));
    const line = {
      ts: new Date().toISOString(),
      doc_id: pick.doc_id,
      address: pick.address,
      source,
      ok: result.problems.length === 0,
      ...result,
    };
    seen.add(pick.doc_id);
    tally(summary, line);
    await appendFile(join(OUT, 'results.jsonl'), `${JSON.stringify(line)}\n`);
    const tmp = join(OUT, 'summary.json.partial');
    await writeFile(tmp, `${JSON.stringify(summary, null, 1)}\n`);
    await rename(tmp, join(OUT, 'summary.json'));
    log(
      `${line.ok ? 'ok  ' : 'FAIL'} ${pick.address} (${line.ms} ms)${line.ok ? '' : ` ${line.problems.join('; ')}`}`,
    );
    done += 1;
    if (Date.now() - reported > REPORT_EVERY) {
      reported = Date.now();
      report();
    }
    if (done % CONTEXT_PAGES === 0) {
      await context.close().catch(() => undefined);
      context = await fresh();
    }
    await sleep(INTERVAL);
  }
  await browser.close();
  log(`trawl: stopped after ${done} page(s)`);
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
