"""Heavy steps in child processes with their own memory ceiling (audit 2026-10-11, C2/C3/H7).

The orchestrator is a small control plane: refine, index and content profiling (pyarrow, Tantivy,
libzim) never run inside it. :class:`IsolatedRunner` starts ``python -m wsp_orchestrator.child
<module:function>`` as a **transient systemd unit** in ``whisipedia-background.slice``::

    systemd-run --user --wait --collect --slice=whisipedia-background.slice
      -p MemoryHigh=3G -p MemoryMax=4G -p MemorySwapMax=512M -p KillMode=mixed -p TimeoutStopSec=60 ...

and reads how it ended:

* ``success`` with a result file: the function's JSON value is returned;
* the function raised: the child classified the error (:mod:`wsp_orchestrator.errors`) and the same
  kind is raised here (a held lock stays ``BUSY``: the step is deferred, no attempt counted);
* ``oom-kill``, a signal, a core dump, or no result at all: :class:`ResourceExhaustedError`, which
  the pipeline counts as a crash of that state (alert after two), never as a content failure;
* the unit could not start (e.g. a unit of that name from before a restart is still stopping):
  :class:`ResourceBusyError`.

Shutdown (``should_stop``) stops the unit (``KillMode=mixed``: SIGTERM to the main process, SIGKILL
to the rest after ``TimeoutStopSec``) without waiting for it, so the daemon still exits within its
grace period. Without ``systemd-run`` (tests, containers) :class:`SubprocessLauncher` runs the same
command as a plain child process in its own session.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any, Protocol, cast, runtime_checkable

from wsp_orchestrator.errors import (
    FailureKind,
    ResourceBusyError,
    ResourceExhaustedError,
    StepCancelledError,
    StorageUnavailableError,
)

log = logging.getLogger("wsp.orchestrator.isolation")

DEFAULT_SLICE = "whisipedia-background.slice"
REQUEST = "request.json"
RESULT = "result.json"
# Environment handed to the child: a transient unit starts from systemd's environment, not ours.
PASS_ENV_PREFIXES = ("WSP_", "OMP_", "MKL_", "TOKENIZERS_", "XDG_", "HF_")
PASS_ENV = ("HOME", "LANG", "LC_ALL", "TZ", "PATH")


class ChildStepError(RuntimeError):
    """The child's function raised a content error (counts an attempt)."""


@dataclass(frozen=True, slots=True)
class UnitLimits:
    """Resource properties of a transient step unit."""

    # One heavy step at a time (the HEAVY lock); sized into whisipedia-background.slice next to the
    # orchestrator and the fetch lanes (deploy/systemd/whisipedia-background.slice, audit 2026-10-12 H1).
    memory_high: str = "3G"
    memory_max: str = "4G"
    memory_swap_max: str = "512M"
    timeout_stop_sec: int = 60
    cpu_weight: int = 20
    nice: int = 19
    oom_score_adjust: int = 500

    def properties(self) -> list[str]:
        return [
            f"MemoryHigh={self.memory_high}",
            f"MemoryMax={self.memory_max}",
            f"MemorySwapMax={self.memory_swap_max}",
            "KillMode=mixed",
            f"TimeoutStopSec={self.timeout_stop_sec}",
            f"CPUWeight={self.cpu_weight}",
            f"Nice={self.nice}",
            f"OOMScoreAdjust={self.oom_score_adjust}",
            # best-effort/7, not idle: an idle-class child holding a lock starves everyone (LOW finding)
            "IOSchedulingClass=best-effort",
            "IOSchedulingPriority=7",
        ]


@dataclass(frozen=True, slots=True)
class ChildExit:
    """How a child ended, in systemd's ``Result=`` vocabulary."""

    result: str  # success | exit-code | signal | core-dump | oom-kill | timeout | start-failed | ...
    exit_code: int | None = None
    status: str | None = None  # e.g. "KILL" for code=killed/status=KILL
    memory_peak: str | None = None
    detail: str = ""


class ChildProcess(Protocol):
    def wait(self, timeout: float) -> ChildExit | None:
        """The exit once the child has ended, else ``None`` after ``timeout`` seconds."""
        ...

    def stop(self) -> None:
        """Ask the child to stop (SIGTERM, SIGKILL after the unit's stop timeout); does not block."""
        ...


class Launcher(Protocol):
    def start(
        self, unit: str, argv: Sequence[str], *, env: Mapping[str, str], cwd: Path, limits: UnitLimits
    ) -> ChildProcess: ...


@runtime_checkable
class KnowsUnits(Protocol):
    """Optional launcher capability: is a unit of this name still running (or stopping)?"""

    def unit_running(self, unit: str) -> bool: ...


# --- systemd-run -------------------------------------------------------------------------------------


def parse_systemd_run(stderr: str, returncode: int) -> ChildExit:
    """Parse the summary ``systemd-run --wait`` prints when the unit ends."""
    result: str | None = None
    status: str | None = None
    code: int | None = None
    peak: str | None = None
    for line in stderr.splitlines():
        line = line.strip()
        if line.startswith("Finished with result:"):
            result = line.split(":", 1)[1].strip()
        elif line.startswith("Main processes terminated with:"):
            # "code=exited/status=1" or "code=killed/status=KILL"
            fields = dict(
                part.split("=", 1) for part in line.split(":", 1)[1].strip().split("/") if "=" in part
            )
            status = fields.get("status")
            if fields.get("code") == "exited" and status is not None and status.isdigit():
                code = int(status)
        elif line.startswith("Memory peak:"):
            peak = line.split(":", 1)[1].strip()
    if result is None:
        return ChildExit("start-failed", returncode, None, None, stderr.strip()[-2000:])
    return ChildExit(result, code, status, peak, stderr.strip()[-2000:])


class _SystemdChild:
    def __init__(self, unit: str, proc: subprocess.Popen[bytes], stderr_path: Path, systemctl: str | None):
        self.unit = unit
        self.proc = proc
        self.stderr_path = stderr_path
        self.systemctl = systemctl
        self._stopped = False
        self._exit: ChildExit | None = None

    def wait(self, timeout: float) -> ChildExit | None:
        if self._exit is not None:
            return self._exit
        try:
            rc = self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return None
        try:
            text = self.stderr_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        self.stderr_path.unlink(missing_ok=True)
        self._exit = parse_systemd_run(text, rc)
        return self._exit

    def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        if self.systemctl is None:
            with contextlib.suppress(ProcessLookupError):
                self.proc.terminate()
            return
        with contextlib.suppress(OSError, subprocess.TimeoutExpired):
            subprocess.run(  # noqa: S603 - fixed argv, no shell
                [self.systemctl, "--user", "stop", "--no-block", self.unit],
                capture_output=True,
                timeout=10,
                check=False,
            )


@dataclass
class SystemdRunLauncher:
    """Transient ``systemd --user`` units in ``slice``."""

    slice: str = DEFAULT_SLICE
    systemd_run: str | None = None
    systemctl: str | None = None
    log_dir: Path | None = None

    def __post_init__(self) -> None:
        self.systemd_run = self.systemd_run or shutil.which("systemd-run")
        self.systemctl = self.systemctl or shutil.which("systemctl")

    def unit_running(self, unit: str) -> bool:
        """True unless systemd reports the unit gone (``inactive``, ``failed`` or unknown).

        ``activating``, ``active``, ``deactivating`` and ``reloading`` all count as running; when
        systemctl cannot answer, the unit is assumed running (deferring is safe, clearing its box is not).
        """
        if self.systemctl is None:
            return False
        try:
            done = subprocess.run(  # noqa: S603 - fixed argv, no shell
                [self.systemctl, "--user", "show", "-p", "ActiveState", "--value", unit],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return True
        if done.returncode != 0:
            return True
        return done.stdout.strip() not in ("inactive", "failed", "")

    @staticmethod
    def available() -> bool:
        """``systemd-run`` exists and a user manager is reachable (``$XDG_RUNTIME_DIR/systemd``)."""
        runtime = os.environ.get("XDG_RUNTIME_DIR")
        return (
            shutil.which("systemd-run") is not None
            and runtime is not None
            and Path(runtime, "systemd", "private").exists()
        )

    def command(
        self, unit: str, argv: Sequence[str], *, env: Mapping[str, str], cwd: Path, limits: UnitLimits
    ) -> list[str]:
        cmd = [
            self.systemd_run or "systemd-run",
            "--user",
            "--wait",
            "--collect",
            "--service-type=exec",
            f"--unit={unit}",
            f"--slice={self.slice}",
            f"--working-directory={cwd}",
        ]
        for prop in limits.properties():
            cmd += ["-p", prop]
        # glibc's per-thread arenas kept gigabytes of freed memory under a step's ceiling (the index
        # unit sets the same, 2026-10-13); a caller's own value wins.
        for key, value in sorted({"MALLOC_ARENA_MAX": "2", **env}.items()):
            cmd += ["-E", f"{key}={value}"]
        return [*cmd, "--", *argv]

    def start(
        self, unit: str, argv: Sequence[str], *, env: Mapping[str, str], cwd: Path, limits: UnitLimits
    ) -> ChildProcess:
        log_dir = self.log_dir or cwd
        log_dir.mkdir(parents=True, exist_ok=True)
        stderr_path = log_dir / f"{unit}.systemd-run.log"
        with stderr_path.open("wb") as err:
            proc = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
                self.command(unit, argv, env=env, cwd=cwd, limits=limits),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=err,
                start_new_session=True,
            )
        return _SystemdChild(unit, proc, stderr_path, self.systemctl)


# --- plain subprocess (no systemd) --------------------------------------------------------------------


class _PlainChild:
    def __init__(self, proc: subprocess.Popen[bytes], kill_after: float) -> None:
        self.proc = proc
        self.kill_after = kill_after
        self._kill_at: float | None = None

    def _signal(self, sig: int) -> None:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(self.proc.pid, sig)

    def wait(self, timeout: float) -> ChildExit | None:
        try:
            rc = self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            if self._kill_at is not None and time.monotonic() >= self._kill_at:
                self._signal(signal.SIGKILL)
            return None
        if rc == 0:
            return ChildExit("success", 0)
        if rc < 0:
            name = signal.Signals(-rc).name.removeprefix("SIG")
            return ChildExit("signal", None, name)
        return ChildExit("exit-code", rc, str(rc))

    def stop(self) -> None:
        if self._kill_at is None:
            self._kill_at = time.monotonic() + self.kill_after
            self._signal(signal.SIGTERM)


@dataclass
class SubprocessLauncher:
    """Fallback without systemd: a child process in its own session (no memory ceiling)."""

    def start(
        self, unit: str, argv: Sequence[str], *, env: Mapping[str, str], cwd: Path, limits: UnitLimits
    ) -> ChildProcess:
        proc = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
            list(argv), cwd=cwd, env={**os.environ, **env}, stdin=subprocess.DEVNULL, start_new_session=True
        )
        return _PlainChild(proc, float(limits.timeout_stop_sec))


def default_launcher(log_dir: Path | None = None, environ: Mapping[str, str] | None = None) -> Launcher:
    """Transient units when the daemon itself runs as a systemd service, else plain children.

    ``$WSP_HEAVY_LAUNCHER`` (``systemd`` or ``subprocess``) overrides. Under systemd a service has
    ``$INVOCATION_ID``; a test run or a shell does not, so they never start real units.
    """
    env = os.environ if environ is None else environ
    choice = env.get("WSP_HEAVY_LAUNCHER", "").strip().lower()
    if choice == "subprocess":
        return SubprocessLauncher()
    if (choice == "systemd" or "INVOCATION_ID" in env) and SystemdRunLauncher.available():
        return SystemdRunLauncher(log_dir=log_dir)
    if choice == "systemd" or "INVOCATION_ID" in env:
        log.warning("systemd-run is not available: heavy steps run as plain child processes (no cap)")
    return SubprocessLauncher()


# --- the runner -----------------------------------------------------------------------------------------


def unit_name(step: str, key: str) -> str:
    """A stable, valid unit name per (step, release): ``whisipedia-step-refine-<hash>``."""
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in step)[:24]
    return f"whisipedia-step-{safe}-{hashlib.sha256(key.encode()).hexdigest()[:16]}"


def child_env(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    env = {
        k: v
        for k, v in os.environ.items()
        if k in PASS_ENV or any(k.startswith(p) for p in PASS_ENV_PREFIXES)
    }
    env["PYTHONUNBUFFERED"] = "1"
    env.update(extra or {})
    return env


def _raise_for(kind: str, message: str) -> None:
    match kind:
        case FailureKind.BUSY.value:
            raise ResourceBusyError(message)
        case FailureKind.UNAVAILABLE.value:
            raise StorageUnavailableError(message)
        case FailureKind.RESOURCE.value:
            raise ResourceExhaustedError(message)
        case _:
            raise ChildStepError(message)


class IsolatedRunner:
    """Run ``module:function(payload) -> JSON`` in a child with a memory ceiling (module docstring)."""

    def __init__(
        self,
        workdir: Path,
        *,
        launcher: Launcher | None = None,
        limits: UnitLimits | None = None,
        should_stop: Callable[[], bool] = lambda: False,
        python: str | None = None,
        poll_seconds: float = 0.5,
        cancel_wait_seconds: float = 3.0,
        env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
    ) -> None:
        self.workdir = workdir
        self.launcher = launcher if launcher is not None else default_launcher(workdir / "_steps")
        self.limits = limits or UnitLimits()
        self.should_stop = should_stop
        self.python = python or sys.executable
        self.poll_seconds = poll_seconds
        self.cancel_wait_seconds = cancel_wait_seconds
        self.env = dict(env or {})
        self.cwd = cwd or Path.cwd()

    def call(
        self,
        step: str,
        target: str,
        payload: Mapping[str, Any],
        *,
        key: str,
        timeout: float | None = None,
        limits: UnitLimits | None = None,
    ) -> Any:
        """Run ``target`` on ``payload`` in a child unit and return its JSON value (or raise)."""
        if self.should_stop():
            raise StepCancelledError(f"{step}: shutdown in progress")
        unit = unit_name(step, key)
        lim = limits or self.limits
        box = self.workdir / "_steps" / unit
        if box.exists() and isinstance(self.launcher, KnowsUnits) and self.launcher.unit_running(unit):
            # Left by an earlier daemon whose child unit still runs: its request and result files are
            # in there. Clearing them would throw its work away; wait for it instead (audit
            # 2026-10-12, L5). A busy step is deferred, no attempt counted.
            raise ResourceBusyError(f"{step}: {unit} from an earlier run is still running")
        shutil.rmtree(box, ignore_errors=True)
        box.mkdir(parents=True)
        try:
            request, result = box / REQUEST, box / RESULT
            # the request carries the whole config (it may hold credentials): owner-only, removed below
            fd = os.open(request, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(dict(payload), fh, default=str)
            argv = [self.python, "-m", "wsp_orchestrator.child", target, str(request), str(result)]
            started = time.monotonic()
            log.info("heavy step started", extra={"fields": {"step": step, "unit": unit, "key": key}})
            proc = self.launcher.start(unit, argv, env=child_env(self.env), cwd=self.cwd, limits=lim)
            outcome = self._wait(proc, step, unit, started, timeout)
            log.info(
                "heavy step ended",
                extra={
                    "fields": {
                        "step": step,
                        "unit": unit,
                        "result": outcome.result,
                        "status": outcome.status,
                        "memory_peak": outcome.memory_peak,
                        "seconds": round(time.monotonic() - started, 1),
                    }
                },
            )
            return self._value(step, unit, outcome, result, lim)
        finally:
            shutil.rmtree(box, ignore_errors=True)

    def _wait(
        self, proc: ChildProcess, step: str, unit: str, started: float, timeout: float | None
    ) -> ChildExit:
        while True:
            outcome = proc.wait(self.poll_seconds)
            if outcome is not None:
                return outcome
            reason: str | None = None
            if self.should_stop():
                reason = "shutdown"
            elif timeout is not None and time.monotonic() - started > timeout:
                reason = f"timeout after {timeout:.0f}s"
            if reason is None:
                continue
            proc.stop()
            deadline = time.monotonic() + self.cancel_wait_seconds
            while time.monotonic() < deadline and proc.wait(0.1) is None:
                pass
            if reason == "shutdown":
                raise StepCancelledError(f"{step} ({unit}) stopped for shutdown")
            raise ResourceExhaustedError(f"{step} ({unit}) stopped: {reason}")

    @staticmethod
    def _value(step: str, unit: str, outcome: ChildExit, result: Path, limits: UnitLimits) -> Any:
        doc: Mapping[str, Any] | None = None
        if result.is_file():
            with contextlib.suppress(ValueError, OSError):
                doc = cast(Mapping[str, Any], json.loads(result.read_text(encoding="utf-8")))
        if outcome.result == "oom-kill":
            raise ResourceExhaustedError(
                f"{step} was OOM-killed (MemoryMax={limits.memory_max}, peak {outcome.memory_peak or '?'})"
            )
        if outcome.result == "start-failed":
            detail = outcome.detail or "no detail"
            if any(m in detail for m in ("already exists", "already loaded", "is active")):
                # the unit of an earlier daemon is still running or stopping: wait for it
                raise ResourceBusyError(f"{step}: {unit} is still running: {detail}")
            # anything else (a property systemd refuses, no user manager) is counted like a crash:
            # retried, then backed off with an alert that carries systemd's message
            raise ResourceExhaustedError(f"{step}: could not start {unit}: {detail}")
        if doc is not None and doc.get("ok") is True and outcome.result == "success":
            return doc.get("value")
        if doc is not None and doc.get("ok") is False:
            _raise_for(str(doc.get("kind", "")), f"{step}: {doc.get('error', 'failed')}")
        raise ResourceExhaustedError(
            f"{step} died without a result ({outcome.result}"
            f"{', status ' + outcome.status if outcome.status else ''}); probably killed for memory"
        )


def write_json_atomic(path: Path, doc: Mapping[str, Any]) -> None:
    tmp = path.with_name(f"{path.name}.{os.getpid()}.partial")
    fh: IO[str]
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(dict(doc), fh, default=str)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
