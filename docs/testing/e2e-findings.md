# E2E findings

App bugs found by the Playwright suite ([e2e.md](e2e.md)), most severe first.
Each one stays covered by a test marked `test.fail()` that names it, so the
suite is green while the bug is open; when the bug is fixed, that test fails
the run as "unexpectedly passing" and the marker should be removed.

Found on 2026-10-10 against the mock (production bundle) and the live library
at `http://127.0.0.1:8737` (API 1.2.0, then 1.3.0). To get a screenshot, run the
pinning test with `npm run e2e:live -- -g '<test title>'`: the HTML report
(`npm run e2e:report`) shows the window at the failure, and
`navigator/e2e-results/<test>/` holds the PNG and trace (gitignored).
Live timings were taken while other jobs (index builds, benchmarks; load
average up to 200) were loading the machine, so treat the latency numbers as
"under load". Under that load most live journey failures were E2E-2: pages
that never finished loading within 30 s.

| Id | Severity | Area | Summary | Status |
|---|---|---|---|---|
| E2E-4 | High | API (HTML rewrite) | Stack Exchange book front pages link to `wsp://stackexchange/questions/…` without the book: every question, tag and image is 404 | **Fixed** 2026-10-11: links resolve against the ZIM entry actually served (`ZimAssetStore.base`); covered by `live-zim.spec.ts` › fixed bug E2E-4 |
| E2E-2 | High | API + Navigator | `/v1/sources`, `/v1/portal` and `/v1/system` can take 15–120 s; every page waits for `/v1/sources`, so the Navigator shows a blank page for that long | **Fixed** 2026-10-11: vault manifest cache, site cards cached 30 s and refreshed in the background, ledger replay off the request path, Navigator waits at most 3 s for the site list |
| E2E-7 | Medium | API (search) | Navigational queries do not jump: `wikipedia Alan Turing` gives a results page | **Fixed** 2026-10-11: `navigate_to` counts pages served from a ZIM |
| E2E-6 | Medium | API (search) | The top hit depends on the page size, so I'm Feeling Lucky lands on a different page than result #1 | **Fixed** 2026-10-11: the oracle always ranks at least its configured depth |
| E2E-5 | Medium | API (search) | With a kind filter, `total` stays the unfiltered total; the pager offers pages that are empty | **Fixed** 2026-10-11: the oracle cache cut deep pages short; facet counts are now per kind |
| E2E-3 | Medium | API (ZIM catalogue) | The first Stack Exchange book in the list (`3dprinting…_2026-07a`) is 404 | **Fixed** 2026-10-11: re-release suffixes (`_2026-07a`) are stripped from book names |
| E2E-10 | Medium | API (search) | No "Did you mean" on live: `diabetis insulen` and `vacine` return `did_you_mean: null` | Fixed 2026-10-12: the live library offers `diabetes insulin`; `search.spec.ts` › misspelled query passes on live (no longer pinned) |
| E2E-8 | Low | Index coverage | A search for `alan turing` returns no Wikipedia article, only Wiktionary and MDWiki pages | Open: the Wikipedia index build is running. Matching every query word now also earns a bonus (`coordination_boost`) |
| E2E-9 | Low | API (text extraction) | The `text` of ZIM HTML documents includes inline script source (`RLCONF = {…}`) | **Fixed** 2026-10-11 for pages served live from a ZIM |
| E2E-11 | Low | Index (titles) | Result titles come from the URL slug: "MRNA vaccine" in results, "mRNA vaccine" on the page | Open (observation; tests compare titles case-insensitively) |
| E2E-1 | Low | Navigator (sanitizer) | Opening any page with `style` attributes or `<style>` elements logs CSP violations | **Fixed** 2026-10-11: the API's HTML rewrite drops inline styles; covered by `documents.spec.ts` › fixed bug E2E-1 |

---

## E2E-4: Stack Exchange book pages link outside their book (High)

**Repro (live):**

1. Open `wsp://stackexchange`, click **Enter site**, click **askubuntu.com en all**.
2. The front page "Highest Voted Questions - Ask Ubuntu" renders, but the Ask
   Ubuntu logo and the nav icons show as broken, and **Home** is a broken link.
3. Click any question, for example "How to unzip a zip file from the Terminal?".

**Expected:** the question page opens (`wsp://stackexchange/askubuntu.com_en_all/questions/86849/…`,
which the API serves: `GET /v1/documents/wsp%3Astackexchange%2Faskubuntu.com_en_all%2Fquestions%2F86849` is 200).

**Actual:** "Not Found" for `wsp://stackexchange/questions/86849/how-to-unzip-a-zip-file-from-the-terminal`.
The images 404 the same way: `/v1/assets/stackexchange/2026-08/static/img/home.svg` is 404,
`/v1/assets/stackexchange/2026-08/askubuntu.com_en_all/static/img/home.svg` is 200.

**Cause (likely):** `wsp_api.htmlrewrite._resolve` joins relative references to
`posixpath.dirname(base_native_id)`. For a book front page the native id is
`askubuntu.com_en_all` (no slash), so the directory is `""` and the book prefix
is lost. The front page behaves like the book's directory index; its base should
be `askubuntu.com_en_all/`. The same applies to `href="./"` (Home), which
resolves to nothing. Every multi-book source (Stack Exchange, the curated
Kiwix bundle) is affected; beyond the front page, Stack Exchange is not browsable.

**Screenshot:** run the pinning test (see the table); the report shows the page.

## E2E-2: slow `/v1/sources`, `/v1/portal`, `/v1/system` leave the Navigator blank (High)

**Repro (live):** `time curl 'http://127.0.0.1:8737/v1/sources?limit=200'`, a few times.
Then open the Navigator at `http://127.0.0.1:8737/`.

**Expected:** under a second (PLAN §9.3 puts search results at p95 < 100 ms;
the site list is static metadata). The page shows something while it waits.

**Actual:** measured during the E2E runs:

| Endpoint | Times seen |
|---|---|
| `/v1/sources?limit=200` | 0.9 s, 1.0 s, 2.0 s, 4.4 s, 49 s, 95.8 s, > 120 s (timeout) |
| `/v1/sources` (default limit) | > 60 s (timeout), then 1.0 s, 0.2 s |
| `/v1/portal` | 0.4 s, 6 s, 10 s, 14 s, 17 s, 25 s, 31 s, > 120 s (timeout) |
| `/v1/system` | > 60 s (timeout) |
| `/v1/health` | always < 5 ms |

While `/v1/sources` is pending the Navigator shows an empty page with
"Host contacted. Waiting for reply…" and no way to tell why. That happens on
**every** page, including documents that have already loaded, because
`App.sources()` is awaited by the site, document (webring), search and directory
renderers. There is no client timeout, so a hung request means a page that
never appears (live tests failed on this before the suite gained a warm-up step).
One run also saw `500` from `/v1/portal` while the daemon restarted. In the
last runs even `GET /` (the static `index.html`) sometimes took 13–45 s while
slow API calls were in flight, which suggests the slow handlers exhaust the
server's worker threads; that is when most live journeys timed out.

**Cause (likely):** `Views.source_info` calls `vault.current(source_id)` and
`catalog.doc_count(source_id)` for each of the ~60 sources on every request,
reading the NAS, with no cache. The slow cases coincided with index builds
writing to the same NAS.

**Suggested direction:** cache source infos per generation in the API; in the
Navigator, render documents without waiting for the source list (fill the
webring in later) and give API calls a timeout with a "the library is slow to
answer" message.

**Screenshot:** none kept (the first live run's failure screenshot showed the
blank page; it is reproduced whenever `/v1/sources` is slow).

## E2E-7: navigational queries do not jump (Medium)

**Repro (live):** press Ctrl+L, type `wikipedia Alan Turing`, press Enter.
Or `curl 'http://127.0.0.1:8737/v1/search?q=wikipedia+Alan+Turing'`.

**Expected:** the Navigator opens `wsp://wikipedia-en-zim/Alan_Turing`
(docs/components/navigator.md, PLAN §10.3: "Known navigational queries
(…"wikipedia Alan Turing") jump straight to the page"). That page exists and renders.

**Actual:** `navigate_to` is `null`; a results page of Wiktionary entries appears.

**Cause (likely):** `wsp_api.search.navigate_to` checks existence with
`services.documents.get(doc_id)`, which does not see pages served straight from
ZIM archives (and the title fallback uses the search index, which holds no
Wikipedia ZIM pages; see E2E-8).

**Screenshot:** run the pinning test (see the table); the report shows the page.

## E2E-6: the top hit depends on the page size; Lucky goes elsewhere (Medium)

**Repro (live):**

```sh
curl 'http://127.0.0.1:8737/v1/search?q=alan+turing&limit=10'   # hits[0]: "Turing" (Wiktionary)
curl 'http://127.0.0.1:8737/v1/search?q=alan+turing&limit=1'    # hits[0]: "Allan–Herndon–Dudley syndrome" (MDWiki)
```

In the Navigator: search `alan turing`, then press **I'm Feeling Lucky**.

**Expected:** Lucky opens result #1 of the same query; a result's rank does not
depend on how many results were asked for.

**Actual:** Lucky (which asks for `page_size=1`) opens "Allan–Herndon–Dudley
syndrome". The `limit=3` response also returns that page first, with an empty snippet.

**Screenshot:** run the pinning test (see the table); the report shows the page.

## E2E-5: a kind filter keeps the unfiltered total; the pager runs into empty pages (Medium)

**Repro (live):** search `how do vaccines train the immune system`. The filters
read *Medicine (148)*, *Dictionary (52)*. Click **Dictionary (52)**.

**Expected:** "Results 1 - 10 of about 52", and a pager with 6 pages.

**Actual:** "Results 1 - 10 of more than 200" and a pager up to page 10.
`GET /v1/search?…&kind=dictionary` returns `total: 200`, and pages 6 and later
(`offset=50`, `offset=60`) return no hits ("No documents matched").

**Screenshot:** run the pinning test (see the table); the report shows the page.

## E2E-3: a Stack Exchange book in the list is 404 (Medium)

**Repro (live):** open `wsp://stackexchange/_books` (Enter site), click the first
book, **3dprinting.stackexchange.com en all 2026-07a**.

**Expected:** the book's front page.

**Actual:** "Not Found" for `wsp://stackexchange/3dprinting.stackexchange.com_en_all_2026-07a`.
The undated id `3dprinting.stackexchange.com_en_all` is 200. It is the only one of
the 181 books whose id carries a date suffix.

**Screenshot:** run the pinning test (see the table); the report shows the page.

## E2E-10: no "Did you mean" on the live library (Medium)

**Repro (live, API 1.3.0 after the spelling work merged):** Ctrl+L, type
`diabetis insulen`, Enter. Or `curl 'http://127.0.0.1:8737/v1/search?q=diabetis+insulen&limit=3'`.

**Expected:** "Did you mean: *diabetes insulin*?" above the results (the mock
offers it for `vacine`, and the journey passes there).

**Actual:** `did_you_mean: null`; six hits ("Insulitis", "Insulin degludec/insulin
aspart", …). `diabetis` and `vacine` alone also return `null` (both are Wiktionary
headwords, which may make them "known" words to the speller). If the live speller
data has simply not been built yet, this is an operations gap rather than a code
bug; the pinned test will start passing once it is offered.

## E2E-11: result titles from URL slugs (Low, observation)

`GET /v1/search?q=how+do+vaccines+train+the+immune+system` returns the hit title
"MRNA vaccine" (`wsp:mdwiki/MRNA_vaccine`); the page itself is titled "mRNA vaccine".
Titles in the index seem to be rebuilt from the native id with the first letter
upper-cased, which mangles names such as mRNA, iPhone or eBay.

## E2E-8: Wikipedia articles are not in search results (Low, observation)

**Repro (live):** search `alan turing`. All 200 hits are Wiktionary and MDWiki
pages (facets: Dictionary 169, Medicine 31). The article
`wsp://wikipedia-en-zim/Alan_Turing` exists and renders.

This is probably index coverage (the Wikipedia ZIM is not indexed yet) rather
than a ranking bug, but it is the first thing a person tries. It also explains
part of E2E-7.

## E2E-9: document text includes script source (Low, observation)

**Repro (live):** `curl 'http://127.0.0.1:8737/v1/documents/wsp%3Awikipedia-en-zim%2FAlan_Turing'`
and look at `text`: it starts `Alan Turing document.documentElement.classList.replace('client-nojs', 'client-js') RLCONF = {"wgBreakFrames":false,…`.
Stack Exchange pages include CSS (`a.external-link { background-image: … }`).

**Expected:** `text` is the readable text, without `<script>` and `<style>` contents.

**Impact:** the time machine's diff, summaries and any snippet built from `text`
show code. The rendered page is fine (the HTML path is sanitized).

## E2E-1: CSP violations from parsing document HTML (Low)

**Repro (mock or live):** open `wsp://wikipedia-en-zim/Alan_Turing` with the
browser console open.

**Expected:** no CSP violations; the sanitizer drops `style` attributes and
`<style>` elements anyway.

**Actual:** one "Refused to apply inline style because it violates the following
Content Security Policy directive: "default-src 'self'"" error per `style`
attribute or `<style>` element (`style-src-attr` / `style-src-elem`), dozens on a
Wikipedia page. Nothing is applied, so this is noise, not a hole, but it buries
real CSP violations and would flood a `report-uri` if one is ever added.

**Cause:** `sanitizeDocumentHtml` (`navigator/src/core/links.ts`) parses with
`DOMParser`. Chrome checks inline styles against the page's CSP while parsing,
even in an inert document (also true of `<template>` and
`document.implementation.createHTMLDocument()`). Stripping `style` attributes
and `<style>` elements before parsing (in the API's HTML rewrite, or with a
pre-pass) avoids it.

**Screenshot:** not visual; see the console output of the pinned test.
