# Changelog

All notable changes are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]
### Added
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
