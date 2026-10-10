"""One error envelope for every failure: ``{"error": {"code": ..., "message": ...}}``."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

from wsp_api.schemas import ErrorBody, ErrorResponse

_STATUS_CODES = {
    400: "bad_request",
    404: "not_found",
    405: "method_not_allowed",
    406: "not_acceptable",
    415: "unsupported_media_type",
    422: "invalid_request",
    500: "internal_error",
    501: "not_implemented",
}


class ApiError(Exception):
    """Raise from a route to answer with the error envelope."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def error_response(status: int, code: str, message: str) -> JSONResponse:
    body = ErrorResponse(error=ErrorBody(code=code, message=message))
    return JSONResponse(body.model_dump(), status_code=status)


async def _api_error(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, ApiError)
    return error_response(exc.status, exc.code, exc.message)


async def _http_error(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StarletteHTTPException)
    return error_response(exc.status_code, _STATUS_CODES.get(exc.status_code, "error"), str(exc.detail))


async def _validation_error(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)
    parts = [f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()]
    return error_response(422, "invalid_request", "; ".join(parts) or "invalid request")


async def _unhandled(_: Request, exc: Exception) -> JSONResponse:
    # Never echo internals (paths, hostnames) to the client; the log has the traceback.
    return error_response(500, "internal_error", type(exc).__name__)


def install_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(ApiError, _api_error)
    app.add_exception_handler(StarletteHTTPException, _http_error)
    app.add_exception_handler(RequestValidationError, _validation_error)
    app.add_exception_handler(Exception, _unhandled)


def errors(*statuses: int) -> dict[int | str, dict[str, object]]:
    """OpenAPI ``responses=`` entry documenting the envelope for the given statuses."""
    return {s: {"model": ErrorResponse, "description": _STATUS_CODES.get(s, "error")} for s in statuses}


def streamed(model: type[BaseModel], media_type: str, description: str) -> dict[int | str, dict[str, object]]:
    """OpenAPI ``responses=`` entry for a 200 stream whose items are ``model`` (see ``WspApp.openapi``)."""
    return {200: {"model": model, "content": {media_type: {}}, "description": description}}
