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
// XML namespace identifiers are not fetched.
const ALLOWED_URLS = [/^http:\/\/www\.w3\.org\//];

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
const rows = [];

for (const f of files) {
  const rel = relative(DIST, f);
  const buf = readFileSync(f);
  const ext = f.split('.').pop();
  const gz = gzipSync(buf, { level: 9 }).length;
  if (ext === 'js') totals.js += gz;
  if (ext === 'css') totals.css += gz;
  rows.push([rel, buf.length, gz]);

  if (['js', 'css', 'html', 'svg', 'json'].includes(ext)) {
    const text = buf.toString('utf8');
    for (const m of text.matchAll(/\b(?:https?:)?\/\/[a-z0-9.-]+\.[a-z]{2,}[^\s"'`)]*/gi)) {
      const url = m[0];
      if (!/^https?:/i.test(url)) continue;
      if (ALLOWED_URLS.some((re) => re.test(url))) continue;
      // Fixture/example text in mock data never ships; anything else is a leak.
      errors.push(`${rel}: remote URL reference ${url}`);
    }
    if (ext === 'css' && /@import\s+url\(\s*['"]?https?:/i.test(text))
      errors.push(`${rel}: remote @import`);
    if (ext === 'js' && /\beval\s*\(|new\s+Function\s*\(/.test(text))
      errors.push(`${rel}: eval/new Function`);
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
if (totals.js > BUDGET.js) errors.push(`JS over budget: ${kb(totals.js)} > ${kb(BUDGET.js)}`);
if (totals.css > BUDGET.css) errors.push(`CSS over budget: ${kb(totals.css)} > ${kb(BUDGET.css)}`);

if (errors.length) {
  console.error(`\nBundle check FAILED:\n- ${errors.join('\n- ')}`);
  process.exit(1);
}
console.log('\nBundle check passed: within budget, no remote references, CSP-clean HTML.');
