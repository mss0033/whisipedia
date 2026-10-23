"""A collection's downloads never follow a redirect to a host it may not use (2026-10-23: BODC's GEBCO
link redirected to CEDA, whose robots.txt disallows every agent, and 7.5 GB came from there)."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from wsp_adapters.doccollection.adapter import download_from_origins
from wsp_adapters.doccollection.discovery import Throttle
from wsp_core.errors import FetchError
from wsp_core.model import RemoteFile
from wsp_fetch import FetchConfig, HttpFetcher

UA = "Whisipedia/0.1 (personal offline archive)"
CFG = FetchConfig(user_agent=UA, backoff_base=0.01, backoff_max=0.05, max_attempts=2)
DATA = b"grid " * 1000


def _world(target: str) -> tuple[httpx.MockTransport, list[str]]:
    reached: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        reached.append(f"{request.url.host}{request.url.path}")
        if request.url.path == "/robots.txt":
            rules = "User-agent: *\nDisallow: /\n" if request.url.host == "dap.ceda.example" else ""
            return httpx.Response(200, stream=httpx.ByteStream(rules.encode()))
        if request.url.host == "www.bodc.example":
            return httpx.Response(302, headers={"Location": f"https://{target}/gebco.nc"})
        return httpx.Response(200, stream=httpx.ByteStream(DATA), headers={"Content-Length": str(len(DATA))})

    return httpx.MockTransport(handler), reached


def _throttle(fetcher: HttpFetcher) -> Throttle:
    return Throttle(fetcher, interval=0, sleep=lambda _s: None, robots=UA)


def test_a_redirect_to_a_host_whose_robots_txt_disallows_us_downloads_nothing(tmp_path: Path) -> None:
    transport, reached = _world("dap.ceda.example")
    remote = RemoteFile("https://www.bodc.example/gebco/zip/", "gebco.zip")
    origins = ("www.bodc.example", "dap.ceda.example")  # even an allowed origin: robots.txt decides
    with (
        HttpFetcher(CFG, transport=transport) as f,
        pytest.raises(
            FetchError,
            match=r"dap\.ceda\.example.*disallowed by|disallowed by dap\.ceda\.example/robots\.txt",
        ),
    ):
        download_from_origins(_throttle(f), remote, tmp_path / "gebco.zip", origins)
    assert "dap.ceda.example/gebco.nc" not in reached
    assert "dap.ceda.example/robots.txt" in reached
    assert not list(tmp_path.iterdir())


def test_a_redirect_off_the_origins_downloads_nothing(tmp_path: Path) -> None:
    transport, reached = _world("cdn.elsewhere.example")
    remote = RemoteFile("https://www.bodc.example/gebco/zip/", "gebco.zip")
    with (
        HttpFetcher(CFG, transport=transport) as f,
        pytest.raises(FetchError, match="not an allowed HTTPS origin"),
    ):
        download_from_origins(_throttle(f), remote, tmp_path / "gebco.zip", ("www.bodc.example",))
    assert "cdn.elsewhere.example/gebco.nc" not in reached
    assert not list(tmp_path.iterdir())


def test_a_redirect_within_the_origins_to_a_host_that_allows_us_downloads(tmp_path: Path) -> None:
    transport, _ = _world("files.bodc.example")
    remote = RemoteFile("https://www.bodc.example/gebco/zip/", "gebco.zip")
    with HttpFetcher(replace(CFG, unverified_policy="allow"), transport=transport) as f:
        result = download_from_origins(_throttle(f), remote, tmp_path / "gebco.zip", ("bodc.example",))
    assert Path(result.path).read_bytes() == DATA
