# ADR-0005: Port revision v0.2

- Status: Accepted
- Date: 2026-10-10

## Context
Seven packages were built in parallel against the v0.1 ports in `wsp_core.ports`. Each component
document listed the port changes its author needed, and several worked around gaps: fetch digests
thrown away and recomputed, `.meta4` URLs smuggled into `RemoteFile.mirrors`, license and format
passed through `ReleaseRef.meta` by convention, a cursor error type defined privately in two places,
the whole ledger replayed to show its last 20 events, and the "P1M means follow the global cycle"
convention in the scheduler. This ADR consolidates those proposals into one revision.

## Decision

Adopted:

| Change | Why |
|---|---|
| `Fetcher.download` returns `FetchResult` (path, size, sha256, blake3, verified upstream digests, `verdict`) | The fetcher already hashes every byte in flight. The pipeline persists the result, skips the adapter's local re-hash when every upstream digest matched, and passes it to the vault as `expected`, so the vault's read-back is checked against the download (end to end). The vault still reads its own copy back (PLAN §8.1). `wsp_fetch.FetchReport` subclasses it with transfer statistics. |
| `wsp_core.errors`: `FetchError`, `NotFoundError` (404/410), `ChecksumMismatchError`, `InvalidCursorError` (a `ValueError`) | Callers catch port-level types; implementations subclass them. |
| `RemoteFile.metalink`; `RemoteFile.mirrors` documented as full URLs of byte-identical copies | No more `.meta4` entries inside `mirrors`. |
| `ReleaseRef.license` / `ReleaseRef.format` | Typed inputs for the auto-pause rules; `meta` is adapter-private. |
| `Vault.ingest(..., upstream=, expected=)` and `Vault.adopt(...)` | Upstream checksums are verified and recorded in the manifest; adopt moves files already on the vault filesystem (bulk downloads in `_incoming/`) without copying. |
| `Ledger.tail(n)`, `Ledger.append(..., idempotency_key=)` | `/v1/system` no longer needs a replay; the orchestrator's outbox flush becomes "append with key = event_id" instead of scanning the ledger. The key is stored as `data["idempotency_key"]`, so the event format and hash chain are unchanged. |
| `close()` on `LexicalIndex` and `ChangeFeed` (idempotent) | Composition roots own the lifecycle of local stores. |
| `DocumentStore.get(at_generation=g)` = newest generation at or before `g`; missing there = tombstoned (`None`); `history()` lists content changes only | Pinned in the contract suite and implemented by the Parquet store and the API fake. |
| `DocRecord.aliases` | Wikidata labels feed suggest and search directly; the Tantivy index adds them to `set_aliases`. Refinery Parquet format bumps to 2 (rebuildable derived data). |
| Optional `SupportsHead` capability (`head(url) -> RemoteHead`) | A separate runtime-checkable protocol rather than an optional method, so pyright strict keeps working and callers test with `isinstance`. |
| `wsp-core[testing]` extra | `wsp_core.testing` imports pytest; runtime installs of wsp-core no longer need it. Package dev groups depend on the extra. |

**Warden is a separate port.** `manifest`, `mark_degraded` and `prune` live on `Warden`, not
`Vault`. The API and the release pipeline hold a `Vault` and must never be able to degrade or prune
data; scrub, repair and retention are the only holders of that authority. `FileVault` implements
both protocols. `Warden.manifest` returns a structural `ManifestView`, so `wsp_vault.Manifest`
stays the concrete type. `FileVault.prune` moves the generation to `<root>/pruned/` (same-filesystem
rename) and refuses the current and the newest sealed generation; emptying `pruned/` is a manual
owner action, keeping "nothing is ever deleted automatically".

**Domain resolution is SourceSpec-based.** A source's shelf is `SourceSpec.vault_domain` (its first
registry `domain`), passed to `FileVault` via `domains_from_specs`; an existing directory always
wins so data is never split. Config does not carry a second mapping.

Rejected or deferred:

- Adapter `fetch()` keeps yielding per-file results rather than a whole-release object: the pipeline
  persists them per file, which is what resume needs.
- A `Fetcher.head()` method on the main protocol (see above).
- Moving `wsp_vault.Manifest` into core: the structural view is enough for every caller.

## Consequences
Every implementation, fake and contract suite was updated together; the API fakes now also run the
core contract suites. The orchestrator state store migrates schema 1 → 2 (`fetched_json`) in place.
Refinery Parquet written before this change must be rebuilt (`wsp index build`); there was no
production data yet.
