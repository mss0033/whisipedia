import httpx
import pytest

from wsp_api.schemas import Resolution


@pytest.mark.parametrize(
    ("address", "kind", "canonical", "href"),
    [
        ("wsp://home", "home", "wsp://home", "/v1/portal"),
        ("home", "home", "wsp://home", "/v1/portal"),
        ("wsp://system", "system", "wsp://system", "/v1/system"),
        ("wsp://whatsnew", "whatsnew", "wsp://whatsnew", "/v1/changes/recent"),
        ("wsp://directory", "directory", "wsp://directory", "/v1/directory"),
        ("wsp://directory/books", "directory", "wsp://directory/books", "/v1/directory?path=books"),
        ("wsp://random", "random", "wsp://random", "/v1/random"),
        ("wsp://hotlist", "client", "wsp://hotlist", None),
        ("wsp://search", "search", "wsp://search", None),
        ("wsp://rfc", "source", "wsp://rfc", "/v1/sources/rfc"),
        ("wsp://rfc/", "source", "wsp://rfc", "/v1/sources/rfc"),
        ("wsp://rfc/9110", "document", "wsp://rfc/9110", "/v1/documents/wsp%3Arfc%2F9110"),
        (
            "wsp://wikipedia-en-zim/A/Alan_Turing",
            "document",
            "wsp://wikipedia-en-zim/A/Alan_Turing",
            "/v1/documents/wsp%3Awikipedia-en-zim%2FA%2FAlan_Turing",
        ),
        ("wsp:rfc/9110", "document", "wsp://rfc/9110", "/v1/documents/wsp%3Arfc%2F9110"),
    ],
)
def test_resolve(client: httpx.Client, address: str, kind: str, canonical: str, href: str | None) -> None:
    r = client.get(f"/go/{address}")
    assert r.status_code == 200, r.text
    res = Resolution.model_validate(r.json())
    assert (res.kind, res.address, res.href) == (kind, canonical, href)


def test_resolve_percent_encoded_address(client: httpx.Client) -> None:
    res = Resolution.model_validate(client.get("/go/wsp%3A%2F%2Frfc%2F9110").json())
    assert res.href == "/v1/documents/wsp%3Arfc%2F9110"


def test_search_address_carries_query(client: httpx.Client) -> None:
    res = Resolution.model_validate(client.get("/go/wsp://search", params={"q": "http caching"}).json())
    assert res.address == "wsp://search?q=http+caching"
    assert res.href == "/v1/search?q=http+caching"


def test_redirect_mode(client: httpx.Client) -> None:
    r = client.get("/go/wsp://rfc/9110", params={"redirect": "true"}, follow_redirects=False)
    assert r.status_code == 307
    assert r.headers["location"] == "/v1/documents/wsp%3Arfc%2F9110"
    followed = client.get("/go/wsp://rfc/9110", params={"redirect": "true"}, follow_redirects=True)
    assert followed.json()["title"] == "RFC 9110: HTTP Semantics"


def test_redirect_mode_for_client_side_page_returns_json(client: httpx.Client) -> None:
    r = client.get("/go/wsp://hotlist", params={"redirect": "true"}, follow_redirects=False)
    assert r.status_code == 200
    assert r.json()["href"] is None


@pytest.mark.parametrize(
    ("address", "status", "code"),
    [
        ("wsp://nowhere", 404, "address_not_found"),
        ("wsp://nowhere/x", 404, "address_not_found"),
        ("wsp://rfc/0000", 404, "not_in_library"),
        ("https://example.org/", 400, "external_address"),
    ],
)
def test_unresolvable(client: httpx.Client, address: str, status: int, code: str) -> None:
    r = client.get(f"/go/{address}")
    assert r.status_code == status
    assert r.json()["error"]["code"] == code


def test_an_rfc_cited_by_number_resolves_to_its_key() -> None:
    """RFCs are keyed rfc:RFC-3171; the document route took wsp://rfc/3171, /go did not (2026-10-18
    audit: a field guide's link to RFC 3171 was dead for every check that resolves through /go)."""
    from types import SimpleNamespace
    from typing import Any, cast

    from wsp_api.addresses import resolve

    held = {"wsp:rfc/rfc:RFC-3171"}

    def source(sid: str) -> object | None:
        return object() if sid == "rfc" else None

    services = cast(Any, SimpleNamespace(source=source))
    res = resolve(services, "wsp://rfc/3171", {}, exists=lambda d: d in held)
    assert (res.kind, res.address) == ("document", "wsp://rfc/rfc:RFC-3171")
    assert res.href == "/v1/documents/wsp%3Arfc%2Frfc%3ARFC-3171"


def test_a_pages_links_that_lead_nowhere_are_named_at_once(client: httpx.Client) -> None:
    """2026-10-19 audit: links to pages a site's own archive never captured opened Not Found; the
    Navigator now asks first and marks them."""
    asked = [
        "wsp://rfc/9110",
        "wsp://rfc/9110",  # asked twice, answered once
        "wsp://rfc/999999",
        "wsp://wikipedia-en-zim/A/Alan_Turing",
        "wsp://no-such-site/page",
        "wsp://home",
        "wsp://wikipedia-en-zim/A/Nowhere_at_all",
    ]
    r = client.post("/v1/links/missing", json={"addresses": asked})
    assert r.status_code == 200, r.text
    assert r.json() == {
        "missing": ["wsp://rfc/999999", "wsp://no-such-site/page", "wsp://wikipedia-en-zim/A/Nowhere_at_all"],
        "unchecked": [],
    }
    assert client.post("/v1/links/missing", json={"addresses": ["wsp://rfc/1"] * 501}).status_code == 422


def test_the_link_check_answers_in_time_without_the_links_it_could_not_check(
    client: httpx.Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One archive slow to answer (a cold open on a busy NAS) must not hold up the page's other links."""
    import time

    from wsp_api import browse
    from wsp_api.errors import ApiError

    def fake_resolve(services: object, address: str, query: object, **_: object) -> None:
        if address.endswith("/slow"):
            time.sleep(2)
        if "gone" in address:
            raise ApiError(404, "not_in_library", address)

    monkeypatch.setattr(browse, "resolve", fake_resolve)
    monkeypatch.setattr(browse, "LINK_CHECK_SECONDS", 0.5)
    asked = ["wsp://s/gone/slow", *(f"wsp://s/gone/{i}" for i in range(6)), "wsp://s/here"]
    started = time.monotonic()
    r = client.post("/v1/links/missing", json={"addresses": asked})
    assert time.monotonic() - started < 1.5
    assert r.json()["missing"] == [f"wsp://s/gone/{i}" for i in range(6)]
    assert r.json()["unchecked"] == ["wsp://s/gone/slow"]  # asked again by the Navigator
