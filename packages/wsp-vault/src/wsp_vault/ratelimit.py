"""Byte-rate limiter for background reads of the vault (scrub, parity) (vault-fetch audit #10).

A token bucket: tokens accrue at ``bytes_per_sec`` up to ``burst_seconds`` worth, and a read of ``n``
bytes takes ``n`` tokens, sleeping off any debt. The earlier limiter measured the average rate since
its first call, so an hour of idleness between two hourly scrub slices was banked as an hour of
unlimited reading (a 25 GB slice slept 0 s instead of ~500 s). Here idle time is worth at most one
burst, and :meth:`RateLimiter.reset` (called at the start of every run) empties the bucket.

``should_stop`` and ``should_pause`` are checked inside :meth:`RateLimiter.consume` (at most every
``check_every`` seconds, and between sleep slices), so a paced read of a 250 GB file stops within a
fraction of a second on shutdown (:class:`~wsp_vault.cancel.Interrupted`) or when the owner starts
using the library (:class:`~wsp_vault.cancel.Paused`), not only between files.

``ionice`` does not throttle reads of a CIFS mount (they are network I/O, not block I/O); this
limiter is what keeps background work from saturating the NAS link.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from wsp_vault.cancel import Interrupted, Paused, never

_SLICE = 0.25


class RateLimiter:
    """``bytes_per_sec=None`` disables pacing (stop and pause checks still apply)."""

    def __init__(
        self,
        bytes_per_sec: float | None,
        *,
        burst_seconds: float = 1.0,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        should_stop: Callable[[], bool] = never,
        should_pause: Callable[[], bool] = never,
        check_every: float = 0.5,
    ) -> None:
        if bytes_per_sec is not None and bytes_per_sec <= 0:
            raise ValueError("bytes_per_sec must be positive")
        if burst_seconds < 0:
            raise ValueError("burst_seconds must not be negative")
        self._rate = bytes_per_sec
        self._burst = (bytes_per_sec or 0.0) * burst_seconds
        self._monotonic = monotonic
        self._sleep = sleep
        self.should_stop = should_stop
        self.should_pause = should_pause
        self._check_every = check_every
        self._tokens = 0.0
        self._last: float | None = None
        self._checked: float | None = None

    @property
    def rate(self) -> float | None:
        return self._rate

    def reset(self) -> None:
        """Start a run with an empty bucket: no credit carried over from before."""
        self._tokens = 0.0
        self._last = None
        self._checked = None

    def check(self) -> None:
        """Raise :class:`Interrupted` (stop) or :class:`Paused` (busy) when asked to."""
        if self.should_stop():
            raise Interrupted("stop requested")
        if self.should_pause():
            raise Paused("the library is in interactive use")

    def _maybe_check(self, now: float) -> None:
        if self._checked is None or now - self._checked >= self._check_every:
            self._checked = now
            self.check()

    def consume(self, n: int) -> None:
        now = self._monotonic()
        self._maybe_check(now)
        if self._rate is None:
            return
        if self._last is None:
            self._last = now
        self._tokens = min(self._burst, self._tokens + (now - self._last) * self._rate)
        self._last = now
        self._tokens -= n
        debt = -self._tokens / self._rate if self._tokens < 0 else 0.0
        interruptible = self.should_stop is not never or self.should_pause is not never
        while debt > 0:
            step = min(debt, _SLICE) if interruptible else debt
            self._sleep(step)
            debt -= step
            if interruptible:
                self.check()
