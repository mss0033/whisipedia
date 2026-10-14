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
