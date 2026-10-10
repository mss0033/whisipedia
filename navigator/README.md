# Whisipedia Navigator

A retro web browser for the offline library: `wsp://` addresses, Back/Forward,
a Hotlist, a Yahoo!-style directory and the **WhizBang!** offline search
engine, styled after mid-90s browsers (PLAN §10).

Vanilla TypeScript, no runtime dependencies, built with Vite. The bundle makes
no external requests and runs under `Content-Security-Policy: default-src 'self'`.

## Quick start

```sh
cd navigator
npm ci
npm run dev        # http://127.0.0.1:5173 with the built-in mock API (no backend needed)
npm run check      # typecheck + lint + tests + build + bundle budget + smoke test
```

| Script | What it does |
|---|---|
| `npm run dev` | Vite dev server with the mock API under `/v1` |
| `WSP_MOCK=0 WSP_API=http://127.0.0.1:8740 npm run dev` | Dev server proxying `/v1` to a real daemon |
| `npm run build` | Production bundle in `dist/` (CSP meta tag injected) |
| `npm run preview` | Serve `dist/` with the CSP header and the mock API |
| `npm run typecheck` | `tsc --noEmit`, strict settings |
| `npm run lint` / `npm run format` | Biome (lint + format) |
| `npm test` | Vitest (jsdom) |
| `npm run budget` | Gzip budget (JS < 150 KB, CSS < 40 KB) and offline/CSP checks on `dist/` |
| `npm run smoke` | Serves `dist/` + mock API on an ephemeral port and fetches every asset |

## Serving from the daemon

The daemon serves `dist/` at `/` and the API at `/v1` on the same origin. It
should send the same policy the build embeds as a meta tag:

```
Content-Security-Policy: default-src 'self'; base-uri 'none'; object-src 'none'; form-action 'self'
```

The app routes inside the URL fragment (`/#wsp://rfc/9110`), so the daemon
only needs to serve `index.html` at `/`; no SPA fallback rules are required.

## Layout

```
src/
  api/types.ts      every API type the UI uses (align with OpenAPI here)
  api/client.ts     typed fetch client, SSE parser for /v1/summarize
  core/             address parsing, router, history/hotlist, themes,
                    link rewriting + HTML sanitizer, highlighting, diff
  chrome/           menu bar, suggestions combobox, dialogs, icons/throbber
  pages/            one module per wsp:// site
  styles/           base chrome, theme token sets, page styles
mock/               mock API (handler + fixtures + Vite plugin)
scripts/            bundle budget and smoke checks
tests/              Vitest unit + jsdom app tests
```

See [`docs/components/navigator.md`](../docs/components/navigator.md) for the
design, the API contract and known gaps.
