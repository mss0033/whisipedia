# Audit: wsp-index build/sync path at 32M–60M documents

**Status key:** **[F]** fixed in `b9d64e6`, **[P]** partly fixed, **[O]** open.

**Evidence on disk after the failed 6.5 h run:**

| Location | What it held |
|---|---|
| `lexical/tantivy/` | 81.5 GB. Only 1.59 GB was live: 7 segments and 9.27M docs per `meta.json`. The other ~80 GB were uncommitted, orphaned segments. |
| `lexical/meta.sqlite-wal` | 3.0 GB, next to a 670 MB database |
| `changes.sqlite` | 1.1 GB, plus a 1.2 GB WAL |

## CRITICAL

**C1 [F]: Every uncommitted `delete_documents_by_term` holds ~1.2 KB of native Tantivy heap, and production never committed mid-run.**
- Where: `lexical.py:225`, `lexical.py:79`, `bootstrap.py:369`, `sync.py:63`.
- Measured (tantivy-py 0.26.2, 2M ops):

  | Operation | Extra RSS | Time |
  |---|---|---|
  | add only | +154 MB | 9.8 s |
  | delete only | +2,457 MB | 6.4 s |
  | delete + add | +2,365 MB | 39.6 s |

  At 32M documents that is about 39 GB.
- Committing every 250k ops held RSS flat at about 830 MB.
- Fixed by:
  - committing per part;
  - skipping deletes on first loads (`replace=False`);
  - deferring suggest rebuilds during bulk loads.

**C2 [F]: The diff path cost ~750 B per document and was O(N) Python even for a one-document update.**
- Where: `to_pylist` (408 B per row), `ChangeRecord` (+88 B), feed `publish` tuples (+104 B), plus the Arrow join.
- Fixed by `wsp_index.delta`, a 32-byte key index per document.
- The auditor's faster variant is still open. It adds an `fp` column to `_ids.parquet` so the diff is a pure numpy merge over the sorted `h` arrays: 1.0 s and 1 GB at 32M documents.

**C3 [F]: The apply step fetched every document by random point lookup.**
- Measured 10.8k docs/s, against 1.25M rows/s for a sequential scan.
- Fixed by streaming parts in physical order and reading only the changed row groups.

**C4 [F]: The build was not resumable, despite the docstring.**
- Fixed by per-part commits and a feed-stored checkpoint.

## HIGH

**H1 [F]: The default writer configuration is ~4× slower.**
- Measured: 4 threads with a 1 GB heap gave 144k docs/s; the default (8 threads sharing 256 MB) gave 34k.

**H2 [P]: Every commit was coupled to a full suggest-trie rebuild, at ~410 B per key.**
- 100M keys would need about 41 GB.
- Fixed:
  - no rebuild on open, or on close, of a large index;
  - rebuilds deferred during bulk loads;
  - capped at `suggest_max_keys`, highest prior first.
- Open:
  - a persisted overlay table instead of the dirty flag;
  - a sharded or external-sort build;
  - an atomic two-file swap (see M2).

**H3 [F]: The suggest fallback enumerated every key under a prefix** (`suggest.py:176-178`).
- Measured 2.1 s and 500 MB per keystroke on the 9.6M-key trie.
- Now capped at 4,096 keys.
- Still open: store the top-k per heavy prefix deduplicated by document and title at build time, with k of 64–128.

**H4 [P]: `RefineryReader` memory.**
- Measured:
  - one `ParquetFile` retains 2.5 MB of footer (196 row groups × 16 columns);
  - the id index costs 20 B per doc, not the 16 B in the docstring;
  - the docstore LRU of 16 readers can reach ~16 × 2.3 GB.
- Fixed: open files are LRU-bounded (64); row groups are sized by text.
- Open:
  - memory-map the ids as `.npy`;
  - history should read a per-source version log, not every generation (pubmed set ids would be ~146 GB per year);
  - GC superseded sets.

**H5 [F]: `_duplicates_mask` reopened a footer per duplicate row** (`files.setdefault(part, pq.ParquetFile(...))` is evaluated eagerly). Now one row-group read per (part, rg).

**H6 [F]: A set-id collision could delete a sealed, current set.** Numbering is now max+1, and a sealed set is never removed.

**H7 [O]: No inter-process lock on the orchestrator's index hooks.** Effects:
- `refine` rmtrees another process's in-progress directory;
- `compose_set` races;
- the `index-state.json` read-modify-write can lose an update;
- the trie `.partial` name is fixed, so concurrent writers can collide on it.

Fix: one flock taken by all writers, plus per-PID partial names.

**H8 [P]: SQLite** gets one write transaction per build, and the WAL never shrinks.
- Fixed:
  - per-part commits;
  - `journal_size_limit`;
  - `busy_timeout`;
  - `cache_size`;
  - checkpoint(TRUNCATE) on close.
- Open: give the feed DB the same pragmas.

**H9 [O]: Publication order.** Today the index is committed and the feed published before `CURRENT.txt` moves, so search returns ADDED documents that `/v1/documents` answers with 404. In `index_partitioned`, member aliases are applied after the state is saved, so a crash loses them for good.

Fix:
- flip the refinery `CURRENT` before the refresh;
- apply aliases before saving state.

**H10 [P]: Lost deletions.**
- Fixed: when the old generation is missing, documents are reconciled from the index (`_orphans`).
- Open: a partitioned set is a pure union, so a `DeleteCitation` in a daily pubmed partition cannot remove a PMID held by the baseline. Needs tombstone rows.

## MEDIUM

1. **[O]** Re-ranking reads whole stored docs, including text, for 100–200 hits just to get the `doc_id` (13–33 ms). Make `doc_id` and the static-rank feature fast fields.
2. **[O]** The two-file trie swap is not atomic, and only the titles file is stamped. A reader that loads between the two renames can raise `KeyError` from `suggest()`. Use a versioned directory with an atomic pointer.
3. **[F]** Shadowed scans were 4.4× slower because of per-row tuple-in-set checks.
4. **[O]** Alias semantics:
   - `set_aliases` deletes the `DocRecord.aliases` that `upsert` inserted;
   - removed aliases are never deleted;
   - every partitioned run re-applies every member's aliases.
5. **[O]** Change detection hashes only the text, so a change to the title, aliases, lang or meta is never re-indexed. Hash every indexed field.
6. **[O]** The feed stores ~120 B per row (hashes as 64-char hex TEXT) and has no retention. Store them as BLOBs and add retention.
7. **[O]** `normalize_key` costs 3.5 µs per title; a `str.translate` version takes 1.0 µs. It also keeps Cf characters.
8. **[O]** CJK is unsearchable: the simple tokenizer, and `remove_long(40)` counts bytes, not characters.
9. **[F]** Row groups had a fixed 256 rows; they are now sized by text.
10. **[O]** Hybrid search builds snippets for depth 50 but shows 10, and applies the static rank twice.
11. **[O]** `refine` loads the 611 MB id index just to count rows; `_SUCCESS.json` already holds the count.

## LOW

- **[F]** No directory fsync after renames in the Refinery. Still open: `CURRENT.txt` and `index-state.json` have no file fsync.
- **[F]** `rollback()` rebuilt the trie needlessly.
- **[O]** Set ids can sort before legacy ids.
- **[O]** No single-flight on docstore reader construction.
- **[O]** English stopwords are applied to every language: "The Who discography" becomes "discography".
- **[O]** `_heavy_prefixes` `chr(ord+1)` overflows at U+10FFFF.
- **[O]** float32 and float64 scores are mixed in `heapq.merge`.
- **[O]** `inlink_counts` is unused.
- **[O]** The feed uses AUTOINCREMENT needlessly.
- **[O]** `ensure_local` misses `fuse.*` and virtiofs.

## Design target (60M docs on 23 GiB)

1. A fixed-width `.npy` sidecar per generation (`h`, `fp`, row: 20 B per doc), memory-mapped. Row groups of ~1 MB.
2. Changes kept as numpy/Arrow arrays end to end. Chunked commits, with a resume cursor in the feed transaction.
3. Tantivy writer with 4 threads and a 1–2 GB heap. `doc_id` and the rank feature as fast fields.
4. Partitioned sources diff per partition, with tombstones. GC old sets.
5. Suggest as an offline, sharded, versioned build. Serving processes never build.
6. Single-writer flock. Chunked SQLite. Publication order: index, then `CURRENT`, then feed.
