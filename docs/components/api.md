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
chosen. Today it uses the in-memory fakes from `wsp_api.testing.fakes`, seeded with a small sample
library. Each `TODO(integration)` comment in that function marks where a real component goes.

The fakes are meant to be reused by other packages' tests. `sample_services()` returns a fully wired
`Services`.

## HTTP endpoints (v1)

| Method and path | Purpose |
|---|---|
| `GET /v1/health` | Liveness check, API version, source count |
| `GET /v1/sources`, `/v1/sources/{id}`, `/v1/sources/{id}/generations` | Registry plus the vault's generations (snapshot handles) |
| `GET /v1/documents/{doc_id}` (`?at=<generation>`), `/v1/documents/{doc_id}/history` | Documents and their version hashes |
| `GET /v1/search?q=&sources=&limit=&offset=`, `GET /v1/suggest?q=` | Search and title autocomplete |
| `GET /v1/changes?since=`, `GET /v1/changes/stream` (SSE) | Change feed |
| `GET /v1/system` | System Monitor data: freshness, degraded generations, ledger tail and its integrity |
| `POST /v1/summarize` | Cited summary as an NDJSON stream of `SummaryChunk`. Returns `501 not_configured` if no `Generator` is set |
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
- **Document ids** contain `/` (`wsp:wikipedia-en-zim/A/Alan_Turing`). Percent-encode `?`, `#` and `%`
  in them. One known ambiguity: a native id that ends in `/history` collides with the history route.

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
following (`stream_changes`) and summary streaming. Errors are typed: `WspApiError`, `NotFoundError`
and `NotConfiguredError`.

The models in `wsp_sdk.models` are a hand-written mirror of `wsp_api.schemas`. Installing the SDK
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
```

Read commands go through the SDK. They use `--url` or `$WSP_API_URL` when set. Otherwise they run
the API in-process, without opening a socket.

The CLI uses argparse rather than Typer. The surface is small, argparse adds no dependencies (which
keeps the offline audit simple), and it type-checks cleanly under pyright strict.
