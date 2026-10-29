"""Control-plane regressions of the 2026-10-11 audit: main-loop spin (H6), shutdown (H7), watchdog
(M2), retention (M7), version order (M8), health alerts (M9), schema migration and LOW items."""

from __future__ import annotations

import json
import os
import signal
import sqlite3
import threading
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from wsp_core.model import Generation, ReleaseState, SourceSpec, SourceStatus
from wsp_orchestrator.clock import ManualClock, SystemClock
from wsp_orchestrator.daemon import _install_signals  # pyright: ignore[reportPrivateUsage]
from wsp_orchestrator.errors import ResourceBusyError
from wsp_orchestrator.jobs import JobKind, JobQueue, JobStatus
from wsp_orchestrator.orchestrator import DaemonLockedError, Orchestrator
from wsp_orchestrator.pipeline import RefineResult
from wsp_orchestrator.policy import Candidate, CandidateKind, Decision, GateConfig, Proposal
from wsp_orchestrator.settings import OrchestratorSettings, capped_workers
from wsp_orchestrator.store import StateStore
from wsp_orchestrator.testing import FakeWorld, make_settings, make_spec, record_promotions
from wsp_orchestrator.versions import is_newer, newest, version_key

T0 = datetime(2026, 10, 15, 6, tzinfo=UTC)
FILES = {"a.zim": b"a" * 1000}


def build(
    tmp_path: Path,
    sources: list[SourceSpec],
    *,
    clock: Any = None,
    wiring_extra: Mapping[str, Any] | None = None,
    **kw: Any,
) -> tuple[Orchestrator, FakeWorld]:
    clock = clock or ManualClock(T0)
    world = FakeWorld.build(tmp_path, clock, sources)
    kw.setdefault("scrub_slices", 1)
    orch = Orchestrator(
        make_settings(tmp_path, **kw), world.wiring(**(wiring_extra or {})), clock=clock, check_local=False
    )
    return orch, world


def _jobs_seq(store: StateStore) -> int:
    rows = store.query("SELECT seq FROM sqlite_sequence WHERE name='jobs'")
    return int(rows[0]["seq"]) if rows else 0


# --- H6: no spinning ---------------------------------------------------------------------------------


def test_main_loop_sleeps_while_a_due_job_waits_for_capacity(tmp_path: Path) -> None:
    gate = threading.Event()
    orch, world = build(
        tmp_path,
        [make_spec("a"), make_spec("b")],
        clock=SystemClock(),
        poll_interval_seconds=30.0,
        job_limits={**dict.fromkeys(JobKind, 1), JobKind.FETCH_RELEASE: 1},
    )
    for sid in ("a", "b"):
        world.adapters[sid].add_release("2026-10-01", {"x.txt": b"x"})

    def slow_refine(source: SourceSpec, generation: Generation, generation_dir: Path) -> RefineResult:
        gate.wait(10)  # occupies the only FETCH_RELEASE slot
        return world.refinery.refine(source, generation, generation_dir)

    orch.pipeline.refine = slow_refine
    orch.start()
    claims = {"n": 0}
    real_claim = orch.queue.claim

    def counting_claim(*a: Any, **k: Any) -> Any:
        claims["n"] += 1
        return real_claim(*a, **k)

    orch.queue.claim = counting_claim  # type: ignore[method-assign]

    def loop(seconds: float) -> int:
        n, stop = 0, time.monotonic() + seconds
        while time.monotonic() < stop:  # the daemon's main loop body
            n += 1
            orch.scheduler.tick()
            orch.dispatch()
            orch.maybe_watchdog()
            orch.maybe_refresh_metrics()
            orch.waker.wait(min(orch.sleep_seconds(), max(stop - time.monotonic(), 0)))
        return n

    loop(1.0)  # discovery, probes, bots and drills finish; one refine holds the only slot
    seq, claims["n"] = _jobs_seq(orch.store), 0
    loops = loop(2.0)
    seq_after = _jobs_seq(orch.store)
    gate.set()
    assert orch.shutdown(grace_seconds=10)
    orch.close()
    assert loops <= 2, f"{loops} loop iterations in 2 s with nothing to do"
    assert claims["n"] <= 2
    assert seq_after == seq, "no AUTOINCREMENT ids burnt by ignored INSERTs"


def test_a_finished_job_wakes_the_loop(tmp_path: Path) -> None:
    orch, _ = build(tmp_path, [make_spec("a")], clock=SystemClock(), poll_interval_seconds=30.0)
    orch.queue.enqueue(JobKind.DRILL, "global", now=orch.clock.now())
    t0 = time.monotonic()
    assert orch.dispatch() == 1
    assert orch.waker.wait(5) is True
    assert time.monotonic() - t0 < 2
    orch.shutdown(grace_seconds=2)
    orch.close()


def test_enqueue_of_an_active_job_writes_nothing(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "s.sqlite3", check_local=False)
    q = JobQueue(store)
    assert q.enqueue(JobKind.PRUNE, "a", now=T0) is not None
    changes = store._conn.total_changes  # pyright: ignore[reportPrivateUsage]
    for _ in range(50):
        assert q.enqueue(JobKind.PRUNE, "a", now=T0) is None
    assert store._conn.total_changes == changes  # pyright: ignore[reportPrivateUsage]
    assert _jobs_seq(store) == 1
    store.close()


def test_an_open_alert_is_not_rewritten(tmp_path: Path) -> None:
    orch, _ = build(tmp_path, [make_spec("a")])
    from wsp_orchestrator.alerts import Alert, Severity

    alert = Alert("x", Severity.WARNING, "m")
    assert orch.alerter.raise_(alert)
    changes = orch.store._conn.total_changes  # pyright: ignore[reportPrivateUsage]
    for _ in range(20):
        assert not orch.alerter.raise_(alert)
        assert not orch.alerter.resolve("never-open")
    assert orch.store._conn.total_changes == changes  # pyright: ignore[reportPrivateUsage]
    orch.close()


def test_scheduler_wakeup_is_never_in_the_past(tmp_path: Path) -> None:
    orch, _ = build(tmp_path, [make_spec("a")], job_retry_gap=timedelta(hours=1))
    orch.start()
    now = orch.clock.now()
    # everything due has an active job now: nothing to wake up for before the next cadence
    wake = orch.scheduler.next_wakeup(now)
    assert wake is None or wake > now
    assert orch.sleep_seconds() >= 0.05
    orch.close()


# --- H7: shutdown -----------------------------------------------------------------------------------


@pytest.mark.skipif(threading.current_thread() is not threading.main_thread(), reason="signals")
def test_signal_handler_sets_stopping_at_once_and_wakes_the_loop(tmp_path: Path) -> None:
    orch, _ = build(tmp_path, [make_spec("a")])
    stop = threading.Event()
    previous = _install_signals(stop, orch)
    try:
        signal.raise_signal(signal.SIGTERM)
        assert orch.stopping.is_set()
        assert stop.is_set()
        assert orch.waker.wait(0.5) is True  # the wakeup fd made the pipe readable
    finally:
        for sig, old in previous.items():
            if sig == -1:
                signal.set_wakeup_fd(old)
            else:
                signal.signal(sig, old)
        orch.close()


def test_a_job_failing_during_shutdown_is_deferred_not_counted(tmp_path: Path) -> None:
    orch, _ = build(tmp_path, [make_spec("a")])

    def drill() -> Mapping[str, object]:
        orch.stopping.set()  # SIGTERM arrives; the pool under the job is torn down
        raise RuntimeError("BrokenProcessPool")

    orch.wiring.drill = drill
    orch.queue.enqueue(JobKind.DRILL, "global", now=T0)
    [job] = orch.claim_ready()
    orch.execute(job)
    after = orch.queue.get(job.id)
    assert after.status is JobStatus.PENDING
    assert after.attempts == 0
    assert "job-dead:drill:global" not in orch.alerter.open_keys()
    orch.close()


def test_a_busy_lock_in_a_job_defers_it(tmp_path: Path) -> None:
    orch, _ = build(tmp_path, [make_spec("a")])

    def scrub(index: int, count: int) -> None:
        raise ResourceBusyError("another 'scrub' is running")

    orch.wiring.scrub = scrub
    orch.queue.enqueue(JobKind.SCRUB_SLICE, "rolling", now=T0, payload={"slice": 0, "slices": 1})
    [job] = orch.claim_ready()
    orch.execute(job)
    after = orch.queue.get(job.id)
    assert (after.status, after.attempts) == (JobStatus.PENDING, 0)
    assert after.run_after == T0 + timedelta(minutes=10)
    orch.close()


def test_one_daemon_per_state_db(tmp_path: Path) -> None:
    a, world = build(tmp_path, [make_spec("a")])
    a.start()
    b = Orchestrator(
        make_settings(tmp_path, scrub_slices=1), world.wiring(), clock=a.clock, check_local=False
    )
    with pytest.raises(DaemonLockedError):
        b.start()
    b.close()
    a.close()
    c = Orchestrator(
        make_settings(tmp_path, scrub_slices=1), world.wiring(), clock=a.clock, check_local=False
    )
    c.start()  # released on close
    c.close()


# --- M2: watchdog ------------------------------------------------------------------------------------


def test_watchdog_requeues_a_job_whose_outcome_could_not_be_recorded(tmp_path: Path) -> None:
    orch, _ = build(tmp_path, [make_spec("a")])
    orch.queue.enqueue(JobKind.DRILL, "global", now=T0)
    [job] = orch.claim_ready()

    def broken_complete(*a: Any, **k: Any) -> None:
        raise sqlite3.OperationalError("disk I/O error")

    orch.queue.complete = broken_complete  # type: ignore[method-assign]
    orch.queue.fail = broken_complete  # type: ignore[method-assign]
    with pytest.raises(sqlite3.OperationalError):
        orch.execute(job)
    assert orch.queue.get(job.id).status is JobStatus.RUNNING  # blocks (drill, global) forever...
    orch.watchdog()
    assert orch.queue.get(job.id).status is JobStatus.PENDING  # ...until the watchdog requeues it
    orch.close()


def test_watchdog_alerts_on_a_job_running_too_long(tmp_path: Path) -> None:
    orch, _ = build(tmp_path, [make_spec("a")], job_stuck_after=timedelta(seconds=0))
    orch.queue.enqueue(JobKind.DRILL, "global", now=T0)
    orch.claim_ready()
    time.sleep(0.01)
    orch.watchdog()
    assert "job-stuck:drill:global" in orch.alerter.open_keys()
    assert orch.queue.running_ids()  # alert only: a live job is never requeued
    orch.close()


# --- M9: health alerts and metrics -----------------------------------------------------------------


def test_crash_loop_alert_after_three_unclean_stops(tmp_path: Path) -> None:
    clock = ManualClock(T0)
    world = FakeWorld.build(tmp_path, clock, [make_spec("a")])
    settings = make_settings(tmp_path, scrub_slices=1)
    for _ in range(3):  # start, then get killed (no clean-stop mark): 0, 1, 2 unclean stops seen
        orch = Orchestrator(settings, world.wiring(), clock=clock, check_local=False)
        orch.start()
        assert "crash-loop" not in orch.alerter.open_keys()
        assert orch._daemon_lock is not None  # pyright: ignore[reportPrivateUsage]
        os.close(orch._daemon_lock)  # pyright: ignore[reportPrivateUsage] - the kernel drops the flock
        orch._daemon_lock = None  # pyright: ignore[reportPrivateUsage]
        orch.store.close()
        orch.waker.close()
        clock.advance(timedelta(minutes=5))
    orch = Orchestrator(settings, world.wiring(), clock=clock, check_local=False)
    orch.start()  # the third unclean stop within an hour
    assert "crash-loop" in orch.alerter.open_keys()
    assert "crash-loop" in world.sink.alert_keys()
    orch.close()


def test_stuck_release_alert(tmp_path: Path) -> None:
    orch, world = build(tmp_path, [make_spec("a")], stuck_after=timedelta(days=2))
    world.adapters["a"].add_release("2026-10-01", FILES)
    world.adapters["a"].failures["plan"] = [ResourceBusyError("lock")] * 500
    orch.start()
    orch.run_pending()
    assert orch.check_stuck_releases() == []
    orch.clock.advance(timedelta(days=3))  # type: ignore[attr-defined]
    assert orch.check_stuck_releases() == ["stuck:a/2026-10-01"]
    world.adapters["a"].failures["plan"] = []
    orch.clock.advance(timedelta(minutes=11))  # type: ignore[attr-defined]
    orch.run_pending()
    orch.check_stuck_releases()
    assert "stuck:a/2026-10-01" not in orch.alerter.open_keys()
    orch.close()


def test_resumed_releases_counts_unfinished_releases(tmp_path: Path) -> None:
    orch, world = build(tmp_path, [make_spec("a")])
    world.adapters["a"].add_release("2026-10-01", FILES)
    world.adapters["a"].failures["fetch"] = [OSError("reset")]
    orch.start()
    orch.run_pending()
    orch.close()
    again = Orchestrator(
        make_settings(tmp_path, scrub_slices=1), world.wiring(), clock=orch.clock, check_local=False
    )
    again.start()
    assert again.metrics.get("resumed_releases") == 1
    again.watchdog()
    assert (again.metrics.get("rss_bytes") or 0) > 0
    again.close()


# --- M7: retention ------------------------------------------------------------------------------------


def _never_last(source_id: str, generation: Generation) -> bool:
    return False


def _pruner(world: FakeWorld) -> Callable[[str, str], None]:
    def prune(source_id: str, generation_id: str) -> None:
        world.vault.prune(source_id, generation_id)

    return prune


def test_prune_never_moves_a_held_generation(tmp_path: Path) -> None:
    spec = make_spec("src", raw={"retention": {"gfs": {"last": 1, "monthly": 0, "yearly": 0}}})
    orch, world = build(tmp_path, [spec], wiring_extra={"last_copy": _never_last})
    orch.wiring.pruner = _pruner(world)
    adapter = world.adapters["src"]
    world.refinery.doc_counts = {"2026-09-01": 1000, "2026-10-01": 10}
    adapter.add_release("2026-09-01", FILES)
    orch.start()
    orch.run_pending()
    world.vault.add_sealed("src", "2026-11-01")  # a newer sealed generation from `wsp ingest`
    adapter.add_release("2026-10-01", FILES)
    orch.pipeline.register(adapter.releases[-1])
    orch.pipeline.run("src", "2026-10-01")
    assert orch.store.get_release("src", "2026-10-01").hold_reason is not None  # type: ignore[union-attr]
    orch.queue.enqueue(JobKind.PRUNE, "src", now=orch.clock.now(), payload={"source_id": "src"})
    orch.run_pending()
    assert ("src", "2026-10-01") not in world.vault.pruned
    plan = world.ledger.of_kind("retention.plan")[-1].data
    assert "2026-10-01" in plan["keep"]  # type: ignore[operator]
    orch.close()


def test_prune_does_nothing_without_promotion_history(tmp_path: Path) -> None:
    spec = make_spec("src", raw={"retention": {"gfs": {"last": 1, "monthly": 0, "yearly": 0}}})
    orch, world = build(tmp_path, [spec], wiring_extra={"last_copy": _never_last})
    orch.wiring.pruner = _pruner(world)
    for gid in ("2026-08-01", "2026-09-01"):
        world.vault.add_sealed("src", gid)
    world.vault.add_sealed("src", "2026-10-01", current=True)
    orch.queue.enqueue(JobKind.PRUNE, "src", now=T0, payload={"source_id": "src"})
    orch.run_pending()
    assert world.vault.pruned == []  # the state DB was lost: anchors unknown, nothing moved
    record_promotions(orch.store, "src", ["2026-10-01"], T0)
    orch.queue.enqueue(JobKind.PRUNE, "src", now=T0, payload={"source_id": "src"})
    orch.run_pending()
    assert sorted(world.vault.pruned) == [("src", "2026-08-01"), ("src", "2026-09-01")]
    orch.close()


def test_candidates_are_pruned_but_not_discovered(tmp_path: Path) -> None:
    """Candidates are fetched by the collection lanes, not discovered here; their generations still
    need retention (partitioned ones since audit M7, every one since the 2026-10-26 review)."""
    spec = make_spec(
        "cia", status=SourceStatus.CANDIDATE, raw={"access": {"adapter": "cia", "partitioned": True}}
    )
    assert spec.partitioned
    plain_candidate = make_spec("later", status=SourceStatus.CANDIDATE)
    orch, _ = build(tmp_path, [spec, plain_candidate])
    kinds = {(e.kind, e.key) for e in orch.scheduler.entries()}
    assert (JobKind.PRUNE, "cia") in kinds
    assert (JobKind.DISCOVER, "cia") not in kinds
    assert {k for k in kinds if k[1] == "later"} == {(JobKind.PRUNE, "later")}
    orch.close()


# --- M8: version order -----------------------------------------------------------------------------


def test_versions_compare_naturally() -> None:
    assert is_newer("pubmed26-u1000", "pubmed26-u999")
    assert is_newer("2026-10", "2026-9")
    assert newest(["u999", "u1000", "u10"]) == "u1000"
    assert version_key("2026-10-01") < version_key("2026-10-02")
    assert version_key("2026-10-01") < version_key("2026-10-01.ca")


def test_discover_picks_the_numerically_newest_release(tmp_path: Path) -> None:
    orch, world = build(tmp_path, [make_spec("pubmed")])
    for v in ("u998", "u999", "u1000"):
        world.adapters["pubmed"].add_release(v, FILES)
    orch.start()
    orch.run_pending()
    assert world.vault.current("pubmed").generation_id == "u1000"  # type: ignore[union-attr]
    assert {r.version for r in orch.store.releases()} == {"u1000"}
    orch.close()


# --- schema v5 migration ---------------------------------------------------------------------------

_V4_RELEASES = """CREATE TABLE releases (
  source_id TEXT NOT NULL, version TEXT NOT NULL, state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
  generation_id TEXT NOT NULL, ref_json TEXT NOT NULL, plan_json TEXT,
  local_files_json TEXT NOT NULL DEFAULT '[]', fetched_json TEXT NOT NULL DEFAULT '{}',
  doc_count INTEGER, size_bytes INTEGER, hold_reason TEXT, approved INTEGER NOT NULL DEFAULT 0,
  last_error TEXT, next_attempt_at TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY (source_id, version))"""


def test_v4_database_is_migrated_idempotently(tmp_path: Path) -> None:
    path = tmp_path / "v4.sqlite3"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    db.execute("INSERT INTO meta VALUES ('schema_version', '4')")
    db.execute(_V4_RELEASES)
    db.execute(
        "INSERT INTO releases(source_id, version, state, attempts, generation_id, ref_json, created_at,"
        " updated_at) VALUES ('gutenberg', '2026-10-10', 'sealed', 0, '2026-10-10', ?, ?, ?)",
        (json.dumps({"source_id": "gutenberg", "version": "2026-10-10", "files": []}),
         "2026-10-10T00:00:00.000000Z", "2026-10-11T02:31:00.000000Z"),
    )  # fmt: skip
    db.commit()
    db.close()
    for _ in range(2):  # opening twice must not fail or change anything
        store = StateStore(path, check_local=False)
        assert store.get_meta("schema_version") == "5"
        rec = store.get_release("gutenberg", "2026-10-10")
        assert rec is not None
        assert (rec.state, rec.row_version, rec.crash_count, rec.step_token) == (
            ReleaseState.SEALED,
            0,
            0,
            None,
        )
        assert rec.state_since == datetime(2026, 10, 11, 2, 31, tzinfo=UTC)
        store.close()


# --- LOW --------------------------------------------------------------------------------------------


def test_workers_honor_wsp_max_workers() -> None:
    assert capped_workers(8, {"WSP_MAX_WORKERS": "2"}) == 2
    assert capped_workers(1, {"WSP_MAX_WORKERS": "4"}) == 1
    assert capped_workers(4, {"WSP_MAX_WORKERS": "junk"}) == 4
    assert capped_workers(4, {}) == 4
    cfg = {"orchestrator": {"state_db": "/x/s.sqlite3", "workdir": "/x/w", "max_workers": 8}}
    old = os.environ.get("WSP_MAX_WORKERS")
    os.environ["WSP_MAX_WORKERS"] = "3"
    try:
        assert OrchestratorSettings.from_config(cfg).max_workers == 3
    finally:
        if old is None:
            del os.environ["WSP_MAX_WORKERS"]
        else:
            os.environ["WSP_MAX_WORKERS"] = old


def test_a_proposal_is_recorded_before_it_is_applied(tmp_path: Path) -> None:
    def promoter(p: Proposal) -> None:
        raise OSError("overlay not writable")

    gate = GateConfig(  # the gate of test_discovery_bots_produce_deduplicated_proposals: auto-promote
        license_allowlist=frozenset({"CC-BY-SA-4.0"}),
        kiwix_categories=frozenset({"wikipedia"}),
        domain_budget_bytes={"encyclopedia": 100},
    )
    orch, _ = build(tmp_path, [], wiring_extra={"promoter": promoter, "gate": gate})
    candidate = Candidate(
        key="kiwix:wikipedia_de_all_maxi",
        name="Wikipedia DE maxi",
        kind=CandidateKind.KIWIX_TITLE,
        license="CC-BY-SA-4.0",
        domain=("encyclopedia",),
        found_by="kiwix-opds",
        scores=dict.fromkeys(
            ("authority", "density", "uniqueness", "license", "bulk", "health", "longevity", "cost"), 4
        ),
        size_bytes=10,
        category="wikipedia",
        registry_entry={"id": "wikipedia-de"},
    )
    with pytest.raises(OSError, match="overlay"):
        orch.handle_candidates([candidate])
    rows = orch.store.proposals(status=None)
    assert [(r["candidate_key"], r["decision"]) for r in rows] == [
        ("kiwix:wikipedia_de_all_maxi", Decision.AUTO_PROMOTE.value)
    ]
    assert rows[0]["status"] == "open"  # recorded first; the failed apply never marked it applied
    orch.close()
