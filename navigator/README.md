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
npm run check      # API types fresh + typecheck + lint + tests + build + budget + smoke
```

| Script | What it does |
|---|---|
| `npm run dev` | Vite dev server with the mock API under `/v1` |
| `WSP_MOCK=0 npm run dev` | Dev server proxying `/v1` to a real daemon (`WSP_API`, default `http://127.0.0.1:8737`) |
| `npm run gen:api` | Regenerate `src/api/generated.ts` from `docs/api/openapi.json` |
| `npm run check:api` | Fail if `src/api/generated.ts` is stale |
| `npm run build` | Production bundle in `dist/` (CSP meta tag injected) |
| `npm run preview` | Serve `dist/` with the CSP header and the mock API |
| `npm run typecheck` | `tsc --noEmit`, strict settings |
| `npm run lint` / `npm run format` | Biome (lint + format) |
| `npm test` | Vitest (jsdom) |
| `npm run budget` | Gzip budget (JS < 150 KB, CSS < 40 KB) and offline/CSP checks on `dist/` |
| `npm run smoke` | Serves `dist/` + mock API on an ephemeral port and fetches every asset |
| `npm run e2e` | Playwright journeys in real Chromium against the built bundle + mock API ([docs/testing/e2e.md](../docs/testing/e2e.md)) |
| `npm run e2e:live` | The same journeys against the running library (`WSP_E2E_URL`, default `http://127.0.0.1:8737`) |
| `npm run e2e:report` | Open the last Playwright HTML report (`e2e-report/`) |

## The API contract

`docs/api/openapi.json` (exported from the FastAPI app) is the single source of
truth. `npm run gen:api` turns it into `src/api/generated.ts` with a small
dependency-free generator (`scripts/gen-api.mjs`; `openapi-typescript` needs the
TypeScript 5 compiler API, this project uses TypeScript 7). The mock API is
typed by the same file, so neither the client nor the mock can drift from the
spec without failing `npm run typecheck`, and `npm run check` fails while the
generated file is stale.

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
  api/generated.ts  wire types GENERATED from docs/api/openapi.json (do not edit)
  api/types.ts      UI names for the generated types, plus UI-only types
  api/client.ts     fetch client typed by the generated Routes table
  api/changes.ts    live change feed: EventSource, polling fallback
  core/             address parsing, router, history/hotlist, themes,
                    link rewriting + HTML sanitizer, highlighting, diff
  chrome/           menu bar, suggestions combobox, dialogs, icons/throbber
  pages/            one module per wsp:// site
  styles/           base chrome, theme token sets, page styles
mock/               mock API (handler + fixtures + Vite plugin)
scripts/            API type generator, bundle budget and smoke checks
tests/              Vitest unit + jsdom app tests
e2e/                Playwright end-to-end journeys (real browser)
```

See [`docs/components/navigator.md`](../docs/components/navigator.md) for the
design, the API contract and known gaps.
