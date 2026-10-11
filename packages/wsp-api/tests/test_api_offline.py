"""Offline guarantee (PLAN §10.5, §12.4): exercising every endpoint opens no non-loopback socket.

``socket.socket.connect`` (and ``connect_ex``/``create_connection``/``getaddrinfo``) are wrapped for the
whole test. Any attempt to reach a non-loopback address fails the test, even if the caller swallows it.
"""

import ipaddress
import socket
from typing import Any

import pytest

from wsp_api import create_app
from wsp_api.addresses import document_href
from wsp_api.testing import local_client
from wsp_api.testing.browse import browse_services


def _is_loopback(address: object) -> bool:
    if isinstance(address, str):  # AF_UNIX path
        return True
    if isinstance(address, tuple) and address:
        host = str(address[0])  # pyright: ignore[reportUnknownArgumentType]
        if host == "localhost":
            return True
        try:
            return ipaddress.ip_address(host.split("%")[0]).is_loopback
        except ValueError:
            return False
    return False


@pytest.fixture
def outbound(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    attempts: list[object] = []
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_getaddrinfo = socket.getaddrinfo

    def connect(self: socket.socket, address: Any) -> None:
        if not _is_loopback(address):
            attempts.append(address)
            raise OSError("network disabled in offline test")
        real_connect(self, address)

    def connect_ex(self: socket.socket, address: Any) -> int:
        if not _is_loopback(address):
            attempts.append(address)
            return 101  # ENETUNREACH
        return real_connect_ex(self, address)

    def getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
        if host not in (None, "localhost") and not _is_loopback((host,)):
            attempts.append(("getaddrinfo", host))
            raise socket.gaierror("name resolution disabled in offline test")
        return real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    return attempts


def test_guard_catches_outbound(outbound: list[object]) -> None:
    with (
        socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s,
        pytest.raises(OSError, match="network disabled"),
    ):
        s.connect(("192.0.2.1", 80))  # TEST-NET-1, never routable
    assert outbound == [("192.0.2.1", 80)]
    outbound.clear()


def test_full_api_surface_makes_no_outbound_connections(outbound: list[object]) -> None:
    app = create_app(browse_services())
    turing = "wsp:wikipedia-en-zim/A/Alan_Turing"
    with local_client(app) as c:
        for path in [
            "/",
            "/v1/health",
            "/v1/openapi.json",
            "/v1/sources",
            "/v1/sources/rfc",
            "/v1/sources/rfc/generations",
            document_href(turing),
            document_href(turing) + "/history",
            document_href("wsp:wikipedia-en-zim/A/Enigma_machine"),
            "/v1/assets/wikipedia-en-zim/2026-10-01/I/enigma.png",
            "/v1/portal",
            "/v1/directory",
            "/v1/random",
            "/v1/changes/recent",
            "/v1/search?q=computer",
            "/v1/suggest?q=al",
            "/v1/changes",
            "/v1/changes/stream?max_seconds=0.05",
            "/v1/system",
            "/go/wsp://rfc/9110",
            "/go/https://example.org/",
            "/v1/does-not-exist",
        ]:
            assert c.get(path).status_code < 500, path
        assert c.post("/v1/gaps", json={"href": "https://example.org/"}).status_code == 204
    assert outbound == []
