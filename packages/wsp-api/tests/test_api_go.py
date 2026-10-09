import httpx
import pytest

from wsp_api.schemas import Resolution


@pytest.mark.parametrize(
    ("address", "kind", "canonical", "href"),
    [
        ("wsp://home", "home", "wsp://home", "/v1/sources"),
        ("home", "home", "wsp://home", "/v1/sources"),
        ("wsp://system", "system", "wsp://system", "/v1/system"),
        ("wsp://whatsnew", "whatsnew", "wsp://whatsnew", "/v1/changes"),
        ("wsp://directory", "directory", "wsp://directory", None),
        ("wsp://search", "search", "wsp://search", None),
        ("wsp://rfc", "source", "wsp://rfc", "/v1/sources/rfc"),
        ("wsp://rfc/", "source", "wsp://rfc", "/v1/sources/rfc"),
        ("wsp://rfc/9110", "document", "wsp://rfc/9110", "/v1/documents/wsp:rfc/9110"),
        (
            "wsp://wikipedia-en-zim/A/Alan_Turing",
            "document",
            "wsp://wikipedia-en-zim/A/Alan_Turing",
            "/v1/documents/wsp:wikipedia-en-zim/A/Alan_Turing",
        ),
        ("wsp:rfc/9110", "document", "wsp://rfc/9110", "/v1/documents/wsp:rfc/9110"),
    ],
)
def test_resolve(client: httpx.Client, address: str, kind: str, canonical: str, href: str | None) -> None:
    r = client.get(f"/go/{address}")
    assert r.status_code == 200, r.text
    res = Resolution.model_validate(r.json())
    assert (res.kind, res.address, res.href) == (kind, canonical, href)


def test_resolve_percent_encoded_address(client: httpx.Client) -> None:
    res = Resolution.model_validate(client.get("/go/wsp%3A%2F%2Frfc%2F9110").json())
    assert res.href == "/v1/documents/wsp:rfc/9110"


def test_search_address_carries_query(client: httpx.Client) -> None:
    res = Resolution.model_validate(client.get("/go/wsp://search", params={"q": "http caching"}).json())
    assert res.address == "wsp://search?q=http+caching"
    assert res.href == "/v1/search?q=http+caching"


def test_redirect_mode(client: httpx.Client) -> None:
    r = client.get("/go/wsp://rfc/9110", params={"redirect": "true"}, follow_redirects=False)
    assert r.status_code == 307
    assert r.headers["location"] == "/v1/documents/wsp:rfc/9110"
    followed = client.get("/go/wsp://rfc/9110", params={"redirect": "true"}, follow_redirects=True)
    assert followed.json()["title"] == "RFC 9110: HTTP Semantics"


def test_redirect_mode_for_client_side_page_returns_json(client: httpx.Client) -> None:
    r = client.get("/go/wsp://directory", params={"redirect": "true"}, follow_redirects=False)
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
