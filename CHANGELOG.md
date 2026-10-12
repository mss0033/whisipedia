# Changelog

All notable changes are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]
### Changed: document collections admit all public material outside a narrow content core (owner decision 2026-10-11)
* Still excluded: classified or restricted documents; explosives and demolitions; weapons and munitions
  technical data; weapons employment and gunnery; targeting and fire control; weapon-system crew drills;
  CBRN weapons production; offensive cyber tooling.
* `Rules.builtin_review` now defaults to off, so a document is no longer held just for mentioning
  "chemical" or "combat". A source can opt back in.
* `SAFETY_DENY` is narrowed: "munitions", "small arms", "mine warfare", "fire support", "field
  artillery", "joint fires" and "close air support" are replaced by munitions manufacture and
  effectiveness, mine laying and gunnery.
* Registry: FEMA (x2), OSTI, DOE, CISA, Army, Joint Publications, MCDP and Calhoun now default to allow
  instead of review. WHO IRIS drops its review keywords.
* The Army filter keeps only the weapons, gunnery and targeting series and 19 core titles. The Joint
  Publications filter denies only JP 3-60 (targeting).

### Fixed: collections and ranking, second pass (2026-10-12)
* A partial listing (a timed-out page, a failed resolve) never becomes a generation and never marks
  documents withdrawn: the fetch stops with exit 4 and lanes retry. archive.org listings are cached a
  week; XML answers with stray control characters parse (OSTI had stopped at 600 of 51,881).
* Sources that listed nothing now list: Borden (Gatsby page data; ~676 chapter PDFs), USGS
  publications (one query per topic), Army CMH (GovInfo D 114, 236 books), DISA STIGs (the quarterly
  SRG-STIG library zip; zips are screened by README and member names). JP and MCDP have no official
  channel yet (GovInfo D 5 and D 214 hold other titles).
* Screening: firefighting 'fire control' is not weapons fire control; a bare SECRET heading is not a
  banner (only at a page edge); GOV.UK HTML publications are kept as HTML; Word (.docx) is read.
* Fetch prints every rejection with its rule; resumed fetches keep MD5-verified downloads; per-source
  concurrency (starts still 1/s) and archive.org connections.
* Search: tiered lexical search (every word in the query's language first: 17 ms instead of 0.4-3 s on
  130M documents); PubMed has no ranking bonus (rank_prior: 0); same-title duplicates collapse; how-to
  queries prefer Wikibooks inside bundles and MDWiki; ZIM-only sites show page counts and random pages.

### Fixed: browsing and search on the live library (2026-10-12)
* Pages failing at random: a site's recent changes scanned 200,000 feed rows under the GIL, and
  concurrent site pages starved the API's thread pool. The feed has a (source, seq) index now.
* Stack Exchange documents timed out (503): the ZIM pool kept one open archive per Kiwix source, and the
  115 GB Stack Overflow archive takes 99 s to open over SMB. All archives stay open and are opened ahead
  of time; the Navigator retries a 503 by itself ("This page is still loading").
* A restart answered 503 for a minute or more: the last library snapshot is saved and loaded at start.
  The first portal request read every Parquet footer of every source (13.5 s): catalog counts come
  from _SUCCESS.json and random picks from the memory-mapped id index (0.34 s cold).
* Planned sources with nothing downloaded, and raw data, are no longer listed as sites (portal,
  directory, webring, Related). Random page works on ZIM-only sites.
* Wikimedia links (HTML and plain text) open the library's copy. Stack Exchange tag listings are not
  documents and never results.
* Ranking: mdwiki (the only source with passage embeddings) won almost every query; sources without
  embeddings now get their lexical rank on the dense side too. Near-ties prefer the query's language.
  Snippets are cut only for the page shown, and come from the Refinery (Tantivy no longer stores text:
  -105 GB).
* Collections: a re-fetch of an interrupted ingest failed on identical staged files; resumed fetches
  re-downloaded everything; restarts after midnight UTC started a new generation; archive.org round
  trips held the CIA reading room to 0.3 requests/s (now up to 4 in flight, starts still 1/s).
* API memory: the Refinery id index is memory-mapped; model staging memory is trimmed; a health timer
  restarts an API that stops answering.

### Changed: issuer publication counts as release (owner decision 2026-10-11)
* New distribution requirement `published`: a document the issuer posted openly is released even when
  it prints no release line. 17 publisher-hosted sources use it. Restrictive markings still deny
  first: classified without a release stamp, FOUO/CUI, export control, Distribution B-F. DoD
  sources keep requiring Distribution A.
* archive.org batches can be stable hashes of the identifier (`buckets: N`). The CIA reading room uses
  100. Collection fetches report resolve progress every 500 items.

### Fixed
* API hang (2026-10-11): the API sat over its memory ceiling, which throttles rather than kills, and
  one start-page request took 3.8 hours.
  * The Refinery id index (20 B/doc, 0.8 GB for wikipedia-top10) is now a memory-mapped sidecar
    (`_ids.{h,loc}.npy`), written on first use. The document store no longer charges it to its budget.
  * Freed model-staging memory is trimmed after the Oracle loads, and the unit sets
    `MALLOC_ARENA_MAX=2`.
  * Runtime overrides capping the API at 3G were removed.
  * New `whisipedia-api-health.timer` (`tools/wsp-api-health`): restarts the API when the start page
    does not answer in 60 s, twice in a row.
  * Measured after the fix: anon 1.6-1.9 GB, down from 2.5-3.9 GB; start page 0.05 s warm.
* Vector search scans exactly once the candidate set covers the whole store. Before, a filtered search
  over many near-ties could miss matches in the approximate graph (an intermittent test failure).

### Fixed: backend audit 2026-10-11 (docs/audits/2026-10-11/)
- **Orchestrator.**
  - Heavy steps (refine, index, profile) run in capped child units, one at a time machine-wide (`HEAVY`/`INDEX` locks).
  - Crashes are counted per step (two in a state: backoff and a `crashed:` alert; never quarantine).
  - Lock, busy and NAS failures defer without an attempt.
  - The document-policy screen always runs, even when digests verified.
  - The promotion gate runs before the index step, so held content is never searchable.
  - Rows are version-checked; older releases never move CURRENT backwards (`wsp approve --force`).
  - No busy loop; a watchdog; workdir cleanup; crash-loop, stuck and RSS alerts; `wsp retry`. Schema v5 migrates on open.
  - A release with nothing new is superseded quietly.
- **Vault and fetch.**
  - Signing keys are pinned (never auto-created once the vault has signed content).
  - The repair ladder has a circuit breaker; a verified `wsp undegrade` clears DEGRADED.
  - I/O errors are UNREADABLE and never repaired; a mount canary runs before each scrub slice.
  - Ledger torn-append recovery (`wsp ledger recover`).
  - Unique temp files, with no-replace publish via `renameat2` or link (verified on the NAS share).
  - Per-source and per-generation vault locks.
  - Parity is bound to bytes verified in the same pass.
  - Checksum-less files are corroborated by a second full fetch; resume validators are required.
  - Expired signed redirects are re-resolved; HTTPS→HTTP is refused; a token-bucket scrub rate.
  - An incremental scrub inventory; manifest signatures are checked on read and paths validated.
- **Adapters.**
  - The doc-collection gate fails closed: requirements are checked against markings, all distribution statements are read, a document with no text layer is held, and text is normalized against ligature, dash, soft-hyphen and spacing bypasses.
  - Origins are enforced on every redirect hop; git sources are allow-listed and screened.
  - Generations read their full content set across earlier generations (PubMed, Kiwix bundles, Gutenberg, Open Library, arXiv; `depends_on`). Wikidata never publishes an empty lookup.
  - Cluster-ordered ZIM refine (about 10× faster).
  - PubMed DOIs come from the article, not its references.
  - Selective Wikidata parsing; byte-bounded batches; Kiwix re-release versions; boilerplate-free HTML text.
- **API.**
  - ZIM reads happen in worker processes (a SIGBUS kills a worker, not the API).
  - A background library snapshot means requests never touch the NAS (`/v1/system` 5 s → 2 ms).
  - Async health; bounded NAS pools with 503 + `Retry-After`.
  - One-retrieval search with consistent facets (offset ≤ 200).
  - Cached, gzip and ETag documents (text only with `fields=text`).
  - `/go` finds ZIM pages; a read-only change feed; no signing key created. API contract 1.4.0.
- **Rank, spelling, catalog, Oracle.**
  - Explicit site words; language-aware stopwords (MRR@10 0.41 → 0.79 on real titles).
  - Redirect aliases keep their language path.
  - Rank uses 64-bit vectorised keys, a CSR and per-language PageRank (32M nodes in 13 min, 3.7 GB).
  - `WSPSPEL2` per-language spelling with one shared normalizer.
  - Per-language featured articles and directory.
  - Centred binary codes (recall@10 0.81 → 0.96); MADV_RANDOM reads.
  - A crash-safe vector store; the chunker keeps whole books; CJK/Indic tokenization.
- **Index.**
  - Streaming, resumable refresh; thread-safe Refinery reads; capped suggest builds.
  - Refine flushes by size; set ids never repeat.
  - Only current generations are refined.
  - Safe visibility order (documents, then search, then feed).
  - The id index loads lazily.
  - Indexes built with older analyzers are refused: **rebuild the lexical index**, then rerun `wsp index rank` (`WSPRANK2`) and `wsp index spell` (`WSPSPEL2`).

### Fixed (2026-10-11 resource and robustness pass, first round)
- The API could crash (double free, SIGSEGV) when request threads read the same Parquet part at once:
  `RefineryReader` now serializes reads per open file (and bounds open files), with a 16-thread
  regression test that crashed the old reader.
- Index refresh no longer materializes a generation diff: `wsp_index.delta` keeps a 32-byte key per
  old document and reads full rows only for changed row groups; `sync.refresh` commits and publishes
  part by part, with a checkpoint stored atomically with the change-feed records, so a crash resumes
  without losing or repeating a record. First loads add without delete terms (Tantivy held ~1.2 KB
  per pending delete); without the old Refinery generation, stale documents are found from the index.
- Tantivy writer: 4 threads and a 1 GiB heap (measured 4× faster than the default), orphaned segment
  files from crashed runs are collected; meta SQLite gets a busy timeout, a WAL size limit and a
  checkpoint on close.
- Suggest: opening an index never rebuilds a large trie (a refine step that merely opened the index
  took 12.8 GB), commits during bulk loads defer the rebuild, rebuilds are capped at
  `suggest_max_keys` best-ranked keys, and the heavy-prefix fallback scans at most 4,096 keys.
- Refinery writer flushes by text size as well as document count (50,000 Gutenberg books were ~77 GB
  at flush) and sizes row groups by text; set-generation numbers never repeat after a set is deleted,
  and a sealed set is never removed; duplicate resolution reads each row group once; renames are
  followed by a directory fsync. Shadowed rows are kept in numpy.
- `wsp ingest` no longer skips files that `wsp collections fetch` just wrote (the settle wait now only
  applies to downloads that can still be growing) and exits 4 when a version is incomplete, so lanes
  cannot mistake "nothing ingested" for success.
- A partitioned source's Navigator site card now sums every current partition.
### Added
- `deploy/systemd/whisipedia-background.slice`: one memory, swap, CPU and IO budget for the
  orchestrator, index refreshes and ad-hoc jobs (`tools/wsp-bg NAME [MAX] -- CMD`); the API has its
  own memory ceiling.
### Changed
- usda-farmers-bulletins: the Internet Archive microfilm scans are rejected as a channel (owner
  decision 2026-10-11: not an official mirror); the agency's own 31 bulletins remain.
### Added
- Partitioned sources (ADR-0010, port revision v0.4): `access.partitioned: true` sources such as
  `usgs-historical-topo` (one state per run) and `cia-reading-room` (batches `b0001`…`b0090`) keep
  one generation `<cycle>.<partition>` per partition. `FileVault.promote` writes
  `CURRENT.d/<partition>.txt` (plus `CURRENT.txt` as before); `Vault.current_set` is the current
  generation of every partition; the indexer refines each partition once (`refinery/<id>/_parts/`)
  and indexes their union as a hard-linked set generation, so search, documents and history cover
  every partition fetched so far. `wsp collections fetch --partition` carries over only that
  partition's catalog; `wsp ingest` promotes unless a newer generation of the same partition is current.
- Retention plans partitioned sources per partition (`plan_partitioned_retention`) and never prunes a
  generation a current one reads from (new optional adapter capability `GenerationDependencies`,
  implemented by `doc-collection` from its catalog); `FileVault.prune` refuses every generation of the
  current set and the newest of each partition.
- Document policy in the release pipeline: `VerdictStatus.REJECTED` / `HELD`. A rejected document is
  deleted and a held one quarantined in the orchestrator work directory, the release goes on without
  them (ledger `release.withheld`, alert `withheld:<source>/<version>`, one doc-review proposal via
  `Wiring.on_withheld`); withholding never counts as a checksum failure or pauses the source.
- Source integrity (ADR-0009): per-release provenance (URLs, redirect chains, TLS peer certificates,
  HTTP validators, which mirror served which byte ranges, rejected mirrors, spot checks) stored as a
  signed `PROVENANCE.json` next to the manifest; digests re-fetched through an independent channel
  (Wikimedia checksum lists from the primary, Kiwix `.sha256` from `download.kiwix.org`, sidecars
  from a second mirror); any channel or mirror disagreement quarantines the release and pauses the
  source; origin pinning on first use with holds for unknown domains and alerts for new hosts or a
  changed certificate issuer; content profiles compared before promotion (namespaces, size
  distribution, changed/deleted fractions, new MIME types, `<script>` density, binary/executable
  content) with a signed `PROMOTION.json` and owner holds; known-good anchoring in retention;
  `ops.integrity` in `/v1/system` and an "integrity" check in `wsp doctor`; runbook "Investigating a
  held release / possible compromise".
- The API marks interactive use (`<state_dir>/last-interactive`); scrub and parity backfill pause
  while it is recent (`vault.yield_to_interactive_seconds`).
- Link-analysis static rank: per-source and cross-source PageRank over `DocRecord.links`, computed
  offline in bounded memory (`wsp index rank`, also the last step of `wsp index build`), blended into
  BM25 and fusion, and used for suggest priors and the portal's featured article.
- "Did you mean": noisy-channel spelling correction over the index's own vocabulary (symmetric-delete
  candidates), offered only for sparse or low-score results (`wsp index spell`).
- `SearchResults.scope` (API 1.3.0): a query naming a site reports where it was searched; the
  Navigator says quietly when it fell back to the whole library.
- Eval harness: `--rank-weights` and `--typos` ablations.
- Document collections (PLAN D11, D13, D14; research doc §5): the `doc-collection` adapter with
  discoverers for index pages (follow, table rows, detail pages), official APIs (NGA MSI, GovInfo,
  Congress.gov, NTRS, OSTI, USGS Publications Warehouse, GOV.UK, DSpace 7, TNM), OAI-PMH,
  archive.org (provenance recorded), S3 listings and git repositories; a deterministic document
  filter (series allow/deny lists, front-matter marking parse for Distribution B–F, FOUO/CUI,
  classification and export-control warnings, built-in §13 safety lists, keyword screen; unknowns go
  to owner review) with rules files in `registry/filters/`; edition currency (superseded chains,
  historical collections and cut-off dates) in a per-generation `meta/documents.jsonl`.
- `wsp collections dry-run` (metadata-only survey with filter counts and size estimates) and
  `wsp collections fetch` (≤ 1 request/s per host, restricted files deleted with an alert, held files
  quarantined with one review proposal per batch, `_incoming/_plans/targets.d/` ingest plans);
  `wsp ingest` reads `targets.d/*.json` and honours `meta.relpath`; accepting or rejecting a review
  proposal records the decision in the source's rules file.
- Historical banner (D13): `DocRecord.meta` (additive, stored as an optional Refinery column that
  older generations simply lack), `wsp_core.banners`, `Document.currency` and `Document.banner` in the
  API and SDK, and a period "Notice" box on Navigator document pages.
- `wsp plan multilingual` (D15): per-language selection of the best value-per-GB Kiwix ZIMs and
  Wikipedia text dumps within `[curator.multilingual] budget_tb_per_language`, written to
  `registry/plans/multilingual.yaml`; Kiwix selectors accept `name@flavour`.
- Registry: official access channels for the WAF-blocked sources (`access.mirrors`), discovery
  configurations for the top picks, D14 plans for `cia-reading-room` and `usgs-historical-topo`,
  `doe-fundamentals-handbooks`, `crs-reports`, ten `kiwix-multilingual-<lang>` bundles and
  `wikipedia-text-extra`; `[curator.api_keys]` for api.data.gov keys (local config only);
  runbook `docs/runbooks/document-collections.md`.
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
- par2 runs with `-t2` by default (`vault.parity_threads`, was 4) and `-m256` (`vault.parity_memory_mb`).
- Orchestrator state schema v4 (`origin_pins`, `provenance`); `orchestrator.shutdown_grace_seconds`
  defaults to 10 (was 60).
- Search totals and facets now count the same routed scope as the results (`rfc 9110` showed 0 hits
  with 28 in the facets).
- A registry source without `update.cadence` follows `acquirer.cycle`; explicit cadences are always
  overrides (`acquirer.follow_cycle_cadences` removed). `pytest` is now the optional `wsp-core[testing]` extra.
- Default update cycle is monthly instead of weekly, with quarterly as a single config option (PLAN §6.7).
### Fixed
- Partitioned collections (ADR-0010): only the last ingested partition was current, searchable and
  safe from retention; a partition's fetch carried another partition's catalog over (labelling its
  documents `withdrawn-from-index`); `wsp ingest` refused to promote a partition whose cycle sorted
  before the current one. One restricted or held document no longer fails a whole orchestrator
  release, counts as a checksum failure or auto-pauses the source.
- Browser-test findings (docs/testing/e2e-findings.md): Stack Exchange book front pages linked outside
  their book (E2E-4); re-released ZIMs (`_2026-07a`) were listed under a name that did not open
  (E2E-3); "wikipedia Alan Turing" did not jump to a page served straight from a ZIM (E2E-7); the
  oracle's result cache cut filtered result pages short (E2E-5) and the top hit depended on the page
  size (E2E-6); facet counts now count each kind on its own; live page text no longer includes
  script and style source (E2E-9); the HTML rewrite drops inline styles, which the Navigator's CSP
  reported as violations (E2E-1).
- Slow site list and System Monitor on a busy NAS (E2E-2): the vault caches parsed manifests
  (revalidated by mtime and size), the API reuses site cards for 30 s and refreshes them in the
  background, the ledger replay no longer blocks requests, and the Navigator renders a page after
  waiting at most 3 s for the site list.
- Lexical ranking: documents matching every query word get a coordination bonus
  (`index.coordination_boost`), so one rare word plus a source prior no longer outranks pages about
  the whole query.
- The orchestrator now stops within seconds of SIGTERM instead of being killed after systemd's
  `TimeoutStopSec`: downloads, scrub, parity and par2 are cancelled, job threads are daemon threads
  and a lingering non-daemon thread no longer keeps the process alive.
