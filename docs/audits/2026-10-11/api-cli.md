# Audit: wsp-api, wsp-cli, wsp-sdk

About 150 GETs against the live API, plus in-process experiments in `/tmp/claude-1000/-home-elizabeth-dev-whisipedia/audit-api-cli/`.

**Housekeeping.** The crash reproductions made 3 core dumps, which the auditor deleted. The API's own dumps (6.2 GB and 7.3 GB) and others, about 18 GB in total, are in `C:\Users\whis\AppData\Local\Temp\wsl-crashes\`. They are left for the owner to remove.

## CRITICAL

**1 [F in b9d64e6]. The API crashes when two threads read the same Parquet file.**
- Where: `refinery.py:575-586`. `read_row_group` ran outside the lock, on a shared `ParquetFile`.
- Production: 3 crashes on 10–11 Oct (double free, SIGSEGV) within 1 s of the Navigator's parallel requests.
- gdb shows the race in `parquet::SerializedFile::PreBuffer` and `ReadRangeCache`.
- Reproduced: 3 of 3 runs crashed with 16 threads; with a lock, none did.

**2 [O]. libzim memory-maps ZIM data on the soft CIFS mount, so an I/O error becomes SIGBUS and kills the process.**
- Where: `zim.py:17,64,125,133,188-198`. `nm` confirms libzim imports `mmap`.
- Fix: serve ZIMs from worker processes so a SIGBUS kills a restartable worker, or cache hot ZIMs on local disk. At minimum, add a SIGBUS handler that exits cleanly.

**3 [P]. Memory.**
- The docstore LRU holds 16 readers.
- Each reader carries a full id index: about 640 MB for wikipedia-top10, 184 MB for wiktionary.
- Each reader caches 32 row groups including text.
- History and `?at=` open older readers.
- Live: `VmHWM` is 4.59 GB.
- Fix:
  - mmap the ids;
  - bound caches by bytes;
  - project columns.

## HIGH

**4 [O]. `/v1/system` takes 2.6–5 s warm and 10–20 s cold, recomputed on every request.**

| Cost | Time | Where |
|---|---|---|
| `vault.generations()` × 79 | 2.9–3.2 s | `app.py:526-530` |
| `vault.current()` × 79 | 0.33–0.97 s | — |
| `ops.status` | 0.46–0.57 s | 75 YAMLs through pure-Python SafeLoader, 8 sqlite connects, full lane-log line counts |
| Ledger replay on the first request | the cold 20 s | — |

- Root cause: `FileVault.domain_of()` (`vault.py:200-208`) does `iterdir` over SMB on every call, uncached.
- Fix:
  - cache `source → domain`;
  - build one `VaultSnapshot` in a background refresher;
  - use CSafeLoader;
  - never block on the ledger.

**5 [O]. Thread-pool exhaustion.** Every route is a sync `def` on the 40-token AnyIO limiter, `health` included. A stalled NAS starves liveness.
- Fix:
  - async health;
  - a separate bounded executor with timeouts and 503s for NAS work.

**6 [O]. The ZIM archive cache.**
- Bogus generation ids evict real archives (16 requests evict Wikipedia) and walk the NAS.
- The bundle fallback opens all 181 archives on a miss, because of a name mismatch.
- The open happens outside the lock, so archives get opened twice.
- Fix:
  - validate the generation first;
  - single-flight opens;
  - a persisted book→file map;
  - size the LRU to the number of sources.

**7 [O]. Search runs 5–12 index queries per request, and the facets are wrong.**
- Per request:
  - a window, a page, one window per kind, `scope`;
  - `navigate_to`: up to 5 candidates × every named source, each `exists` reading and parsing the whole article HTML;
  - `did_you_mean`: a full Oracle search per unknown word.
- Measured wrong: `q=rfc 9110` total=28, facet medicine 4, but filtered total is 11; a dictionary facet of 0 is shown. Cause: per-kind windows change Oracle routing.
- `zzqxv blorf` returns 11 dense hits.
- "Did you mean" is dead: there is no `spelling.bin`, and the unknown-word test goes through dense search, which never returns empty.
- Fix:
  - one-pass facets (Tantivy aggregation on a `kind` fast field);
  - `total` from the same retrieval as the page;
  - existence as an id lookup;
  - unknown words checked against the vocabulary.

**8 [O]. `GET /v1/documents` (ZIM pages) is slow and heavy.**
- The entry is read up to 3 times.
- The rewrite is pure-Python `HTMLParser`: 77 ms for Alan_Turing, 221 ms for United_States.
- The response carries both html and text: 0.84 MB and 2.16 MB.
- No gzip, ETag or Cache-Control.
- `history` is called twice; `links` makes up to 1,000 `get` calls.
- Fix:
  - read the entry once;
  - cache the rewritten page per (source, gen, entry) with an ETag and `immutable`;
  - make text opt-in;
  - gzip/brotli;
  - a C HTML parser.

**9 [O]. N+1 full-row Parquet reads just to get titles.**
- Places: the portal (`title` × 400), search (`get` × 100), `links`, `resolve`, `change`.
- A cold `get` costs 5.2 ms against 0.3 ms for an id-only lookup.
- Fix: a local `doc_meta` store, or column projection.

**10 [O]. `/go/<source>/<native>` returns 404 for ZIM-served pages.** Route existence through `Library.exists`.

**11 [O]. Cold-start herds.**
- Missing source cards are computed by every request at once.
- Every `create_app` starts warm-up threads (16 ZIMs over SMB, the search model, cards, the ledger), including the in-process CLI, so `wsp search` loads the dense model.
- CLI imports cost 0.5 s.
- Fix:
  - single-flight;
  - warm-up only in `cmd_serve`;
  - lazy imports.

## MEDIUM

- **12.** The Oracle result cache:
  - isn't invalidated by another process's rebuild;
  - has no single-flight;
  - accepts offsets up to 10k (3.1 s, with deep results cached).

  Key it on the index opstamp, and cap offset at 200.
- **13.** The change feed is opened read-write by the API. It can create a new epoch and invalidate every client cursor. One global lock serializes reads, and SSE polls every second. Use `mode=ro` per thread.
- **14.** `build_services` uses `load_signer(create=True)`, so `wsp serve` can write a signing key.
- **15.** HTML rewrite:
  - root-relative src/href in bundles drop the book prefix, matching the live 404s on `/-/static/...`;
  - `svg image`, `srcset`, `link` and `script` are not rewritten.

  This is not exploitable, because the Navigator sanitizer and CSP stop it.
- **16.** `generation_of` scans all of history, and `serving_generation` walks the NAS, on every document view.
- **17.** SDK `stream_changes` has `timeout=None` and no reconnect. `SSEParser` resets the id per event.
- **18.** CLI:
  - `doctor --require <typo>` runs nothing and exits 0;
  - `collections fetch` returns 0 when downloads failed, and takes no lock;
  - `index catalog`, `scrub` and `drill` take no locks;
  - `service uninstall` disables units it then keeps;
  - `systemctl` runs with no timeout;
  - `proposals accept` is only half-applied if validation fails.

## LOW

- 19: `_homes` is not locked; `Services.source()` is a linear scan.
- 20: `exists` reads the whole article.
- 21: access logs are too verbose; hashed assets lack `immutable`.
- 22: `cmd_status` walks generations twice; `meta.sqlite-wal` was 3.1 GB.
- 23: `touch()` does I/O on the event loop.
- 24: `source_info_seconds` is hard-coded.
- 25: `took_ms` excludes serialization, and the WSL clock jumps.

## Design target (<100 ms per page over 40M docs on a NAS)

1. Nothing on the request path touches the NAS except immutable byte reads. One `LibrarySnapshot` is refreshed in the background and swapped atomically.
2. ZIMs are read in worker processes, so SIGBUS stays contained. An on-disk content-addressed cache holds rendered pages, served with ETag + `immutable` + brotli.
3. A local memory-mapped metadata index (id → title, kind, license, ts, lang, url, flags; about 60 B per doc). Parquet is used only for text.
4. One-shot search: page, facet aggregation and count in one Tantivy query. Dense search on the top-k under a time budget, cached by opstamp. Navigation via an FST. Spelling via the term dictionary.
5. Async routes for snapshot reads; separate bounded executors with timeouts and 503 shedding; no shared native objects without a thread-safety guarantee.
6. Byte budgets on every cache, and RSS exported to the System Monitor.
