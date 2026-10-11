"""The daemon stops promptly on SIGTERM (systemd TimeoutStopSec=120; target < 15 s)."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from wsp_orchestrator.clock import ManualClock
from wsp_orchestrator.jobs import JobKind
from wsp_orchestrator.orchestrator import Orchestrator
from wsp_orchestrator.settings import OrchestratorSettings
from wsp_orchestrator.testing import FakeFetcher, FakeWorld, make_settings, make_spec


def test_default_grace_is_well_under_systemd_timeout() -> None:
    cfg = {"orchestrator": {"state_db": "/nonexistent/x.sqlite3", "workdir": "/nonexistent/w"}}
    assert OrchestratorSettings.from_config(cfg).shutdown_grace_seconds <= 10


def _orchestrator(
    tmp_path: Path, scrub: Callable[[int, int], None], **extra: Any
) -> tuple[Orchestrator, FakeWorld]:
    clock = ManualClock(datetime(2026, 10, 15, tzinfo=UTC))
    world = FakeWorld.build(tmp_path, clock, [make_spec("src")])
    wiring = world.wiring(**extra)
    wiring.scrub = scrub
    orch = Orchestrator(make_settings(tmp_path, scrub_slices=1), wiring, clock=clock, check_local=False)
    return orch, world


def _start_scrub(orch: Orchestrator) -> None:
    orch.queue.enqueue(JobKind.SCRUB_SLICE, "0", now=orch.clock.now(), payload={"slice": 0, "slices": 1})
    assert orch.dispatch() >= 1


def test_cooperative_job_is_cancelled_and_finishes(tmp_path: Path) -> None:
    stop = threading.Event()
    started = threading.Event()

    def scrub(index: int, count: int) -> None:
        started.set()
        stop.wait(3600)  # a scrub that honours its should_stop

    orch, _ = _orchestrator(tmp_path, scrub, on_shutdown=[stop.set])
    _start_scrub(orch)
    assert started.wait(5)
    t0 = time.monotonic()
    assert orch.shutdown(grace_seconds=5) is True
    assert time.monotonic() - t0 < 2
    orch.close()


def test_stubborn_job_is_abandoned_after_the_grace_period(tmp_path: Path) -> None:
    started = threading.Event()
    release = threading.Event()

    def scrub(index: int, count: int) -> None:
        started.set()
        release.wait(3600)  # ignores cancellation

    orch, _ = _orchestrator(tmp_path, scrub)
    _start_scrub(orch)
    assert started.wait(5)
    t0 = time.monotonic()
    assert orch.shutdown(grace_seconds=0.5) is False
    assert time.monotonic() - t0 < 2
    assert all(t.daemon for t in threading.enumerate() if t.name.startswith("wsp-job"))
    fetcher = orch.wiring.fetcher
    assert isinstance(fetcher, FakeFetcher)
    assert fetcher.cancelled  # the fetcher's cancel() was called too
    release.set()
    orch.close()


@pytest.mark.skipif(sys.platform != "linux", reason="POSIX signals")
def test_daemon_exits_within_15s_of_sigterm_even_with_a_stubborn_job(tmp_path: Path) -> None:
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    orch = {
        "state_db": str(tmp_path / "state" / "o.sqlite3"),
        "workdir": str(tmp_path / "work"),
        "metrics_path": str(tmp_path / "metrics" / "o.prom"),
        "poll_interval_seconds": 0.05,
        "scrub_slices": 1,
        "shutdown_grace_seconds": 3,
        "wiring": "wsp_orchestrator.testing:stubborn_wiring",
    }
    text = '[acquirer]\ncycle = "monthly"\n[orchestrator]\n' + "".join(
        f"{k} = {json.dumps(v)}\n" for k, v in orch.items()
    )
    (cfg_dir / "default.toml").write_text(text)
    env = {**os.environ, "WSP_NO_LOCAL_CONFIG": "1"}
    proc = subprocess.Popen(
        [sys.executable, "-m", "wsp_orchestrator.daemon", "--config-dir", str(cfg_dir)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        marker = tmp_path / "work" / "stubborn-started"
        deadline = time.monotonic() + 30
        while not marker.exists() and time.monotonic() < deadline and proc.poll() is None:
            time.sleep(0.05)
        assert marker.exists(), "the stubborn job never started"
        t0 = time.monotonic()
        proc.send_signal(signal.SIGTERM)
        code = proc.wait(timeout=15)
        elapsed = time.monotonic() - t0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    assert elapsed < 15
    assert code == 0
    assert proc.stderr is not None
    log = proc.stderr.read().decode()
    assert "shutdown grace expired" in log
    assert "non-daemon threads still running" in log
