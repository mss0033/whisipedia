"""Writes to the NAS that a moment's trouble on the share must not fail: tried again a few times.

2026-10-24, with the NAS saturated (random reads at 113 ms): the CIFS client logged "Error -104
sending data on socket to server" and reconnected; an ERA5 year failed opening its copy (EAGAIN,
"Resource temporarily unavailable") and a map part failed in its fsync ("Software caused
connection abort"). Both were whole once the share was back.

What counts as "a moment's trouble" is defined here once, for every package (trawl 2026-11-10,
core-correctness-06): :func:`is_transient`. The CIFS mount reports a NAS that is away as a plain
``OSError`` with EHOSTDOWN, EIO, ESTALE or EFAULT, which the three exception classes of
:data:`TRANSIENT` never matched: a collection fetch ended on "[Errno 112] Host is down" where the
code meant "this document failed, next run".

Scope is by call site, not by path (an ``OSError`` from ``fsync`` or a read names no file): call
:func:`retrying` only for work on the share. EIO from a local disk is not transient; if such a call
slips in, every loop is bounded and raises the original error, so the cost is a delay with one
warning per try, never a swallowed error.

``fn`` must do its whole piece of work again: write the file again, read the row group again, open
by path again (ESTALE on a handle that stays open never recovers by waiting). A retry that only
repeats an ``fsync`` is NOT safe for the wide errno list: after a failed fsync the kernel reports the
lost write-back once and marks the pages clean, so the second fsync succeeds with bytes missing.
The fsync-only site, ``wsp_fetch.http`` (the end of a ranged download), therefore passes
``transient=is_blip``. The Refinery's part publish (``wsp_index.refinery``) writes the part again
when the fsync of its ``.partial`` was refused; its retry of the directory fsync alone, after the
rename, is deliberate (the part's bytes were synced before the rename; only the entry is at stake).
Keep it so when this list grows.
"""

from __future__ import annotations

import errno
import logging
import time
from collections.abc import Callable

log = logging.getLogger(__name__)

TRIES = 5
# EAGAIN; ECONNABORTED and ECONNRESET (ConnectionError); a timed-out request on the share.
TRANSIENT: tuple[type[OSError], ...] = (BlockingIOError, ConnectionError, TimeoutError)

TRANSIENT_ERRNOS = frozenset(
    {
        errno.EIO,  # a ``soft`` CIFS mount turns a server timeout or reconnect into EIO
        # CIFS answered preadv with EFAULT ("Bad address") under load on 2026-10-13; the next attempt
        # read the same chunk fine. Three releases were quarantined after five attempts each.
        errno.EFAULT,
        errno.ETIMEDOUT,
        errno.EAGAIN,
        errno.ESTALE,
        errno.ECONNRESET,
        errno.ECONNABORTED,
        errno.EHOSTDOWN,
        errno.EHOSTUNREACH,
        errno.ENETDOWN,
        errno.ENETUNREACH,
        errno.ENETRESET,
    }
)
"""Errnos of a share that a moment later may answer (one set for the vault, the COD reader and
:func:`retrying`)."""

# The NAS or the network is away, not just busy: worth a longer, still bounded wait (a CIFS reconnect
# takes a minute or two). ESTALE is here because the handle went with the old connection; it recovers
# only because every ``fn`` opens by path again, never by waiting on an open descriptor.
OUTAGE_ERRNOS = frozenset(
    {errno.EHOSTDOWN, errno.EHOSTUNREACH, errno.ENETDOWN, errno.ENETUNREACH, errno.ESTALE}
)
OUTAGE_TRIES = 8
OUTAGE_WAIT_MAX_S = 60.0
"""An outage is waited out for 2, 4, 8, 16, 32, 60, 60 s (about 3 minutes). Not longer: some callers
hold the ingest lock, and a NAS that stays away is handled one level up (the lanes' later runs, the
orchestrator's restart ladder)."""


def is_transient(exc: BaseException) -> bool:
    """Whether ``exc`` is the share having a moment: a class of :data:`TRANSIENT` or an ``OSError``
    whose errno is in :data:`TRANSIENT_ERRNOS`."""
    return isinstance(exc, TRANSIENT) or (isinstance(exc, OSError) and exc.errno in TRANSIENT_ERRNOS)


def is_blip(exc: BaseException) -> bool:
    """The narrow test (:data:`TRANSIENT` only, no EIO), for a retry that repeats only an ``fsync``:
    see the module docstring."""
    return isinstance(exc, TRANSIENT)


def retrying[T](
    what: str,
    fn: Callable[[], T],
    *,
    sleep: Callable[[float], None] = time.sleep,
    transient: Callable[[BaseException], bool] = is_transient,
) -> T:
    """``fn()``, started again after 2, 4, 8, 16 s when the share refuses it for a moment
    (``transient(exc)``); up to :data:`OUTAGE_TRIES` tries when the errno says the NAS is away."""
    attempt = 0
    while True:
        attempt += 1
        try:
            return fn()
        except OSError as exc:
            if not transient(exc):
                raise
            limit = OUTAGE_TRIES if exc.errno in OUTAGE_ERRNOS else TRIES
            if attempt >= limit:
                raise
            log.warning("%s: %s; trying again (%d of %d)", what, exc, attempt + 1, limit)
            sleep(min(2.0**attempt, OUTAGE_WAIT_MAX_S))
