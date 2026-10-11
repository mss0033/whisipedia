"""HTTP plumbing around the routes: the Navigator's static files and a quiet request log (audit #21).

* Vite names built assets by content hash (``assets/index-B1x2c3d4.js``): those are served with
  ``Cache-Control: immutable`` for a year. ``index.html`` and other unhashed files are revalidated.
* uvicorn's access log writes a line per request (hundreds per page view with images). The server
  turns it off; :class:`SlowRequestLog` logs only failures and slow requests.
"""

from __future__ import annotations

import logging
import re
import time

from starlette.responses import Response
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Message, Receive, Scope, Send

log = logging.getLogger("wsp_api.requests")

IMMUTABLE = "public, max-age=31536000, immutable"
REVALIDATE = "no-cache"
_HASHED = re.compile(r"(^|/)assets/[^/]+-[A-Za-z0-9_-]{8,}\.[a-z0-9]+$")


class NavigatorFiles(StaticFiles):
    """``StaticFiles`` with cache headers: hashed build assets are immutable, the rest revalidates."""

    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        if response.status_code in (200, 304):
            response.headers["Cache-Control"] = IMMUTABLE if _HASHED.search(path) else REVALIDATE
        return response


class SlowRequestLog:
    """Log requests that fail (status >= 500) or take at least ``slow_seconds``; nothing else."""

    def __init__(self, app: ASGIApp, slow_seconds: float = 2.0) -> None:
        self.app = app
        self.slow_seconds = slow_seconds

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = time.perf_counter()
        status = 0

        async def watch(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = int(message["status"])
            await send(message)

        try:
            await self.app(scope, receive, watch)
        finally:
            took = time.perf_counter() - started
            streaming = str(scope.get("path", "")).endswith("/stream")
            if status >= 500 or status == 0 or (took >= self.slow_seconds and not streaming):
                log.warning(
                    "%s %s -> %s in %.2f s",
                    scope.get("method", "?"),
                    scope.get("path", "?"),
                    status or "-",
                    took,
                )
