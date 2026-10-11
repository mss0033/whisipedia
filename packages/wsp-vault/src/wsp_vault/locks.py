"""Vault mutation locks: ``flock`` files on LOCAL disk, shared by every process and thread.

SMB byte-range locks are unreliable (ADR-0002), so the locks live in a directory on the machine that
runs every vault writer (the orchestrator daemon, ``wsp ingest``, ``wsp scrub`` and its repair
ladder, parity backfill). :class:`~wsp_vault.vault.FileVault` takes them itself, so no caller can
forget (vault-fetch audit #4, #5).

Three lock levels, always taken in this order (so no two holders can deadlock):

1. **source** ``<source>`` (exclusive): the pointer files ``CURRENT.txt`` and ``CURRENT.d/``. Taken
   by ``promote`` (and so by the repair ladder's rollback) and ``prune``.
2. **generation** ``<source>/<generation>``: *shared* while files are added to staging (``ingest``,
   ``adopt``: many writers may copy different files at once), *exclusive* for anything that changes a
   generation as a whole or reads a decision from it (``seal``, ``mark_degraded``, clearing DEGRADED,
   restoring a damaged file, parity and attestations, ``prune``). ``promote`` holds it shared, so a
   generation cannot be marked DEGRADED between the check and the pointer write.
3. **record** ``<source>/<generation>`` (exclusive): the read-modify-write of the staging record
   ``.wsp-staging.json`` (audit #5: concurrent ingests lost each other's entries).

``flock`` locks belong to an open file description, so two threads of one process exclude each other
exactly like two processes do, and the kernel drops them when a process dies (no stale locks). The
same thread may re-acquire a lock it holds (nested calls); asking for exclusive while holding shared
is a programming error and raises.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import tempfile
import threading
from collections.abc import Generator
from dataclasses import dataclass
from pathlib import Path

import blake3


class LockOrderError(RuntimeError):
    """A thread asked for an exclusive lock it already holds shared (cannot upgrade without a gap)."""


@dataclass(slots=True)
class _Held:
    fd: int
    exclusive: bool
    depth: int


def default_lock_dir(vault_root: Path) -> Path:
    """Where a :class:`~wsp_vault.vault.FileVault` locks when no ``lock_dir`` is given: a per-user
    directory under the local temp dir, keyed by the vault's resolved path, so every process of this
    user that opens the same vault without configuration still shares the locks. Production code
    passes the configured local lock directory (``<index root>/../locks``) instead."""
    key = blake3.blake3(str(vault_root.resolve()).encode()).hexdigest()[:16]
    return Path(tempfile.gettempdir()) / f"whisipedia-vault-locks-{os.getuid()}" / key


class VaultLocks:
    """The lock files of one vault. See the module docstring."""

    def __init__(self, lock_dir: Path) -> None:
        self._dir = lock_dir
        self._local = threading.local()

    @property
    def directory(self) -> Path:
        return self._dir

    def _held(self) -> dict[str, _Held]:
        held: dict[str, _Held] | None = getattr(self._local, "held", None)
        if held is None:
            held = {}
            self._local.held = held
        return held

    def _file(self, name: str) -> Path:
        # Names contain "/" and arbitrary generation ids; a digest keeps file names short and flat.
        digest = blake3.blake3(name.encode()).hexdigest()[:24]
        return self._dir / f"vault-{name.split('/', 1)[0][:40]}-{digest}.lock"

    @contextlib.contextmanager
    def _lock(self, name: str, *, exclusive: bool) -> Generator[None]:
        held = self._held()
        mine = held.get(name)
        if mine is not None:
            if exclusive and not mine.exclusive:
                raise LockOrderError(f"{name}: exclusive lock requested while holding it shared")
            mine.depth += 1
            try:
                yield
            finally:
                mine.depth -= 1
            return
        self._dir.mkdir(parents=True, exist_ok=True)
        fd = os.open(self._file(name), os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            held[name] = _Held(fd, exclusive, 1)
            try:
                yield
            finally:
                del held[name]
        finally:
            os.close(fd)  # releases the flock

    def source(self, source_id: str) -> contextlib.AbstractContextManager[None]:
        """Exclusive lock on a source's pointers (``CURRENT.txt``, ``CURRENT.d/``)."""
        return self._lock(f"{source_id}/@pointers", exclusive=True)

    def generation(
        self, source_id: str, generation_id: str, *, exclusive: bool = True
    ) -> contextlib.AbstractContextManager[None]:
        """Shared (adding files to staging) or exclusive (whole-generation changes) generation lock."""
        return self._lock(f"{source_id}/{generation_id}", exclusive=exclusive)

    def record(self, source_id: str, generation_id: str) -> contextlib.AbstractContextManager[None]:
        """Exclusive lock on the staging record of one generation."""
        return self._lock(f"{source_id}/{generation_id}/@record", exclusive=True)
