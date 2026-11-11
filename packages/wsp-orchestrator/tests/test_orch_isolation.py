"""Heavy steps in child units (audit 2026-10-11, C2/C3/H7): runner, launchers, result mapping.

No test starts a real systemd unit: transient-unit behaviour is exercised through ``FakeLauncher``
and by checking the ``systemd-run`` command line; the plain-subprocess fallback runs for real.
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from wsp_orchestrator import isolation
from wsp_orchestrator.child import run as run_child
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
    NoResultError,
    SubprocessLauncher,
    SystemdRunLauncher,
    UnitLimits,
    default_launcher,
    parse_systemd_run,
    unit_name,
)
from wsp_orchestrator.testing import FakeLauncher

# bound here, at import: the repository conftest replaces the module's name in every test
real_system_stopping = isolation.system_stopping


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
        assert "MemoryMax=6G" in str(info.value)


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
    with pytest.raises(ResourceExhaustedError, match="killed without a result"):
        r.call("refine", "wsp_orchestrator.testing:child_echo", {}, key="s/1")


# --- a shutdown is not a crash (2026-11-10) ----------------------------------------------------------
# systemd stopped a profile's child 35 ms before the daemon handled its own SIGTERM: the child had
# ended "success" with no result, and the step was counted as a crash, "probably killed for memory".

NO_RESULT = ChildExit("success", None, None)


class _Ended:
    def __init__(self, outcome: ChildExit) -> None:
        self.outcome = outcome

    def wait(self, timeout: float) -> ChildExit | None:
        return self.outcome

    def stop(self) -> None:
        pass


class _ShutdownFollows:
    """A launcher whose child has ended as ``outcome`` by the time it is looked at; the daemon's stop
    flag follows ``after`` seconds later (None: never). ``run=True`` lets the child write its result
    first."""

    def __init__(self, outcome: ChildExit, *, after: float | None = 0.05, run: bool = False) -> None:
        self.outcome = outcome
        self.after = after
        self.run = run
        self.stop = threading.Event()

    def start(
        self, unit: str, argv: Sequence[str], *, env: Mapping[str, str], cwd: Path, limits: UnitLimits
    ) -> _Ended:
        if self.run:
            target, request, result = argv[-3:]
            run_child(target, Path(request), Path(result))
        if self.after is not None:
            timer = threading.Timer(self.after, self.stop.set)
            timer.daemon = True
            timer.start()
        return _Ended(self.outcome)


def _call(r: IsolatedRunner, target: str = "child_echo", **payload: object) -> object:
    return r.call("profile", f"wsp_orchestrator.testing:{target}", payload or {"value": 1}, key="s/1")


def test_a_child_ended_just_before_the_shutdown_signal_is_cancelled_not_crashed(tmp_path: Path) -> None:
    launcher = _ShutdownFollows(NO_RESULT, after=0.02)
    asked: list[int] = []
    r = runner(
        tmp_path,
        launcher,
        should_stop=launcher.stop.is_set,
        shutdown_grace_seconds=5.0,
        system_stopping=lambda: bool(asked.append(1)),
    )
    t0 = time.monotonic()
    with pytest.raises(StepCancelledError, match="stopped by a shutdown") as info:
        _call(r)
    assert classify(info.value) is FailureKind.BUSY  # deferred: no crash and no attempt
    assert time.monotonic() - t0 < 2, "the grace ends as soon as the stop flag is seen"
    assert asked == [], "our own stop flag is watched first; systemd is asked only after 0.3 s"


def test_a_child_ended_while_systemd_is_stopping_is_cancelled(tmp_path: Path) -> None:
    r = runner(tmp_path, FakeLauncher(exits=[NO_RESULT]), system_stopping=lambda: True)
    with pytest.raises(StepCancelledError, match="stopped by a shutdown"):
        _call(r)


def test_a_shutdown_that_shows_only_after_the_grace_still_cancels(tmp_path: Path) -> None:
    # the background slice can be frozen for the whole grace: systemd is asked once more at its end
    answers = iter([False, True])
    r = runner(tmp_path, FakeLauncher(exits=[NO_RESULT]), system_stopping=lambda: next(answers))
    with pytest.raises(StepCancelledError):
        _call(r)
    assert list(answers) == []


def test_a_unit_that_could_not_start_during_a_shutdown_is_cancelled(tmp_path: Path) -> None:
    # systemd-run that lost its manager prints no result line: "start-failed"
    lost = ChildExit("start-failed", 1, None, None, "Failed to connect to bus: Connection refused")
    r = runner(tmp_path, FakeLauncher(exits=[lost]), system_stopping=lambda: True)
    with pytest.raises(StepCancelledError):
        _call(r)
    r = runner(tmp_path, FakeLauncher(exits=[lost]))  # no shutdown: counted, as before
    with pytest.raises(NoResultError, match="could not start") as info:
        _call(r)
    assert classify(info.value) is FailureKind.RESOURCE


def test_a_child_signalled_after_it_wrote_its_result_at_a_shutdown_is_cancelled(tmp_path: Path) -> None:
    launcher = _ShutdownFollows(ChildExit("signal", None, "TERM"), run=True)
    r = runner(tmp_path, launcher, should_stop=launcher.stop.is_set, shutdown_grace_seconds=5.0)
    with pytest.raises(StepCancelledError, match="stopped by a shutdown"):
        _call(r)


def test_a_child_stopped_from_outside_with_no_shutdown_is_still_a_crash_and_says_so(tmp_path: Path) -> None:
    r = runner(tmp_path, FakeLauncher(exits=[NO_RESULT, ChildExit("signal", None, "TERM")]))
    for _ in range(2):
        with pytest.raises(ResourceExhaustedError, match="stopped from outside") as info:
            _call(r)
        assert classify(info.value) is FailureKind.RESOURCE
        assert "memory" not in str(info.value)
    r = runner(tmp_path, FakeLauncher(exits=[ChildExit("exit-code", 9, "9")]))
    with pytest.raises(ResourceExhaustedError, match=r"died without a result \(exit-code, status 9\)$"):
        _call(r)  # exit code 9 is not SIGKILL


def test_an_oom_kill_is_never_taken_for_a_shutdown(tmp_path: Path) -> None:
    """A guard (it passes on the code before 2026-11-11 too)."""
    launcher = _ShutdownFollows(ChildExit("oom-kill", None, "KILL", "6.0G"), after=0)
    r = runner(tmp_path, launcher, should_stop=launcher.stop.is_set, system_stopping=lambda: True)
    with pytest.raises(ResourceExhaustedError, match="OOM-killed") as info:
        _call(r)
    assert not isinstance(info.value, NoResultError)
    assert classify(info.value) is FailureKind.RESOURCE


def test_a_child_that_reported_its_own_memory_error_is_a_crash_even_at_a_shutdown(tmp_path: Path) -> None:
    """A guard (it passes on the code before 2026-11-11 too)."""
    launcher = _ShutdownFollows(ChildExit("exit-code", 1, "1"), after=0, run=True)
    r = runner(tmp_path, launcher, should_stop=launcher.stop.is_set, system_stopping=lambda: True)
    with pytest.raises(ResourceExhaustedError) as info:
        _call(r, "child_raise", error="memory")
    assert not isinstance(info.value, NoResultError | StepCancelledError)


def test_the_default_shutdown_probe_is_looked_up_when_the_runner_is_built(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # a default argument would be bound at import: no test (and no conftest) could replace it
    monkeypatch.setattr(isolation, "system_stopping", lambda: True)
    monkeypatch.setattr(isolation, "SHUTDOWN_GRACE_SECONDS", 0.01)
    r = IsolatedRunner(tmp_path / "work", launcher=FakeLauncher(exits=[NO_RESULT]), cwd=tmp_path)
    assert r.shutdown_grace_seconds == 0.01
    with pytest.raises(StepCancelledError):
        _call(r)


def test_system_stopping_reads_both_managers(monkeypatch: pytest.MonkeyPatch) -> None:
    def found(name: str) -> str | None:
        return f"/usr/bin/{name}"

    def missing(name: str) -> str | None:
        return None

    monkeypatch.setattr("shutil.which", found)
    calls: list[list[str]] = []

    def answers(system: str, user: str) -> bool:
        calls.clear()

        def run(argv: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
            calls.append(argv)
            assert kw == {"capture_output": True, "text": True, "timeout": 2, "check": False}
            out = user if "--user" in argv else system
            if out == "timeout":
                raise subprocess.TimeoutExpired(argv, 2)
            return subprocess.CompletedProcess(argv, 0 if out == "running" else 1, out + "\n", "")

        return real_system_stopping(run)

    assert answers("running", "running") is False
    assert calls == [
        ["/usr/bin/systemctl", "is-system-running"],
        ["/usr/bin/systemctl", "--user", "is-system-running"],
    ]
    assert answers("running", "stopping") is True
    assert answers("stopping", "running") is True
    assert answers("degraded", "timeout") is False
    assert answers("timeout", "stopping") is True
    monkeypatch.setattr("shutil.which", missing)
    assert answers("stopping", "stopping") is False, "no systemctl: not known to be a shutdown"
    assert calls == []


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
        "MemoryHigh=5G",
        "MemoryMax=6G",
        "MemorySwapMax=1G",
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


class _UsageLauncher(FakeLauncher):
    """A hanging child whose usage the test scripts: throttled, with ``cpu_rate`` CPU-seconds a second."""

    def __init__(self, now: list[float], cpu_rate: float, throttled: bool = True) -> None:
        super().__init__(hang=True)
        self.now, self.cpu_rate, self.throttled = now, cpu_rate, throttled

    def usage(self, unit: str) -> object:
        from wsp_orchestrator.isolation import UnitUsage

        return UnitUsage(cpu_seconds=self.now[0] * self.cpu_rate, throttled=self.throttled)


def _clocked(now: list[float]) -> object:
    def clock() -> float:
        now[0] += 30.0  # every look at the clock is half a minute later
        return now[0]

    return clock


def test_a_step_stalled_at_its_memory_ceiling_is_stopped(tmp_path: Path) -> None:
    now = [0.0]
    fake = _UsageLauncher(now, cpu_rate=0.01)
    r = runner(tmp_path, fake, poll_seconds=0.001, cancel_wait_seconds=0.1, clock=_clocked(now))
    with pytest.raises(ResourceExhaustedError, match="stalled at its memory ceiling"):
        r.call("index", "wsp_orchestrator.testing:child_echo", {}, key="s/1", timeout=30)
    assert fake.children[0].stopped.is_set()  # type: ignore[union-attr]


@pytest.mark.parametrize(("rate", "throttled"), [(0.5, True), (0.0, False)])
def test_a_busy_or_unthrottled_step_is_not_a_stall(tmp_path: Path, rate: float, throttled: bool) -> None:
    now = [0.0]
    fake = _UsageLauncher(now, cpu_rate=rate, throttled=throttled)
    r = runner(tmp_path, fake, poll_seconds=0.001, cancel_wait_seconds=0.1, clock=_clocked(now))
    with pytest.raises(ResourceExhaustedError, match="timeout"):
        r.call("index", "wsp_orchestrator.testing:child_echo", {}, key="s/1", timeout=1.0)


def test_usage_is_read_from_the_cgroup(tmp_path: Path) -> None:
    from wsp_orchestrator.isolation import read_usage

    (tmp_path / "cpu.stat").write_text("usage_usec 12500000\nuser_usec 1\n")
    (tmp_path / "memory.current").write_text("3221225000\n")
    (tmp_path / "memory.high").write_text("3221225472\n")
    u = read_usage(tmp_path)
    assert u is not None
    assert u.cpu_seconds == 12.5
    assert u.throttled
    (tmp_path / "memory.high").write_text("max\n")
    assert read_usage(tmp_path).throttled is False  # type: ignore[union-attr]
    assert read_usage(tmp_path / "gone") is None
