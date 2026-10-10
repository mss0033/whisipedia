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
| `jobs.py` | Persistent job queue: `discover`, `fetch_release`, `scrub_slice`, `drill`, `health_probe`, `prune`, `discovery_bots` |
| `scheduler.py` | Cadences → jobs, catch-up, rolling scrub, retention-window alerts |
| `durations.py` | ISO 8601 durations; calendar-anchored and interval cadences |
| `policy.py` | Promotion sanity checks, auto-pause rules, candidate policy gate → `Proposal` |
| `retention.py` | Pure GFS retention planner |
| `orchestrator.py` | `Wiring` (composition root input), `Orchestrator` (handlers, dispatch, limits, metrics) |
| `daemon.py` | `run(config)` main loop, signals, `wsp-orchestrator` CLI |
| `metrics.py`, `jsonlog.py` | Prometheus text file, JSON log lines |
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
| `discovery_bots` | global cycle |
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

## Retention (`retention.plan_retention`)

GFS per source from registry `retention.gfs` (default last 3, monthly 12, yearly forever): keep the
last N, the newest generation of each of the M most recent months *that have generations*, and the
newest of each year. Never pruned: the newest sealed generation, the current generation, anything
whose `holds_last_copy` is true, and anything not `SEALED`. Non-GFS policies (base + segments) are
not pruned. Without a `Wiring.last_copy` oracle **and** a `Wiring.pruner`, the prune job only records
a dry-run `retention.plan` ledger event. Property tests (hypothesis) check the invariants and
idempotence.

## Plugging in real components

Write a composition-root factory and point `orchestrator.wiring` at it:

```python
# e.g. packages/wsp-cli/src/wsp_cli/wiring.py
def build(config) -> Wiring:
    sources = load_registry(Path("registry/sources.yaml"), Path("registry/schema/source.schema.json"))
    return Wiring(
        sources=sources,
        adapters=load_adapter_entry_points(),  # {"kiwix-opds": KiwixAdapter(), ...}
        fetcher=HttpFetcher(config),
        vault=NasVault(config),
        ledger=JsonlLedger(config),
        refine=refinery.refine_generation,
        index=indexer.upsert_generation,
        scrub=warden.scrub_slice,
        drill=warden.monthly_drill,
        discovery_bots=curator.run_bots,
        gate=GateConfig(...),
        proposal_sink=curator.open_pr,
        pruner=vault.prune,
        last_copy=history.holds_last_copy,
    )
```

`wsp_orchestrator.testing:demo_wiring` is a self-contained fake world for smoke tests.

## Operations

* `wsp-orchestrator --config-dir config` (systemd user unit: `deploy/systemd/whisipedia-orchestrator.service`;
  container sketch: `deploy/compose.yaml`, where only the orchestrator has a network).
* SIGTERM/SIGINT: stop claiming, pipelines stop between steps, wait `shutdown_grace_seconds`;
  unfinished jobs return to the queue on next start.
* Logs: JSON lines on stderr (`ts, level, logger, msg` + structured fields).
* Metrics: `orchestrator.metrics_path` (Prometheus text, written atomically): `jobs`, `jobs_total`,
  `job_seconds_total`, `releases{state}`, `alerts_open`, `sources_paused`, `last_refresh_timestamp_seconds`.
* State DB: refuses cifs/smb/nfs/9p/drvfs filesystems and any path under `vault.root`.
  Losing it costs only re-planning: the vault and ledger remain the source of truth.
