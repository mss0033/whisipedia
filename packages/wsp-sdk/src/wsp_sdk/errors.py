"""Exceptions raised by the SDK."""

from __future__ import annotations

import httpx
from pydantic import ValidationError

from wsp_sdk.models import ErrorResponse


class WspError(Exception):
    """Base class for SDK errors."""


class WspApiError(WspError):
    """The server answered with the error envelope. ``code`` is stable; ``message`` is for humans."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(f"{status} {code}: {message}")
        self.status = status
        self.code = code
        self.message = message


class NotFoundError(WspApiError):
    pass


class NotConfiguredError(WspApiError):
    """The feature needs a component that is not configured (e.g. no local LLM for summaries)."""


def raise_for_error(response: httpx.Response, body: bytes | None = None) -> None:
    if response.status_code < 400:
        return
    raw = response.content if body is None else body
    try:
        err = ErrorResponse.model_validate_json(raw).error
        code, message = err.code, err.message
    except ValidationError:
        code, message = "http_error", raw.decode("utf-8", "replace")[:500]
    cls = {404: NotFoundError, 501: NotConfiguredError}.get(response.status_code, WspApiError)
    raise cls(response.status_code, code, message)
