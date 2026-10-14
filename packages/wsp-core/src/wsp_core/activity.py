"""Is the owner using the library right now? A tiny local signal from the API to the Warden.

The API touches a marker file (its mtime is the signal) on interactive requests, at most once every
``min_interval`` seconds; the scrub and the parity backfill read it and pause while it is recent
(PLAN §8.2: "paused while the browser is in active use"). Polling endpoints (health, system status,
event streams) do not count. Nothing else is stored: no paths, no queries.
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Awaitable, Callable, MutableMapping
from pathlib import Path
from typing import Any

MARKER_NAME = "last-interactive"
QUIET_PREFIXES = ("/v1/health", "/v1/system", "/v1/events", "/v1/openapi.json", "/favicon")
PROBE_HEADER = b"x-whisipedia-probe"
"""A request carrying this header is a machine's probe, not the owner browsing: the health check asks
for the start page every two minutes, and counted as interactive use it kept the library "busy" for
good (scrub and parity never ran, ingests stayed at their slow share; found 2026-10-14)."""

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
App = Callable[[Scope, Receive, Send], Awaitable[None]]


def touch(path: Path) -> None:
    try:
        path.touch()
    except FileNotFoundError:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()


def seconds_since(path: Path | None, *, now: float | None = None) -> float | None:
    """Seconds since the last interactive request, ``None`` if never (or no marker configured)."""
    if path is None:
        return None
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    return max(0.0, (time.time() if now is None else now) - mtime)


def recently_active(path: Path | None, window_seconds: float, *, now: float | None = None) -> bool:
    age = seconds_since(path, now=now)
    return age is not None and age < window_seconds


class ActivityMiddleware:
    """Pure ASGI middleware: mark interactive HTTP requests. ``path=None`` disables it."""

    def __init__(self, app: App, path: Path | None = None, min_interval: float = 30.0) -> None:
        self.app = app
        self.path = path
        self.min_interval = min_interval
        self._last = float("-inf")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if self.path is not None and scope.get("type") == "http":
            req = str(scope.get("path", ""))
            now = time.monotonic()
            headers = scope.get("headers") or ()
            probe = any(k == PROBE_HEADER for k, _v in headers)
            if now - self._last >= self.min_interval and not probe and not req.startswith(QUIET_PREFIXES):
                self._last = now
                with contextlib.suppress(OSError):  # a read-only state dir must never break browsing
                    touch(self.path)
        await self.app(scope, receive, send)


def default_marker(state_dir: Path | None) -> Path | None:
    return state_dir / MARKER_NAME if state_dir is not None else None
