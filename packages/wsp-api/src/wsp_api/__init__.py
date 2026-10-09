"""Whisipedia HTTP API: ``/v1`` read API, change feed (SSE), ``/go`` address resolver, Navigator host."""

from wsp_api.app import create_app
from wsp_api.schemas import API_VERSION
from wsp_api.services import ApiSettings, Services

__version__ = "0.1.0"
__all__ = ["API_VERSION", "ApiSettings", "Services", "create_app"]
