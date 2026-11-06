"""Offline hardening as a pure ASGI middleware (PLAN §10.1, §10.5).

* Strict CSP ``default-src 'self'`` and friends on *every* response, including errors and static files,
  so any outbound request from the Navigator fails visibly in the browser.
* Host-header allow-list: a page on another origin cannot reach the API through DNS rebinding.
* No CORS: no ``Access-Control-Allow-*`` headers are ever sent, so other origins cannot read responses.
* No cross-site writes: a POST, PUT, PATCH or DELETE a browser sends for another page (its
  ``Sec-Fetch-Site`` is not ``same-origin``, or its ``Origin`` is not this one) is refused. Another
  origin cannot read the answer, but a body-less ``no-cors`` POST still ran: any page the owner opened
  could start a weather pull (2026-11-04 trawl). Requests with neither header (curl, ``tools/``) pass.

Pure ASGI (not ``BaseHTTPMiddleware``) so streaming responses (SSE, NDJSON) are not buffered.
"""

from __future__ import annotations

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from wsp_api.errors import error_response
from wsp_api.originals import files_csp
from wsp_api.services import ApiSettings

SECURITY_HEADERS: tuple[tuple[bytes, bytes], ...] = (
    (
        b"content-security-policy",
        # media-src blob:: the Screening Room's projection test plays a reel made in the page.
        # img-src data:: the Atlas's map controls draw their icons as inline SVG (MapLibre's stylesheet);
        # blocked, the zoom, compass and fullscreen buttons were blank (2026-10-18). A data: image is in
        # the page itself: it fetches nothing and, shown as an image, runs nothing. Scripts stay 'self'.
        b"default-src 'self'; img-src 'self' data:; media-src 'self' blob:; frame-ancestors 'none'; "
        b"base-uri 'self'; "
        b"form-action 'self'",
    ),
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"no-referrer"),
    (b"cross-origin-opener-policy", b"same-origin"),
    (b"cross-origin-resource-policy", b"same-origin"),
)

# Asset bytes come from upstream containers. A second, stricter policy (browsers enforce both) keeps
# an SVG opened directly from running anything at our origin.
ASSET_CSP: tuple[bytes, bytes] = (
    b"content-security-policy",
    b"default-src 'none'; style-src 'unsafe-inline'; sandbox",
)
ASSET_PREFIX = "/v1/assets/"
# A PDF from a container is shown in the Navigator's frame, as a stored original's PDF is
# (originals.files_csp): Chrome's PDF viewer draws nothing in a sandboxed document, so a PDF gets no
# sandbox, only no subresources and no framing but by our own pages (2026-11-05 deep dive).
ASSET_PDF_CSP = b"default-src 'none'; frame-ancestors 'self'"


def asset_csp(path: str, content_type: bytes) -> bytes | None:
    """The policy for a ``/v1/assets/`` PDF, which replaces the page policy and :data:`ASSET_CSP`;
    ``None`` for any other response."""
    if path.startswith(ASSET_PREFIX) and content_type.split(b";")[0].strip().lower() == b"application/pdf":
        return ASSET_PDF_CSP
    return None


SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def _header(scope: Scope, wanted: bytes) -> str | None:
    for name, value in scope.get("headers", []):
        if name == wanted:
            return value.decode("latin-1")
    return None


def _cross_site(scope: Scope) -> bool:
    """A state-changing request a browser sent from another origin (another site, or another port
    of this one: a different local service)."""
    if str(scope.get("method", "GET")).upper() in SAFE_METHODS:
        return False
    fetch_site = _header(scope, b"sec-fetch-site")
    if fetch_site is not None and fetch_site.strip().lower() not in ("same-origin", "none"):
        return True
    origin = _header(scope, b"origin")
    if origin is None:
        return False
    scheme, sep, netloc = origin.strip().lower().partition("://")
    host = (_header(scope, b"host") or "").strip().lower()
    return not sep or scheme not in ("http", "https") or not host or netloc != host


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

        path = str(scope.get("path", ""))

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                drop = {k for k, _ in SECURITY_HEADERS}
                headers = [(k, v) for k, v in message.get("headers", []) if k.lower() not in drop]
                base = SECURITY_HEADERS
                ctype = next((v for k, v in headers if k.lower() == b"content-type"), b"")
                pdf = asset_csp(path, ctype)
                extra = (ASSET_CSP,) if path.startswith(ASSET_PREFIX) and pdf is None else ()
                own = files_csp(path, ctype) or pdf
                if own is not None:  # a stored original or an archive's PDF: its policy replaces the page's
                    base = tuple(
                        (k, own if k == b"content-security-policy" else v) for k, v in SECURITY_HEADERS
                    )
                message["headers"] = [*headers, *base, *extra]
            await send(message)

        if _host_of(scope) not in self.settings.allowed_hosts:
            response = error_response(400, "invalid_host", "Host header is not an allowed local name")
            await response(scope, receive, send_with_headers)
            return
        if _cross_site(scope):
            response = error_response(
                403, "cross_site", "a request from another site cannot change anything here"
            )
            await response(scope, receive, send_with_headers)
            return
        await self.app(scope, receive, send_with_headers)
