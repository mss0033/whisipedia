"""Doc ids travel as ONE percent-encoded path segment: ``/v1/documents/wsp%3Arfc%2F9110[/history]``.

ASGI servers decode the path before routing, so ``%2F`` would become ``/`` and a native id ending in
``/history`` (Wiktionary's entry "history") would collide with the history route. This middleware
routes ``/v1/documents/…`` on the *raw* path instead, so ``%2F`` stays inside its segment; the routes
unquote the segment. See docs/api/addresses.md and ADR-0006.
"""

from __future__ import annotations

from starlette.types import ASGIApp, Receive, Scope, Send

RAW_PREFIXES: tuple[str, ...] = ("/v1/documents/", "/v1/files/")


class RawSegmentRouting:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and str(scope.get("path", "")).startswith(RAW_PREFIXES):
            raw = scope.get("raw_path")
            if isinstance(raw, bytes | bytearray):
                path = bytes(raw).split(b"?", 1)[0].decode("latin-1")
                if path.startswith(RAW_PREFIXES):
                    scope = {**scope, "path": path}
        await self.app(scope, receive, send)
