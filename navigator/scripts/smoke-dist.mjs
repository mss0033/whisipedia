#!/usr/bin/env node
import { readFile } from 'node:fs/promises';
// Smoke test for the built bundle: serve dist/ (as the daemon will, with the
// CSP header) plus the mock API on an ephemeral 127.0.0.1 port, then fetch
// index.html, every referenced asset and a few API routes.
import { createServer } from 'node:http';
import { extname, join, normalize } from 'node:path';
import { handle } from '../mock/handler.ts';

const DIST = new URL('../dist/', import.meta.url).pathname;
const CSP = "default-src 'self'; base-uri 'none'; object-src 'none'; form-action 'self'";
const TYPES = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.svg': 'image/svg+xml',
  '.json': 'application/json',
};

const server = createServer(async (req, res) => {
  const url = new URL(req.url ?? '/', 'http://127.0.0.1');
  if (url.pathname.startsWith('/v1/')) {
    let body;
    if (req.method === 'POST') {
      const chunks = [];
      for await (const c of req) chunks.push(c);
      body = JSON.parse(Buffer.concat(chunks).toString('utf8') || '{}');
    }
    const r = handle(req.method ?? 'GET', url.pathname, url.searchParams, body);
    res.writeHead(r.status, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify(r.json));
    return;
  }
  const rel = url.pathname === '/' ? 'index.html' : normalize(url.pathname).replace(/^[/\\]+/, '');
  if (rel.includes('..')) {
    res.writeHead(400).end();
    return;
  }
  try {
    const data = await readFile(join(DIST, rel));
    res.writeHead(200, {
      'Content-Type': TYPES[extname(rel)] ?? 'application/octet-stream',
      'Content-Security-Policy': CSP,
      'X-Content-Type-Options': 'nosniff',
    });
    res.end(data);
  } catch {
    res.writeHead(404).end('not found');
  }
});

await new Promise((r) => server.listen(0, '127.0.0.1', r));
const { port } = server.address();
const base = `http://127.0.0.1:${port}`;
const failures = [];
const check = (cond, msg) => {
  if (!cond) failures.push(msg);
};

try {
  const index = await fetch(`${base}/`);
  const html = await index.text();
  check(index.status === 200, `index.html status ${index.status}`);
  check(html.includes('Content-Security-Policy'), 'index.html lacks CSP meta');
  check(html.includes('id="app"'), 'index.html lacks #app root');
  const assets = [...html.matchAll(/(?:src|href)="(\/[^"]+)"/g)].map((m) => m[1]);
  check(
    assets.some((a) => a.endsWith('.js')),
    'no JS asset referenced',
  );
  check(
    assets.some((a) => a.endsWith('.css')),
    'no CSS asset referenced',
  );
  for (const a of assets) {
    const r = await fetch(base + a);
    const text = await r.text();
    check(r.status === 200, `${a} status ${r.status}`);
    check(text.length > 0, `${a} is empty`);
    if (a.endsWith('.js'))
      check(text.includes('wsp://'), `${a} does not look like the Navigator bundle`);
  }
  const api = [
    '/v1/sources',
    '/v1/search?q=vaccines',
    '/v1/suggest?q=tur',
    `/v1/documents/${encodeURIComponent('wsp:rfc/9110')}`,
    `/v1/documents/${encodeURIComponent('wsp:wikipedia-en-zim/Alan_Turing')}/history`,
    '/v1/changes',
    '/v1/changes/recent',
    '/v1/system',
    '/v1/portal',
    '/v1/directory',
    '/v1/random',
  ];
  for (const p of api) {
    const r = await fetch(base + p);
    check(r.status === 200, `${p} status ${r.status}`);
    await r.json();
  }
  console.log(
    `Smoke: index + ${assets.length} assets + ${api.length} API routes served from ${base}`,
  );
} catch (err) {
  failures.push(String(err));
} finally {
  server.close();
}

if (failures.length) {
  console.error(`Smoke test FAILED:\n- ${failures.join('\n- ')}`);
  process.exit(1);
}
console.log('Smoke test passed.');
