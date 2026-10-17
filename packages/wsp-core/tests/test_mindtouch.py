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


GROWTH = "Bookshelves/Calculus/Stewart/03%3A_Rules/3.08%3A_Growth"


def test_a_reuse_widget_holds_the_page_it_names() -> None:
    """12,021 of LibreTexts Mathematics' 38,184 pages lacked the parts a content-reuse widget fills
    in, and 546 were empty (2026-10-17)."""
    pages = mindtouch.Pages(
        {
            "Bookshelves/Calculus/Calculus_(OpenStax)/04:_Apps/4.7E:_Exercises": "11",
            "Bookshelves/Calculus/Stewart/03:_Rules/3.08:_Growth": "12",
            "Courses/A/1:_Intro/Exercises": "13",
            "Courses/B/1:_Intro/Exercises": "14",  # the same last two parts twice: never by tail
        }
    )
    assert pages.find("Bookshelves/Calculus/Stewart/03%3A_Rules/3.08%3A_Growth") == "12"
    assert (
        pages.find("Bookshelves/Calculus/Book:_Calculus_(OpenStax)/04:_Apps/4.7E:_Exercises") == "11"
    )  # renamed book
    assert pages.find("Somewhere/Else/04:_Apps/4.7E:_Exercises") == "11"  # by its chapter and page
    assert pages.find("Courses/C/1:_Intro/Exercises") is None
    bodies = {
        "12": {
            "htmlBody": "<p>Growth is exponential.</p>"
            f'<div class="mt-contentreuse-widget" data-page="{GROWTH}"></div>'
        },
        "11": {"htmlBody": "<p>Exercise 1.</p>"},
    }

    def load(pid: str) -> bytes | None:
        return json.dumps(bodies[pid]).encode() if pid in bodies else None

    widget = f'<div class="mt-contentreuse-widget" data-page="{GROWTH}" data-section="">'
    body = (
        f'<p>Intro.</p>{widget}<div class="mt-include"></div></div><div class="mt-contentreuse-widget"></div>'
    )
    out = mindtouch.expand_reuse(body, pages, load)
    # the reused page (which names itself again: not repeated) inside the widget; an empty widget stays
    assert out == (
        f"<p>Intro.</p>{widget}<p>Growth is exponential.</p>"
        f'<div class="mt-contentreuse-widget" data-page="{GROWTH}"></div>'
        '<div class="mt-include"></div></div><div class="mt-contentreuse-widget"></div>'
    )
    page = mindtouch.page_html(
        STUB, json.dumps({"htmlBody": body}).encode(), pages.ids, pages=pages, load=load
    )
    assert page is not None
    assert "Growth is exponential." in page
