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
