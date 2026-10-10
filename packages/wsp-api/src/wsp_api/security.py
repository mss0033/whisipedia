"""Offline hardening as a pure ASGI middleware (PLAN §10.1, §10.5).

* Strict CSP ``default-src 'self'`` and friends on *every* response, including errors and static files,
  so any outbound request from the Navigator fails visibly in the browser.
* Host-header allow-list: a page on another origin cannot reach the API through DNS rebinding.
* No CORS: no ``Access-Control-Allow-*`` headers are ever sent, so other origins cannot read responses.

Pure ASGI (not ``BaseHTTPMiddleware``) so streaming responses (SSE, NDJSON) are not buffered.
"""

from __future__ import annotations

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from wsp_api.errors import error_response
from wsp_api.services import ApiSettings

SECURITY_HEADERS: tuple[tuple[bytes, bytes], ...] = (
    (
        b"content-security-policy",
        b"default-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'",
    ),
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"no-referrer"),
    (b"cross-origin-opener-policy", b"same-origin"),
    (b"cross-origin-resource-policy", b"same-origin"),
)


def _host_of(scope: Scope) -> str:
    for name, value in scope.get("headers", []):
        if name == b"host":
            host = value.decode("latin-1")
            if host.startswith("["):  # [::1]:8737
                return host[1 : host.find("]")]
            return host.rsplit(":", 1)[0] if host.count(":") == 1 else host
    return ""


class SecurityMiddleware:
    def __init__(self, app: ASGIApp, settings: ApiSettings) -> None:
        self.app = app
        self.settings = settings

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                drop = {k for k, _ in SECURITY_HEADERS}
                headers = [(k, v) for k, v in message.get("headers", []) if k.lower() not in drop]
                message["headers"] = [*headers, *SECURITY_HEADERS]
            await send(message)

        if _host_of(scope) not in self.settings.allowed_hosts:
            response = error_response(400, "invalid_host", "Host header is not an allowed local name")
            await response(scope, receive, send_with_headers)
            return
        await self.app(scope, receive, send_with_headers)
