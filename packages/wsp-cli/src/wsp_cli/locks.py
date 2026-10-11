"""Single-writer locks for operator commands (ADR-0002: lock files live on local disk, never the NAS).

The vault ledger and the index each assume one writer. Two concurrent ``wsp ingest`` runs, for example,
would interleave ledger appends and race on staging renames.

Lock names shared by every process (CLI, orchestrator, timer, ``tools/wsp-bg``):

* ``INDEX`` — the one writer of the Refinery, the Tantivy index, the suggest trie and the change feed;
* ``HEAVY`` — at most one memory-heavy job on the machine at a time (refine, index, rank, spell, embed,
  profile), so their budgets never add up inside ``whisipedia-background.slice``;
* ``INGEST`` — the one writer of vault staging and promotion from ``_incoming``.

A job that needs both takes ``HEAVY`` first, then ``INDEX`` (one global order, no deadlock).
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import time
from collections.abc import Callable, Generator
from pathlib import Path

INDEX = "index"
HEAVY = "heavy"
INGEST = "ingest"


class LockHeldError(RuntimeError):
    """Another process holds the lock."""


@contextlib.contextmanager
def exclusive(
    lock_dir: Path,
    name: str,
    *,
    wait: float | None = None,
    poll: float = 5.0,
    cancel: Callable[[], bool] | None = None,
) -> Generator[Path]:
    """Hold ``<lock_dir>/<name>.lock`` for the duration.

    ``wait=None`` (default) raises :class:`LockHeldError` at once if another process holds it;
    ``wait=seconds`` keeps trying every ``poll`` seconds for that long (``float("inf")``: forever)
    and stops early when ``cancel()`` turns true. ``flock`` locks are released by the kernel when the
    process exits, so a crash never leaves a stale lock behind. The file records the holder's pid.
    """
    lock_dir.mkdir(parents=True, exist_ok=True)
    path = lock_dir / f"{name}.lock"
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        deadline = None if wait is None else time.monotonic() + wait
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                done = deadline is None or time.monotonic() >= deadline or (cancel is not None and cancel())
                if done:
                    holder = path.read_text(encoding="utf-8").strip() or "unknown"
                    raise LockHeldError(f"another '{name}' is running (pid {holder}); lock: {path}") from None
                time.sleep(poll)
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode())
        yield path
    finally:
        os.close(fd)
