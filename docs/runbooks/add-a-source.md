# Runbook: add a source

Sources enter the library only through the registry (PLAN §5.1). There are three ways in.

## 1. Accept a Curator proposal

The bots file proposals every cycle (Kiwix titles, Wikimedia ranking changes, gap-miner topics,
registry fixes). Each is a YAML file under `<index.root>/../proposals/` with the score breakdown,
the gate's checks and a drafted registry entry.

```sh
wsp proposals list --status open
wsp proposals show 12
wsp proposals accept 12 --note "worth the 40 GB"   # edits registry/sources.yaml in the working tree
git diff registry/sources.yaml && git commit -am "feat(registry): add <source>"
wsp proposals reject 13 --note "duplicate of X"   # never proposed again
```

`accept` validates the whole registry before writing. A proposal without a drafted entry (a gap-miner
"seek a source for X" task) is only marked accepted: research the source and add it by hand.

## 2. Auto-promotions

Within policy (allow-listed license, a version/variant of an active source or a Kiwix title in
`curator.kiwix_auto_categories`, within the domain budget, score >= 3.5) the gate promotes on its own
into `config/registry.local.yaml` (git-ignored). It takes effect when the orchestrator restarts
(`systemctl --user restart whisipedia-orchestrator`). To make it permanent, `wsp proposals accept`
the matching proposal (status `applied`): the entry moves from the overlay into the registry.

## 3. By hand

1. Add an entry to `registry/sources.yaml` (copy a similar one): `id`, `name`, `domain`, `tier`,
   `status: active`, `license` (SPDX), `legal_basis`, `access.adapter` (+ selector/endpoints),
   `update.strategy`, `retention`, `scores`. Omit `update.cadence` to follow the global cycle.
2. `wsp registry validate`
3. Commit, then restart the orchestrator: its `discover` job picks the source up immediately.
4. For a bulk first download outside the orchestrator, put the files under
   `<vault>/_incoming/<source>/<version>/` and run `wsp ingest --source <id>`, then `wsp index build`.

Check the result with `wsp status` and `wsp://system` (freshness against the SLO, parity coverage).
