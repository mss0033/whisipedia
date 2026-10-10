"""Typed Python client for the Whisipedia ``/v1`` API (PLAN §11.1, §12.2)."""

from wsp_sdk.client import DEFAULT_BASE_URL, AsyncWspClient, WspClient
from wsp_sdk.errors import NotConfiguredError, NotFoundError, WspApiError, WspError

__version__ = "0.1.0"
__all__ = [
    "DEFAULT_BASE_URL",
    "AsyncWspClient",
    "NotConfiguredError",
    "NotFoundError",
    "WspApiError",
    "WspClient",
    "WspError",
]
