"""Thread-safe token bucket shared by every connection of a fetcher (PLAN §6.5 bandwidth cap)."""

from __future__ import annotations

import threading
import time


class TokenBucket:
    """``rate`` bytes/s with a burst of ``capacity`` bytes. ``consume`` may go into debt and then sleeps.

    Going into debt lets a caller consume a chunk larger than the burst without deadlocking; the
    long-run average still converges on ``rate``.
    """

    def __init__(self, rate: float, capacity: float | None = None) -> None:
        if rate <= 0:
            raise ValueError("rate must be positive")
        self.rate = float(rate)
        self.capacity = float(capacity if capacity is not None else max(rate / 4, 64 * 1024))
        self._tokens = self.capacity
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def consume(self, n: int) -> None:
        with self._lock:
            now = time.monotonic()
            self._tokens = min(self.capacity, self._tokens + (now - self._last) * self.rate)
            self._last = now
            self._tokens -= n
            wait = -self._tokens / self.rate if self._tokens < 0 else 0.0
        if wait > 0:
            time.sleep(wait)
