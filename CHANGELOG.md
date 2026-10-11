# Changelog

All notable changes are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]
### Added
- Link-analysis static rank: per-source and cross-source PageRank over `DocRecord.links`, computed
  offline in bounded memory (`wsp index rank`, also the last step of `wsp index build`), blended into
  BM25 and fusion, and used for suggest priors and the portal's featured article.
- "Did you mean": noisy-channel spelling correction over the index's own vocabulary (symmetric-delete
  candidates), offered only for sparse or low-score results (`wsp index spell`).
- `SearchResults.scope` (API 1.3.0): a query naming a site reports where it was searched; the
  Navigator says quietly when it fell back to the whole library.
- Eval harness: `--rank-weights` and `--typos` ablations.
- `wsp-oracle` (Phase 3, ADR-0007): section-aware passages, a multilingual embedder on the GPU,
  a binary-HNSW + int8 vector store, a deterministic query router, hybrid retrieval with
  cross-encoder reranking, a resumable embedding job
  (`scripts/embed_sources.py`), an evaluation harness with a 125-query gold set, `build_oracle()`
  for the composition root, and `VectorIndex`/`Embedder` contract suites.
- The remaining T1 source adapters (`docs/components/adapters.md`):
  - `arxiv`: OAI-PMH metadata from `oaipmh.arxiv.org` (≤ 1 request / 3 s, resumption tokens,
    incremental `from=`) plus the newest PDF months from the public GCS mirror; `html_ref` points at the
    newest PDF version; optional PDF text with `pypdfium2`.
  - `wsp ingest` seals the arXiv bulk pull chunk by chunk as part generations `<snapshot>.pdf.<chunk>`,
    only for chunks the pull has finished, by rename after the MD5 check (`FileVault.adopt_many`).
  - `pubmed` (baseline + updates, newest version wins, `DeleteCitation` honoured), `courtlistener`
    (opinions joined with clusters, dockets and courts through a local SQLite lookup), `open-library`
    (explicit catalog records), `gutenberg` (catalog, RDF and plain-text feeds streamed from the ZIP,
    linked to the Kiwix ZIM), `wikidata` (multilingual entity documents plus a local QID lookup for
    entity linking), `osm-planet` (named places with Wikidata QIDs via pyosmium).
  - Parallel bz2 decoding for single-stream dumps (`indexed_bzip2`), about 8× faster on 15 threads.
- Phase 5 "Autonomy": a Refinery-backed catalog (featured article, On this day, directory categories,
  uniform random pages; precomputed at index time); par2 parity for all sealed generations
  (`vault.parity_scope`), created and backfilled after sealing with a signed `PARITY.json` sidecar,
  verified by the scrub and used by the repair ladder; real retention pruning guarded by a last-copy
  oracle from cached upstream offers; Curator bots (Kiwix OPDS watcher, Wikimedia top-10 ranking,
  registry health probes, adapter contract smoke, gap miner) feeding the policy gate, proposal files,
  a local registry overlay and `wsp proposals`; System Monitor operations data and `/v1/proposals`
  (API 1.2.0); systemd user units with `wsp service`, `wsp doctor`; runbooks in `docs/runbooks/`.
- Integration of the component packages: port revision v0.2 (ADR-0005) with typed fetch results and
  errors, `Vault.adopt`, a separate `Warden` port, `Ledger.tail` and idempotent appends, document
  aliases, and the browsing ports moved into wsp-core; a real composition root (`wsp_cli.bootstrap`,
  `wsp_cli.wiring:build_wiring`) with an Ed25519 signing key generated outside the repository; operator
  commands `wsp ingest`, `wsp index build`, `wsp scrub`, `wsp drill`, `wsp approve|reject|resume` and
  `wsp status`; an offline end-to-end test from `_incoming` to API search; `config/local.toml.example`.
- Project plan, source research, source registry seed, initial ADRs, repository hygiene configuration.
### Removed
- **Generative summaries, by owner decision (ADR-0008):** the Navigator's "Summarize these results"
  button, `POST /v1/summarize` and its `Summary*` schemas (API 1.3.0), `summarize` in the SDK, the
  `Generator` port (port revision v0.3), the llama.cpp generator and `wsp-oracle-llm`,
  `[oracle.generator]`, `[api] demo_generator` and the summary-faithfulness evaluation. The results
  page is fully extractive; dense retrieval and reranking stay.
### Changed
- Search totals and facets now count the same routed scope as the results (`rfc 9110` showed 0 hits
  with 28 in the facets).
- A registry source without `update.cadence` follows `acquirer.cycle`; explicit cadences are always
  overrides (`acquirer.follow_cycle_cadences` removed). `pytest` is now the optional `wsp-core[testing]` extra.
- Default update cycle is monthly instead of weekly, with quarterly as a single config option (PLAN §6.7).
