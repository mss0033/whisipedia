# Component: Navigator

The Navigator is the browser-shaped front end described in PLAN §10. You browse
the library like the web in 1994–1999, and natural-language search is one site
inside that web. Code lives in [`navigator/`](../../navigator/).

## Design

- **Stack.** Vanilla TypeScript, Vite build, no runtime dependencies. A small
  hyperscript helper (`core/dom.ts`) builds the DOM directly. The app never
  uses `innerHTML`, inline styles or inline handlers, so it runs under
  `default-src 'self'`.
- **Offline guarantee.** No CDNs or web fonts. Themes use system font stacks
  that evoke each era. `scripts/check-bundle.mjs` fails the build if `dist/`
  contains a remote URL, an inline script or style, `eval`, or no CSP meta tag.
- **Addresses.** `wsp://<host>/<path>?<params>#<fragment>`. Built-in hosts are
  `home`, `directory`, `search`, `whatsnew`, `system`, `history`, `hotlist`,
  `about`, `help`, `random` and `offline`. Any other host is a source id, and
  `wsp://rfc/9110` maps 1:1 to `doc_id` `wsp:rfc/9110` (PLAN §9.2). Aliases such
  as `wikipedia.en` → `wikipedia-en-zim` and `maps` → `osm-planet` are accepted.
- **Location bar.** Accepts addresses, client-side shortcuts (`rfc 9110`,
  `gutenberg 84`, `Q42`, `arxiv:…`, `pmid:…`) and plain text. Plain text runs a
  search. If the search response has `navigate_to`, the Navigator jumps
  straight to that page ("wikipedia Alan Turing"). Internet URLs open a "not in
  library" page. The label reads *Netsite:* on built-in sites, *Location:* on
  documents and *Go to:* while you type, as in Netscape 3.
- **Navigation.** The app keeps its own session stack, mirrored into
  `history.pushState`, so the toolbar, Alt+Left/Right and the browser's own Back
  button agree. The address goes in the URL fragment, so reload and "open in
  new tab" work. Global history and the Hotlist live in `localStorage`, behind
  `SafeStorage`, which never throws.
- **Documents.** The client sanitizes API HTML with an allowlist (`core/links.ts`):
  scripts, styles, frames, forms, event handlers and remote images are dropped.
  Every link is rewritten into the offline web. The API's `links[]` resolution
  table wins. After that, `wsp:` links and relative ZIM-style links resolve
  locally, and anything else becomes a period broken link to `wsp://offline`.
  Plain-text sources (RFCs, Gutenberg) get autolinked RFC references.
- **Search (WhizBang!).** Search-as-you-type is debounced 50 ms and stale
  requests are aborted. The results page shows ten blue links, highlighted
  snippets, a green address line, source and license badges, an "as of" date,
  kind filters, paging, instant-answer boxes (definition, fact, map locator)
  and an opt-in streamed "Summarize these results" with citations back to
  result numbers.
- **Themes.** Navigator '96 (default), Mosaic '93, Encarta '95, Phosphor
  (amber or green, optional scanlines) and System 7. Each is a set of CSS
  custom properties keyed by `data-theme`. A high-contrast override works over
  any theme. Text scale is set with Ctrl+] and Ctrl+[. Motion follows the OS
  or can be forced to reduce or full; with reduced motion the throbber lights
  up instead of animating.
- **Accessibility.** Banner, menubar, toolbar, search, main and contentinfo
  landmarks. ARIA menubar keyboard model (F10, arrow keys, typeahead).
  Combobox suggestions. A skip link. Focus moves to the page heading after
  navigation, and a polite live region announces loaded pages. Focus rings are
  visible in every theme.

## Pages

| Address | Content |
|---|---|
| `wsp://home` | Portal: featured article, On This Day, Did You Know, What's New, random buttons, every site grouped by kind, visitor odometer |
| `wsp://directory/<path>` | Yahoo-style categories with counts, `@` cross-links and listings |
| `wsp://search` | WhizBang! home, results, instant answers, summary |
| `wsp://<source>` | Site front page: stats, health, site search, random page, related sites |
| `wsp://<source>/<id>` | Document in the period frame: site banner, webring bar, provenance footer, View Source Data, time machine (`?at=YYYY-MM-DD`, `&diff=1`) |
| `wsp://whatsnew` | Change feed grouped by day, NEW! badges, paging by cursor |
| `wsp://system` | System Monitor: generation, scrub, disk budget, freshness, degraded items, ledger tail; refreshes every 5 s |
| `wsp://history`, `wsp://hotlist` | Local history (filter, remove, clear) and bookmarks (reorder, delete) |
| `wsp://about`, `wsp://help` | About and the handbook, including keyboard shortcuts |

## API contract

The contract is [`docs/api/openapi.json`](../api/openapi.json) (ADR-0005). The
Navigator's wire types are generated from it into
[`navigator/src/api/generated.ts`](../../navigator/src/api/generated.ts) by
`npm run gen:api` (`scripts/gen-api.mjs`, no dependencies); `npm run check`
fails while that file is stale. The client and the mock API are typed by the
generated `Routes` table, so neither can drift from the spec. Summary:

| Endpoint | Notes |
|---|---|
| `GET /v1/search?q&sources&kind&limit&offset` | `SearchResults`: `hits[]` with source name, kind, license, `as_of`, plain-text snippet and code-point `highlights`; `facets`, `total`, `took_ms`, `intent`, `navigate_to` (location-bar jumps), `instant`, `did_you_mean`, `low_confidence`. The UI maps its 1-based pages to `offset` |
| `GET /v1/suggest?q&limit` | `{query, suggestions: string[]}` (titles) |
| `GET /v1/documents/{doc_id}?at=<generation>` | `doc_id` is ONE percent-encoded segment. `format: html\|text`, `html` (rewritten original), `text`, `links[{href,target}]`, `vault{generation_id,path,manifest,entry,size,sha256,blake3}` or null, `has_history` |
| `GET /v1/documents/{doc_id}/history` | `items[{generation_id, content_hash, revision, timestamp, kind}]`, oldest first. The time machine uses generation ids for `?at=` |
| `GET /v1/sources` | `SourcePage` (`items`, `next_cursor`); `health` drives the status lamp (`status` is the registry lifecycle) |
| `GET /v1/changes/recent`, `/v1/changes/stream`, `/v1/changes?since` | What's New: newest first, then live over SSE (EventSource), polling `/v1/changes` as the fallback |
| `GET /v1/system` | Freshness and health per site, degraded generations, ledger tail and `ledger_ok`, uptime |
| `POST /v1/summarize` | SSE events `token`, `citation`, `notice`, `done`, `error`. 404/501/503 mean "no local LLM", except `404 document_not_found` |
| `GET /v1/portal`, `/v1/directory?path=`, `/v1/random?source=` | Start page, directory and random page. The UI falls back to `/v1/sources` and the change feed on 404/501 (older daemons) |
| `POST /v1/gaps` | "Not in library" links and missing pages, reported once per session |
| Errors | Any non-2xx returns `{error:{code,message}}` |

Document images load only from same-origin `/v1/assets/...` paths: the API
rewrites ZIM image references there, and the sanitizer drops anything else.

## Performance

- The JS bundle is about 27 KB gzipped against a 150 KB budget. CSS is about 7 KB.
- A Vitest check holds rendering ten results to under 16 ms (median, jsdom).
- Documents are kept in an LRU so Back and Forward are instant. Suggestions are
  debounced 50 ms and cancelled when stale.

## Known gaps

- There are no bundled bitmap or pixel fonts yet; the themes use system stacks.
  If fonts are added, they must be open-licensed and ship with their license files.
- Maps show only a coordinate locator box. The MapLibre viewer with local tiles
  is still to come, as are the PDF and EPUB readers.
- The directory comes from registry domains and per-source categories; the
  Wikidata / Library of Congress / LCC trees are still to come. "On this day" and
  "Did you know" stay empty until a source for them is indexed.
- There is no Playwright suite. App flows are covered by jsdom tests, and real
  Chrome was checked by hand under the CSP.
