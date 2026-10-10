"""Injectable time source so the scheduler, backoff and policy can be tested by time travel."""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    def now(self) -> datetime:
        """Current time, timezone-aware (UTC)."""
        ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class ManualClock:
    """A clock that only moves when told to. Thread-safe."""

    def __init__(self, start: datetime) -> None:
        if start.tzinfo is None:
            raise ValueError("ManualClock needs a timezone-aware start time")
        self._now = start
        self._lock = threading.Lock()

    def now(self) -> datetime:
        with self._lock:
            return self._now

    def advance(self, delta: timedelta) -> datetime:
        with self._lock:
            self._now += delta
            return self._now

    def set(self, when: datetime) -> None:
        if when.tzinfo is None:
            raise ValueError("timezone-aware time required")
        with self._lock:
            self._now = when


def ensure_utc(when: datetime) -> datetime:
    """Normalize to UTC; naive values are assumed to already be UTC."""
    if when.tzinfo is None:
        return when.replace(tzinfo=UTC)
    return when.astimezone(UTC)
