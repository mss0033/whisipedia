# wsp-index: Refinery, lexical index, suggest, documents, change feed, hybrid search

Package `packages/wsp-index` (import `wsp_index`). Implements PLAN §9.1–9.3 and the read side of §11.1.

| Port (`wsp_core.ports`) | Reference implementation | Contract suite (`wsp_core.testing`) |
|---|---|---|
| `LexicalIndex` | `TantivyIndex` | `lexical_index_contract.LexicalIndexContract` |
| `DocumentStore` | `ParquetDocumentStore` | `document_store_contract.DocumentStoreContract` |
| `ChangeFeed` | `SqliteChangeFeed` | `change_feed_contract.ChangeFeedContract` |
| `VectorIndex` | `NullVectorIndex`, `NumpyVectorIndex` (exact, brute force) | (unit tests) |
| `Embedder` | `HashingEmbedder` (feature hashing, not semantic) | (unit tests) |

`HybridSearcher` fuses a `LexicalIndex` with an optional `VectorIndex` + `Embedder`. `sync.refresh` diffs
two Refinery generations, updates the index, then publishes the changes to the feed.

## Storage placement (ADR-0002)

| Data | Where | Why |
|---|---|---|
| Refinery Parquet | NAS or local | write-once, sealed files; atomic `*.partial` and rename; no mmap (a soft SMB mount would turn I/O errors into SIGBUS) |
| Tantivy index, `meta.sqlite`, suggest tries | **local disk only** | mutable and locked; `ensure_local()` refuses `cifs`/`smb3`/`nfs`/`9p`/... mounts |
| Change feed SQLite (WAL) | **local disk only** | same reason |

## Refinery layout

```
<root>/<source_id>/<generation_id>/
    part-00000.parquet   DocRecords sorted by doc_id, zstd level 6, 256-row row groups
    _ids.parquet         blake2b-64(doc_id) -> (part, row group, row), sorted by hash
    _SUCCESS.json        seal marker, written last
```

**Point lookup.** Parquet min/max statistics only prune when a whole generation is globally sorted,
and a streaming writer cannot guarantee that without an external sort, so with N part files a lookup
would probe up to N files. Each generation gets a small hash index instead: 16 bytes per document,
loaded into numpy and binary-searched. A lookup is one `searchsorted` plus decoding one row group.
Hash collisions are resolved by comparing the stored `doc_id`; this is tested with a hash function
that always collides. **Row-group size** was measured on 10k synthetic documents (about 1.8 KB of text
each, no row-group cache):

| rows per group | size | lookup p50 | lookup p95 |
|---|---|---|---|
| 64 | 9.4 MB | 0.65 ms | 1.04 ms |
| **256** | 8.8 MB | 1.04 ms | 1.57 ms |
| 1024 | 8.7 MB | 2.51 ms | 4.31 ms |
| 4096 | 8.6 MB | 10.14 ms | 14.44 ms |

256 rows is the default: lookups take about a millisecond, and the files are within 2% of the best
compression measured. Groups larger than 1,024 rows push lookups past the API budget.

`diff_generations(old, new)` runs an Arrow hash join over `(doc_id, content_hash)` and yields
`ADDED`/`MODIFIED`/`REMOVED` (tombstone) records. It does not detect renames.

## Lexical index (`TantivyIndex`)

- Fields: `doc_id` (raw, stored), `source_id` (raw, filter), `lang` (raw, stored), and for each
  configured language `L`, `title_L` and `text_L` analyzed by `wsp_L`. The analyzer is: simple
  tokenizer, drop tokens over 40 characters, lowercase, Snowball stemmer, ASCII folding. Documents in
  any other language go to `title_any`/`text_any`, which do not stem. Queries run over all of these
  fields, with titles boosted ×3. Adding a language means adding it to `TantivyConfig.languages` and
  reindexing. The schema signature is stored, so a mismatched config fails loudly.
- Queries are plain language. Syntax characters are neutralized, balanced quotes still make
  phrases, and terms are OR'ed and ranked with BM25.
- `upsert` is delete-by-term on `doc_id` followed by add. Writes become visible on `commit()`, or
  automatically every `commit_every` operations. The writer heap and thread count are configurable.
- Snippets come from Tantivy's `SnippetGenerator` (`<b>` highlighting, HTML-escaped). When only the
  title matched, the snippet falls back to the first 200 characters. Storing body text for snippets
  costs index space; `store_text=False` turns it off.

## Suggest (< 10 ms)

Every title and alias becomes one key, `normalized\x1fTitle\x1fdoc_id`, stored in a MARISA trie
(`marisa-trie`, mmapped) with a float32 score. The score is `prior + 1/(1+len(title))`, where
`prior = log1p(inbound links)` is set with `set_priors`. Very short prefixes match too many keys to
rank on every keystroke, so at build time each prefix with more than 512 completions stores its
top 32 keys in a second trie. Every other prefix ranks at most 512 candidates. Between rebuilds,
incremental changes go to an in-memory overlay. The overlay is folded into the trie on commit once
more than 2,000 titles have changed, and on close.

Alternatives measured by `scripts/bench_index.py` on the same 200k titles:

| design | p50 | p95 | p99 | memory |
|---|---|---|---|---|
| **MARISA + heavy-prefix top-k (chosen)** | 0.024 ms | 0.112 ms | 0.148 ms | 6.6 MB on disk, mmapped |
| sorted Python list + bisect + argpartition | 0.011 ms | 0.100 ms | 0.121 ms | 14.9 MB Python heap |
| MARISA without the heavy-prefix table | 0.114 ms | 16.6 ms | 20.0 ms | 6.6 MB |

Python's sorted list with bisect is the fastest in-process option. It needs every title as a
Python object, though: several GB of heap for the ~20M titles and aliases of the T1 library, and a
full rebuild on every start. The MARISA tries are mmapped, so they open instantly and are shared by
worker processes. They are 2.3 times smaller and stay about 70 times inside the budget at p99. The heavy-prefix
table is what keeps p99 flat for one- and two-letter prefixes.

## Benchmarks

`uv run python packages/wsp-index/scripts/bench_index.py --docs 200000 --out data/bench` writes
results to `data/bench/results.json` (gitignored). The corpus is synthetic: Zipf(1.07) over 60k
pseudo-words, log-normal document lengths (median 300 words), and a power-law link graph. Results
on a 16-thread workstation (WSL2, NVMe):

| metric (200,000 docs, 353 MB text, avg 1.8 KB/doc) | value |
|---|---|
| synthesize + write Refinery (5 sources) | 29 s; Parquet 178 MB |
| Refinery point lookup p50 / p95 / p99 | 0.89 / 1.42 / 1.87 ms |
| index build (read Parquet, index, commit, build suggest) | 10.4 s = **19,300 docs/s**, 34 MB/s of text |
| Tantivy size (text stored for snippets) | 461 MB |
| set priors + rebuild suggest trie | 2.5 s; tries 6.6 MB |
| search top 10 with snippets, p50 / p95 / p99 (2,000 queries) | **2.0 / 3.9 / 5.0 ms** |
| search page 2, p50 / p95 / p99 | 2.2 / 3.7 / 4.8 ms |
| search with a 2-source filter, p50 / p95 / p99 | 2.3 / 3.2 / 3.7 ms |
| suggest p50 / p95 / p99 (5,000 prefixes of 1–10 chars) | **0.024 / 0.112 / 0.148 ms** |

Search stays well inside the 30 ms lexical budget at this size. Latency grows roughly with posting
list length, so check the result again on the real English Wikipedia index (35× larger).

## Known gaps

- `DocRecord.aliases` (Refinery format 2) are added to the suggest aliases on upsert; redirects still come in through `TantivyIndex.set_aliases` (`wsp index build` does this).
- Suggest matches title prefixes only. It does not complete words in the middle of a title.
- Dense retrieval is a seam only (`NumpyVectorIndex` is exact and O(n)). ANN and a real embedder
  are Phase 3.
- `diff_generations` loads `(doc_id, content_hash)` for two generations into memory, about 100
  bytes per document. Partition it by hash before running it on the 7M-article sources.
- `meta.sqlite` commits after the Tantivy commit. A crash between the two leaves suggest one batch
  behind until that batch is re-applied (upserts are idempotent).
