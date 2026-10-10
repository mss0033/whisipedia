# wsp-fetch: fast, polite, hash-verified downloads

Package `packages/wsp-fetch` (import `wsp_fetch`) implements the `Fetcher` port (PLAN §6.5) twice:

| Implementation | When to use |
|---|---|
| `HttpFetcher` | Reference implementation. Pure Python (`httpx` + threads). Supports everything below. |
| `Aria2Fetcher` | Drives `aria2c` (resolved on `PATH`). The same contract, and we re-hash the result ourselves. Useful as a cross-check and when aria2's C++ transfer engine is faster on a given link. |

Both pass `wsp_core.testing.fetcher_contract.FetcherContract`. Both are configured with `FetchConfig`, built from the `[acquirer]` config section via `FetchConfig.from_mapping`.

```python
from wsp_fetch import FetchConfig, HttpFetcher, load_wikimedia_catalog

catalog = load_wikimedia_catalog()
cfg = FetchConfig.from_mapping(config["acquirer"])
cfg = dataclasses.replace(cfg, host_connections={**catalog.host_connections(), **cfg.host_connections})
with HttpFetcher(cfg) as fetcher:
    result = fetcher.fetch(catalog.with_mirrors(remote_file), workdir / remote_file.name)
    result.sha256, result.blake3, result.verdict  # digests for the manifest
```

`download()` (the port method) returns only the path. `fetch()` also returns a `FetchResult` with every digest it computed and per-mirror byte counts.

## Speed techniques

1. **Segmented, multi-mirror transfer.** The file is cut into pieces: 64 KiB-aligned, 1–8 MiB, or the Metalink piece length when one is published. Every usable mirror gets as many worker threads as its host's connection cap. Workers pull the lowest-numbered outstanding piece from a shared queue (work stealing), so fast mirrors naturally take more pieces.
2. **Mirror discovery.** Mirrors come from three places: `RemoteFile.mirrors`, the packaged Wikimedia catalog (`data/wikimedia_mirrors.toml`, which needs periodic refresh and warns once its `refresh_by` date has passed), and the Kiwix Metalink. For hosts listed in `metalink_hosts`, the fetcher automatically reads `<url>.meta4`, or reads the URL itself if it points at a `.meta4`.
3. **Probing.** Every candidate gets one `Range: bytes=0-0` request in parallel. The probe learns the size, whether Range is supported, the final URL after redirects, and the ETag. A mirror is dropped before any data moves if it 404s or advertises a different size.
4. **Adaptive mirrors.** A mirror that fails repeatedly is dropped. A mirror with a fatal error (404, ignored Range, wrong `Content-Range`) is dropped immediately. A mirror whose per-connection throughput is below best ÷ `slow_mirror_factor` is cut to one connection.
5. **Endgame.** Once the queue is empty, idle connections duplicate any straggling piece that has been in flight on another mirror for more than 0.5 s. The first copy to verify wins and the other is cancelled mid-stream. One slow mirror can therefore never hold up the tail of the download.
6. **Hashing in one pass, not two.** SHA-256, MD5 and SHA-1 cannot be combined from out-of-order pieces, so an **in-order hasher thread** runs a sliding cursor over the pieces. Workers hand it the piece buffers they already hold in RAM, so the bytes are never re-read from disk. Pieces that finish far ahead of the cursor are dropped from memory once they exceed `hash_window_bytes` (256 MiB by default), and are re-read from the page cache when the cursor reaches them. For each buffer, every algorithm runs on its own thread (`hashlib` and `blake3` release the GIL), so a buffer costs the time of the slowest algorithm rather than the sum. BLAKE3 itself runs multi-threaded. We chose this over "hash pieces, then re-hash the assembled file", which costs a second full read of the file.
7. **Zero-copy-ish writes.** Each piece is streamed into a preallocated `bytearray` and written with a single `os.pwrite` into a sparse `.partial` file.
8. **Threads, not asyncio.** Every hot path (socket reads, `pwrite`, hashing) releases the GIL, and only a few dozen connections exist at a time. asyncio would still need executor threads for hashing and disk I/O. Threads keep the sync `Fetcher` port simple.

## Politeness (PLAN §6.5)

- **Per-host cap.** A process-wide semaphore per `host[:port]` limits concurrent requests, probes included, and holds across concurrent downloads in the same fetcher. The default comes from `max_connections_per_host` (2, which is Wikimedia's per-IP limit); `host_connections` overrides it per host. The mirror catalog supplies these values.
- **Bandwidth cap.** `bandwidth_limit` (bytes/s) is enforced by a global token bucket shared by all connections.
- **User-Agent.** The `User-Agent` comes from config. Requests send `Accept-Encoding: identity`, so byte ranges always refer to the stored bytes.
- **Retries.** Retries use exponential backoff with equal jitter. `Retry-After` (seconds or HTTP-date) is honoured: that mirror cools down while the other mirrors keep working. A `Retry-After` longer than `max_retry_after` drops the mirror.

## Verification guarantees

- **Piece verification (Metalink).** Each piece is checked against its Metalink hash *before* it is written. A bad piece is never retried on the mirror that served it, and a mirror that serves two bad pieces is dropped.
- **Full-file verification.** At the end, every upstream digest must match: `RemoteFile.checksums` merged with the Metalink's. If two sources publish different digests for the same algorithm, the download fails with `ConflictingChecksumsError`. An algorithm we cannot compute raises `UnsupportedChecksumError` before any download starts.
- **Typed errors.** A mismatch raises `ChecksumMismatchError`, a wrong size raises `SizeMismatchError`, and running out of mirrors raises `MirrorsExhaustedError`. All of them are subclasses of `FetchError`. On a mismatch the `.partial` file is deleted, and nothing is ever renamed into place.
- **Atomic finish (ADR-0002).** The finish sequence is: write `<dest>.partial`, `fsync`, verify, optionally read back (`verify_readback=True` drops the page cache and re-hashes from disk, which is meant for SMB targets), `os.replace`, then fsync the directory.
- **Always our own digests.** SHA-256 and BLAKE3 are always computed, even when upstream publishes nothing. `FetchResult.verdict` is `unverifiable` in that case.
- **Resume.** `<dest>.partial.state.json` lists the completed pieces and each piece's digest. It is written atomically and only *after* an `fdatasync` of the data file, so every piece it lists was durable when it was recorded. On restart, a recorded piece is read back once, re-verified against its stored digest and its Metalink hash, and fed to the in-order hasher. A torn or rotted piece is downloaded again. The state is discarded if the URL, size, piece geometry, checksums or ETag changed.
- **aria2.** `Aria2Fetcher` hands aria2 a Metalink we generate. It contains all mirrors, the size, the digests and the piece hashes, and aria2 runs with `--realtime-chunk-checksum`. aria2 aborts on a bad piece instead of switching mirror. When that happens, the driver resumes it from its control file without the offending URI. After aria2 exits, the file is fsynced, **re-hashed by us** and passed through the same `finalize` step.

## Testing

`wsp_core.testing.fetcher_contract.RangeServer` is an in-process HTTP/1.1 server on 127.0.0.1 with Range support. Each server instance can be set to inject latency, error statuses with `Retry-After`, corrupted bytes, ignored ranges or dropped connections. It also records every request and the peak concurrency. The wsp-fetch tests use it to prove:

- parallel multi-mirror correctness
- per-host caps (exactly 2 by default, ≤ 4 when configured)
- re-fetching of corrupt pieces from another mirror
- recovery after SIGKILL, including a piece rotted on disk
- typed errors on a full-hash mismatch
- `Retry-After` handling
- the bandwidth cap
- single-stream fallback
- endgame behaviour against a slow mirror

`packages/wsp-fetch/scripts/bench_fetch.py` (network) benchmarks both fetchers against a real Kiwix file.

## Known limits

- Without Metalink piece hashes, a mirror that serves corrupted bytes is detected only by the full-file digest. The whole file then has to be downloaded again.
- `Aria2Fetcher` cannot honour `Retry-After`, and it can only apply one per-server connection cap to all hosts (we use the minimum).
- HTTP/2 multiplexing is deliberately not used, so that connection caps keep meaning what upstream operators expect.
