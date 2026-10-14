# Runbook: rebuild indexes from scratch

Everything under `index.root` (local disk) is derived from the sealed vault and can be rebuilt:
Refinery Parquet (`refinery/`), Tantivy and suggest tries (`lexical/`), the change feed
(`changes.sqlite`), the portal/directory catalog (`catalog/`) and `index-state.json`. The search
logs (`gaps.jsonl`, `queries.jsonl`) and the proposals next to it are not derived: keep them.

## When

* A format bump (Refinery format, Tantivy schema or analyzer languages changed: the index refuses to
  open with a mismatched schema signature).
* The local disk failed or the index is corrupt.
* Only the start page / directory looks wrong: rebuild just the catalog with `wsp index catalog`.

## Steps

1. Stop the API and the index timer:
   `systemctl --user stop whisipedia-api whisipedia-index.timer` (the orchestrator may keep running,
   but stop it too if a release is being indexed).
2. Move the old index aside (do not delete it until the new one works):
   `mv <index.root> <index.root>.old && mkdir <index.root>`, then move `gaps.jsonl*` and
   `queries.jsonl*` back into the new directory.
3. Rebuild: `wsp index build` (all sources) or `wsp index build --source <id>`. Each sealed
   generation is refined once (resumable: a crash re-does one generation), then the current one is
   indexed and its catalog is built. Expect hours for Wikipedia-sized sources.
4. Note: the change feed restarts empty. Clients holding an old cursor get `invalid_cursor` and must
   start again (Living's contract).
5. `wsp doctor`, `wsp search "alan turing"`, open `wsp://home`, then start the services again and
   delete `<index.root>.old`.

## Merging the search index's segments (`wsp index merge`)

Bulk loads leave the Tantivy index in many segments (32 on 2026-10-14); each query walks every
segment's term dictionary and postings. `wsp index merge` packs them into segments of at most
`--target-docs` documents (35M by default: 32 became 7) with `tools/tantivy-merge`, a small Rust
tool on the same Tantivy version as the Python binding (tantivy-py exposes no merge).

- Build once: `cd tools/tantivy-merge && cargo build --release` (its `rust-toolchain.toml` pins
  Rust 1.90; rustup installs it beside the default toolchain).
- `wsp index merge --dry-run` prints the plan. Without it, the command waits for the HEAVY and
  INDEX locks (up to `--wait-hours`), so no refine, release step or nightly build writes meanwhile.
- Searches keep working: readers switch to the merged segments when `meta.json` changes. The
  merged segments need free space about the size of what is rewritten until the old files go.
- Run it in a quiet period: it reads and writes the whole rewritten part of the index (hours).
