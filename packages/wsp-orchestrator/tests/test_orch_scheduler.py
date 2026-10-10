from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from wsp_core.model import SourceSpec, SourceStatus
from wsp_orchestrator.clock import ManualClock
from wsp_orchestrator.durations import CalendarCadence, IntervalCadence, IsoDuration
from wsp_orchestrator.jobs import JobKind, JobStatus
from wsp_orchestrator.orchestrator import Orchestrator
from wsp_orchestrator.testing import FakeWorld, make_settings, make_spec

T0 = datetime(2026, 10, 15, 6, tzinfo=UTC)
FILES = {"a.zim": b"a" * 1000}


def build(
    tmp_path: Path, clock: ManualClock, sources: list[SourceSpec], world: FakeWorld | None = None, **kw: Any
) -> tuple[Orchestrator, FakeWorld]:
    world = world or FakeWorld.build(tmp_path, clock, sources)
    orch = Orchestrator(make_settings(tmp_path, **kw), world.wiring(), clock=clock, check_local=False)
    return orch, world


def due_kinds(orch: Orchestrator) -> set[tuple[str, str]]:
    return {(d.entry.kind.value, d.entry.key) for d in orch.scheduler.due()}


def test_first_start_runs_everything_and_promotes(tmp_path: Path) -> None:
    clock = ManualClock(T0)
    orch, world = build(tmp_path, clock, [make_spec("src")])
    world.adapters["src"].add_release("2026-10-01", FILES)
    started = {(j.kind.value, j.key) for j in orch.start()}
    assert started == {
        ("discover", "src"),
        ("health_probe", "src"),
        ("prune", "src"),
        ("discovery_bots", "global"),
        ("drill", "global"),
        ("scrub_slice", "rolling"),
    }
    orch.run_pending()
    current = world.vault.current("src")
    assert current is not None
    assert current.generation_id == "2026-10-01"
    assert due_kinds(orch) == set()


def test_monthly_cycle(tmp_path: Path) -> None:
    clock = ManualClock(T0)
    orch, world = build(tmp_path, clock, [make_spec("src")], scrub_slices=1)
    world.adapters["src"].add_release("2026-10-01", FILES)
    orch.start()
    orch.run_pending()
    clock.set(datetime(2026, 11, 14, 23, tzinfo=UTC))
    assert ("discover", "src") not in due_kinds(orch)
    clock.set(datetime(2026, 11, 15, tzinfo=UTC))
    assert ("discover", "src") in due_kinds(orch)
    assert ("discovery_bots", "global") in due_kinds(orch)


def test_quarterly_cycle_and_overrides(tmp_path: Path) -> None:
    clock = ManualClock(T0)
    sources = [
        make_spec("follows", cadence="P1M"),  # P1M = "follow acquirer.cycle"
        make_spec("weekly", cadence="P1W"),
        make_spec("absent"),
        make_spec("candidate", status=SourceStatus.CANDIDATE),
    ]
    orch, world = build(tmp_path, clock, sources, cycle="quarterly", scrub_slices=1)
    for s in ("follows", "weekly", "absent"):
        world.adapters[s].add_release("2026-10-01", FILES)
    sched = orch.scheduler
    assert sched.source_cadence(sources[0]) == CalendarCadence(3, 15)
    assert sched.source_cadence(sources[1]) == IntervalCadence(IsoDuration.parse("P1W"))
    assert sched.source_cadence(sources[2]) == CalendarCadence(3, 15)
    assert ("discover", "candidate") not in {(e.kind.value, e.key) for e in sched.entries()}

    orch.start()
    orch.run_pending()
    for month in (11, 12):
        clock.set(datetime(2026, month, 16, tzinfo=UTC))
        # keep the weekly source fresh, otherwise it is (correctly) auto-paused as stale
        world.adapters["weekly"].add_release(f"2026-{month}-14", FILES)
        due = due_kinds(orch)
        assert ("discover", "follows") not in due
        assert ("discover", "absent") not in due
        assert ("discover", "weekly") in due
        orch.run_pending()
    clock.set(datetime(2027, 1, 15, tzinfo=UTC))
    assert {("discover", "follows"), ("discover", "absent")} <= due_kinds(orch)


def test_catch_up_after_downtime_runs_each_overdue_job_once(tmp_path: Path) -> None:
    clock = ManualClock(T0)
    orch, world = build(tmp_path, clock, [make_spec("src")], scrub_slices=30)
    adapter = world.adapters["src"]
    adapter.add_release("2026-10-01", FILES)
    orch.start()
    orch.run_pending()
    orch.close()

    # Three months of downtime; upstream published three new releases meanwhile.
    for v in ("2026-11-01", "2026-12-01", "2027-01-01"):
        adapter.add_release(v, FILES)
    clock.set(datetime(2027, 1, 20, tzinfo=UTC))
    orch, _ = build(tmp_path, clock, [make_spec("src")], world=world, scrub_slices=30)
    started = [(j.kind.value, j.key) for j in orch.start()]
    assert started.count(("discover", "src")) == 1
    discovers_before = adapter.calls["discover"]
    orch.run_pending()

    current = world.vault.current("src")
    assert current is not None
    assert current.generation_id == "2027-01-01", "jumps straight to the newest release"
    assert {r.version for r in orch.store.releases()} == {"2026-10-01", "2027-01-01"}
    assert adapter.calls["discover"] - discovers_before == 2  # one discover + one health probe
    # The overdue scrub slices ran back-to-back, completing the interrupted 30-day pass.
    assert [i for i, _ in world.scrubbed].count(29) == 1
    assert sorted({i for i, _ in world.scrubbed}) == list(range(30))
    start, cursor = orch.scheduler.scrub_position()
    assert start == clock.now()
    assert cursor == 1


def test_scrub_slices_spread_over_cycle(tmp_path: Path) -> None:
    clock = ManualClock(T0)
    orch, world = build(tmp_path, clock, [], scrub_slices=10, scrub_cycle_days=30)
    orch.start()
    orch.run_pending()
    assert world.scrubbed == [(0, 10)]
    clock.advance(timedelta(days=2, hours=23))
    orch.run_pending()
    assert world.scrubbed == [(0, 10)]
    clock.advance(timedelta(hours=1))
    orch.run_pending()
    assert world.scrubbed == [(0, 10), (1, 10)]
    orch.scheduler.scrub_slice_done(0)  # stale completion is ignored
    assert orch.scheduler.scrub_position()[1] == 2


def test_retention_window_alert_at_half_window(tmp_path: Path) -> None:
    clock = ManualClock(T0)
    spec = make_spec("wiki", adapter="wikimedia-dumps")
    orch, world = build(tmp_path, clock, [spec], scrub_slices=1, job_retry_gap=timedelta(hours=1))
    adapter = world.adapters["wikimedia-dumps"]
    adapter.failures["discover"] = [OSError("dumps.example unreachable")] * 1000
    orch.start()
    orch.run_pending()
    clock.advance(timedelta(days=29))
    orch.run_pending()
    assert "retention-window:wiki" not in world.sink.alert_keys()
    clock.advance(timedelta(days=2))
    orch.run_pending()
    assert world.sink.alert_keys().count("retention-window:wiki") == 1
    orch.run_pending()
    assert world.sink.alert_keys().count("retention-window:wiki") == 1, "raised once"

    adapter.failures["discover"].clear()
    adapter.add_release("2026-10-01", FILES)
    clock.advance(timedelta(hours=2))
    orch.run_pending()
    assert world.vault.current("wiki") is not None
    assert "retention-window:wiki" not in orch.alerter.open_keys()


def test_retention_window_uses_last_cycle(tmp_path: Path) -> None:
    clock = ManualClock(T0)
    spec = make_spec("wiki", adapter="wikimedia-dumps")
    orch, world = build(tmp_path, clock, [spec], scrub_slices=1)
    world.adapters["wikimedia-dumps"].add_release("2026-10-01", FILES)
    orch.start()
    orch.run_pending()
    # Next cycle due 2026-11-15; half of the 60-day window is 30 days later.
    assert orch.scheduler.check_retention_windows(datetime(2026, 12, 14, tzinfo=UTC)) == []
    late = datetime(2026, 12, 16, tzinfo=UTC)
    assert orch.scheduler.check_retention_windows(late) == ["retention-window:wiki"]


def test_failing_job_retries_then_dies_with_alert(tmp_path: Path) -> None:
    clock = ManualClock(T0)
    orch, world = build(tmp_path, clock, [make_spec("src")], scrub_slices=1)
    world.adapters["src"].failures["discover"] = [OSError("down")] * 100
    orch.start()
    for _ in range(6):
        orch.run_pending()
        clock.advance(timedelta(hours=3))
    dead = orch.queue.jobs(status=JobStatus.DEAD, kind=JobKind.DISCOVER)
    assert dead, "gave up after max attempts"
    assert all(j.attempts == 3 for j in dead)
    # once dead, the scheduler tries again after job_retry_gap rather than hot-looping
    assert len(orch.queue.jobs(kind=JobKind.DISCOVER)) <= 3
    assert world.sink.alert_keys().count("job-dead:discover:src") == 1
