# wsp-oracle: natural-language retrieval and cited summaries

Package `packages/wsp-oracle` (import `wsp_oracle`). Implements PLAN §9.3–9.5 and the summary
rules of §10.3. Model choices, licences and the measurements behind them: [ADR-0007](../adr/0007-oracle-models.md).

| Port (`wsp_core.ports`) | Implementation | Contract suite (`wsp_core.testing`) |
|---|---|---|
| `Embedder` | `TransformerEmbedder` (Hugging Face encoder, fp16 on CUDA, CPU fallback) | `embedder_contract.EmbedderContract` |
| `VectorIndex` | `ChunkVectorStore` (passages), `DocVectorIndex` (documents over it) | `vector_index_contract.VectorIndexContract` |
| `Reranker` | `CrossEncoderReranker`, `NullReranker` | (unit + GPU tests) |
| `Generator` | `CitedGenerator` over `LlamaServerGenerator` | (unit tests with a mock server) |
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
                     CrossEncoderReranker over the top 50, scoring the passage that matched
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
  at document level and the reranker and summaries see the passage that matched.
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
    hnsw-b1.usearch    usearch HNSW over sign bits (hamming): 48 bytes per 384-d passage + graph links
    int8.bin           int8 vectors (row = key), memory-mapped, read only for the candidates
    scale.f32          per-row dequantization scale
```

A query takes `limit × oversample` candidates from the binary graph and rescores them exactly
against their int8 rows. Writes append; an updated passage gets a new key and its old row becomes
garbage until a rebuild. `save()` flushes rows, writes the graph atomically and then commits the id
map; a crash in between is repaired on open (keys missing from the graph are re-added from their
int8 rows). The API opens the store with `mode = "view"` (graph memory-mapped, read-only); the
embed job opens it writable. Like every index file it must be on local disk (`ensure_local`).

Sizing for English Wikipedia at 384 dimensions (~25M passages): ~1.2 GB of binary codes plus
~3–4 GB of graph links in RAM (or page cache in view mode), ~9.6 GB of int8 rows and ~35 GB of
`meta.sqlite` with passage text (`store_text = false` saves most of that; the reranker then falls
back to the lexical snippet for each hit).

## Embedding build

```
uv sync --all-packages --extra gpu
uv run python packages/wsp-oracle/scripts/embed_sources.py --sources wikipedia-en-zim
```

`embed_generation()` reads the current sealed Refinery generation of each source, skips
documents whose `content_hash` the store already holds, re-chunks and replaces changed documents,
and deletes documents that disappeared (only on a full run). It saves every 250k passages, so the
same command resumes after an interruption, and a monthly update costs only the changed documents.
Reading and chunking run in a producer thread and `--workers` processes while the GPU embeds.
Progress (throughput and ETA) is logged every 10 s. `--limit N` embeds a prefix for benchmarks.

## Summaries (PLAN §10.3)

```
uv run wsp-oracle-llm          # llama-server on 127.0.0.1:8738 with the GGUF from [oracle.generator]
```

* `LlamaServerGenerator` streams from `llama-server`'s OpenAI-compatible endpoint. It refuses any
  URL that is not loopback and ignores proxy environment variables. Thinking is switched off on
  the server (`--reasoning off`) and per request; `reasoning_content` is never shown.
* `Summarizer` refuses without calling the model when the best passage's reranker probability is
  below `generator.min_confidence`. The model is told to cite with `[n]` and to answer `NO_ANSWER`
  when the passages do not answer; that sentinel becomes a refusal too.
* `CitationGuard` validates the stream sentence by sentence. A sentence passes only with at least
  one `[n]` naming a passage that exists; unknown markers are removed; sentences left without a
  valid marker are dropped. Markers after the full stop (`... end. [2]`) belong to that sentence;
  abbreviations, initials and decimals do not end sentences.
* `Summarizer.events()` emits the API's SSE types: `notice` (refusal; "N sentences without a valid
  citation were left out"), `token`, `citation` (`index`, `doc_id`, `title`, `address`), `done`,
  `error`.
* `CitedGenerator` implements the `Generator` port for the existing `/v1/summarize` route: it
  parses the route's numbered passages back out of its prompt and applies the gate and the guard.
  The port carries text only, so a refusal arrives as one plain sentence and dropped sentences are
  not announced; a route that calls `Summarizer.events()` gets the `notice` events.

### Installing the generator

The prebuilt CUDA build of llama.cpp is unpacked user-local, never in the repo:

```
mkdir -p ~/.local/share/whisipedia/llama.cpp && cd ~/.local/share/whisipedia/llama.cpp
# from https://github.com/ggml-org/llama.cpp/releases (build b11541 was tested):
#   llama-<build>-bin-ubuntu-cuda-12.8-x64.tar.gz and cudart-llama-<build>-bin-ubuntu-cuda-12.8-x64.tar.gz
tar xzf llama-*.tar.gz && tar xzf cudart-*.tar.gz && cp cudart-*/lib* llama-*/ && ln -sfn llama-<build> current
# model (sha256 91898433cf5ce0a8f45516a4cc3e9343b6e01d052d01f684309098c66a326c59):
curl -L -o ~/.cache/whisipedia/models/llm/Qwen3.5-9B-Q6_K.gguf \
  https://huggingface.co/unsloth/Qwen3.5-9B-GGUF/resolve/main/Qwen3.5-9B-Q6_K.gguf
```

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
| `rrf_k`, `lexical_weight`, `dense_weight` | `60`, `1.0`, `1.0` | fusion |
| `depth` | `50` | candidates per retriever |
| `source_priors` | `{}` | multiplicative prior per source id |
| `chunk_words`, `max_chunks_per_doc` | `200`, `256` | chunker |
| `cache_size` | `256` | result lists kept for paging and facets |
| `budget_ms` | `100` | logged when a query exceeds it |
| `embedder.model` | `ibm-granite--granite-embedding-97m-multilingual-r2` | model directory |
| `embedder.preset` | `auto` | pooling and prefixes: `granite`, `e5`, `bge-m3`, `qwen3`, `gte` |
| `embedder.device` | `auto` | `auto`, `cuda`, `cuda:N`, `cpu` |
| `embedder.batch_size`, `embedder.max_tokens` | `256`, `512` | |
| `embedder.dimensions` | `0` | Matryoshka truncation (0 = the model's size) |
| `reranker.enabled`, `reranker.model` | `true`, `BAAI--bge-reranker-v2-m3` | |
| `reranker.device`, `reranker.cpu_fallback` | `auto`, `false` | without a GPU the reranker is skipped unless `cpu_fallback` |
| `reranker.top_k`, `reranker.max_tokens`, `reranker.batch_size` | `50`, `256`, `64` | |
| `vectors.path` | `""` | `""` = `<index.root>/vectors` (local disk) |
| `vectors.mode` | `load` | `view` memory-maps the graph read-only (the factory always opens the API's store in view mode) |
| `vectors.oversample` | `8` | binary candidates per result, rescored with int8 |
| `vectors.connectivity`, `vectors.expansion_add`, `vectors.expansion_search` | `16`, `128`, `96` | HNSW |
| `vectors.store_text` | `true` | keep passage text for reranking and summaries |
| `generator.enabled` | `false` | summaries on |
| `generator.url` | `http://127.0.0.1:8738` | loopback only |
| `generator.model`, `generator.server_binary` | `llm/Qwen3.5-9B-Q6_K.gguf`, `~/.local/share/whisipedia/llama.cpp/current/llama-server` | used by `wsp-oracle-llm` |
| `generator.context`, `generator.gpu_layers` | `8192`, `999` | per slot |
| `generator.min_confidence` | `0.2` | refuse below this reranker probability |
| `generator.passages`, `generator.passage_chars` | `8`, `1500` | prompt budget |
| `generator.temperature`, `generator.timeout_s` | `0.2`, `60` | |

## Wiring (composition root)

`build_oracle(cfg, lexical=..., sources=...) -> OracleServices` returns `index` (an `OracleIndex`,
which is a `LexicalIndex`), `generator`, `summarizer`, `searcher`, `router` and the open model
objects. In `wsp_cli.bootstrap.build_services` (real backends):

```python
oracle = build_oracle(cfg, lexical=open_index(cfg), sources=[s.id for s in specs])
Services(index=oracle.index, generator=oracle.generator, ...)
```

With no vector store or without `wsp-oracle[gpu]` this degrades to the router + lexical search,
which is what the API serves today.

## Evaluation (PLAN §9.5)

```
uv run --extra gpu wsp-oracle-eval --modes lexical,hybrid,full [--index-root DIR] [--summaries 30] --out report.json
```

The gold set `eval/gold/queries.jsonl` (125 hand-written queries across 14 domains, 10 of them
non-English, graded `relevant` doc ids) is generated from `eval/gold/queries.src.tsv` by
`eval/resolve_titles.py --zim <wikipedia ZIM>`, which follows redirects to the canonical article
ids. Add lines to the TSV and re-run; the set is meant to grow toward 500. Metrics: nDCG@10 (gains
`2^g - 1`), Recall@50, MRR, latency p50/p95/p99, per-domain nDCG; with `--summaries`, claim-level
faithfulness (each sentence must cite a passage that supports it, judged by the local model or,
in CI, by word overlap) and summary first-token latency. `tests/test_oracle_pipeline.py` runs the
same harness on `eval/gold/fixture.jsonl` in CI with deterministic fakes.

Benchmarks: `scripts/sample_zim.py` writes a sample Refinery generation from a ZIM (the gold
documents, their linked articles as distractors, and random article runs);
`scripts/bench_oracle.py models|ann|rerank` produces the numbers in ADR-0007.

## Known gaps

* The `Generator` port carries text only: refusals and dropped-sentence counts reach the browser as
  text, not `notice` events, until the summarize route calls `Summarizer.events()` and uses the
  Oracle's best passages instead of `doc.text[:2000]`.
* `low_confidence` is computed but `SearchResults.low_confidence` in the API still means "no hits";
  the route would need `OracleIndex.result(q).low_confidence`.
* Dense retrieval cannot filter by source inside the ANN; filtered queries over-fetch 4×.
* Updated passages leave garbage int8 rows; a periodic rebuild compacts them.
* The faithfulness judge in CI is lexical; the nightly number needs the LLM judge.
