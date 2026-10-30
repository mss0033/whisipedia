"""Writes to the NAS that a moment's trouble on the share must not fail: tried again a few times.

2026-10-24, with the NAS saturated (random reads at 113 ms): the CIFS client logged "Error -104
sending data on socket to server" and reconnected; an ERA5 year failed opening its copy (EAGAIN,
"Resource temporarily unavailable") and a map part failed in its fsync ("Software caused
connection abort"). Both were whole once the share was back.
"""

from __future__ import annotations

import hashlib
import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

TRIES = 5
# EAGAIN; ECONNABORTED and ECONNRESET (ConnectionError); a timed-out request on the share.
TRANSIENT: tuple[type[OSError], ...] = (BlockingIOError, ConnectionError, TimeoutError)


def retrying[T](what: str, fn: Callable[[], T], *, sleep: Callable[[float], None] = time.sleep) -> T:
    """``fn()``, started again after 2, 4, 8, 16 s when the share refuses it for a moment."""
    for attempt in range(1, TRIES + 1):
        try:
            return fn()
        except TRANSIENT as exc:
            if attempt == TRIES:
                raise
            log.warning("%s: %s; trying again (%d of %d)", what, exc, attempt + 1, TRIES)
            sleep(2.0**attempt)
    raise AssertionError("unreachable")


COPY_CHUNK = 8 << 20
# A part is synced this often, and a copy the share refuses goes on from the last synced byte: a
# 76 GB map part, synced once at its end, failed there on all five tries (380 GB written) on
# 2026-10-28 and again on 2026-10-30.
SYNC_EVERY = 1 << 30


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
    tried again from the last synced byte."""
    tmp = dest.with_name(dest.name + ".partial")
    synced = _Synced()

    def once() -> str:
        at, digest = synced.at, synced.digest.copy()
        resume = at > 0 and tmp.is_file() and tmp.stat().st_size >= at
        if not resume:
            at, digest = 0, hashlib.sha256()
        with src.open("rb") as fin, tmp.open("r+b" if resume else "wb") as fout:
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
                    synced.at, synced.digest, unsynced = at, digest.copy(), 0
            fout.flush()
            os.fsync(fout.fileno())
        tmp.replace(dest)
        return digest.hexdigest()

    return retrying(f"copy {src.name}", once, sleep=sleep)
