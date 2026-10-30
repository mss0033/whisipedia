"""Writes to the NAS that a moment's trouble on the share must not fail: tried again a few times.

2026-10-24, with the NAS saturated (random reads at 113 ms): the CIFS client logged "Error -104
sending data on socket to server" and reconnected; an ERA5 year failed opening its copy (EAGAIN,
"Resource temporarily unavailable") and a map part failed in its fsync ("Software caused
connection abort"). Both were whole once the share was back.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

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
