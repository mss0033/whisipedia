# ADR-0005: One API contract for the Navigator and the API, generated from OpenAPI

- Status: Proposed
- Date: 2026-10-10

## Context
The API (`packages/wsp-api`) and the Navigator (`navigator/`) were built in parallel. Their contracts
drifted: different field names and shapes, summaries as NDJSON vs server-sent events, three endpoints the
Navigator needed that did not exist, and two ways to put a doc id in a URL. The API spec also documented
an ambiguity: with `/v1/documents/{doc_id:path}`, a native id ending in `/history` (the Wiktionary entry
"history", for example) collides with the history route. ASGI servers decode `%2F` before routing, so
percent-encoding alone did not help.

v1 is not frozen yet (PLAN §11.1 freezes it before Phase 6), so breaking changes are still possible.

## Decision
1. **`docs/api/openapi.json` is the single source of truth.** It is exported from `wsp_api.schemas` (a
   test fails while it is stale). The Navigator's wire types are generated from it
   (`navigator/scripts/gen-api.mjs` → `src/api/generated.ts`; `npm run check` fails while stale), and its
   client and mock API are typed by the generated operation table. The SDK mirrors the schemas and a
   test compares them.
2. **A doc id is ONE percent-encoded path segment**: `/v1/documents/wsp%3Arfc%2F9110` and
   `/v1/documents/wsp%3Arfc%2F9110/history`, exactly what `encodeURIComponent` produces. The server
   routes `/v1/documents/…` on the raw path (`wsp_api.routing`), so `%2F` stays inside its segment and
   the history route can never collide with a native id. Unencoded slashes answer
   `404 doc_id_not_encoded` with a hint. Rejected alternative: keep `{doc_id:path}` and move history to
   another prefix; that breaks the route PLAN §11.1 names and still leaves `{doc_id:path}` greedy.
3. **Breaking changes accepted now:** `Document.links` becomes a `{href, target}` resolution table;
   `POST /v1/summarize` streams SSE events (`token`, `citation`, `notice`, `done`, `error`) instead of
   NDJSON chunks; `/go` maps `wsp://home` to `/v1/portal` and `wsp://whatsnew` to
   `/v1/changes/recent`. Everything else is additive. `API_VERSION` becomes 1.1.0.
4. **New read seams** (`Catalog`, `AssetStore`, `GapLog`) are defined in `wsp_api.seams` until the core
   ports are revised, each with a working default so the composition root needs no change.

## Consequences
Contract drift between the front end and the API now fails CI on whichever side changed. Clients built
against 1.0.0 must re-encode doc ids, read `links` as objects and parse SSE summaries. The seams must
move to `wsp_core.ports` (with contract tests) and be wired in `wsp_cli.bootstrap`.
