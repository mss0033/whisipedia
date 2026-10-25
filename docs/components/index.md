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

## Storage placement (ADR-0002, ADR-0011)

| Data | Where | Why |
|---|---|---|
| Refinery Parquet (`[index] refinery_root`) | NAS or local | write-once, sealed files; atomic `*.partial` and rename; no mmap over the NAS (a soft SMB mount turns I/O errors into SIGBUS) |
| Refinery id maps (`<index>/refinery-maps`) | local | memory-mapped on every document lookup |
| Tantivy index (`lexical/TANTIVY.txt` names it) | NAS or local | built on local disk, copied once (`wsp index point-tantivy`); a NAS outage can crash a reader, never damage the index |
| `meta.sqlite` (settings, source numbers), suggest tries, rank, spelling | **local disk only** | SQLite cannot run on the SMB share; the rest is read on every keystroke or query |
| Change feed SQLite (WAL) | **local disk only** | same reason; old ranges move to the archive (`[index] changes_archive`, NAS) |

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
`prior` is the static-rank feature (PageRank, below), read from `static_rank.bin`. A full rebuild
takes its titles and aliases from the Refinery and the redirect tables (`wsp_index.titles`), never
from a copy in a database (ADR-0011). Very short prefixes match too many keys to
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

## Static rank: PageRank over the link graph

`wsp_index.linkrank` computes a link-analysis prior offline. `wsp index rank` runs it alone; `wsp index
build` runs it last whenever something changed (`--no-rank` skips it).

* **Graph.** Nodes are the documents of every source's current Refinery generation; edges are
  `DocRecord.links` that start with `wsp:`. Targets resolve with `pyarrow.compute.index_in` (C++): the
  exact doc id first, then a loose key (lower case, `_` as space, no `#fragment`) that also covers
  redirects and aliases (`TitleSources.iter_aliases`: the Refinery's `aliases` column and the
  redirect tables). Self-links and duplicate links are dropped. Edges go to an
  on-disk list under `work/_rank/`, split into links inside one source and links across sources.
* **Ranks.** One streamed power iteration (damping 0.85, L1 tolerance 1e-6, at most 50 rounds) yields
  two PageRanks: **per source** (only links inside the source; teleport and dangling mass stay in the
  source) and **global** (the whole cross-source graph). With one source they are the same and only
  one is computed.
* **Feature.** `(1 - mix) * log1p(pr_source * N_source) + mix * log1p(pr_global * N)` with `mix = 0.5`
  (`--mix`): ≈ 0.14 for a page nobody links to, 0.69 for an average page, 8-10 for the hubs.
* **Sidecar.** `lexical/static_rank.bin`: sorted blake2b-64 doc-id hashes plus float32 feature and both
  PageRanks, memory-mapped, replaced atomically; `static_rank.json` holds the build statistics. Readers
  re-check the file every 5 s, so a rebuild needs no restart.
* **Use.** `TantivyIndex` re-sorts its top `rank_depth` (100) BM25 hits by `bm25 + rank_weight *
  feature` (`rank_weight` 1.0; deeper hits keep BM25 order, so paging stays stable). Oracle fusion
  multiplies the RRF score by `1 + static_rank_weight * feature` (`[oracle] static_rank_weight`, 0.1).
  Suggest priors are the feature (replacing raw inbound-link counts), and the portal's featured
  article is chosen from the best-ranked long articles of each encyclopedia.

Scale, measured with `scripts/bench_rank.py` under `nice -n 19 ionice -c 3`, 4 threads, on a shared
workstation with ~2 GB of free RAM:

| benchmark | result |
|---|---|
| graph from Parquet, 300k docs × 20 links (`graph`) | 6.0M edges resolved in 4.1 s (1.5M links/s), 12 iterations in 1.8 s, peak RSS 0.5 GB |
| power iteration, 7M nodes / 150M edges (`iterate`, English Wikipedia scale) | 7.0 s per iteration, peak RSS 1.7 GB; ≤ 50 iterations ≈ 6 min |

Link resolution at 1.5M links/s puts English Wikipedia (~150M links) at under 2 minutes. Node ids are
held as one Arrow string array (~35 bytes per node, ~250 MB at 7M), never as Python strings.

## "Did you mean" (spelling)

`wsp_index.spelling` is a classic noisy-channel corrector. `wsp index spell` builds the vocabulary;
`wsp index build` rebuilds it when something changed.

* **Language model:** one vocabulary per language (languages under 50 documents share one), word
  counts from the indexed text (first 3,000 characters per document, a sample of at most 400k
  documents per language from row groups spread over its parts, titles counted ×5), tokenized in
  Arrow. Words of 3-24 letters; from 100k documents on, words seen fewer than 3 times are dropped (the
  corpus's own typos); at most 300k words per language.
* **Not read:** catalogues (registry domain `catalog`, i.e. Open Library): their records are book titles
  in hundreds of languages, not the library's text. A language a source scatters thinly (under 8 rows
  per row group on average) is sampled from at most 64 of that source's row groups, because each is
  read whole for its few rows. Before 2026-10-25 the step read 5.5 million row groups and ran ~40 hours.
* **Reuse:** `<index root>/spell-cache/` keeps each generation's language map and each language's
  counts under a key of the files its sample comes from; a rebuild counts only the languages whose
  sources changed (the sample is deterministic: a kept vocabulary is the one a recount would make)
  and removes what it no longer uses. It is safe to delete; the next rebuild reads everything once.
* **Error model:** each edit (insertion, deletion, substitution, adjacent transposition) costs
  ε = 0.01, plus one more when the first letter changes (people rarely mistype it). A word becomes
  `argmax count(c) · ε^cost`, the word itself included, so a known word is only replaced by a much
  more frequent neighbour.
* **Candidates:** a symmetric-delete index (SymSpell): each word stores hashes of its variants with up
  to two deletions in its first 7 letters; the query word's variants are binary-searched and the
  candidates verified with the optimal-string-alignment distance. Words of 3-4 letters get one edit.
* **File:** `lexical/spelling.bin` (hashes, counts, a UTF-8 blob, the delete index), memory-mapped and
  replaced atomically; the API's `ReloadingSpeller` picks up a rebuild within 5 s.

On the 20k-document Wikipedia sample: 228,691 words and 5.3M delete keys (built in 10 s, 70 MB); a
correction takes 1-4 ms per query.

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

- Documents from Kiwix ZIMs carry no `links` (the ZIM adapter does not extract them), so
  `wikipedia-en-zim` gets a flat static rank; the link graph comes from the dump source
  (`wikipedia-en`) and the other adapters that emit `wsp:` links.

- `DocRecord.aliases` (Refinery format 2) join the suggest overlay on upsert; redirect aliases join at
  the next full trie rebuild (a new generation with redirects marks the trie stale).
- Suggest matches title prefixes only. It does not complete words in the middle of a title.
- Dense retrieval is a seam only (`NumpyVectorIndex` is exact and O(n)). ANN and a real embedder
  are Phase 3.
- `diff_generations` loads `(doc_id, content_hash)` for two generations into memory, about 100
  bytes per document. Partition it by hash before running it on the 7M-article sources.
- Without the old generation (lost `index-state.json`), stale documents of a source are found in its
  other Refinery generations; leftovers of a generation deleted from the Refinery by hand are not
  (`wsp index rebuild-lexical` clears them).
