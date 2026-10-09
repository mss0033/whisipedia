# ADR-0004: Technology stack

- Status: Proposed
- Date: 2026-10-09

## Decision
- Python 3.12+ (uv workspaces) for orchestration, adapters, API and SDK. It has the strongest ecosystem for libzim, MediaWiki XML, Parquet and ML.
- Rust for hot paths when profiling justifies it. Tantivy is used through its Python bindings at first.
- TypeScript for the Navigator front end, with minimal dependencies and all assets vendored (no CDN).
- Open formats only: upstream originals, Parquet, JSONL+zstd, UTF-8 text.

## Consequences
Components sit behind ports (PLAN §12.2), so any choice above can be replaced by passing the contract suites.
