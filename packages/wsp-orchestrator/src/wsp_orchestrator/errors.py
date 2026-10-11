"""Failure taxonomy of release steps (audit 2026-10-11, M1/H1).

Not every failure says something about the release. A step that could not take a lock, found the NAS
unmounted or was killed for memory has learnt nothing about the data, so it must never count towards
quarantine. :func:`classify` sorts an exception into one of:

* ``BUSY`` - a lock or a single-writer resource is held by someone else (``wsp index build``, the
  nightly timer, another heavy job). The step is *deferred*: same state, no attempt counted.
* ``UNAVAILABLE`` - storage the step needs is not there (the NAS soft mount is down). Deferred.
* ``RESOURCE`` - the step ran out of memory or was killed (an ``oom-kill`` of its transient unit,
  ``MemoryError``, ``ENOMEM``/``ENOSPC``). Counted as a *crash* of that state, never as an attempt:
  the second one in the same state backs off and alerts ("probably OOM").
* ``CONTENT`` - everything else: the data, the adapter or upstream is at fault. Counts an attempt;
  enough of them quarantine the release.
"""

from __future__ import annotations

import errno
import sqlite3
from collections.abc import Callable
from enum import StrEnum


class FailureKind(StrEnum):
    BUSY = "busy"
    UNAVAILABLE = "unavailable"
    RESOURCE = "resource"
    CONTENT = "content"

    @property
    def counts_attempt(self) -> bool:
        return self is FailureKind.CONTENT


class TransientError(RuntimeError):
    """A failure that says nothing about the release; see :class:`FailureKind`."""

    kind: FailureKind = FailureKind.BUSY


class ResourceBusyError(TransientError):
    """A lock or single-writer resource is held elsewhere: try again later, no attempt counted."""

    kind = FailureKind.BUSY


class StorageUnavailableError(TransientError):
    """Storage the step needs (the NAS) is not reachable."""

    kind = FailureKind.UNAVAILABLE


class ResourceExhaustedError(TransientError):
    """The step was killed for memory (or died without a result): a crash, not a content failure."""

    kind = FailureKind.RESOURCE


class StepCancelledError(TransientError):
    """The step was stopped because the daemon is shutting down."""

    kind = FailureKind.BUSY


# errno values that mean "the network filesystem went away", never "this file is bad".
_STORAGE_ERRNOS = frozenset(
    e
    for e in (
        getattr(errno, "EHOSTDOWN", None),
        getattr(errno, "ESTALE", None),
        getattr(errno, "ENOTCONN", None),
        getattr(errno, "ENOMEDIUM", None),
        getattr(errno, "EREMOTEIO", None),
    )
    if e is not None
)
_RESOURCE_ERRNOS = frozenset({errno.ENOMEM, errno.ENOSPC})
_BUSY_MARKERS = (
    "lockbusy",
    "database is locked",
    "database table is locked",
    "resource temporarily unavailable",
)


def classify(exc: BaseException, *, storage_available: Callable[[], bool] | None = None) -> FailureKind:
    """The :class:`FailureKind` of ``exc``.

    ``storage_available`` (a cheap probe of the vault mount) turns an ``OSError`` raised while the
    NAS is gone into ``UNAVAILABLE`` whatever its errno.
    """
    if isinstance(exc, TransientError):
        return exc.kind
    if isinstance(exc, MemoryError):
        return FailureKind.RESOURCE
    text = str(exc).lower()
    if isinstance(exc, sqlite3.OperationalError) and any(m in text for m in _BUSY_MARKERS):
        return FailureKind.BUSY
    if "lockbusy" in text or "failed to acquire lockfile" in text:  # tantivy's IndexWriter lock
        return FailureKind.BUSY
    if type(exc).__name__ == "FetchLockedError":  # another process downloads the same file (wsp_fetch)
        return FailureKind.BUSY
    if isinstance(exc, OSError):
        if exc.errno in _RESOURCE_ERRNOS:
            return FailureKind.RESOURCE
        if exc.errno in _STORAGE_ERRNOS:
            return FailureKind.UNAVAILABLE
        if storage_available is not None and not _safe_probe(storage_available):
            return FailureKind.UNAVAILABLE
    return FailureKind.CONTENT


def _safe_probe(probe: Callable[[], bool]) -> bool:
    try:
        return bool(probe())
    except Exception:
        return False
