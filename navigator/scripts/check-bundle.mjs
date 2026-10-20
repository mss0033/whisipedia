#!/usr/bin/env node
// Bundle budget + offline guarantee checks for navigator/dist.
// - JS gzip total under budget, CSS gzip total under budget
// - no references to remote origins (CDNs, fonts, analytics)
// - no inline scripts/styles or eval in the shipped HTML/JS (CSP default-src 'self')
import { readdirSync, readFileSync, statSync } from 'node:fs';
import { join, relative } from 'node:path';
import { gzipSync } from 'node:zlib';

const DIST = new URL('../dist/', import.meta.url).pathname;
const BUDGET = { js: 150 * 1024, css: 40 * 1024 };
// The maps (MapLibre GL and its worker, the Atlas, the weather page) load only on wsp://atlas and
// wsp://weather: their own budget.
const MAP_CHUNK = /^assets\/(atlas-map|weather-view|maplibre|maplibre-gl-worker)-/;
const MAP_BUDGET = { js: 500 * 1024, css: 16 * 1024 };
// TeX typesetting (Temml) loads only for a page with formulas written as TeX: its own budget.
const TEX_CHUNK = /^assets\/(texrender|temml)-/;
const TEX_BUDGET = { js: 120 * 1024 };
// XML namespace identifiers are not fetched.
const ALLOWED_URLS = [/^http:\/\/www\.w3\.org\//];
// MapLibre's inert strings: licence and issue references in comments, the `.invalid` base it parses
// relative URLs against, an error message's help link, and its default attribution link (the Atlas
// replaces it). None is fetched.
const MAP_ALLOWED_URLS = [
  /^https:\/\/github\.com\/(maplibre|mapbox)\/[\w.-]+\/(blob|issues)\//,
  /^https:\/\/maplibre\.invalid\//,
  /^https:\/\/wiki\.openstreetmap\.org\/wiki\/This_map_requires_WebGL/,
  /^https:\/\/maplibre\.org\/$/,
];
// MapLibre's importScriptInWorkers() falls back to eval; the Atlas never calls it.
const MAP_ALLOWED_EVAL = /else globalThis\.eval\(\w\)\}/;

function walk(dir) {
  return readdirSync(dir).flatMap((name) => {
    const p = join(dir, name);
    return statSync(p).isDirectory() ? walk(p) : [p];
  });
}

let files;
try {
  files = walk(DIST);
} catch {
  console.error('dist/ not found: run `npm run build` first.');
  process.exit(1);
}

const errors = [];
const totals = { js: 0, css: 0 };
const mapTotals = { js: 0, css: 0 };
const texTotals = { js: 0 };
const rows = [];

for (const f of files) {
  const rel = relative(DIST, f);
  const buf = readFileSync(f);
  const ext = f.split('.').pop();
  const gz = gzipSync(buf, { level: 9 }).length;
  const isMap = MAP_CHUNK.test(rel);
  const isTex = TEX_CHUNK.test(rel);
  const sums = isMap ? mapTotals : totals;
  if (ext === 'js') (isTex ? texTotals : sums).js += gz;
  if (ext === 'css') sums.css += gz;
  rows.push([rel, buf.length, gz]);

  if (['js', 'css', 'html', 'svg', 'json'].includes(ext)) {
    const text = buf.toString('utf8');
    for (const m of text.matchAll(/\b(?:https?:)?\/\/[a-z0-9.-]+\.[a-z]{2,}[^\s"'`)]*/gi)) {
      const url = m[0];
      if (!/^https?:/i.test(url)) continue;
      if (ALLOWED_URLS.some((re) => re.test(url))) continue;
      if (isMap && MAP_ALLOWED_URLS.some((re) => re.test(url))) continue;
      // Fixture/example text in mock data never ships; anything else is a leak.
      errors.push(`${rel}: remote URL reference ${url}`);
    }
    if (ext === 'css' && /@import\s+url\(\s*['"]?https?:/i.test(text))
      errors.push(`${rel}: remote @import`);
    if (ext === 'js') {
      const code = isMap ? text.replace(MAP_ALLOWED_EVAL, '') : text;
      if (/\beval\s*\(|new\s+Function\s*\(/.test(code)) errors.push(`${rel}: eval/new Function`);
    }
    if (ext === 'html') {
      for (const m of text.matchAll(/<script\b([^>]*)>([\s\S]*?)<\/script>/gi)) {
        if (!/\bsrc=/.test(m[1]) || m[2].trim()) errors.push(`${rel}: inline <script>`);
      }
      if (/<style\b/i.test(text)) errors.push(`${rel}: inline <style>`);
      if (/\sstyle=/i.test(text)) errors.push(`${rel}: inline style attribute`);
      if (/\son[a-z]+=/i.test(text)) errors.push(`${rel}: inline event handler`);
      if (rel === 'index.html' && !/Content-Security-Policy/.test(text))
        errors.push(`${rel}: missing CSP meta`);
    }
  }
}

rows.sort((a, b) => b[2] - a[2]);
for (const [rel, raw, gz] of rows) {
  console.log(`${rel.padEnd(40)} ${String(raw).padStart(8)} B  ${String(gz).padStart(7)} B gz`);
}
const kb = (n) => `${(n / 1024).toFixed(1)} KB`;
console.log(`\nJS  total ${kb(totals.js)} gz (budget ${kb(BUDGET.js)})`);
console.log(`CSS total ${kb(totals.css)} gz (budget ${kb(BUDGET.css)})`);
console.log(`TeX JS ${kb(texTotals.js)} gz (budget ${kb(TEX_BUDGET.js)})`);
if (texTotals.js > TEX_BUDGET.js) errors.push(`TeX JS over budget: ${kb(texTotals.js)}`);
if (totals.js > BUDGET.js) errors.push(`JS over budget: ${kb(totals.js)} > ${kb(BUDGET.js)}`);
if (totals.css > BUDGET.css) errors.push(`CSS over budget: ${kb(totals.css)} > ${kb(BUDGET.css)}`);
console.log(
  `Map JS ${kb(mapTotals.js)} gz (budget ${kb(MAP_BUDGET.js)}), CSS ${kb(mapTotals.css)} gz (budget ${kb(MAP_BUDGET.css)})`,
);
if (mapTotals.js > MAP_BUDGET.js)
  errors.push(`map JS over budget: ${kb(mapTotals.js)} > ${kb(MAP_BUDGET.js)}`);
if (mapTotals.css > MAP_BUDGET.css)
  errors.push(`map CSS over budget: ${kb(mapTotals.css)} > ${kb(MAP_BUDGET.css)}`);

if (errors.length) {
  console.error(`\nBundle check FAILED:\n- ${errors.join('\n- ')}`);
  process.exit(1);
}
console.log('\nBundle check passed: within budget, no remote references, CSP-clean HTML.');
