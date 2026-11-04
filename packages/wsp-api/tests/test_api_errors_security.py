from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from wsp_api import ApiSettings, create_app
from wsp_api.testing import local_client
from wsp_api.testing.fakes import sample_services

CSP = "default-src 'self'"


@pytest.mark.parametrize(
    ("path", "status", "code"),
    [
        ("/v1/sources/nope", 404, "source_not_found"),
        ("/v1/sources/nope/generations", 404, "source_not_found"),
        ("/v1/documents/wsp%3Arfc%2F0", 404, "document_not_found"),
        ("/v1/documents/wsp%3Arfc%2F0/history", 404, "document_not_found"),
        ("/v1/documents/wsp:rfc/9110", 404, "doc_id_not_encoded"),
        ("/v1/documents/rfc-9110", 400, "invalid_doc_id"),
        ("/v1/sources?cursor=garbage", 400, "invalid_cursor"),
        ("/v1/sources?cursor=eyJvIjogLTF9", 400, "invalid_cursor"),
        ("/v1/changes?since=999", 400, "invalid_cursor"),
        ("/v1/search", 422, "invalid_request"),
        ("/v1/search?q=x&limit=0", 422, "invalid_request"),
        ("/v1/nothing-here", 404, "not_found"),
    ],
)
def test_error_envelope(client: httpx.Client, path: str, status: int, code: str) -> None:
    r = client.get(path)
    assert r.status_code == status
    body = r.json()
    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message"}
    assert body["error"]["code"] == code
    assert r.headers["content-security-policy"].startswith(CSP)


def test_method_not_allowed_uses_envelope(client: httpx.Client) -> None:
    r = client.delete("/v1/health")
    assert r.status_code == 405
    assert r.json()["error"]["code"] == "method_not_allowed"


def test_unhandled_exception_is_500_envelope_without_internals() -> None:
    services = sample_services()

    def boom(*_: object, **__: object) -> None:
        raise RuntimeError("/secret/path/on/disk")

    services.index.suggest = boom  # type: ignore[method-assign]
    with local_client(create_app(services)) as c:
        r = c.get("/v1/suggest", params={"q": "a"})
    assert r.status_code == 500
    assert r.json() == {"error": {"code": "internal_error", "message": "RuntimeError"}}


@pytest.mark.parametrize("path", ["/v1/health", "/", "/go/wsp://home", "/v1/openapi.json"])
def test_security_headers_everywhere(client: httpx.Client, path: str) -> None:
    r = client.get(path)
    assert r.status_code == 200
    assert r.headers["content-security-policy"].startswith(CSP)
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["referrer-policy"] == "no-referrer"


def test_no_cors_even_for_cross_origin_preflight(client: httpx.Client) -> None:
    r = client.options(
        "/v1/health",
        headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "GET"},
    )
    assert not any(h.lower().startswith("access-control-") for h in r.headers)
    r = client.get("/v1/health", headers={"Origin": "https://evil.example"})
    assert not any(h.lower().startswith("access-control-") for h in r.headers)


@pytest.mark.parametrize("host", ["evil.example", "evil.example:8737", "192.0.2.10"])
def test_foreign_host_header_rejected(client: httpx.Client, host: str) -> None:
    r = client.get("/v1/health", headers={"Host": host})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_host"
    assert r.headers["content-security-policy"].startswith(CSP)


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost:8737", "[::1]:8737"])
def test_loopback_host_headers_allowed(client: httpx.Client, host: str) -> None:
    assert client.get("/v1/health", headers={"Host": host}).status_code == 200


@pytest.mark.parametrize(
    "headers",
    [
        {"Sec-Fetch-Site": "cross-site", "Origin": "https://evil.example"},
        {"Sec-Fetch-Site": "same-site", "Origin": "http://127.0.0.1:5000"},  # another local service
        {"Origin": "https://evil.example"},  # a browser without Fetch Metadata
        {"Origin": "http://127.0.0.1:5000"},
        {"Origin": "null"},  # a sandboxed frame, a file: page
    ],
)
def test_a_write_from_another_site_is_refused(client: httpx.Client, headers: dict[str, str]) -> None:
    """2026-11-04 trawl: any page the owner opened could POST to the API (a no-cors POST with no body
    needs no preflight) and start a weather pull; the answer was unreadable but the pull ran."""
    r = client.post("/v1/links/missing", json={"addresses": []}, headers=headers)
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "cross_site"
    assert r.headers["content-security-policy"].startswith(CSP)
    assert client.get("/v1/health", headers=headers).status_code == 200  # reads are not writes


@pytest.mark.parametrize(
    "headers",
    [
        {},  # curl, the tools/ scripts
        {"Sec-Fetch-Site": "same-origin", "Origin": "http://127.0.0.1:8737"},  # the Navigator
        {"Origin": "http://127.0.0.1:8737"},
        {"Sec-Fetch-Site": "none"},  # typed or bookmarked by the owner
        {"Sec-Fetch-Site": "same-origin", "Origin": "http://127.0.0.1:5173", "Host": "127.0.0.1:5173"},
    ],
)
def test_the_navigator_and_local_tools_still_write(client: httpx.Client, headers: dict[str, str]) -> None:
    """The last case is the Navigator's dev server, whose proxy passes Host on (changeOrigin: false)."""
    assert client.post("/v1/links/missing", json={"addresses": []}, headers=headers).status_code == 200


def test_no_swagger_ui_since_it_needs_a_cdn(client: httpx.Client) -> None:
    assert client.get("/docs").status_code == 404
    assert client.get("/redoc").status_code == 404


def test_placeholder_page_without_bundle(client: httpx.Client) -> None:
    r = client.get("/")
    assert r.status_code == 200
    assert "Navigator bundle is not built" in r.text


def test_static_bundle_is_mounted_at_root(tmp_path: Path) -> None:
    (tmp_path / "index.html").write_text("<!doctype html><title>Navigator</title>")
    (tmp_path / "app.js").write_text("console.log('hi')")
    app: FastAPI = create_app(sample_services(), ApiSettings(static_dir=tmp_path))
    with local_client(app) as c:
        index = c.get("/")
        assert index.status_code == 200
        assert "Navigator" in index.text
        js = c.get("/app.js")
        assert js.status_code == 200
        assert js.headers["content-security-policy"].startswith(CSP)
        assert c.get("/v1/health").status_code == 200  # API routes win over the mount
        missing = c.get("/nope.js")
        assert missing.status_code == 404
        assert missing.json()["error"]["code"] == "not_found"


def test_inline_images_are_allowed_and_nothing_else_outside(client: httpx.Client) -> None:
    """The Atlas's map controls draw their icons as data: SVGs; the header blocked them while the page's
    own meta policy allowed them (browsers enforce both), so the buttons were blank (2026-10-18)."""
    csp = client.get("/v1/health").headers["content-security-policy"]
    directives = dict(d.strip().split(" ", 1) for d in csp.split(";") if d.strip())
    assert directives["img-src"].split() == ["'self'", "data:"]
    assert directives["default-src"] == "'self'"
