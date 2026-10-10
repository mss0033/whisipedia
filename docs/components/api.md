# API, SDK and CLI

Whisipedia has three public surfaces (PLAN §12.2). All three depend only on the ports in
`wsp_core.ports`.

| Package | Module | What it is |
|---|---|---|
| `packages/wsp-api` | `wsp_api` | FastAPI app factory `create_app(services, settings)` |
| `packages/wsp-sdk` | `wsp_sdk` | `WspClient` / `AsyncWspClient` (httpx) |
| `packages/wsp-cli` | `wsp_cli` | the `wsp` console script and the composition root `bootstrap.py` |

## Dependency injection

`wsp_api.Services` is a dataclass of ports: `LexicalIndex`, `DocumentStore`, `ChangeFeed`, `Vault`,
`Ledger`, the registry `SourceSpec`s and an optional `Generator`. Routes never import an
implementation.

`wsp_cli.bootstrap.build_services(cfg, layout)` is the single place where implementations are
chosen. With `[api] backend = "real"` (or `"auto"` and an existing `vault.root`) it builds `FileVault`,
`JsonlLedger`, `TantivyIndex`, `ParquetDocumentStore`, `SqliteChangeFeed`, `FeedCatalog`,
`ZimAssetStore` and `JsonlGapLog(<index.root>/gaps.jsonl)`; otherwise the in-memory fakes from
`wsp_api.testing.fakes`, seeded with a small sample library. The browsing ports (`Catalog`,
`AssetStore`, `GapLog`) live in `wsp_core.ports` since port revision v0.2 (ADR-0005).

The fakes are meant to be reused by other packages' tests. `sample_services()` returns a fully wired
`Services`.

Three more read seams live in `wsp_api.seams` until they move to `wsp_core.ports` (TODO(port)). Each
has a default, so `Services` works without wiring them:

| Seam | Purpose | Default when `None` |
|---|---|---|
| `Catalog` | random pages, doc counts, newest changes, per-source categories | `wsp_api.catalog.FeedCatalog` (replays the change feed) |
| `AssetStore` | original HTML and images inside sealed containers | `wsp_api.zim.ZimAssetStore` (libzim, over the vault) |
| `GapLog` | "not in library" reports for the gap miner | none: `POST /v1/gaps` answers 501; `wsp_api.gaps.JsonlGapLog(<index root>/gaps.jsonl)` is the reference |

In-memory versions are in `wsp_api.testing.browse`; `browse_services()` adds them, an HTML document with
an image and a few categories to the sample library.

## HTTP endpoints (v1)

| Method and path | Purpose |
|---|---|
| `GET /v1/health` | Liveness check, API version, source count |
| `GET /v1/sources`, `/v1/sources/{id}`, `/v1/sources/{id}/generations` | Registry plus the vault's generations (snapshot handles) |
| `GET /v1/documents/{doc_id}` (`?at=<generation>`), `/v1/documents/{doc_id}/history` | Documents (text, original HTML when available, link table, vault location) and their versions |
| `GET /v1/search?q=&sources=&kind=&limit=&offset=`, `GET /v1/suggest?q=` | Search (facets, code-point highlights, `navigate_to` for navigational queries) and title autocomplete |
| `GET /v1/changes?since=`, `GET /v1/changes/stream` (SSE), `GET /v1/changes/recent` | Change feed; `recent` is newest first with the cursor to follow live |
| `GET /v1/system` | System Monitor data: freshness and health, degraded generations, ledger tail and its integrity |
| `POST /v1/summarize` | Cited summary as server-sent events (`token`, `citation`, `notice`, `done`, `error`). Returns `501 not_configured` if no `Generator` is set |
| `GET /v1/portal` | Start page: featured page of the day, newest changes, a random page, every site |
| `GET /v1/directory?path=` | Directory: Top › registry domain › site › category |
| `GET /v1/random?source=&seed=` | A random page |
| `GET /v1/assets/{source}/{generation}/{path}` | An image inside a sealed ZIM. Images only, immutable cache, sandboxed CSP |
| `POST /v1/gaps` | Record a "not in library" link (loopback clients only, `204`) |
| `GET /go/{address}` | Resolves a `wsp://` address (see [addresses](../api/addresses.md)) |
| `GET /v1/openapi.json` | The live spec |
| `/` | The Navigator bundle (`navigator/dist`, configurable), or a placeholder page if it is not built |

The contract is [`docs/api/openapi.json`](../api/openapi.json) (OpenAPI 3.1). Regenerate it with
`uv run python -m wsp_api.openapi_export`. A test fails while the committed copy is stale, so every
API change shows up as a diff to that file. Removing or renaming anything in it is a breaking change
and needs an ADR.

### Conventions

- **Errors.** Every non-2xx response has the shape `{"error": {"code", "message"}}`, including 404s
  from routing, 422 validation errors, 405s and 500s. The 500 message never contains internal
  details. `code` is stable; `message` is not.
- **Pagination.** List endpoints take `cursor`/`limit` and return `next_cursor`. Cursors are opaque.
  Search uses `limit`/`offset` and returns `next_offset`.
- **Change feed.** `since` is the feed's own opaque cursor. On the SSE stream, the last event of each
  batch carries `id: <cursor>`, so `Last-Event-ID` resumes the stream. Delivery is at-least-once.
  `?max_seconds=` closes the stream after that many seconds, for proxies and tests.
- **Document ids** contain `/` (`wsp:wikipedia-en-zim/A/Alan_Turing`). In a URL path a doc id is ONE
  percent-encoded segment (`encodeURIComponent`: `/v1/documents/wsp%3Arfc%2F9110`). The server routes
  on the raw path, so native ids ending in `/history` do not collide (ADR-0006,
  [addresses](../api/addresses.md)).
- **Search** is navigational when the query names one page (`rfc 9110`, `wikipedia Alan Turing`,
  `wsp://rfc/9110`): exact native id or exact title within a named site, no fuzzy matching
  (`wsp_api.search`). `highlights` are `[start, end)` code-point ranges into `snippet`.
- **Document HTML** is the original (ZIM) HTML with relative images rewritten to `/v1/assets/…` and
  relative links to `wsp://` addresses. It is untrusted: clients sanitize it.

## Security and offline guarantees

- `wsp serve` binds `127.0.0.1:8737` and refuses any non-loopback address unless `--allow-remote`
  is given.
- Every response carries `Content-Security-Policy: default-src 'self'; frame-ancestors 'none'; …`,
  plus `nosniff`, `no-referrer` and same-origin COOP/CORP headers.
- The server checks the `Host` header against an allow-list (loopback names by default). This blocks
  DNS-rebinding attacks.
- CORS is off: the server never sends `Access-Control-*` headers.
- Swagger and ReDoc are disabled because they load assets from CDNs.
- FastAPI's built-in OpenTelemetry is switched off explicitly. Since 0.143 it is on by default, and
  `FASTAPI_OTEL_AUTO_CONFIGURE` could otherwise turn on exporters.
- `packages/wsp-api/tests/test_api_offline.py` calls every endpoint while `socket.connect`, `connect_ex` and
  `getaddrinfo` are wrapped, and fails if any non-loopback connection is attempted. The tests use an
  in-process transport (`wsp_api.inprocess.InProcessTransport`), so they also pass under
  `unshare -n`, where even loopback is down.

## SDK

```python
from wsp_sdk import WspClient

with WspClient() as wsp:  # http://127.0.0.1:8737
    for hit in wsp.iter_search("rfc about http caching"):
        print(hit.title, hit.address)
    for page in wsp.iter_changes(since=saved_cursor):
        ...
        saved_cursor = page.cursor
```

The SDK mirrors every endpoint, with iterators for paginated results, change-feed draining, SSE
following (`stream_changes`), summary streaming (SSE events) and the Navigator resources (`portal`,
`directory`, `random`, `recent_changes`, `report_gap`). Errors are typed: `WspApiError`, `NotFoundError`
and `NotConfiguredError`.

The models in `wsp_sdk.models` are a mirror of `wsp_api.schemas`. Installing the SDK
(for example in Living) therefore does not pull in FastAPI, Starlette or wsp-core.
`test_sdk_models.py` compares the JSON schemas of the two sets, so drift fails CI. The SDK ignores
unknown fields, so a newer server never breaks an older client.

## CLI

```
wsp serve [--host 127.0.0.1] [--port 8737] [--static-dir navigator/dist] [--allow-remote]
wsp search <words…> [--source ID]… [--limit N] [--offset N] [--json]
wsp doc <doc_id> [--at GENERATION] [--json]
wsp sources [--json]
wsp changes [--since CURSOR] [--limit N] [--follow --max-seconds S] [--json]
wsp registry validate [--registry PATH] [--schema PATH]
wsp config show                       # merged config with secret-looking keys shown as ***

# operator commands (real library from config/; see wsp_cli.ops)
wsp ingest [--source ID] [--dry-run] [--offline] [--allow-mirror] [--json]
wsp index build [--source ID]         # refine sealed generations -> Parquet -> Tantivy + suggest (local disk)
wsp scrub [--budget-gb N]             # re-hash due files, repair ladder on findings
wsp drill [--kind KIND] [--source ID] # chaos drill on a sandboxed copy
wsp approve|reject <source> [<version>] [--note TEXT]
wsp resume <source>
wsp status                            # sources, generations, degraded, orchestrator queue
```

`wsp ingest` adopts complete bulk downloads from `<vault.root>/_incoming/<source>/<version>/` using
`_incoming/_plans/targets.json`: a file is complete when no `<name>.aria2` control file exists, its
size matches and it has settled; expected checksums come from the plan (or the Kiwix `.zim.sha256`
via the adapter). Complete versions are adopted by rename, sealed, signed, logged and promoted (never
over a newer generation), then README/CATALOG are regenerated. It is idempotent, resumable and never
deletes. `--dry-run` only reads.

Read commands go through the SDK. They use `--url` or `$WSP_API_URL` when set. Otherwise they run
the API in-process, without opening a socket.

The CLI uses argparse rather than Typer. The surface is small, argparse adds no dependencies (which
keeps the offline audit simple), and it type-checks cleanly under pyright strict.
