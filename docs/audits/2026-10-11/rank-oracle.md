# Audit: rank, spelling, catalog, text and wsp-oracle at 41M documents

Measured on the real `refinery/wikipedia-top10/2026-10-01` (32,048,621 docs in 641 parts) and on `vectors/` (430,671 e5-base passages).

Two facts about the environment shape several findings:

- **Readahead is large.** The root disk has `read_ahead_kb = 8192`, so one random mmap fault can read 8 MB.
- **The multi-language source is stored in language blocks:**

  | Parts | Language |
  |---|---|
  | 0–127 | ceb |
  | 128–191 | de |
  | 192–335 | en |
  | … | … |

  Parts are sorted by doc_id, and only doc_id has Parquet statistics.

**Confirmed:**

- `index_in` per batch costs 0.79–0.91 s against a 5M set, ≈29 h in total, and its memory pool grows by 2.8× the set size on each call.
- Spelling and featured articles are biased toward Cebuano. Top words: 'kategoriya', 'mga'. Featured: 'Pamilya_(biyolohiya)'.
- usearch costs 244 B per vector, ≈6.6 GB at 27M.

## Critical

1. **The router turns common words into site names** (`router.py:332-337, 349-350`).
   - The first `-` segment of every registry id becomes a site word: 'who', 'joint', 'standard', 'open', 'us', 'army', 'uk'.
   - "who invented the telephone" is searched only inside WHO IRIS.
   - Fix:
     - make site words an explicit registry field;
     - never use a stop-list word or an English dictionary word as a site word;
     - scope hard only for `<site> <identifier>`;
     - otherwise apply the site as a soft prior.

2. **The cross-language stop list deletes content words** (`router.py:64-258, 302-309`, `text.py:94-169`). Examples:

   | Query | After stopwords |
   |---|---|
   | "causes of the cold war?" | "causes cold" |
   | "To Be or Not to Be" | "Not" |
   | "Doctor Who" | "Doctor" |
   | "vitamin e deficiency" | loses "e" |
   | "el nino" | loses "el" |

   Fix: apply only the query language's list, never drop capitalized or title tokens, or stop dropping words at all (IDF already discounts them).

3. **Redirect alias keys lose the language path** (`linkrank.py:180-184`, `indexer.py:89`).
   - All 29,446,241 redirects of the main source are unusable.
   - Resolution of 1.15M real English links:

     | Method | Links resolved |
     |---|---|
     | Exact doc id | 47.5% |
     | + shipped aliases | +0.8% |
     | + language-prefixed aliases | +30.0% |

   - Fix: key = `f"{head}/{native.rpartition('/')[0]}/{alias}"`, and `_alias_title` must stop cutting titles at '/' ("AC/DC").

4. **`alias_keys` overflows Arrow's 2 GiB string offsets** (`linkrank.py:150-153, 221-231`).
   - Measured failure: `concat` of 4 × 600 MB raises "ArrowInvalid offset overflow".
   - `doc_ids` alone is ≈1.75 GB.
   - Fix: use `large_string`, or keep the data chunked.

5. **`build_graph` rebuilds the hash table for every batch.**
   - Cheapest fix: about 20M links per `index_in` call.
   - Faster: `Table.join`.
   - Best: u64 keys plus `searchsorted`.

## High

6. **Speller arrays are misaligned** (`spelling.py:21, 311-315`).
   - The blob sits before `delete_hash`, so 7 of 8 files have a misaligned view, and `searchsorted` copies 93 MB on every lookup.
   - Measured: 17.46 ms against 0.153 ms aligned.
   - Fix: pad the blob (`WSPSPEL2`), or move it to the end.

7. **Binary codes are not centered** (`vectors.py:86-87, 252, 362`).
   - The mean has norm 0.869, and 429 of 768 bits are more than 90% constant.
   - Recall@10:

     | Candidates | Raw | Centered |
     |---|---|---|
     | 80 | 0.573 | 0.905 |
     | 160 | 0.679 | 0.953 |

   - Fix: store μ and binarize `x − μ`; ITQ later.

8. **Cold int8 rescoring reads gigabytes.** 2048 random rows:

   | Access method | Time | Data read |
   |---|---|---|
   | np.memmap | 2.3 s | 5.7 GB |
   | MADV_RANDOM | 345 ms | 9.4 MB |
   | sorted pread, 16 threads | 133 ms | 9.4 MB |

   Fix: `madvise(MADV_RANDOM)` on every mmap (vectors, usearch view, StaticRank, Speller), keep `scale.f32` in RAM, and fewer candidates with centered bits.

9. **Serving holds an SQLite read snapshot forever** (`vectors.py:134` `BEGIN` in view mode).
   - The WAL grows without bound, and readers see stale data.
   - Fix: autocommit reads, with `mode=ro`.

10. **Crash recovery is dead code** (`vectors.py:131, 156-157, 221-223, 440-455`).
    - `dirty` is never 1 on disk.
    - A crash between the graph replace and COMMIT leaves duplicate keys, which block resume permanently.
    - Fix: consistency check on open, fsync the graph partial and its dir, and `synchronous=FULL` in `save()`.

11. **The chunker drops most of a book.**
    - A "Notes"/"Sources" heading ends the document.
    - `max_chunks=256` caps a document at ≈51k words.
    - CJK paragraphs are not split, so 85% is truncated.
    - Fix:
      - skip those headings only in the last third of wiki-like sources;
      - count tokens with the tokenizer;
      - split CJK at 。！？;
      - lift the cap for books.

12. **Spelling normalizes differently at build and query time.** Examples:
    - Straße → strasse
    - λόγος → λόγοσ
    - नमस्ते gets split
    - "Don't" → "Don"

    The byte-length filter rejects 3- and 4-byte scripts, and an unknown word is always replaced (zzq → zzz). Fix: one shared normalizer and split, plus a minimum count ratio.

13. **Languages are mixed in rank, featured and fusion.** Cebuano stubs funnel PageRank mass, and the fusion boost reaches 2×. Fix: rank per (source, language), and use percentile features with a capped boost.

14. **The analyzer breaks CJK and Indic text** (`text.py:47-61`). Clauses become single tokens, `remove_long` counts bytes, and Hindi is shredded. Fix: per-language tokenizers, or CJK bigrams.

## Medium

15. The whole graph is saved every 250k passages, ≈360 GB of writes in total. Build the graph once at the end.
16. Dense retrieval ignores the source filter, and the effective oversample is ≈1.7×. Use per-source segments or a filter predicate.
17. Query stages run serially and repeat work: snippets for 50 hits, plus extra BM25 queries.
18. Re-embedding with `--langs` deletes the other languages' vectors as stale.
19. The producer reads every column before filtering (0.3–0.6 s per part, against 0.03 s).
20. Chunk workers fork after CUDA starts. Drop the pool (the chunker does 48k chunks/s on one core) or use spawn.
21. PageRank keeps 14–16 float64 vectors of size n (≈5 GB at 41M). A tolerance of 1e-6 is not reachable in 50 iterations.
22. Dense-only hits for removed docs leak into results, with the id shown as the title.
23. Serving never reloads a rebuilt graph, and the result cache is never invalidated.
24. No NaN check before quantization. Qwen3 runs in fp16; use bf16 and assert finite.
25. The reranker scores the lead chunk for lexical-only hits instead of the passage that matched.
26. Passage text is inline in SQLite (≈43 GB). The doc index is duplicated, and the cache is 2 MB.
27. Pages can disagree, because a deeper page re-runs the dense fetch.

## Low

- The `edge_chunk` comment is wrong.
- `max_docs` stops in storage order.
- The directory starts with "!!!".
- Non-Latin titles all fall under "Other".
- `_date_key` handles English only.
- `_RowIndex` takes 1 s to load and counts shadowed rows.
- `encode_vocabulary` takes 62 s and 1.2 GB.
- `docs_seen` has a race.
- The ETA ignores `--langs`.
- A thread pool is created on every `encode`.
- View mode opens files for append.
- `exact_search`/`_repair` build 27M Python ints.
- usearch tombstones cost 489 B per live vector.

## Design target

- **Rank.** Store u64 keys for doc ids, links and aliases (full path) in the Refinery. Resolve with `searchsorted`, build a CSR, and run float32 PageRank per (source, language) plus one global pass. Peak memory ≈3 GB.
- **Spelling.** One vocabulary per language, from a stratified sample of row groups. One shared normalizer, an aligned mmap, and lookups routed by the query's script and language.
- **Catalog.** Per-language partitions, with featured articles from the within-language rank.
- **Dense retrieval.**
  - RAM tier (≈7 GB): centered/ITQ codes, an HNSW in view mode with pages pre-loaded, and PQ/int4 codes for rescoring.
  - Disk tier: int8 vectors read with `MADV_RANDOM`/pread for the top ~100, zstd passage text, and numpy key maps.
  - Build the graph once. Updates go to a delta segment plus tombstones.
  - Partition by source, run stages in parallel, and reload on manifest change.
