"""Single-writer locks for operator commands (ADR-0002: lock files live on local disk, never the NAS).

The vault ledger and the index each assume one writer. Two concurrent ``wsp ingest`` runs, for example,
would interleave ledger appends and race on staging renames.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
from collections.abc import Generator
from pathlib import Path


class LockHeldError(RuntimeError):
    """Another process holds the lock."""


@contextlib.contextmanager
def exclusive(lock_dir: Path, name: str) -> Generator[Path]:
    """Hold ``<lock_dir>/<name>.lock`` for the duration; raise :class:`LockHeldError` if it is taken.

    ``flock`` locks are released by the kernel when the process exits, so a crash never leaves a
    stale lock behind. The file records the holder's pid for the error message.
    """
    lock_dir.mkdir(parents=True, exist_ok=True)
    path = lock_dir / f"{name}.lock"
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            holder = path.read_text(encoding="utf-8").strip() or "unknown"
            raise LockHeldError(f"another '{name}' is running (pid {holder}); lock: {path}") from None
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode())
        yield path
    finally:
        os.close(fd)
