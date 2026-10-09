"""Reusable test doubles for the read-side ports (:mod:`wsp_api.testing.fakes`) and client helpers."""

from __future__ import annotations

import httpx
from starlette.types import ASGIApp

from wsp_api.inprocess import InProcessTransport

LOCAL_BASE_URL = "http://127.0.0.1:8737"


def local_client(app: ASGIApp) -> httpx.Client:
    """A sync ``httpx.Client`` that talks to ``app`` in-process (no sockets), with a loopback Host."""
    return httpx.Client(transport=InProcessTransport(app), base_url=LOCAL_BASE_URL)


def local_async_client(app: ASGIApp) -> httpx.AsyncClient:
    """An async ``httpx.AsyncClient`` that talks to ``app`` in-process (no sockets)."""
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 0))
    return httpx.AsyncClient(transport=transport, base_url=LOCAL_BASE_URL)
