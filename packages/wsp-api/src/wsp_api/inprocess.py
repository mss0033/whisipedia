"""A synchronous ``httpx`` transport that calls an ASGI app in-process: no sockets at all.

Used by the CLI when no server URL is given and by tests of sync clients. Each request runs on a fresh
event loop, so it must not be used from inside a running loop (use ``httpx.ASGITransport`` there).
Responses are fully buffered: streaming endpoints need a finite stream (e.g. ``max_seconds``).
"""

from __future__ import annotations

import asyncio

import httpx
from starlette.types import ASGIApp


class InProcessTransport(httpx.BaseTransport):
    def __init__(self, app: ASGIApp, *, client: tuple[str, int] = ("127.0.0.1", 0)) -> None:
        # Like a real server: an unhandled error becomes the 500 envelope instead of propagating.
        self._inner = httpx.ASGITransport(app=app, client=client, raise_app_exceptions=False)

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        body = request.read()
        return asyncio.run(self._call(request, body))

    async def _call(self, request: httpx.Request, body: bytes) -> httpx.Response:
        replay = httpx.Request(request.method, request.url, headers=request.headers, content=body)
        response = await self._inner.handle_async_request(replay)
        # Raw bytes: the new Response decodes them per its (copied) Content-Encoding header, once.
        content = b"".join([chunk async for chunk in response.aiter_raw()])
        await response.aclose()
        return httpx.Response(
            response.status_code, headers=response.headers, content=content, request=request
        )
