"""wsp quality run: a drop is measured against the best recent run, not only the previous one; a slower
search against the best quiet run, never one beside the index build."""

from __future__ import annotations

import argparse
import io
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from wsp_cli.quality import REGRESSION, baseline, regression


def test_a_drop_stays_a_drop_until_quality_recovers(tmp_path: Path) -> None:
    """2026-10-26 review #46: week 3 at the same low score resolved week 2's alert."""
    for name, score in (("2026-10-01.json", 0.62), ("2026-10-08.json", 0.55), ("2026-10-15.json", 0.55)):
        (tmp_path / name).write_text(json.dumps({"ndcg10": score}))
    best = baseline(tmp_path, "2026-10-22.json")
    assert best is not None
    assert best["ndcg10"] == 0.62
    fallen = regression(0.55, best)
    assert fallen is not None
    assert fallen > REGRESSION  # still a regression in week 4
    back = regression(0.61, best)
    assert back is not None
    assert back <= REGRESSION  # recovered


def test_latency_is_measured_against_the_best_quiet_run_only(tmp_path: Path) -> None:
    """2026-11-05 deep dive: the 11-01 run sat inside a catch-up index build; reports from before the
    contention was recorded are not a baseline."""
    from wsp_cli.quality import latency_baseline, latency_regressed

    rows: tuple[tuple[str, dict[str, Any]], ...] = (
        ("2026-10-14.json", {"p50_ms": 3336.0}),  # contention not recorded
        ("2026-11-08.json", {"p50_ms": 4000.0, "contended": []}),
        ("2026-11-15.json", {"p50_ms": 900.0, "contended": ["index"]}),  # beside the build
        ("2026-11-22.json", {"p50_ms": 3500.0, "contended": []}),
    )
    for name, row in rows:
        (tmp_path / name).write_text(json.dumps(row))
    assert latency_baseline(tmp_path, "2026-12-01.json") == 3500.0
    assert latency_baseline(tmp_path, "2026-11-10.json") == 4000.0
    assert latency_baseline(tmp_path, "2026-11-01.json") is None
    assert latency_baseline(tmp_path, "2026-12-01.json", runs=1) == 3500.0
    assert latency_regressed(7001.0, 3500.0)
    assert not latency_regressed(7000.0, 3500.0)
    assert not latency_regressed(1900.0, 100.0)  # 19 times slower, but under 2 s: no news


def test_the_run_waits_for_the_index_build_within_its_cap(tmp_path: Path) -> None:
    from wsp_cli.locks import exclusive
    from wsp_cli.quality import wait_for_build

    clock = [0.0]

    def sleep(seconds: float) -> None:
        clock[0] += seconds

    assert wait_for_build(tmp_path, sleep=sleep, monotonic=lambda: clock[0]) == 0.0  # nothing held
    with exclusive(tmp_path, "index"):
        waited = wait_for_build(tmp_path, wait=3600, poll=300, sleep=sleep, monotonic=lambda: clock[0])
    assert waited == 3600.0  # capped: the run goes on, recorded as contended


def test_a_slower_quiet_run_opens_the_latency_warning_and_a_busy_one_says_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import wsp_oracle.evaluation as evaluation
    from wsp_cli import bootstrap, quality
    from wsp_cli.locks import exclusive, lock_dir_for
    from wsp_oracle.evaluation import EvalReport
    from wsp_orchestrator.store import StateStore

    state_db = tmp_path / "state" / "orchestrator.sqlite3"
    state_db.parent.mkdir()
    StateStore(state_db, check_local=False).close()
    cfg = {"orchestrator": {"state_db": str(state_db)}, "index": {"root": str(tmp_path / "index")}}
    p50 = [0.0]

    def load(_lay: object) -> dict[str, Any]:
        return cfg

    def evaluate(*_a: object, **_k: object) -> EvalReport:
        return EvalReport("api", 1, 0.6, 0.7, 0.5, p50[0], p50[0], p50[0])

    def nothing(_arg: object) -> list[object]:
        return []

    monkeypatch.setattr(bootstrap, "load", load)
    monkeypatch.setattr(evaluation, "evaluate", evaluate)
    monkeypatch.setattr(evaluation, "api_searcher", nothing)
    monkeypatch.setattr(evaluation, "load_gold", nothing)
    week = iter(range(10))

    class Clock(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> Clock:  # one run a week
            return cls(2026, 11, 8, 8, 17, tzinfo=UTC) + timedelta(weeks=next(week))

    monkeypatch.setattr(quality, "datetime", Clock)
    args = argparse.Namespace(config_dir=tmp_path, api="http://x", gold=None, alerts=True)

    def run(ms: float) -> str:
        p50[0] = ms
        out = io.StringIO()
        assert quality.cmd_quality_run(args, out) == 0
        return out.getvalue()

    def latency_alert() -> list[dict[str, Any]]:
        store = StateStore(state_db, check_local=False)
        try:
            return [dict(r) for r in store.query("SELECT * FROM alerts WHERE key = 'quality:latency'")]
        finally:
            store.close()

    run(3000.0)  # the first quiet run: the baseline
    assert "median latency 9000 ms; the best of the quiet runs 3000 ms" in run(9000.0)
    (opened,) = latency_alert()
    assert opened["resolved_at"] is None
    assert "median 9.0 s against 3.0 s" in opened["message"]

    def no_wait(_lock_dir: Path) -> float:  # the build outlived the run's wait
        return 0.0

    monkeypatch.setattr(quality, "wait_for_build", no_wait)
    with exclusive(lock_dir_for(tmp_path / "index"), "index"):
        assert "latency not judged: index ran beside it" in run(2500.0)
    assert latency_alert()[0]["resolved_at"] is None  # a run beside the index build resolves nothing
    saved = sorted((tmp_path / "state" / "quality").glob("*.json"))
    assert json.loads(saved[-1].read_text())["contended"] == ["index"]
    run(4000.0)  # quiet, within twice the baseline
    assert latency_alert()[0]["resolved_at"] is not None


def test_ingest_beside_a_run_is_recorded_but_latency_is_still_judged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """2026-11-05 review: the lanes hold the ingest lock nearly all day; a run gated on it was never
    judged and never made a baseline, so the slowdown the latency warning is for stayed silent."""
    import wsp_oracle.evaluation as evaluation
    from wsp_cli import bootstrap, quality
    from wsp_cli.locks import exclusive, lock_dir_for
    from wsp_oracle.evaluation import EvalReport
    from wsp_orchestrator.store import StateStore

    state_db = tmp_path / "state" / "orchestrator.sqlite3"
    state_db.parent.mkdir()
    StateStore(state_db, check_local=False).close()
    cfg = {"orchestrator": {"state_db": str(state_db)}, "index": {"root": str(tmp_path / "index")}}
    p50 = [0.0]

    def load(_lay: object) -> dict[str, Any]:
        return cfg

    def evaluate(*_a: object, **_k: object) -> EvalReport:
        return EvalReport("api", 1, 0.6, 0.7, 0.5, p50[0], p50[0], p50[0])

    def nothing(_arg: object) -> list[object]:
        return []

    monkeypatch.setattr(bootstrap, "load", load)
    monkeypatch.setattr(evaluation, "evaluate", evaluate)
    monkeypatch.setattr(evaluation, "api_searcher", nothing)
    monkeypatch.setattr(evaluation, "load_gold", nothing)
    week = iter(range(10))

    class Clock(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> Clock:
            return cls(2026, 11, 8, 8, 17, tzinfo=UTC) + timedelta(weeks=next(week))

    monkeypatch.setattr(quality, "datetime", Clock)
    args = argparse.Namespace(config_dir=tmp_path, api="http://x", gold=None, alerts=True)

    def run(ms: float) -> str:
        p50[0] = ms
        out = io.StringIO()
        assert quality.cmd_quality_run(args, out) == 0
        return out.getvalue()

    def latency_alert() -> list[dict[str, Any]]:
        store = StateStore(state_db, check_local=False)
        try:
            return [dict(r) for r in store.query("SELECT * FROM alerts WHERE key = 'quality:latency'")]
        finally:
            store.close()

    lock_dir = lock_dir_for(tmp_path / "index")
    with exclusive(lock_dir, "ingest"):  # held at the run's start and its end, as the lanes hold it
        run(4000.0)  # the baseline, though ingest ran beside it
        out = run(16000.0)
    assert "latency not judged" not in out
    assert "median latency 16000 ms (ingest ran beside it); the best of the quiet runs 4000 ms" in out
    (opened,) = latency_alert()
    assert opened["resolved_at"] is None
    saved = [json.loads(p.read_text()) for p in sorted((tmp_path / "state" / "quality").glob("*.json"))]
    assert [(r["contended"], r["ingest_busy"]) for r in saved] == [([], True), ([], True)]
    assert run(5000.0).count("ingest ran beside it") == 0  # ingest done: judged, recovered
    assert latency_alert()[0]["resolved_at"] is not None
