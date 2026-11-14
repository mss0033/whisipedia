"""The api.data.gov key travels as a request header, through the real stack a lane uses: the
collection adapter's ``env()`` (which wraps the fetcher in a ``Throttle``) over a real ``HttpFetcher``.

Trawl 2026-11-10, core-correctness-04: as ``?api_key=`` the key was printed by every error that named
the URL. The first design asked the env's fetcher for ``set_host_header``; in production that fetcher
is the Throttle, which had none, so the header would never have been used (review of batch 5b), and
tests that handed a fake straight to ``DiscoverEnv`` passed anyway. These go through ``env()``.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from wsp_adapters.doccollection import DocCollectionAdapter
from wsp_adapters.doccollection.discovery import DiscoverEnv, Throttle, discover_all
from wsp_core.model import SourceSpec, SourceStatus, Tier
from wsp_core.ports import Ctx
from wsp_fetch import FetchConfig, HttpFetcher

UA = "Whisipedia/0.1 (personal offline archive)"
CFG = FetchConfig(user_agent=UA, backoff_base=0.001, backoff_max=0.002, max_attempts=2)
KEY = "SECRETSECRETSECRETSECRET"
GOVINFO = [{"kind": "api", "flavor": "govinfo", "collection": "BILLS"}]
Seen = list[tuple[str, str | None]]


def _spec() -> SourceSpec:
    access: dict[str, Any] = {
        "adapter": "doc-collection",
        "issuer": "GPO",
        "key_prefix": "gpo",
        "origins": ["www.govinfo.gov"],
        "discovery": GOVINFO,
        "rate": {"requests_per_second": 100000},
        "filter": {"allow_all": True},
    }
    return SourceSpec(
        id="govinfo-test",
        name="govinfo-test",
        tier=Tier.T1,
        status=SourceStatus.CANDIDATE,
        license="public-domain",
        legal_basis="public-domain",
        domain=("test",),
        adapter="doc-collection",
        raw={"id": "govinfo-test", "access": access},
    )


def _page(ids: list[str], next_page: str | None = None, *, count: int = 0) -> httpx.Response:
    body: dict[str, Any] = {
        "count": count,
        "packages": [{"packageId": i, "title": i, "dateIssued": "2020-01-01"} for i in ids],
    }
    if next_page:
        body["nextPage"] = next_page
    return httpx.Response(200, stream=httpx.ByteStream(json.dumps(body).encode()))


def _world(
    api: Callable[[httpx.Request], httpx.Response], *, robots: int = 200
) -> tuple[httpx.MockTransport, Seen]:
    """Every request as (URL with its query, the X-Api-Key header it carried)."""
    seen: Seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((str(request.url), request.headers.get("X-Api-Key")))
        if request.url.path == "/robots.txt":
            return httpx.Response(robots, stream=httpx.ByteStream(b"User-agent: *\nAllow: /\n"))
        return api(request)

    return httpx.MockTransport(handler), seen


def _env(fetcher: HttpFetcher, tmp_path: Path) -> DiscoverEnv:
    ctx = Ctx(source=_spec(), fetcher=fetcher, workdir=tmp_path / "w", user_agent=UA)
    env = DocCollectionAdapter(keys={"govinfo": KEY}).env(ctx)
    assert isinstance(env.fetcher, Throttle)  # what a lane really gets
    return env


def test_govinfo_sends_the_key_as_a_header_and_no_url_holds_it(tmp_path: Path) -> None:
    def api(request: httpx.Request) -> httpx.Response:
        if request.headers.get("X-Api-Key") != KEY:
            return httpx.Response(403)
        if "offsetMark=%2A" in str(request.url):
            # GovInfo echoes the key in nextPage when it came in the query; never follow it as given
            return _page(
                ["BILLS-1", "BILLS-2"], f"https://api.govinfo.gov/next?offsetMark=AoE%2B%3D&api_key={KEY}"
            )
        return _page(["BILLS-3"])

    transport, seen = _world(api)
    with HttpFetcher(CFG, transport=transport) as f:
        env = _env(f, tmp_path)
        found = [c.pub_number for c in discover_all(GOVINFO, env)]
    assert found == ["BILLS-1", "BILLS-2", "BILLS-3"]
    assert env.notes == []
    assert env.incomplete == []
    assert all(KEY not in url and "api_key" not in url for url, _ in seen), seen
    listing = [(url, key) for url, key in seen if not url.endswith("/robots.txt")]
    assert [key for _, key in listing] == [KEY, KEY]
    assert listing[1][0] == "https://api.govinfo.gov/next?offsetMark=AoE%2B%3D"  # the page mark as given
    robots = [(url, key) for url, key in seen if url.endswith("/robots.txt")]
    # robots.txt is read twice (the fetcher's own check, then the Throttle's through get_bytes): the
    # key goes with neither.
    assert len(robots) == 2
    assert all(key is None for _, key in robots)


def test_govinfo_falls_back_to_the_query_once_when_the_header_is_refused(tmp_path: Path) -> None:
    def api(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("api_key") != KEY:
            return httpx.Response(403)  # api.data.gov: API_KEY_MISSING
        if "offsetMark=%2A" in str(request.url):
            return _page(["BILLS-1"], "https://api.govinfo.gov/next?offsetMark=two")
        return _page(["BILLS-2"])

    transport, seen = _world(api)
    with HttpFetcher(CFG, transport=transport) as f:
        env = _env(f, tmp_path)
        found = [c.pub_number for c in discover_all(GOVINFO, env)]
    assert found == ["BILLS-1", "BILLS-2"]
    assert env.notes == ["api.govinfo.gov: X-Api-Key refused (HTTP 403); key sent as a query parameter"]
    assert env.incomplete == []
    listing = [(url, key) for url, key in seen if not url.endswith("/robots.txt")]
    assert len(listing) == 3  # one refused try, then each page once
    assert listing[0][1] == KEY
    assert "api_key" not in listing[0][0]
    for url, key in listing[1:]:
        assert f"api_key={KEY}" in url
        assert key is None  # the refused header is not sent again beside the query


@pytest.mark.parametrize("status", [401, 403])
def test_a_refusal_after_the_header_was_accepted_is_a_failure_not_a_fallback(
    tmp_path: Path, status: int
) -> None:
    """2026-11-13 review: a 403 on a later page moved the key into the URL and said the header had
    been refused, which the first page had just disproved."""

    def api(request: httpx.Request) -> httpx.Response:
        if "offsetMark=%2A" in str(request.url):
            return _page(["BILLS-1"], "https://api.govinfo.gov/next?offsetMark=two")
        return httpx.Response(status)

    transport, seen = _world(api)
    with HttpFetcher(CFG, transport=transport) as f:
        env = _env(f, tmp_path)
        found = [c.pub_number for c in discover_all(GOVINFO, env)]
    assert found == ["BILLS-1"]
    assert all("api_key" not in url for url, _ in seen), seen
    assert not any("X-Api-Key refused" in note for note in env.notes)
    (line,) = env.incomplete
    assert f"HTTP {status}" in line
    assert KEY not in repr(env)


def test_a_failed_listing_never_writes_the_key_in_header_or_query_mode(tmp_path: Path) -> None:
    """What a lane log gets: the notes and the incomplete lines of a failed listing."""
    for refuse_header in (False, True):

        def api(request: httpx.Request, refuse_header: bool = refuse_header) -> httpx.Response:
            if refuse_header and request.headers.get("X-Api-Key"):
                return httpx.Response(403)
            return httpx.Response(500)

        transport, _ = _world(api)
        with HttpFetcher(CFG, transport=transport) as f:
            env = _env(f, tmp_path)
            assert list(discover_all(GOVINFO, env)) == []
        said = "\n".join([*env.notes, *env.incomplete])
        assert "HTTP 500" in said
        assert KEY not in said
        assert ("api_key=***" in said) is refuse_header  # header mode: no api_key in the URL at all


def test_an_unreachable_robots_txt_refuses_the_listing_without_the_key(tmp_path: Path) -> None:
    """The live case since 2026-10-28: api.govinfo.gov/robots.txt answers 500."""
    transport, seen = _world(lambda request: _page(["BILLS-1"]), robots=500)
    with HttpFetcher(CFG, transport=transport) as f:
        env = _env(f, tmp_path)
        assert list(discover_all(GOVINFO, env)) == []
    assert seen  # robots.txt was asked for, and nothing else
    assert {url for url, _ in seen} == {"https://api.govinfo.gov/robots.txt"}
    assert all(key is None for _, key in seen)
    (line,) = env.incomplete
    assert "RobotsUnreachableError" in line
    assert "disallowed by api.govinfo.gov/robots.txt" in line
    assert KEY not in line
    assert "api_key" not in line


def test_a_next_page_on_another_host_is_refused_and_gets_no_key(tmp_path: Path) -> None:
    def api(request: httpx.Request) -> httpx.Response:
        if request.url.host != "api.govinfo.gov":
            return _page(["EVIL-1"])
        return _page(["BILLS-1"], "https://api.elsewhere.test/next?offsetMark=two")

    transport, seen = _world(api)
    with HttpFetcher(CFG, transport=transport) as f:
        env = _env(f, tmp_path)
        found = [c.pub_number for c in discover_all(GOVINFO, env)]
    assert found == ["BILLS-1"]
    assert not any("elsewhere" in url for url, _ in seen)
    (line,) = env.incomplete
    assert "not a host the API key 'govinfo' is sent to" in line


def test_congress_sends_the_key_as_a_header(tmp_path: Path) -> None:
    def api(request: httpx.Request) -> httpx.Response:
        if request.headers.get("X-Api-Key") != KEY:
            return httpx.Response(403)
        body = {
            "pagination": {"count": 1},
            "CRSReports": [{"id": "R1", "title": "t", "publishDate": "2020-01-01"}],
        }
        return httpx.Response(200, stream=httpx.ByteStream(json.dumps(body).encode()))

    transport, seen = _world(api)
    with HttpFetcher(CFG, transport=transport) as f:
        ctx = Ctx(source=_spec(), fetcher=f, workdir=tmp_path / "w", user_agent=UA)
        env = DocCollectionAdapter(keys={"congress": KEY}).env(ctx)
        found = [c.pub_number for c in discover_all([{"kind": "api", "flavor": "congress-crs"}], env)]
    assert found == ["R1"]
    assert all("api_key" not in url for url, _ in seen)
    assert [key for url, key in seen if "crsreport" in url] == [KEY]


@pytest.mark.parametrize("url", ["http://api.govinfo.gov/x", "https://api.govinfo.gov:8443/x"])
def test_the_key_is_not_given_to_plain_http_or_another_port(tmp_path: Path, url: str) -> None:
    transport, seen = _world(lambda request: _page([]))
    with HttpFetcher(CFG, transport=transport) as f:
        env = _env(f, tmp_path)
        with pytest.raises(Exception, match="not a host the API key"):
            env.keyed_json(url, "govinfo")
    assert seen == []
