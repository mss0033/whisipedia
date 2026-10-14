"""Heavy steps in child units (audit 2026-10-11, C2/C3/H7): runner, launchers, result mapping.

No test starts a real systemd unit: transient-unit behaviour is exercised through ``FakeLauncher``
and by checking the ``systemd-run`` command line; the plain-subprocess fallback runs for real.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

from wsp_orchestrator.errors import (
    FailureKind,
    ResourceBusyError,
    ResourceExhaustedError,
    StepCancelledError,
    StorageUnavailableError,
    classify,
)
from wsp_orchestrator.isolation import (
    ChildExit,
    ChildStepError,
    IsolatedRunner,
    SubprocessLauncher,
    SystemdRunLauncher,
    UnitLimits,
    default_launcher,
    parse_systemd_run,
    unit_name,
)
from wsp_orchestrator.testing import FakeLauncher


def runner(tmp_path: Path, launcher: object, **kw: object) -> IsolatedRunner:
    return IsolatedRunner(tmp_path / "work", launcher=launcher, cwd=tmp_path, **kw)  # type: ignore[arg-type]


def test_value_comes_back_from_the_child(tmp_path: Path) -> None:
    fake = FakeLauncher()
    r = runner(tmp_path, fake)
    assert r.call("refine", "wsp_orchestrator.testing:child_echo", {"value": {"n": 3}}, key="s/1") == {"n": 3}
    [(unit, argv, env)] = fake.started
    assert unit == unit_name("refine", "s/1")
    assert argv[:3] == [sys.executable, "-m", "wsp_orchestrator.child"]
    assert env["PYTHONUNBUFFERED"] == "1"
    assert fake.request_modes == [0o600], "the request (the whole config) is owner-only"
    assert not (tmp_path / "work" / "_steps" / unit).exists(), "the request/result box is removed"


@pytest.mark.parametrize(
    ("error", "raised"),
    [
        ("busy", ResourceBusyError),  # a held lock in the child: deferred, no attempt
        ("memory", ResourceExhaustedError),  # MemoryError: a crash, not a content failure
        ("bad record", ChildStepError),  # content: counts an attempt
    ],
)
def test_child_errors_keep_their_kind(tmp_path: Path, error: str, raised: type[Exception]) -> None:
    r = runner(tmp_path, FakeLauncher())
    with pytest.raises(raised):
        r.call("refine", "wsp_orchestrator.testing:child_raise", {"error": error}, key="s/1")


@pytest.mark.parametrize(
    ("exit_", "raised", "kind"),
    [
        (ChildExit("oom-kill", None, "KILL", "6.0G"), ResourceExhaustedError, FailureKind.RESOURCE),
        (ChildExit("signal", None, "KILL"), ResourceExhaustedError, FailureKind.RESOURCE),
        (ChildExit("core-dump", None, "SEGV"), ResourceExhaustedError, FailureKind.RESOURCE),
        (
            ChildExit("start-failed", 1, None, None, "Unit whisipedia-step-x.service already exists."),
            ResourceBusyError,
            FailureKind.BUSY,
        ),
        (
            ChildExit("start-failed", 1, None, None, "Unknown assignment: Bogus=1"),
            ResourceExhaustedError,
            FailureKind.RESOURCE,
        ),
    ],
)
def test_unit_results_map_to_failure_kinds(
    tmp_path: Path, exit_: ChildExit, raised: type[Exception], kind: FailureKind
) -> None:
    r = runner(tmp_path, FakeLauncher(exits=[exit_]))
    with pytest.raises(raised) as info:
        r.call("refine", "wsp_orchestrator.testing:child_echo", {"value": 1}, key="s/1")
    assert classify(info.value) is kind
    if exit_.result == "oom-kill":
        assert "OOM-killed" in str(info.value)
        assert "MemoryMax=4G" in str(info.value)


def test_shutdown_stops_a_running_child_without_waiting_for_it(tmp_path: Path) -> None:
    fake = FakeLauncher(hang=True)
    stop = threading.Event()
    r = runner(tmp_path, fake, should_stop=stop.is_set, poll_seconds=0.02, cancel_wait_seconds=0.5)
    errors: list[BaseException] = []

    def call() -> None:
        try:
            r.call("index", "wsp_orchestrator.testing:child_echo", {}, key="s/1")
        except BaseException as exc:
            errors.append(exc)

    t = threading.Thread(target=call)
    t.start()
    assert fake.running.wait(5)
    t0 = time.monotonic()
    stop.set()
    t.join(5)
    assert not t.is_alive()
    assert time.monotonic() - t0 < 2
    assert isinstance(errors[0], StepCancelledError)
    assert fake.children[0].stopped.is_set()  # type: ignore[union-attr]


def test_a_step_over_its_time_budget_is_stopped(tmp_path: Path) -> None:
    fake = FakeLauncher(hang=True)
    r = runner(tmp_path, fake, poll_seconds=0.02, cancel_wait_seconds=0.2)
    with pytest.raises(ResourceExhaustedError, match="timeout"):
        r.call("refine", "wsp_orchestrator.testing:child_echo", {}, key="s/1", timeout=0.1)
    assert fake.children[0].stopped.is_set()  # type: ignore[union-attr]


def test_nothing_starts_once_shutdown_began(tmp_path: Path) -> None:
    fake = FakeLauncher()
    r = runner(tmp_path, fake, should_stop=lambda: True)
    with pytest.raises(StepCancelledError):
        r.call("refine", "wsp_orchestrator.testing:child_echo", {}, key="s/1")
    assert fake.started == []


def test_plain_subprocess_fallback_runs_the_child_for_real(tmp_path: Path) -> None:
    r = runner(tmp_path, SubprocessLauncher(), poll_seconds=0.05)
    assert r.call("profile", "wsp_orchestrator.testing:child_echo", {"value": [1, 2]}, key="s/1") == [1, 2]
    with pytest.raises(ResourceBusyError):
        r.call("profile", "wsp_orchestrator.testing:child_raise", {"error": "busy"}, key="s/1")


def test_plain_subprocess_killed_by_a_signal_is_a_crash(tmp_path: Path) -> None:
    r = IsolatedRunner(
        tmp_path / "work", launcher=SubprocessLauncher(), cwd=tmp_path, python=str(tmp_path / "py")
    )
    script = tmp_path / "py"
    script.write_text("#!/bin/sh\nkill -KILL $$\n")
    script.chmod(0o755)
    with pytest.raises(ResourceExhaustedError, match="died without a result"):
        r.call("refine", "wsp_orchestrator.testing:child_echo", {}, key="s/1")


def test_systemd_run_command_caps_memory_in_the_background_slice(tmp_path: Path) -> None:
    launcher = SystemdRunLauncher(systemd_run="/usr/bin/systemd-run", systemctl="/usr/bin/systemctl")
    cmd = launcher.command(
        "whisipedia-step-refine-x",
        ["/venv/bin/python", "-m", "wsp_orchestrator.child", "m:f", "a", "b"],
        env={"WSP_MAX_WORKERS": "4"},
        cwd=tmp_path,
        limits=UnitLimits(),
    )
    assert cmd[:5] == ["/usr/bin/systemd-run", "--user", "--wait", "--collect", "--service-type=exec"]
    assert "--slice=whisipedia-background.slice" in cmd
    props = {cmd[i + 1] for i, a in enumerate(cmd) if a == "-p"}
    assert {
        "MemoryHigh=3G",
        "MemoryMax=4G",
        "MemorySwapMax=512M",
        "KillMode=mixed",
        "TimeoutStopSec=60",
    } <= props
    assert "IOSchedulingClass=idle" not in props  # priority inversion while holding a lock
    envs = {cmd[i + 1] for i, a in enumerate(cmd) if a == "-E"}
    assert envs == {"WSP_MAX_WORKERS=4", "MALLOC_ARENA_MAX=2"}  # arenas capped by default
    assert cmd[cmd.index("--") + 1 :] == ["/venv/bin/python", "-m", "wsp_orchestrator.child", "m:f", "a", "b"]


def test_systemd_run_summary_is_parsed() -> None:
    oom = parse_systemd_run(
        "Running as unit: whisipedia-step-refine-x.service; invocation ID: 1\n"
        "Finished with result: oom-kill\n"
        "Main processes terminated with: code=killed/status=KILL\n"
        "Service runtime: 1h 2min\n"
        "Memory peak: 6.0G\n",
        137,
    )
    assert (oom.result, oom.status, oom.memory_peak) == ("oom-kill", "KILL", "6.0G")
    ok = parse_systemd_run(
        "Finished with result: exit-code\nMain processes terminated with: code=exited/status=1\n", 1
    )
    assert (ok.result, ok.exit_code) == ("exit-code", 1)
    failed = parse_systemd_run("Failed to start transient service unit: Unit x.service already exists.\n", 1)
    assert failed.result == "start-failed"
    assert "already exists" in failed.detail


def test_only_a_systemd_service_starts_transient_units(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(SystemdRunLauncher, "available", staticmethod(lambda: True))
    assert isinstance(default_launcher(environ={}), SubprocessLauncher)  # a shell or a test run
    assert isinstance(default_launcher(environ={"INVOCATION_ID": "x"}), SystemdRunLauncher)
    assert isinstance(
        default_launcher(environ={"INVOCATION_ID": "x", "WSP_HEAVY_LAUNCHER": "subprocess"}),
        SubprocessLauncher,
    )
    monkeypatch.setattr(SystemdRunLauncher, "available", staticmethod(lambda: False))
    assert isinstance(default_launcher(environ={"INVOCATION_ID": "x"}), SubprocessLauncher)


def test_unit_names_are_stable_and_valid() -> None:
    name = unit_name("refine", "wikipedia-top10/2026-10-01")
    assert name == unit_name("refine", "wikipedia-top10/2026-10-01")
    assert name != unit_name("index", "wikipedia-top10/2026-10-01")
    assert all(c.isalnum() or c in "-_" for c in name)


def test_classify_taxonomy() -> None:
    import errno
    import sqlite3

    assert classify(ValueError("Failed to acquire Lockfile: LockBusy")) is FailureKind.BUSY
    assert classify(sqlite3.OperationalError("database is locked")) is FailureKind.BUSY
    assert classify(OSError(errno.EHOSTDOWN, "Host is down")) is FailureKind.UNAVAILABLE
    assert classify(OSError(errno.ENOSPC, "No space")) is FailureKind.RESOURCE
    assert classify(StorageUnavailableError("nas")) is FailureKind.UNAVAILABLE
    assert classify(OSError("reset")) is FailureKind.CONTENT
    assert classify(OSError("reset"), storage_available=lambda: False) is FailureKind.UNAVAILABLE
    assert classify(RuntimeError("bad")) is FailureKind.CONTENT
    assert FailureKind.CONTENT.counts_attempt
    assert not FailureKind.BUSY.counts_attempt
