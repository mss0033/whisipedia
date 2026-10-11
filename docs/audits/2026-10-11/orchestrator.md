# Audit: orchestrator (wsp-orchestrator, wsp_cli wiring/integrity/locks/service/ops, systemd units)

**Status key:** **[F]** fixed, **[P]** partial, **[O]** open.

## What production showed

- **Six global OOM kills** of the main process: 23:21 (15.3 GB anon), 00:09 (17.6), 00:34 (18.8), 00:56 (17.5), 01:32 (10.3) and 02:05 (12.8).
  - No cgroup limit was set.
  - `oom_score_adj 200` made the orchestrator the kernel's pick.
- **Two stop timeouts**, both with no "shutdown requested" log line. The likely cause is that a GIL-holding C call starved the signal handler.
- **Releases unchanged by the crashes:** gutenberg/2026-10-10, pubmed/pubmed26-u1671 and kiwix-wikisource-en-all/2026-09 are all `sealed` with `attempts=0`.
  - Job 1044 has been `running` since 02:31.
  - Two refines ran at once in one process.
- **The main loop spins:** `sqlite_sequence(jobs)` = 121,972 against 63 rows, i.e. about 83 ignored INSERTs per second.

## CRITICAL

**C1 [O] Crashes are never counted, so one bad release crash-loops the service forever.**
- Where:
  - `jobs.py:369-377`: `recover` decrements `attempts`.
  - `pipeline.py:305-318`: `attempts` only grows in `_fail`, which a SIGKILL never reaches.
  - The unit has `Restart=on-failure` and no `StartLimit*`.
- Fix:
  - Persist `step_started_at`/`step_token` before the heavy side effect, in the same transaction.
  - At startup, an unfinished token adds `crash_count += 1`.
  - After 2 crashes in the same state: FAILED with backoff and an alert ("crashed in refine, probably OOM").
  - After N crashes: hold, not quarantine.
  - In the unit: `StartLimitIntervalSec=2h` and `StartLimitBurst=4`.

**C2 [P] The refine step held a fixed 50,000 documents in memory, whatever their size.**
- Measured: about 4.9× the UTF-8 size in memory, so about 77 GB for 50k Gutenberg books.
- `FETCH_RELEASE: 2` lets two refines run in one process.
- Restarts rmtree the partial output (`indexer.py:166-167`).
- Partly fixed in `b9d64e6`: the writer now flushes by text size.
- Open:
  - Stream through `pq.ParquetWriter`.
  - Checkpoint per part, so a restart resumes instead of starting over.
  - Run refine in a capped child process.

**C3 [F] Every open or close of the index rebuilt the whole suggest trie inside the orchestrator** (`lexical.py:149-153, 300-311, 344-345`).
- Fixed in `b9d64e6`.
- Still open: `IndexHooks.refine` builds a full `Indexer`/`TantivyIndex` it never uses. It should not.

**C4 [F] The index step diffed whole generations in memory.** Fixed in `b9d64e6` (`wsp_index.delta`).

## HIGH

**H1 [O] The orchestrator ignores the "index" lock, and lock contention quarantines good releases.**
- Demonstrated: LockBusy in the index step led to QUARANTINED after 5 attempts and 14h10m.
- Other unlocked writes:
  - the refine rmtree;
  - the fixed trie `.partial` name;
  - the read-modify-write of `index-state.json`;
  - the 5 s busy timeout.
- Fix:
  - One flock per writer, taken by every writer: orchestrator, CLI and timer.
  - If it's busy, the step is DEFERRED and doesn't count as an attempt.
  - Classify lock, busy and NAS errors as "retry without an attempt".

**H2 [O] Generations are indexed before the promotion gate** (`pipeline.py:443-448`).
- A held generation is searchable.
- The nightly build re-indexes the vault's `CURRENT` and flips search back.
- Fix: promote (or hold) first, then index. Alternatively, index into a staging slot that is switched atomically together with `vault.promote`.

**H3 [O] The document policy is skipped whenever the fetcher verified digests.**
- `pipeline.py:503-509` skips `adapter.verify` when `FetchResult.verdict` is OK. The doc-collection adapter carries upstream sha256/md5, and its `verify` is the only place `screen_file`/`well_formed` run.
- Demonstrated: the REJECTED `restricted.pdf` was sealed and promoted.
- Fix: split checksum verification from a policy screen that always runs.

**H4 [O] No optimistic concurrency.**
- `step()` passes a stale record to `_fail`, and `update_release` writes every column with no version guard. Demonstrated effects:
  - (a) Withheld files come back after a later failure.
  - (b) A ledger-flush error after a commit turns REFINED into FAILED and loses `doc_count`.
  - (c) An owner's reject during a running step is overwritten, and the release is promoted anyway.
- Health read-modify-write loses a concurrent pause.
- Fix:
  - `row_version`, with `UPDATE … WHERE version=?`.
  - Read the record inside the transaction.
  - Swallow ledger-flush errors after commit.
  - Update health with SQL deltas.

**H5 [O] Approving an old held release moves CURRENT backwards.** Refuse that without `--force`, and supersede older holds.

**H6 [O] The main loop spins at 10–80 Hz** (`orchestrator.py:836-843`, `jobs.py:390-392`, `scheduler.py:169-178, 216-250`).
- Fix:
  - Wake only for work that can actually be enqueued.
  - Use a Condition for job completion.
  - SELECT before INSERT.
  - Touch alerts only when their state changes.

**H7 [O] Shutdown is unreliable.**
- Only the daemon's `stop` is set by the signal handler; `orch.stopping` is set later.
- `KillMode=control-group` sends SIGTERM to pool workers at the same moment.
- A `BrokenProcessPool` can then be counted as a failure.
- Heavy steps can't be cancelled.
- Fix:
  - Run heavy steps as child processes or units.
  - The handler sets `orch.stopping` directly.
  - `KillMode=mixed`.

## MEDIUM

- **M1 [O]** Every failure counts the same and retries restart from PLANNED. Add an error taxonomy, resume from the failed state, and an un-quarantine command.
- **M2 [O]** Leases are never enforced. A job whose `queue.fail` raises stays `running` forever and blocks its `(kind, key)`. Add a watchdog and per-step timeouts.
- **M3 [O]** A failed COMMIT wedges the store (`store.py:497-507`). Use `try COMMIT except ROLLBACK; raise`. Also:
  - clean up the workdir for held, failed and quarantined releases (38 GB now);
  - put a version in the `_quarantine` path.
- **M4 [O]** One connection with `synchronous=FULL` serializes every thread. Ledger appends happen inline, and the watermark can move backwards.
- **M5 [O]** The integrity guard pins hosts from releases it later quarantines. Stage pins with the release and commit them on promotion.
- **M6 [O]** Content profiling:
  - It is blind for partitioned sources (`integrity.py:110-112` looks in `refinery/<sid>/<gid>`, not `_parts/<gid>`).
  - It reads the whole `text` column just for lengths. Use `utf8_length` on narrow reads, or store the length.
- **M7 [O]** Retention:
  - Prune can move aside a held generation.
  - `known_good` is lost if the state DB is lost.
  - Partitioned sources are never pruned.
- **M8 [O]** Discovery compares versions as strings (`"u999" > "u1000"`).
- **M9 [O]** No crash-loop, OOM, stuck-state or RSS alerts. `resumed_releases` is always 0.

## LOW

- Units: no `KillMode=mixed`, `OOMPolicy`, `StartLimit*` or `TasksMax`. `IOSchedulingClass=idle` causes priority inversion on long transactions.
- PubMed hardcodes 4 workers.
- Auto-promotions are applied before the proposal is recorded.
- The feed handle leaks if `index.close()` raises.
- Leases use the wall clock (WSL clock jumps).

## Design target

1. **Small control plane.** The orchestrator stays under 300 MB. Refine, index, suggest, rank, spell, embed, profile and parity each run as a transient systemd unit in `whisipedia-background.slice`, with these properties:
   - `MemoryHigh=5G`, `MemoryMax=6G`, `MemorySwapMax=512M`
   - `KillMode=mixed`, `TimeoutStopSec=60`

   The orchestrator reads the unit's `Result=`. `oom-kill` counts as a resource failure: retry with smaller batches, and alert after two.
2. **One heavy job at a time.** A machine-wide `locks/heavy.lock` is taken by the orchestrator, `wsp index build`, the timer and `tools/wsp-bg`, plus the index-writer lock. Busy means DEFER.
3. **Pressure-aware scheduling.** Check MemAvailable and PSI first. When the desktop is busy, freeze the heavy unit rather than kill it.
4. **Resumable heavy work** everywhere, with crash accounting per step.
5. **Ordering.** Promotion gate, then `vault.promote`, then index. Use version-checked updates.
6. **Unit hygiene.**
   - `KillMode=mixed`
   - `OOMPolicy=continue`
   - `StartLimitIntervalSec=2h`, `StartLimitBurst=4`
   - Per-unit `memory.peak` metrics, and an alert on any OOM.
