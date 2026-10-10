# `wsp://` addresses

The Navigator's location bar shows addresses in the `wsp://` scheme (PLAN §10.2). The server resolves
them with `GET /go/{address}`. The response tells the browser which API resource holds the page.

## Grammar

```
address   = [ "wsp://" ] path [ "?" query ]
          | "wsp:" source-id "/" native-id          ; a bare doc id is also accepted
path      = page | source-id [ "/" native-id ]
page      = "home" | "search" | "directory" | "system" | "whatsnew"
source-id = [a-z0-9][a-z0-9.-]*                     ; same rule as wsp_core.ids
native-id = any non-empty text, may contain "/"
```

Page names are reserved, so a source id must never be one of them. The scheme is optional, which
means `rfc/9110` and `wsp://rfc/9110` are the same address. Inside a native id, percent-encode `?`,
`#`, `%` and spaces (`/` and `:` may stay as they are).

## Mapping

| Address | `kind` | `href` (API resource) |
|---|---|---|
| `wsp://home` | `home` | `/v1/sources` |
| `wsp://search` | `search` | `null`: the search home page is drawn client-side |
| `wsp://search?q=…` | `search` | `/v1/search?q=…` |
| `wsp://directory` | `directory` | `null`: not built yet |
| `wsp://system` | `system` | `/v1/system` |
| `wsp://whatsnew` | `whatsnew` | `/v1/changes` |
| `wsp://<source>` | `source` | `/v1/sources/<source>` |
| `wsp://<source>/<native-id>` | `document` | `/v1/documents/wsp:<source>/<native-id>` |

The document address `wsp://<source>/<native-id>` and the stable id `wsp:<source>/<native-id>`
(PLAN §9.2) carry the same information. Each can be turned into the other without loss.

## Responses

- `200` returns a `Resolution`: `{"address": "<canonical>", "kind": "...", "href": "..." | null}`.
- With `?redirect=true`, the server answers `307` to `href` instead, which is handy with `curl -L`.
  Pages whose `href` is `null` still get the JSON response.
- `404 address_not_found` means no page or source has that name.
- `404 not_in_library` means the source exists but does not hold this document. The Navigator shows
  this as a period-style broken link, and the gap miner logs it (PLAN §5.3).
- `400 external_address` means the address uses another scheme (`http://…`). The library is
  offline, so the server never follows such addresses.
- `400 invalid_address` means the native id is malformed.
