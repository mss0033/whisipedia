import json

from wsp_core import mindtouch

STUB = (
    "<html><head><title>14.6: Spring &amp; Neap Tides</title>"
    '<meta http-equiv="refresh" content="0;URL=\'../index.html#/Bookshelves/Ocean/14.06:_Tides\'" />'
    "</head><body></body></html>"
)
SHARED = json.dumps(
    {"pages": [{"id": "7", "title": "Tides", "path": "Bookshelves/Ocean/14.06:_Tides"}]}
).encode()


def test_only_app_stubs_have_a_content_entry() -> None:
    assert mindtouch.content_entry("index/page_31698", STUB) == "content/page_content_31698.json"
    assert mindtouch.content_entry("index/page_31698", STUB.encode()) == "content/page_content_31698.json"
    assert mindtouch.content_entry("A/page_31698", STUB) is None  # another ZIM's page
    assert mindtouch.content_entry("index/page_1", "<html><body>Real page</body></html>") is None
    other = STUB.replace("../index.html#/", "./IPv6_address#")  # a section redirect, not the app
    assert mindtouch.content_entry("index/page_1", other) is None
    assert mindtouch.content_entry("index/page_1", STUB + " " * mindtouch.STUB_MAX_BYTES) is None


def test_the_page_is_its_body_with_links_and_images_relative_to_the_stub() -> None:
    body = (
        '<p>Spring tides <a href="#/Bookshelves/Ocean/14.06%3A_Tides#king">here</a>, '
        '<a href="#/Not/Listed">gone</a>, <a href="https://x.org/">out</a>.</p>'
        '<img src="content/geo.libretexts.org/%40api/deki/files/1/a.jpg%3Frevision%3D1">'
    )
    page = mindtouch.page_html(STUB, json.dumps({"htmlBody": body}).encode(), mindtouch.page_ids(SHARED))
    assert page == (
        "<html><head><title>14.6: Spring &amp; Neap Tides</title></head><body>"
        "<h1>14.6: Spring &amp; Neap Tides</h1>"
        '<p>Spring tides <a href="page_7#king">here</a>, '
        '<a href="#/Not/Listed">gone</a>, <a href="https://x.org/">out</a>.</p>'
        '<img src="../content/geo.libretexts.org/%40api/deki/files/1/a.jpg%3Frevision%3D1">'
        "</body></html>"
    )
    assert mindtouch.page_html(STUB, b"not json", {}) is None
    assert mindtouch.page_html(STUB, b'{"other": 1}', {}) is None
    assert mindtouch.page_ids(b"[]") == {}
    assert mindtouch.page_ids(b"{bad") == {}
