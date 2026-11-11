"""Is the owner using the library right now? A tiny local signal from the API to the Warden.

The API touches a marker file (its mtime is the signal) on interactive requests, at most once every
``min_interval`` seconds; the scrub and the parity backfill read it and pause while it is recent
(PLAN §8.2: "paused while the browser is in active use"). Polling endpoints (health, system status,
event streams) do not count, nor does automation that marks itself (:func:`is_probe`). Nothing else
is stored in the marker. Next to it, ``<marker>.client`` holds one line: the User-Agent of the client
whose request last set the mark (so that a pause can be traced to a script that did not mark itself;
``tools/wsp-yield`` prints it). That is the client that started a pause, not every client that kept
it going: the mark is set at most once per ``min_interval``. Still no paths and no queries.
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Awaitable, Callable, Iterable, MutableMapping
from pathlib import Path
from typing import Any, cast

MARKER_NAME = "last-interactive"
# The change feed (an open page's live stream, or its polling when the stream is refused) is a page
# left open, not someone reading: counted, an idle tab kept the background frozen (wsp-yield, 2026-10-22).
QUIET_PREFIXES = ("/v1/health", "/v1/system", "/v1/events", "/v1/changes", "/v1/openapi.json", "/favicon")
PROBE_HEADER = b"x-whisipedia-probe"
"""A request carrying this header is a machine's probe, not the owner browsing: the health check asks
for the start page every two minutes, and counted as interactive use it kept the library "busy" for
good (scrub and parity never ran, ingests stayed at their slow share; found 2026-10-14)."""

PROBE_UA_TOKEN = b"whisipedia-probe"
"""For a client that can set its User-Agent but no other header: a User-Agent containing this token
(any case) is a probe too. No browser sends it by accident."""
CLIENT_SUFFIX = ".client"
CLIENT_MAX = 80
NO_USER_AGENT = "(no user agent)"

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


def is_probe(headers: Iterable[tuple[bytes, bytes]]) -> bool:
    """Automation that says so: the probe header (any value), or the token in its User-Agent."""
    return any(k == PROBE_HEADER or (k == b"user-agent" and PROBE_UA_TOKEN in v.lower()) for k, v in headers)


def client_name(headers: Iterable[tuple[bytes, bytes]]) -> str:
    """The request's User-Agent as one printable ASCII line of at most :data:`CLIENT_MAX` characters."""
    raw = next((v for k, v in headers if k == b"user-agent"), b"")
    text = "".join(c if " " <= c <= "~" else "?" for c in raw.decode("latin-1")).strip()
    return text[:CLIENT_MAX] or NO_USER_AGENT


def client_path(path: Path) -> Path:
    return path.with_name(path.name + CLIENT_SUFFIX)


def note_client(path: Path, client: str) -> None:
    """Write who set the mark, next to it. The marker itself stays empty: its mtime is the signal."""
    client_path(path).write_text(client + "\n", encoding="ascii", errors="replace")


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
            headers = cast(Iterable[tuple[bytes, bytes]], scope.get("headers") or ())
            if (
                now - self._last >= self.min_interval
                and not req.startswith(QUIET_PREFIXES)
                and not is_probe(headers)
            ):
                self._last = now
                with contextlib.suppress(OSError):  # a read-only state dir must never break browsing
                    touch(self.path)
                    note_client(self.path, client_name(headers))
        await self.app(scope, receive, send)


def default_marker(state_dir: Path | None) -> Path | None:
    return state_dir / MARKER_NAME if state_dir is not None else None
