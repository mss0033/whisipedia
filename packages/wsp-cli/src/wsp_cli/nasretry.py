"""Writes to the NAS that a moment's trouble on the share must not fail: :func:`wsp_core.nasretry.retrying`
(re-exported) and a resumable, hashed copy built on it.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from wsp_core.nasretry import TRANSIENT as TRANSIENT
from wsp_core.nasretry import TRIES as TRIES
from wsp_core.nasretry import retrying as retrying

COPY_CHUNK = 8 << 20
# A part is synced this often, and a copy the share refuses goes on from the last synced byte: a
# 76 GB map part, synced once at its end, failed there on all five tries (380 GB written) on
# 2026-10-28 and again on 2026-10-30. Below the smallest job budget (768 MiB), so the dirty pages
# between syncs never fill it; the synced pages are dropped at once (the cache is charged to the job).
SYNC_EVERY = 256 << 20


def _drop(fd: int, offset: int, length: int) -> None:
    """Release ``[offset, offset+length)`` of the page cache (best effort)."""
    if hasattr(os, "posix_fadvise"):
        with contextlib.suppress(OSError):
            os.posix_fadvise(fd, offset, length, os.POSIX_FADV_DONTNEED)


def _sequential(fd: int) -> None:
    if hasattr(os, "posix_fadvise"):
        with contextlib.suppress(OSError):
            os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_SEQUENTIAL)


def _shared_prefix(src: Path, other: Path, limit: int, chunk: int) -> tuple[int, Any]:
    """How many bytes from the start ``src`` and ``other`` share, up to ``limit``, and their SHA-256."""
    digest, at = hashlib.sha256(), 0
    with src.open("rb") as a, other.open("rb") as b:
        _sequential(a.fileno())
        _sequential(b.fileno())
        while at < limit:
            n = min(chunk, limit - at)
            x = a.read(n)
            if not x or x != b.read(n):
                break
            digest.update(x)
            _drop(a.fileno(), at, len(x))
            _drop(b.fileno(), at, len(x))
            at += len(x)
    return at, digest


@dataclass
class _Synced:
    at: int = 0  # bytes of the part known to be on the share
    digest: Any = field(default_factory=hashlib.sha256)  # their SHA-256 so far


def copy_hashed(
    src: Path,
    dest: Path,
    pace: Callable[[int], None] | None,
    *,
    sleep: Callable[[float], None] = time.sleep,
    chunk: int = COPY_CHUNK,
    sync_every: int = SYNC_EVERY,
) -> str:
    """Copy ``src`` to ``dest`` (through ``dest.partial``, renamed when whole) and return its SHA-256.
    The part is synced every ``sync_every`` bytes; when the share refuses it for a moment, the copy is
    tried again from the last synced byte. A ``dest`` already whole and identical is read, not written
    (a staging deferred by a refused plan write used to copy every file again); a ``.partial`` another
    run left is continued from its last whole sync that matches ``src`` (a killed 76 GB copy used to
    start over; 2026-10-29 spot check)."""
    tmp = dest.with_name(dest.name + ".partial")
    size = src.stat().st_size
    synced = _Synced()
    if dest.is_file() and dest.stat().st_size == size:
        at, digest = _shared_prefix(src, dest, size, chunk)
        if at == size:
            return digest.hexdigest()
    elif tmp.is_file():
        have = min(tmp.stat().st_size, size) // sync_every * sync_every
        if have:
            synced.at, synced.digest = _shared_prefix(src, tmp, have, chunk)

    def once() -> str:
        at, digest = synced.at, synced.digest.copy()
        resume = at > 0 and tmp.is_file() and tmp.stat().st_size >= at
        if not resume:
            at, digest = 0, hashlib.sha256()
        with src.open("rb") as fin, tmp.open("r+b" if resume else "wb") as fout:
            _sequential(fin.fileno())
            fin.seek(at)
            fout.seek(at)
            fout.truncate()
            unsynced = 0
            while piece := fin.read(chunk):
                digest.update(piece)
                fout.write(piece)
                at += len(piece)
                unsynced += len(piece)
                if pace is not None:
                    pace(len(piece))
                if unsynced >= sync_every:
                    fout.flush()
                    os.fsync(fout.fileno())
                    _drop(fout.fileno(), at - unsynced, unsynced)
                    _drop(fin.fileno(), at - unsynced, unsynced)
                    synced.at, synced.digest, unsynced = at, digest.copy(), 0
            fout.flush()
            os.fsync(fout.fileno())
            _drop(fout.fileno(), 0, at)
            _drop(fin.fileno(), 0, at)
        tmp.replace(dest)
        return digest.hexdigest()

    return retrying(f"copy {src.name}", once, sleep=sleep)
