# ADR-0007: Oracle models, vector index and LLM runtime

- Status: Proposed; the LLM decision is superseded by [ADR-0008](0008-no-generative-ui.md) (no
  generative features: summaries and the llama.cpp runtime were removed)
- Date: 2026-10-10

## Context
Phase 3 needs an embedding model, an approximate nearest-neighbour (ANN) index, a reranker and a
local LLM for cited summaries (PLAN §9.3–9.5, §10.3). All of them must run offline from local
files on one RTX 4090 (24 GB, shared with the desktop) and 23 GiB of RAM, with open licences.

Measurements come from `packages/wsp-oracle/scripts/` on a sample of the English Wikipedia ZIM
(2026-08): the 198 documents of the gold set, ~4,600 articles they link to (topical distractors)
and random article runs. 50,000 documents, 45.7M words, chunked into 278,519 passages of 137 words
on average; the model comparison used 100,000 of those passages (gold documents kept). Quality is
the 125-query gold set (`eval/gold/queries.jsonl`, 10 of the queries are not in English). The GPU was shared with
other work during every run (`nvidia-smi` showed ~80 % utilisation from another process during
the reranker and summary runs), so the latencies are upper bounds.

## Decision

### Embedding model: `intfloat/multilingual-e5-base` (MIT, 278M parameters, 768 dimensions)

Dense-only retrieval on 100,000 passages (passages per second include tokenization; fp16, batch 256):

| model (licence) | dims | passages/s | nDCG@10 | Recall@50 | nDCG@10 non-English |
|---|---|---|---|---|---|
| **multilingual-e5-base** (MIT) | 768 | **1,076** | 0.944 | 0.988 | **0.886** |
| granite-embedding-97m-multilingual-r2 (Apache-2.0) | 384 | 493 | 0.944 | 0.992 | 0.839 |
| multilingual-e5-small (MIT) | 384 | 1,040 | 0.929 | 0.992 | 0.794 |
| granite-embedding-311m-multilingual-r2 (Apache-2.0) | 768 | 174 | 0.936 | 0.988 | 0.843 |
| bge-m3 (MIT) | 1024 | 431 | 0.936 | 0.992 | 0.859 |
| Qwen3-Embedding-0.6B (Apache-2.0) | 1024 | 83 | 0.948 | 0.992 | 0.890 |

e5-base matches the best English quality, is close to the best non-English score, and is the
fastest model that does both. The ModernBERT-based Granite models are slow here because
transformers runs them padded without flash-attention. Qwen3-Embedding is marginally better but 13
times slower (about two weeks for English Wikipedia). `Alibaba-NLP/gte-multilingual-base` was not
benchmarked: it needs `trust_remote_code`, which is not acceptable for an offline system.

Cost of the 768 dimensions (PLAN §9.4 assumed 384): per passage 96 bytes of binary code plus about
140 bytes of HNSW links in RAM, 768 bytes of int8 on disk. For ~27M English Wikipedia passages
that is ~6.5 GB of graph (memory-mapped in the API, so the page cache holds it), ~21 GB of int8
rows and ~33 GB of `meta.sqlite` with passage text, all on local NVMe.

### Vector index: usearch binary HNSW + int8 rescoring (Apache-2.0)

Sign-bit codes in a hamming HNSW find `limit × oversample` candidates, which are rescored exactly
against memory-mapped int8 vectors. Measured on the e5-base store (116,887 passages, the 125 gold
queries, top 150 passages as the pipeline asks for):

| oversample | recall@10 vs exact | p50 | max |
|---|---|---|---|
| 4 | 0.948 | 2.1 ms | 3.8 ms |
| 8 | 0.961 | 3.4 ms | 5.5 ms |
| **16 (default)** | **0.981** | 5.0 ms | 9.4 ms |

On 100,000 384-dimensional Granite vectors (500 perturbed-vector queries, top 10), plain usearch
HNSW reaches recall 0.914 with int8 (85 MB RAM) and 0.993 with f16 (152 MB), against 0.915 for the
binary design at x16. The binary design wins on RAM for the same recall: its 96-byte codes are an
eighth of f16 at 768 dimensions, and only the candidates' int8 rows are read. `hnswlib` has no
binary or int8 mode, and `faiss-cpu`'s `IndexBinaryHNSW` is the same design with a heavier
dependency; `bench_oracle.py ann --with-faiss` compares it when faiss is installed.

### Reranker: `BAAI/bge-reranker-v2-m3` (Apache-2.0, 568M, multilingual cross-encoder)

Measured on the shared GPU: 50 passages × 256 tokens took 237 ms (p50), 50 × 192 tokens 87 ms.
`ibm-granite/granite-embedding-reranker-english-r2` (149M, English only) was no faster under the
same load (210 ms). Defaults are therefore the top 30 with 192 tokens.

Gold-set results on a 20,000-document sample (lexical index, e5-base vectors, the full pipeline):

| mode | nDCG@10 | Recall@50 | MRR | p50 | p95 |
|---|---|---|---|---|---|
| lexical (router + BM25 + title pins) | 0.787 | 0.897 | 0.852 | 28 ms | 52 ms |
| dense only | 0.939 | 0.988 | 0.971 | 48 ms | 70 ms |
| hybrid, RRF dense weight 1 | 0.902 | 0.984 | 0.935 | 48 ms | 74 ms |
| hybrid, RRF dense weight 2 (default) | 0.915 | 0.988 | 0.953 | 40 ms | 64 ms |
| full: hybrid + rerank top 50 × 256 | 0.932 | 0.984 | 0.964 | 301 ms | 342 ms |
| full: hybrid + rerank top 30 × 192 (default) | 0.919 | 0.988 | 0.955 | 96 ms | 126 ms |

On this gold set the dense retriever alone is the strongest; BM25 adds noise to natural-language
questions and the reranker recovers part of it. The reranker stays on by default because its
probabilities are the confidence signal for the summary gate and for `low_confidence`. Where the
100 ms budget matters more, `reranker.enabled = false` gives the hybrid row. The gold set favours
article-level answers; reranking should be re-evaluated as the set grows past its 125 queries.

### LLM: Qwen3.5-9B, GGUF Q6_K (Apache-2.0), served by llama.cpp `llama-server` (MIT)

`unsloth/Qwen3.5-9B-GGUF`, file `Qwen3.5-9B-Q6_K.gguf`, 7.46 GB, sha256
`91898433cf5ce0a8f45516a4cc3e9343b6e01d052d01f684309098c66a326c59`. About 7.5 GB of VRAM with two
8k-token slots. llama.cpp's prebuilt CUDA 12.8 Linux release (build b11541) runs unchanged under
WSL2 and needs no compiler or Python CUDA wheels. vLLM was not chosen: it reserves most of the GPU
up front, which conflicts with the embedder, the reranker and the desktop. Larger current models
(Qwen3.6/3.8-27B, about 17–19 GB at Q4) do not leave room for the rest on a shared 24 GB card; they
are a config change (`generator.model`) on a dedicated GPU. Thinking mode is off (`--reasoning off`).

Summary quality on 25 gold queries (top 8 passages each, LLM judge on claim support):
faithfulness 0.947 (89 of 94 sentences supported), 100 % of shown sentences cite an existing
passage, no refusals. The first *validated* sentence arrived after 1.56 s (p50) and 2.55 s (p95) on
the shared GPU. The model's own first token takes 0.24 s for a short prompt and about 0.5 s with
eight passages. The guard holds each sentence until it is complete, which adds the time to write
the first sentence.

### Storage of weights
Everything lives under `~/.cache/whisipedia/models/` (Hugging Face snapshots as `<org>--<name>/`,
GGUF under `llm/`) and llama.cpp under `~/.local/share/whisipedia/llama.cpp/`. Nothing goes in the
repository or on the NAS. At runtime `HF_HUB_OFFLINE=1` and `local_files_only=True` are set, and
`llama-server --offline` is used.

## Consequences
- 768-dimensional vectors double the int8 storage planned in PLAN §9.4. The HNSW graph is memory-
  mapped, so it costs page cache rather than process RAM.
- Embedding English Wikipedia (~27M passages) takes about 7–8 h at full speed, or about 11 h at the
  ~690 passages/s measured with `nice -n 19` and 4 CPU threads.
- Summary first-sentence latency misses the 1 s target on a shared GPU. Options, in order: a
  dedicated GPU; streaming the first sentence before validation, with retraction (needs a client
  event); a citation-first sentence format.
- Swapping any model is a config change, but the vector store records its model and refuses a
  mismatched embedder, so changing the embedder means re-embedding.
