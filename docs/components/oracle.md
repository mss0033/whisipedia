# wsp-oracle: natural-language retrieval and ranking

Package `packages/wsp-oracle` (import `wsp_oracle`). Implements PLAN §9.3–9.5. Model choices, licences
and the measurements behind them: [ADR-0007](../adr/0007-oracle-models.md). There is no generative model:
summaries were removed by owner decision ([ADR-0008](../adr/0008-no-generative-ui.md)).

| Port (`wsp_core.ports`) | Implementation | Contract suite (`wsp_core.testing`) |
|---|---|---|
| `Embedder` | `TransformerEmbedder` (Hugging Face encoder, fp16 on CUDA, CPU fallback) | `embedder_contract.EmbedderContract` |
| `VectorIndex` | `ChunkVectorStore` (passages), `DocVectorIndex` (documents over it) | `vector_index_contract.VectorIndexContract` |
| `Reranker` | `CrossEncoderReranker`, `NullReranker` | (unit + GPU tests) |
| `LexicalIndex` | `OracleIndex` (the whole pipeline, wrapping `TantivyIndex`) | used by the API unchanged |

## Query pipeline

```
query ─► Router ─┬─► lexical rewrite ─► TantivyIndex (BM25, top 50) ──┐
                 └─► original text ─► Embedder ─► ChunkVectorStore ───┤ passages → best per document
                                                                      ▼
                         HybridSearcher: RRF (k = 60) × source priors × intent preferences
                                                                      ▼
                exact-title pins (lookup/navigate/define/locate; both sides of a comparison)
                                                                      ▼
                     CrossEncoderReranker over the top 30, scoring the passage that matched
                                                                      ▼
                       hits + best passage per hit + confidence (max relevance probability)
```

* **Router** (`router.py`, deterministic, ~20 µs): intents *lookup, define, how-to, navigate, locate,
  compare, ask* by rules in English plus the configured index languages. The lexical retriever gets
  the query without question scaffolding (BM25 ORs every term); the embedder gets the original
  wording. A site named in the query (`rfc 9110`, `wikipedia Alan Turing`) restricts the search to
  that site's sources. The router only *detects* page-name queries; the API's exact `navigate_to`
  resolver still decides whether a jump happens.
* **Dense retrieval** works on ~200-word passages (`chunker.py`): sections are never mixed, the title
  and section head every passage, and reference sections are skipped. Ids are `<doc_id>#<n>`
  and depend only on the text. `DocVectorIndex` keeps each document's best passage, so fusion stays
  at document level and the reranker sees the passage that matched.
* **Fusion** is `wsp_index.HybridSearcher` unchanged. Dense-only hits get titles from
  `TantivyIndex.lookup`.
* **Reranking** runs only with a GPU (or `reranker.cpu_fallback = true`). The best reranker
  probability is the retrieval confidence; below 0.05 the result is flagged `low_confidence`.
* `OracleIndex` caches the last `cache_size` result lists, so the API's three calls per results page
  (window, page, facets) run the pipeline once.

## Vector store (PLAN §9.4)

```
<index.root>/vectors/
    meta.sqlite        chunk id <-> key, doc id, passage text (optional); per-doc content hash + generation
    hnsw-b1.usearch    usearch HNSW over sign bits (hamming): 96 bytes per 768-d passage + graph links
    int8.bin           int8 vectors (row = key), memory-mapped, read only for the candidates
    scale.f32          per-row dequantization scale
```

A query takes `limit × oversample` candidates from the binary graph and rescores them exactly
against their int8 rows. Writes append; an updated passage gets a new key and its old row becomes
garbage until a rebuild. `save()` flushes rows, writes the graph atomically and then commits the id
map; a crash in between is repaired on open (keys missing from the graph are re-added from their
int8 rows). The API opens the store with `mode = "view"` (graph memory-mapped, read-only); the
embed job opens it writable. Like every index file it must be on local disk (`ensure_local`).

Sizing for English Wikipedia with e5-base (768 dimensions, ~27M passages, measured at 20k
documents: 240 B of graph, 768 B of int8 and 1.2 KB of `meta.sqlite` per passage): ~6.5 GB of
graph and binary codes (memory-mapped in view mode, so page cache rather than process RAM),
~21 GB of int8 rows and ~33 GB of `meta.sqlite` with passage text (`store_text = false` saves most
of that; the reranker then falls back to the lexical snippet for each hit).

## Embedding build

```
uv sync --all-packages --extra gpu
uv run python packages/wsp-oracle/scripts/embed_sources.py --sources wikipedia-en-zim
```

On a desktop that must stay usable, run it at the lowest CPU and IO priority (the GPU work is
unaffected, so it costs little throughput):

```
WSP_MAX_WORKERS=4 OMP_NUM_THREADS=4 MKL_NUM_THREADS=4 TOKENIZERS_PARALLELISM=false \
  nice -n 19 ionice -c 3 uv run python packages/wsp-oracle/scripts/embed_sources.py --sources wikipedia-en-zim
```

English Wikipedia is ~27M passages: about 11 h at the ~690 passages/s measured this way (7–8 h
unconstrained), ~2 GB of VRAM, ~3 GB of RAM, and ~60 GB under `<index.root>/vectors`.

`embed_generation()` reads the current sealed Refinery generation of each source, skips
documents whose `content_hash` the store already holds, re-chunks and replaces changed documents,
and deletes documents that disappeared (only on a full run). It saves every 250k passages, so the
same command resumes after an interruption, and a monthly update costs only the changed documents.
Reading and chunking run in a producer thread and `--workers` processes while the GPU embeds.
Progress (throughput and ETA) is logged every 10 s. `--limit N` embeds a prefix for benchmarks.

### Model files

Encoders and the reranker are Hugging Face snapshots under `~/.cache/whisipedia/models/<org>--<name>/`
(`huggingface_hub.snapshot_download(repo, local_dir=...)`). At runtime the Oracle sets
`HF_HUB_OFFLINE=1` and loads with `local_files_only=True`.

## Configuration: `[oracle]`

Every key is optional; unknown keys are rejected.

| key | default | meaning |
|---|---|---|
| `enabled` | `true` | `false`: lexical search with the router only |
| `models_dir` | `~/.cache/whisipedia/models` | base for relative model paths (never the repo, never the NAS) |
| `dense_sources` | `[]` | sources the embed job embeds (PLAN §9.4 priority corpora) |
| `rrf_k`, `lexical_weight`, `dense_weight` | `60`, `1.0`, `2.0` | fusion (dense counts double: measured, ADR-0007) |
| `depth` | `50` | candidates per retriever |
| `source_priors` | `{}` | multiplicative prior per source id |
| `chunk_words`, `max_chunks_per_doc` | `200`, `256` | chunker |
| `cache_size` | `256` | result lists kept for paging and facets |
| `budget_ms` | `100` | logged when a query exceeds it |
| `embedder.model` | `intfloat--multilingual-e5-base` | model directory |
| `embedder.preset` | `auto` | pooling and prefixes: `granite`, `e5`, `bge-m3`, `qwen3`, `gte` |
| `embedder.device` | `auto` | `auto`, `cuda`, `cuda:N`, `cpu` |
| `embedder.batch_size`, `embedder.max_tokens` | `256`, `512` | |
| `embedder.dimensions` | `0` | Matryoshka truncation (0 = the model's size) |
| `reranker.enabled`, `reranker.model` | `true`, `BAAI--bge-reranker-v2-m3` | |
| `reranker.device`, `reranker.cpu_fallback` | `auto`, `false` | without a GPU the reranker is skipped unless `cpu_fallback` |
| `reranker.top_k`, `reranker.max_tokens`, `reranker.batch_size` | `30`, `192`, `64` | 50 × 256 tokens costs ~240 ms on a shared GPU (ADR-0007) |
| `vectors.path` | `""` | `""` = `<index.root>/vectors` (local disk) |
| `vectors.mode` | `load` | `view` memory-maps the graph read-only (the factory always opens the API's store in view mode) |
| `vectors.oversample` | `16` | binary candidates per result, rescored with int8 (recall@10 0.98) |
| `vectors.connectivity`, `vectors.expansion_add`, `vectors.expansion_search` | `16`, `128`, `96` | HNSW |
| `vectors.store_text` | `true` | keep passage text for reranking |

## Wiring (composition root)

`build_oracle(cfg, lexical=..., sources=...) -> OracleServices` returns `index` (an `OracleIndex`,
which is a `LexicalIndex`), `searcher`, `router` and the open model objects. In
`wsp_cli.bootstrap.build_services` (real backends):

```python
oracle = build_oracle(cfg, lexical=open_index(cfg), sources=[s.id for s in specs])
Services(index=oracle.index, ...)
```

With no vector store or without `wsp-oracle[gpu]` this degrades to the router + lexical search,
which is what the API serves today.

## Evaluation (PLAN §9.5)

```
uv run --extra gpu wsp-oracle-eval --modes lexical,hybrid,full [--index-root DIR] --out report.json
```

The gold set `eval/gold/queries.jsonl` (125 hand-written queries across 14 domains, 10 of them
non-English, graded `relevant` doc ids) is generated from `eval/gold/queries.src.tsv` by
`eval/resolve_titles.py --zim <wikipedia ZIM>`, which follows redirects to the canonical article
ids. Add lines to the TSV and re-run; the set is meant to grow toward 500. Metrics: nDCG@10 (gains
`2^g - 1`), Recall@50, MRR, latency p50/p95/p99, per-domain nDCG. `tests/test_oracle_pipeline.py` runs the
same harness on `eval/gold/fixture.jsonl` in CI with deterministic fakes.

Benchmarks: `scripts/sample_zim.py` writes a sample Refinery generation from a ZIM (the gold
documents, their linked articles as distractors, and random article runs);
`scripts/bench_oracle.py models|ann|rerank` produces the numbers in ADR-0007.

## Known gaps

* `low_confidence` is computed but `SearchResults.low_confidence` in the API still means "no hits";
  the route would need `OracleIndex.result(q).low_confidence`.
* Dense retrieval cannot filter by source inside the ANN; filtered queries over-fetch 4×.
* Updated passages leave garbage int8 rows; a periodic rebuild compacts them.
