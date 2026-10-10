# Orchestrator (`packages/wsp-orchestrator`)

The single daemon that drives Whisipedia (PLAN §6.5): it discovers releases, moves each one through the
release state machine, schedules health probes, scrub slices, drills, pruning and discovery bots, and
applies policy. It is coded only against the ports in `wsp_core.ports`, so the vault, ledger, fetcher,
adapters, refinery and index plug in without changes here.

## Modules

| Module | Responsibility |
|---|---|
| `pipeline.py` | `ReleasePipeline`: DISCOVERED → … → PROMOTED, retries, quarantine, review holds |
| `store.py` | SQLite (WAL) state on local disk: releases, schedule marks, alerts, source health, ledger outbox |
| `jobs.py` | Persistent job queue: `discover`, `fetch_release`, `scrub_slice`, `drill`, `health_probe`, `prune`, `discovery_bots`, `parity` |
| `scheduler.py` | Cadences → jobs, catch-up, rolling scrub, retention-window alerts |
| `durations.py` | ISO 8601 durations; calendar-anchored and interval cadences |
| `policy.py` | Promotion sanity checks, auto-pause rules, candidate policy gate → `Proposal` |
| `retention.py` | Pure GFS retention planner |
| `orchestrator.py` | `Wiring` (composition root input), `Orchestrator` (handlers, dispatch, limits, metrics) |
| `daemon.py` | `run(config)` main loop, signals, `wsp-orchestrator` CLI |
| `metrics.py`, `jsonlog.py` | Prometheus text file, JSON log lines |
| `bots.py` | `Bot` protocol, `BotContext`, `BotResult` |
| `curator/` | Bots (`KiwixWatcher`, `WikimediaRankingBot`, `RegistryHealthBot`, `AdapterSmokeBot`, `GapMiner`), `ProposalFiles`, `registry_edit` (overlay, registry text edits) |
| `testing.py` | In-memory fakes of the ports, crash injector, harnesses, `demo_wiring` |

## Release pipeline

```
DISCOVERED → PLANNED → FETCHING → FETCHED → VERIFIED → SEALED → REFINED → INDEXED → PROMOTED
     any non-terminal state ──→ FAILED ──(backoff)──→ PLANNED            FAILED ──(N attempts)──→ QUARANTINED
```

| Step | Work (all idempotent) |
|---|---|
| DISCOVERED → PLANNED | `adapter.plan(ctx, vault.current(), ref)`; plan persisted as JSON |
| PLANNED → FETCHING | `vault.stage()` (reopens) |
| FETCHING → FETCHED | `adapter.fetch()` (resumes partial files); skipped if the generation is already sealed |
| FETCHED → VERIFIED | `adapter.verify()` per planned file; a mismatch counts toward auto-pause |
| VERIFIED → SEALED | `vault.ingest(upstream=..., expected=<FetchResult>)` each file as `upstream/<name>`, `vault.seal()`; reuses an already sealed generation |
| SEALED → REFINED | `refine(spec, generation, path)` hook → document count |
| REFINED → INDEXED | `index(spec, generation)` hook |
| INDEXED → PROMOTED | sanity checks, then `vault.promote()`; workdir cleaned |

* Transitions are checked against `wsp_core.model.allowed_transitions`.
* **Exactly one ledger event per transition** (`kind="release.state"`, `data={from,to,attempt,event_id,…}`).
  The state change and an outbox row commit in one SQLite transaction; the outbox is then appended to
  the ledger with `idempotency_key=event_id`, so re-flushing after a crash returns the existing event.
  Fetch digests are persisted with the release (`fetched_json`); FETCHED → VERIFIED skips the
  adapter's re-hash when the fetcher verified every upstream digest. Tests crash at every checkpoint (with and without a mid-run failure) and assert the
  exact event sequence, one seal, and a verified hash chain.
* Failures go to FAILED with exponential backoff (`orchestrator.retry`); after `max_attempts` the
  release is QUARANTINED and a critical alert is raised.
* **Sanity hold** (PLAN §6.4): if the document count drops by more than 2 % from the last promoted
  generation, or the total size is outside ±10 % of the least-squares trend of up to 6 previous
  generations, the release stays INDEXED with `hold_reason`, an alert is raised and the previous
  generation keeps serving. `Orchestrator.approve_release()` / `reject_release()` resolve it.

## Scheduling (PLAN §6.5, §6.7, §8.2, §8.5)

| Job | Cadence |
|---|---|
| `discover`, `prune` (per active source) | source cadence |
| `health_probe` (per active source, also while paused) | `acquirer.health_probe_interval` (P1W) |
| `discovery_bots` (key = bot name) | the bot's `cadence` (`registry-health`, `adapter-contract`: P1W; the rest: global cycle) |
| `parity` (`backfill`) | `orchestrator.parity_interval` (PT6H), and after every promotion; continues every `parity_continue` while work remains |
| `drill` | `orchestrator.drill_interval` (P1M) |
| `scrub_slice` | continuous: slice *i*/*n* due at `cycle_start + i·(scrub_cycle_days/n)` |

* **Global cycle**: `acquirer.cycle = "monthly"` runs on `anchor_day` (15th) every month; `"quarterly"`
  on the 15th of Jan/Apr/Jul/Oct.
* **Per-source override**: registry `update.cadence`. A source without one follows the global cycle;
  any value present is a deliberate override (even `P1M`). Whole-month durations that divide a
  year (`P3M`, `P6M`, `P1Y`) are calendar-anchored; others (`P1W`, `P10D`) are intervals from the
  last success.
* **Catch-up**: due = `cadence.next_due(last_success)`; never-run jobs are due immediately. After
  downtime each overdue entry runs once (not once per missed period) and discovery jumps straight to
  the newest upstream release. Missed scrub slices run back-to-back until the pass is complete.
  Failed entries are retried no sooner than `job_retry_gap`.
* **Retention-window alert**: for adapters with an upstream retention window
  (`orchestrator.retention_windows`, e.g. `wikimedia-dumps = "P60D"`), a critical alert fires once the
  cycle is overdue by half the window; it resolves when the source completes a cycle.

## Policy

* **Auto-pause** (PLAN §5.4) — one alert, the current generation keeps serving, discovery stops until
  `Orchestrator.resume_source()`:
  license reported upstream differs from the registry; two consecutive checksum mismatches; newest
  upstream release older than 3× the cadence; format changed.
  Adapters report license and format via the typed `ReleaseRef.license` and `ReleaseRef.format`.
* **Policy gate** (PLAN §5.2–5.4): `evaluate_candidate(candidate, GateConfig, sources)` returns a
  `Proposal` (decision `auto-promote | review | reject`, weighted score and breakdown, per-check
  results, a PR title and Markdown body, and the drafted registry entry). Auto-promote needs all of:
  allow-listed license, a version/variant of an *active* source with the same license (or a Kiwix
  title in an allowed category), fits every domain budget, score ≥ 3.5. A license axis below 3 is
  `reject`. Proposals are stored (`proposals` table), logged as `policy.decision` ledger events and
  handed to `Wiring.proposal_sink`.

## Curator automation (PLAN §5.3-5.5)

| Bot | Network | Produces |
|---|---|---|
| `kiwix-opds` | yes | a `kiwix-title` candidate for every catalog title in `curator.kiwix_categories` that no registry selector covers (topic subsets of a held project are skipped) |
| `wikimedia-ranking` | yes | a `new-variant` of `wikipedia-top10` when the top 10 Wikipedias by article count change (always reviewed: size unknown) |
| `registry-health` | yes | after two failed weekly probes of every `access.endpoints` URL: an alert and a `registry-fix` proposal |
| `adapter-contract` | yes | weekly live smoke per adapter in use (`discover` offers a sane release, `plan` accepts it): alerts only |
| `gap-miner` | no | "seek a source for X" from `gaps.jsonl` (broken links by site/domain) and `queries.jsonl` (searches with no results, by query and word) |

Every candidate goes through `Orchestrator.handle_candidates`: the policy gate decides, a candidate
key is proposed **once** (whatever the owner decides later), `policy.decision` is logged, and
`Wiring.proposal_recorder` writes one YAML file per proposal (`ProposalFiles`, default
`<index.root>/../proposals/`). `AUTO_PROMOTE` is applied by `Wiring.promoter` to the git-ignored
overlay `config/registry.local.yaml` (merged by `load_registry`, effective on the next daemon start);
the committed registry changes only through `wsp proposals accept`, which edits
`registry/sources.yaml` in the working tree for the owner to commit. `REJECT` is final.
`/v1/proposals` and the `ops.proposals` block of `/v1/system` show them; the last summary of each bot
is in the state DB (`meta bot:<name>`), its notes are handed to its next run (`BotContext.previous`).

## Parity (PLAN §8.1)

`Wiring.parity` runs one bounded slice of `wsp_vault.paritykeeper.ParityKeeper` over the sealed
generations in scope (`vault.parity_scope = "all"` by default, or `"irreplaceable"`: flagged sources
plus generations holding a last copy), oldest first, at most `vault.parity_bytes_per_run` per job and
`vault.parity_bytes_per_sec`. Data is verified before it is encoded; damaged parity found by the
scrub is quarantined and created again. A summary is stored as `meta parity:last` and logged as
`parity.run`; generations that cannot get parity raise the `parity-skipped` alert.

## Retention (`retention.plan_retention`)

GFS per source from registry `retention.gfs` (default last 3, monthly 12, yearly forever): keep the
last N, the newest generation of each of the M most recent months *that have generations*, and the
newest of each year. Never pruned: the newest sealed generation, the current generation, anything
whose `holds_last_copy` is true, and anything not `SEALED`. Non-GFS policies (base + segments) are
not pruned. Property tests (hypothesis) check the invariants and idempotence.

**Last copy.** `discover` and `health_probe` store what upstream currently serves (state schema v3,
`upstream_offers`). `Orchestrator.holds_last_copy` says a generation is the last copy unless upstream
was seen serving its version within `orchestrator.offer_max_age` (P14D); with no observation it is
kept. `Wiring.last_copy` overrides this. With a `Wiring.pruner`, planned generations are retired
(`FileVault.prune` moves them to `pruned/`, never deletes; one `retention.pruned` event each);
`orchestrator.prune_dry_run = true` (or no pruner) only records the `retention.plan` event, which
lists the last copies it protected.

## Plugging in real components

`config/default.toml` points `orchestrator.wiring` at `wsp_cli.wiring:build_wiring`, which builds the
real components from config: registry sources, entry-point adapters, `HttpFetcher`/`Aria2Fetcher`,
`FileVault` + `JsonlLedger`, the Refinery/Tantivy indexer hooks (`wsp_cli.indexer`), rolling scrub
with the repair ladder (parity findings are routed to the parity keeper), drills on the smallest
sealed generation, `FileVault.prune`, the bots from `curator.bots`, proposal files, the overlay
promoter, live gate budgets (`curator.domain_budget_tb` against sealed bytes per domain) and the
parity backfill.

`wsp_orchestrator.testing:demo_wiring` is a self-contained fake world for smoke tests.

## Operations

* `wsp-orchestrator --config-dir config`, installed as a systemd user unit by `wsp service install`
  (`deploy/systemd/`, see [the runbook](../runbooks/install-services.md)); container sketch:
  `deploy/compose.yaml`, where only the orchestrator has a network.
* `wsp doctor` checks readiness; `wsp status`, `wsp parity status` and `/v1/system` show state.
* SIGTERM/SIGINT: stop claiming, pipelines stop between steps, wait `shutdown_grace_seconds`;
  unfinished jobs return to the queue on next start.
* Logs: JSON lines on stderr (`ts, level, logger, msg` + structured fields).
* Metrics: `orchestrator.metrics_path` (Prometheus text, written atomically): `jobs`, `jobs_total`,
  `job_seconds_total`, `releases{state}`, `alerts_open`, `sources_paused`, `last_refresh_timestamp_seconds`.
* State DB: refuses cifs/smb/nfs/9p/drvfs filesystems and any path under `vault.root`.
  Losing it costs only re-planning: the vault and ledger remain the source of truth.
