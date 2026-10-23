"""Run logs: a structured, timed record of what a command did, for auditing performance and bugs.

A run log is one JSON object per line under ``<state>/logs/<command>/<UTC time>-<pid>.jsonl``: the run's
start (command line, code revision, pid) and end (exit code, wall time, peak memory), and between them
every :func:`phase` (start, end or error, duration, peak memory, the phase's own fields) and whatever the
library's own loggers (:data:`ROOTS`) say at the run's level. The last :data:`KEEP` logs of each
command are kept.

Long jobs keep one always (``wsp index build`` ran 28 hours on 2026-10-21 and left no record of which
step took the time); any command keeps one when ``WSP_DIAG`` is set (``1``: info, ``debug``: debug) or
``--diag`` is given. A phase outside a run log costs a clock read: nothing is written.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import resource
import sys
import time
from collections.abc import Generator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

LOG = logging.getLogger("wsp.diag")
# The loggers a run log listens to: the "wsp." names, and each package's getLogger(__name__) tree.
ROOTS = (
    "wsp",
    "wsp_adapters",
    "wsp_api",
    "wsp_cli",
    "wsp_core",
    "wsp_fetch",
    "wsp_index",
    "wsp_oracle",
    "wsp_orchestrator",
    "wsp_sdk",
    "wsp_vault",
)
KEEP = 30


class JsonFormatter(logging.Formatter):
    """One JSON object per record; structured fields come from ``extra={"fields": {...}}``."""

    def format(self, record: logging.LogRecord) -> str:
        out: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
        }
        fields = getattr(record, "fields", None)
        if isinstance(fields, Mapping):
            for k, v in cast(Mapping[str, Any], fields).items():
                out.setdefault(k, v)
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        return json.dumps(out, default=str, ensure_ascii=False)


def env_level(environ: Mapping[str, str] | None = None) -> str | None:
    """``WSP_DIAG``: ``None`` when unset or off, else the level (``DEBUG`` for ``debug``, ``INFO``)."""
    value = (os.environ if environ is None else environ).get("WSP_DIAG", "").strip().lower()
    if value in ("", "0", "no", "off", "false"):
        return None
    return "DEBUG" if value == "debug" else "INFO"


def peak_rss_mb() -> int:
    """This process's peak resident memory so far, in MB (Linux reports kilobytes)."""
    return int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024)


def _revision() -> str:
    """The checked-out commit (read from ``.git``, no subprocess): which code wrote this log."""
    for folder in Path(__file__).resolve().parents:
        git = folder / ".git"
        if git.is_file():  # a worktree: "gitdir: <path>"
            line = git.read_text(encoding="utf-8").strip()
            git = Path(line.removeprefix("gitdir:").strip()) if line.startswith("gitdir:") else git
        if not git.is_dir():
            continue
        try:
            head = (git / "HEAD").read_text(encoding="utf-8").strip()
            if not head.startswith("ref: "):
                return head[:12]
            ref = head[5:]
            for base in (git, git.parent.parent if git.parent.name == "worktrees" else git):
                if (base / ref).is_file():
                    return (base / ref).read_text(encoding="utf-8").strip()[:12]
            packed = (
                (git / "packed-refs")
                if (git / "packed-refs").is_file()
                else git.parent.parent / "packed-refs"
            )
            for row in packed.read_text(encoding="utf-8").splitlines():
                if row.endswith(" " + ref):
                    return row.split(" ", 1)[0][:12]
        except OSError:
            return ""
        return ""
    return ""


@dataclass
class RunLog:
    path: Path
    handler: logging.Handler
    started: float
    command: str
    previous_levels: dict[str, int]


def _slug(command: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", command.lower()).strip("-") or "wsp"


def start_run(state_dir: Path, command: str, argv: Sequence[str], *, level: str = "INFO") -> RunLog:
    """Open ``<state_dir>/logs/<command>/<time>-<pid>.jsonl`` and log the run's start."""
    folder = state_dir / "logs" / _slug(command)
    folder.mkdir(parents=True, exist_ok=True)
    # Never the log of a run still going: lanes retrying a held lock every minute pushed a six-hour
    # ingest's log out while it was still being written (2026-10-23).
    for old in [f for f in sorted(folder.glob("*.jsonl")) if not _running(f)][: -(KEEP - 1) or None]:
        with contextlib.suppress(OSError):
            old.unlink()
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = folder / f"{stamp}-{os.getpid()}.jsonl"
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(JsonFormatter())
    handler.setLevel(level.upper())
    previous: dict[str, int] = {}
    for name in ROOTS:
        root = logging.getLogger(name)
        previous[name] = root.level
        root.addHandler(handler)
        if root.level == logging.NOTSET or root.level > handler.level:
            root.setLevel(handler.level)
    run = RunLog(path, handler, time.monotonic(), command, previous)
    LOG.info(
        "run start",
        extra={
            "fields": {
                "event": "run-start",
                "command": command,
                "argv": list(argv),
                "pid": os.getpid(),
                "revision": _revision(),
                "python": sys.version.split()[0],
            }
        },
    )
    return run


def _running(path: Path) -> bool:
    """The log's run (``<time>-<pid>.jsonl``) is still going: its process lives, and not this one."""
    pid = path.stem.rpartition("-")[2]
    if not pid.isdigit() or int(pid) == os.getpid():
        return False
    return Path(f"/proc/{pid}").exists()


def end_run(run: RunLog, exit_code: int | str, *, discard: bool = False) -> None:
    """Log the run's end (exit code, wall time, peak memory) and close its file; ``discard`` removes
    it instead (a run that did nothing worth a record, such as one that found a lock held)."""
    LOG.info(
        "run end",
        extra={
            "fields": {
                "event": "run-end",
                "command": run.command,
                "exit": exit_code,
                "seconds": round(time.monotonic() - run.started, 3),
                "peak_rss_mb": peak_rss_mb(),
            }
        },
    )
    for name, level in run.previous_levels.items():
        root = logging.getLogger(name)
        root.removeHandler(run.handler)
        root.setLevel(level)
    run.handler.close()
    if discard:
        with contextlib.suppress(OSError):
            run.path.unlink()


@contextlib.contextmanager
def phase(name: str, **fields: Any) -> Generator[dict[str, Any]]:
    """Time a step: its start, then its end (or error) with the seconds it took and the peak memory.
    The yielded dict takes the step's results (``info["docs"] = n``), logged with its end."""
    result: dict[str, Any] = {}
    started = time.monotonic()
    LOG.info(f"{name} start", extra={"fields": {"event": "phase-start", "phase": name, **fields}})
    try:
        yield result
    except BaseException as exc:
        LOG.error(
            f"{name} failed",
            extra={
                "fields": {
                    "event": "phase-error",
                    "phase": name,
                    **fields,
                    "seconds": round(time.monotonic() - started, 3),
                    "error": f"{type(exc).__name__}: {exc}",
                }
            },
        )
        raise
    LOG.info(
        f"{name} end",
        extra={
            "fields": {
                "event": "phase-end",
                "phase": name,
                **fields,
                **result,
                "seconds": round(time.monotonic() - started, 3),
                "peak_rss_mb": peak_rss_mb(),
            }
        },
    )
