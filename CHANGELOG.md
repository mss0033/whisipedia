# Changelog

All notable changes are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]
### Added
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
### Changed
- A registry source without `update.cadence` follows `acquirer.cycle`; explicit cadences are always
  overrides (`acquirer.follow_cycle_cadences` removed). `pytest` is now the optional `wsp-core[testing]` extra.
- Default update cycle is monthly instead of weekly, with quarterly as a single config option (PLAN §6.7).
