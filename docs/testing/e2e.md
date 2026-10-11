# End-to-end tests (Playwright)

The Navigator's end-to-end suite drives a real Chromium the way a person does:
it types in the location bar, clicks links, presses Tab, opens menus, and checks
what is on screen. It lives in [`navigator/e2e/`](../../navigator/e2e/) and uses
[Playwright](https://playwright.dev/) (`@playwright/test`).

Unit and jsdom tests (`npm test`) cover the pieces. This suite checks that the
pieces work together in a real browser, under the real Content Security Policy.

## Running it

```sh
cd navigator
npm ci
npm run e2e          # mock target: offline, deterministic (this is what CI runs)
npm run e2e:live     # live target: the library running on http://127.0.0.1:8737
npm run e2e:report   # open the last HTML report (navigator/e2e-report/)
```

Useful extras (anything after `--` goes to `playwright test`):

```sh
npm run e2e -- search            # only files matching "search"
npm run e2e -- -g "Hotlist"      # only tests whose title matches
npm run e2e -- --headed          # watch it work
npm run e2e -- --retries=0       # see flakiness instead of hiding it behind a retry
npm run e2e -- --repeat-each=3   # flakiness hunt
WSP_E2E_URL=http://127.0.0.1:8737 npx playwright test --ui   # interactive runner
```

Both scripts run the browser under `nice -n 10` with at most two workers, so the
machine stays usable.

### The browser

The config picks Chromium in this order:

1. `WSP_E2E_CHROMIUM=/path/to/chrome`, if set.
2. The build this Playwright version expects, if installed
   (`npx playwright install chromium`; CI does this).
3. The newest `chromium-*` already in the Playwright cache
   (`~/.cache/ms-playwright`, or `PLAYWRIGHT_BROWSERS_PATH`), so a developer
   machine never has to download another browser.

## Targets

| Target | How | Serves | Use it for |
|---|---|---|---|
| `mock` (default) | `npm run e2e` | `npm run build`, then `vite preview` on port 4179 (`WSP_E2E_PORT`) with the CSP header and the built-in mock API (`navigator/mock/`) | CI, every change to the Navigator. No backend, no network, same data every run |
| `live` | `npm run e2e:live`, or `WSP_E2E_URL=<url> npx playwright test` | The running daemon, which serves the built Navigator at `/` and the API at `/v1` | Checking the real library: ZIM rendering, images, ranking, latency |

The mock target tests the **production bundle** (`dist/`), not the dev server,
so the CSP meta tag and header are the real ones.

The live target is **read-only**. It only sends GETs, plus `POST /v1/gaps` when a
journey opens a missing page (harmless: it logs a gap for the gap miner). A
global setup (`e2e/support/warm-up.ts`) wakes the library first, and timeouts
are longer (30 s per assertion, 2 min per test), because first loads from the NAS
can be slow.

Some tests are target-specific: `live-zim.spec.ts` needs real ZIM archives and
skips on the mock; a few mock-fixture tests (time machine, broken-link fixture)
skip on live. Queries and pages per target are in `e2e/support/data.ts`.

## What every test checks for free

`e2e/fixtures.ts` adds guards to every test (auto fixtures). A test fails if,
at any point:

- **Offline guarantee:** the page requests anything outside the app's origin.
  The request is aborted, so nothing leaves the computer.
- **CSP:** the page reports a Content-Security-Policy violation.
- **Console:** the page logs a console error (this includes failed requests:
  404s, 500s, refused connections) or throws an uncaught exception.

A test that expects an error says so: `test.use({ allowConsole: [/status of 404/] })`.
`e2e/guards.spec.ts` breaks each rule on purpose (marked `test.fail()`), so the
suite goes red if a guard ever stops working.

## Journeys

| File | Covers |
|---|---|
| `start-page.spec.ts` | Start page: title, WhizBang! box, sites by kind, What's New titles, Library Stats, chrome and status bar |
| `search.spec.ts` | Ctrl+L, type a question, Enter; ten results; clean, highlighted snippets; kind filter; Next; Back/Forward; I'm Feeling Lucky; suggestions with arrow keys, Enter and Escape; location-bar shortcuts and navigational queries; "Did you mean" for a misspelled query |
| `documents.spec.ts` | Open a result; one page `<h1>`; sanitized HTML; hover shows the link target in the status bar; follow a link; Back/Forward; broken links and remote images; View Source Data (Ctrl+U); time machine and diff; Not Found and the gap report |
| `browsing.spec.ts` | Directory down to a listing and back; Hotlist (Ctrl+D, Bookmarks menu, Ctrl+B, Delete, survives reload); History (Ctrl+H, Find, Remove); What's New; System Monitor including a 5 s refresh |
| `display.spec.ts` | Every theme from the View menu (a screenshot of each in the report), High Contrast, text size (Ctrl+], menu, Ctrl+0), reduced motion (OS setting and Options menu) |
| `keyboard.spec.ts` | Keyboard only: skip link, Tab to the location bar, the search box and the results, focus ring, F10 menus with arrows and typeahead, `/` to search |
| `live-zim.spec.ts` | Live only: a Wikipedia article whose image really loads; Stack Exchange site, Enter site, book list, a book's front page |
| `guards.spec.ts` | The guards catch an off-origin request and an uncaught exception |

## Artifacts

- `navigator/e2e-report/`: the HTML report (`npm run e2e:report`). Theme
  screenshots and other named snapshots are attached to their tests.
- `navigator/e2e-results/`: per-test output. On failure: a screenshot of the
  window, a screenshot of the failed step (`nav.step`), and the page's
  accessibility snapshot (`error-context.md`). On the first retry: a full trace
  (`npx playwright show-trace <trace.zip>`).

Both folders are gitignored. CI uploads them as an artifact when the job fails.

## Known app bugs in the suite

Bugs the suite has found are listed in [e2e-findings.md](e2e-findings.md). Each
one stays covered by a test marked `test.fail()` (or `test.fail(target === 'live')`)
with a comment naming the finding, so the suite is green while the bug is
tracked. When the bug is fixed that test "unexpectedly passes" and fails the
run: remove the `test.fail()` then. Bugs that would otherwise trip the console
guard in unrelated tests are tolerated through `KNOWN_ISSUES` in
`e2e/fixtures.ts`; delete the entry when the bug is fixed.

## Adding a journey

1. Write it as a person would do it, in a `*.spec.ts` file under `navigator/e2e/`:

   ```ts
   import { expect, test } from './fixtures.ts';

   test('a person can find RFC 9110 from the location bar', async ({ page, nav, data }) => {
     await nav.open('wsp://home');
     await nav.step('type in the location bar', async () => {
       await nav.go('rfc 9110');            // Ctrl+L, type, Enter, wait for the page
     });
     await expect(nav.heading).toContainText('HTTP Semantics');
     await expect(page).toHaveTitle(/RFC 9110/);
   });
   ```

2. Find things by role and accessible name (`getByRole('link', { name: … })`),
   never by CSS class, unless there is nothing a person could name (the status
   bar text, a snippet). If a control has no accessible name, that is a bug to
   report.
3. Drive with the keyboard and mouse (`page.keyboard.press`, `click`, `hover`),
   not with `page.evaluate`. Wait for what a person waits for: `nav.settled()`
   after a navigation, then assertions with `expect` (they retry).
4. Wrap the stages in `nav.step('…', …)` so a failure names the stage and saves a
   screenshot of it.
5. If the journey needs data, add it to both targets in `e2e/support/data.ts`, or
   skip the other target with `test.skip(target === 'live', 'why')`.
6. Run it a few times: `npm run e2e -- --retries=0 --repeat-each=5 <file>`.
   Then `npm run lint` and `npm run typecheck` (the e2e files are linted and
   type-checked with the rest of the Navigator).

The `nav` helper (`e2e/support/navigator.ts`) knows the chrome: `nav.location`
(the location bar), `nav.main`, `nav.heading`, `nav.status` (status bar),
`nav.button('Back')`, `nav.menu('View', 'System 7')`, `nav.go(text)`,
`nav.open(address)`, `nav.address()`, `nav.tabTo(locator)` and `nav.snapshot(name)`.
