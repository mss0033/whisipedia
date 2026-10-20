"""HTML rewrite fixes (audit 2026-10-11 #15) and the rendered-page byte cache (#8)."""

import threading
import time

from wsp_api.htmlrewrite import rewrite_html
from wsp_api.pages import ByteLRU

A = "/v1/assets/qa/g/"


def rw(html: str, native_id: str = "askubuntu.com_en_all/questions/1/foo", root: str = "") -> str:
    return rewrite_html(html, source_id="qa", generation_id="g", native_id=native_id, root=root)


def test_root_relative_urls_resolve_inside_the_book() -> None:
    book = "askubuntu.com_en_all/"
    assert (
        rw('<img src="/-/static/logo.png">', root=book)
        == f'<img src="{A}askubuntu.com_en_all/-/static/logo.png">'
    )
    assert (
        rw('<a href="/questions/2">x</a>', root=book)
        == '<a href="wsp://qa/askubuntu.com_en_all/questions/2">x</a>'
    )
    # a single-book source: the container root is the source root, as before
    assert rw('<img src="/I/x.png">', native_id="A/Page") == f'<img src="{A}I/x.png">'


def test_srcset_candidates_are_rewritten() -> None:
    out = rw('<img src="I/a.png" srcset="I/a2.png 2x, I/a3.png 3x, https://cdn.example/x.png 4x">', "A/P")
    srcset = f"{A}A/I/a2.png 2x, {A}A/I/a3.png 3x, https://cdn.example/x.png 4x"
    assert out == f'<img src="{A}A/I/a.png" srcset="{srcset}">'
    pic = rw(
        '<picture><source srcset="../I/a.webp" type="image/webp"><img src="../I/a.png"></picture>', "A/P"
    )
    assert f'<source srcset="{A}I/a.webp" type="image/webp">' in pic


def test_svg_images_links_scripts_and_media_go_through_the_asset_route() -> None:
    assert (
        rw('<svg><image xlink:href="I/x.png"/></svg>', "A/P")
        == f'<svg><image xlink:href="{A}A/I/x.png" /></svg>'
    )
    assert rw('<svg><image href="../I/x.png"/></svg>', "A/P") == f'<svg><image href="{A}I/x.png" /></svg>'
    assert (
        rw('<link rel="stylesheet" href="../-/s.css">', "A/P") == f'<link rel="stylesheet" href="{A}-/s.css">'
    )
    assert rw('<script src="../-/j.js"></script>', "A/P") == f'<script src="{A}-/j.js"></script>'
    assert rw('<video poster="p.jpg" src="v.webm"></video>', "A/P") == (
        f'<video poster="{A}A/p.jpg" src="{A}A/v.webm"></video>'
    )
    # absolute URLs are untouched (the client marks them as outside the library)
    assert rw('<script src="https://evil.example/x.js"></script>', "A/P") == (
        '<script src="https://evil.example/x.js"></script>'
    )


def test_a_link_to_an_ebook_file_is_a_download_not_a_page() -> None:
    page = "Alice's Adventures in Wonderland_cover.11"
    out = rw('<a href="Alice%27s%20Adventures%20in%20Wonderland.11.epub"><i class="fa"></i></a>', page)
    assert out == f'<a href="{A}Alice%27s%20Adventures%20in%20Wonderland.11.epub"><i class="fa"></i></a>'
    assert rw('<a href="Book.11">x</a>', page) == '<a href="wsp://qa/Book.11">x</a>'
    assert rw('<a href="files/Manual.PDF">x</a>', "A/P") == f'<a href="{A}A/files/Manual.PDF">x</a>'


def test_a_link_to_any_file_the_archive_holds_goes_to_the_asset_route() -> None:
    """66 of the 133 dead links the trawler found (2026-10-17) were files the archive holds — sheet
    music, recordings, pictures, feeds — which the page route never serves."""
    page = "www.mutopiaproject.org/cgibin/piece-info.cgi"
    for name in ("x.mid", "x-a4.ps.gz", "x.ly", "x.ily", "x.png", "x.JPG", "feed.xml", "notes.txt", "a.ogg"):
        assert (
            rw(f'<a href="../ftp/{name}">f</a>', page)
            == f'<a href="{A}www.mutopiaproject.org/ftp/{name}">f</a>'
        )
    assert (
        rw('<a href="../ftp/index.html">f</a>', page)
        == '<a href="wsp://qa/www.mutopiaproject.org/ftp/index.html">f</a>'
    )
    assert (
        rw('<a href="../ftp/BachJS/">f</a>', page)
        == '<a href="wsp://qa/www.mutopiaproject.org/ftp/BachJS">f</a>'
    )


def test_root_relative_urls_try_the_site_roots_only_when_missing() -> None:
    seen: list[str] = []

    def find(path: str) -> str | None:
        seen.append(path)
        return {"h.org/sub/x.png": "h.org/sub/x.png", "h.org/y": "h.org/y/"}.get(path)

    def rr(html: str) -> str:
        return rewrite_html(
            html,
            source_id="qa",
            generation_id="g",
            native_id="h.org/sub/p/q.html",
            site_roots=("h.org/", "h.org/sub/"),
            find=find,
        )

    assert (
        rr('<img src="/x.png"><img src="/x.png">')
        == f'<img src="{A}h.org/sub/x.png"><img src="{A}h.org/sub/x.png">'
    )
    # looked up once for the page, then (once) whether the picture found is held
    assert seen == ["x.png", "h.org/x.png", "h.org/sub/x.png", "h.org/sub/x.png"]
    assert rr('<a href="/y#s">y</a>') == '<a href="wsp://qa/h.org/y%2F#s">y</a>'  # the entry's own slash
    assert (
        rr('<a href="z.html">z</a>') == '<a href="wsp://qa/h.org/sub/p/z.html">z</a>'
    )  # relative: no lookup
    assert "z.html" not in " ".join(seen)


def test_a_picture_the_archive_does_not_hold_is_left_out() -> None:
    """The Factbook's Japan page showed 108 broken photos its archive never kept (2026-10-17)."""

    def rr(html: str) -> str:
        return rewrite_html(
            html,
            source_id="qa",
            generation_id="g",
            native_id="geos/ja.html",
            find=lambda p: p if p == "images/flag.png" else None,
        )

    assert rr('<img src="../images/flag.png">') == f'<img src="{A}images/flag.png">'
    assert (
        rr('<img src="../thumb/JA_017.jpg?152" alt="Mount Fuji">x')
        == '<span class="missing-image">Mount Fuji</span>x'
    )
    assert (
        rr('<a href="../thumb/JA_017.jpg"><img src="../thumb/JA_017.jpg"></a>')
        == f'<a href="{A}thumb/JA_017.jpg"></a>'
    )
    assert rr('<img src="data:image/png;base64,AAAA">') == '<img src="data:image/png;base64,AAAA">'


def test_base_is_dropped_and_everything_else_is_kept() -> None:
    assert rw('<base href="https://evil.example/"><a href="x">x</a>', "A/P") == '<a href="wsp://qa/A/x">x</a>'
    src = '<p class="x">A &amp; B &#169;<!-- c --><br/></p>'
    assert rw(src, "A/P") == src
    assert rw('<img src="data:image/png;base64,AAAA">', "A/P") == '<img src="data:image/png;base64,AAAA">'


def test_byte_cache_is_bounded_and_single_flight() -> None:
    cache: ByteLRU[str, bytes] = ByteLRU(1000, len)
    builds: list[str] = []

    def build(key: str) -> bytes:
        builds.append(key)
        time.sleep(0.1)
        return b"x" * 200

    threads = [
        threading.Thread(target=lambda: cache.get_or_build("a", lambda: build("a"))) for _ in range(10)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert builds == ["a"]
    for k in "bcdefg":
        cache.get_or_build(k, lambda k=k: build(k))
    assert cache.nbytes <= 1000
    assert cache.get("a") is None  # least recently used went first
    assert cache.get_or_build("big", lambda: b"y" * 600) == b"y" * 600
    assert cache.get("big") is None  # larger than a quarter of the budget: served, not cached
    assert cache.get_or_build("none", lambda: None) is None
    assert cache.get_or_build("none", lambda: b"z") == b"z"  # no negative entries


def test_none_host_links_from_stack_exchange_zims_resolve_as_relative() -> None:
    page = '<a href="//:None/../../questions/134882/undoing-a-git-rebase">Undoing</a>'
    out = rw(page, native_id="stackoverflow.com_en_all/questions/tagged/git-rebase")
    assert 'href="wsp://qa/stackoverflow.com_en_all/questions/134882/undoing-a-git-rebase"' in out


def test_event_handler_attributes_are_dropped() -> None:
    """A crawl's share buttons carry onclick: never run, and Chrome reports each one as a CSP violation
    while merely parsing the page (four a page view, citizensinformation.ie, 2026-10-20)."""
    src = (
        '<a class="share" href="#" onclick="window.print()" onmouseover="x()">Print</a><p onload="y()">t</p>'
    )
    assert rw(src, "A/P") == '<a class="share" href="#">Print</a><p>t</p>'
    assert rw('<p data-online="yes" one="1">t</p>', "A/P") == '<p data-online="yes">t</p>'
