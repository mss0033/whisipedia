"""Whisipedia HTTP API: ``/v1`` read API, change feed (SSE), ``/go`` address resolver, Navigator host.

The public names are imported on first use: ZIM worker processes (``wsp_api.zimworker``) and CLI
commands that talk to a remote server must not pay for FastAPI and the whole app at import time.
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from wsp_api.app import create_app
    from wsp_api.schemas import API_VERSION
    from wsp_api.services import ApiSettings, Services

__version__ = "0.1.0"
__all__ = ["API_VERSION", "ApiSettings", "Services", "create_app"]

_HOME = {
    "create_app": "wsp_api.app",
    "API_VERSION": "wsp_api.schemas",
    "ApiSettings": "wsp_api.services",
    "Services": "wsp_api.services",
}


def __getattr__(name: str) -> object:
    module = _HOME.get(name)
    if module is None:
        raise AttributeError(f"module 'wsp_api' has no attribute {name!r}")
    value = getattr(import_module(module), name)
    globals()[name] = value
    return value
