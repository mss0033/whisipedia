# ruff: noqa: E501 - the recorded listing lines read better unwrapped
"""Kiwix from the official mirrors, checked against Kiwix's own checksums (owner decision 2026-10-26:
Kiwix's hosts refuse robots since October 2026)."""

from __future__ import annotations

from pathlib import Path

import pytest
from adapters_testkit import make_ctx, make_spec

from wsp_adapters import kiwix
from wsp_adapters.kiwix import (
    KiwixMirrorAdapter,
    Selector,
    choose_verifiable,
    parse_listing,
    saved_checksum,
)
from wsp_core.errors import FetchError
from wsp_core.model import Checksum
from wsp_core.testing.adapter_contract import FakeFetcher

BASE = "https://ftp.fau.de/kiwix/"
# FAU's Apache index and Wikimedia's nginx index, as served on 2026-10-26.
APACHE = """<pre><img src="/icons/back.gif"> <a href="/kiwix/zim/">Parent Directory</a>
<img src="/icons/unknown.gif"> <a href="wikipedia_ab_all_maxi_2026-07.zim">wikipedia_ab_all_maxi_2026-07.zim</a> 2026-07-30 11:49 54M
<img src="/icons/unknown.gif"> <a href="wikipedia_ab_all_maxi_2026-10.zim">wikipedia_ab_all_maxi_2026-10.zim</a> 2026-10-16 07:07 49M
<img src="/icons/unknown.gif"> <a href="wikipedia_ab_all_nopic_2026-10.zim">wikipedia_ab_all_nopic_2026-10.zim</a> 2026-10-16 05:41 17M
<a href="?C=N;O=D">Name</a> <a href="wikipedia_ab_all_maxi_2026-10.zim.torrent">x</a>"""
NGINX = """<a href="../">../</a>
<a href="wiktionary_en_all_nopic_2026-09a.zim">wiktionary_en_all_nopic_2026-09a.zim</a>   16-Oct-2026 05:07   51711705"""
TOP = '<a href="../">../</a> <a href="wikipedia/">wikipedia/</a> <a href="ifixit/">ifixit/</a>'
IFIXIT = '<a href="ifixit_en_all_2026-06.zim">ifixit_en_all_2026-06.zim</a>'


def sha(n: int) -> str:
    return f"{n:064x}"


def save(d: Path, name: str, digest: str) -> None:
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.sha256").write_text(f"{digest}  {name}\n")


@pytest.fixture(autouse=True)
def fresh_listing() -> None:
    kiwix._listings.clear()  # pyright: ignore[reportPrivateUsage]


def fetcher() -> FakeFetcher:
    return FakeFetcher(
        {
            f"{BASE}zim/": TOP.encode(),
            f"{BASE}zim/wikipedia/": APACHE.encode(),
            f"{BASE}zim/ifixit/": IFIXIT.encode(),
        }
    )


def test_a_listing_page_gives_book_flavour_date_and_url() -> None:
    entries = parse_listing(APACHE, BASE, "wikipedia")
    assert [(e.name, e.flavour, e.release_date) for e in entries] == [
        ("wikipedia_ab_all", "maxi", "2026-07"),
        ("wikipedia_ab_all", "maxi", "2026-10"),
        ("wikipedia_ab_all", "nopic", "2026-10"),
    ]  # sort links, torrents and the parent are not ZIMs
    assert entries[1].file_url == f"{BASE}zim/wikipedia/wikipedia_ab_all_maxi_2026-10.zim"
    assert entries[1].languages == ("abk",) or entries[1].languages == ("ab",)
    (w,) = parse_listing(NGINX, "https://dumps.wikimedia.org/kiwix/", "wiktionary")
    assert (w.name, w.flavour, w.release_date, w.languages) == (
        "wiktionary_en_all",
        "nopic",
        "2026-09a",
        ("eng",),
    )


def test_the_newest_verifiable_file_is_taken_and_the_newer_one_waits(tmp_path: Path) -> None:
    spec = make_spec(
        "kiwix-wikipedia-ab",
        {"adapter": "kiwix-opds", "selector": {"name": "wikipedia_ab_all", "flavour": "maxi"}},
    )
    save(tmp_path, "wikipedia_ab_all_maxi_2026-07.zim", sha(7))  # held: recorded from its manifest
    adapter = KiwixMirrorAdapter(dirs=[tmp_path])
    f = fetcher()
    ctx = make_ctx(spec, f, tmp_path / "work")
    (release,) = adapter.discover(ctx)
    (remote,) = release.files
    assert remote.name == "wikipedia_ab_all_maxi_2026-07.zim"
    assert remote.checksums == (Checksum("sha256", sha(7)),)
    assert remote.url.startswith(BASE)
    assert remote.mirrors == (
        "https://dumps.wikimedia.org/kiwix/zim/wikipedia/wikipedia_ab_all_maxi_2026-07.zim",
    )
    assert [e.file_name for e in adapter.choice(ctx).waiting] == ["wikipedia_ab_all_maxi_2026-10.zim"]
    # The owner saves Kiwix's checksum of the new file: it is taken.
    save(tmp_path / "manual", "wikipedia_ab_all_maxi_2026-10.zim", sha(10))
    adapter = KiwixMirrorAdapter(dirs=[tmp_path, tmp_path / "manual"])
    (release,) = adapter.discover(ctx)
    assert release.files[0].name == "wikipedia_ab_all_maxi_2026-10.zim"
    assert adapter.choice(ctx).waiting == ()
    assert not any("kiwix.org" in r for r in f.requests)  # Kiwix's own hosts are never asked


def test_a_flavour_is_never_swapped_for_one_whose_checksum_is_known(tmp_path: Path) -> None:
    sel = Selector(names=("wikipedia_ab_all",))  # no flavour: maxi preferred
    save(tmp_path, "wikipedia_ab_all_nopic_2026-10.zim", sha(1))
    pick = choose_verifiable(parse_listing(APACHE, BASE, "wikipedia"), sel, [tmp_path])
    assert pick.chosen == ()
    assert pick.unverifiable == ("wikipedia_ab_all",)


def test_a_book_without_any_known_checksum_blocks_the_release(tmp_path: Path) -> None:
    spec = make_spec("kiwix-ifixit", {"adapter": "kiwix-opds", "selector": {"name": "ifixit_en_all"}})
    ctx = make_ctx(spec, fetcher(), tmp_path / "work")
    with pytest.raises(FetchError, match="no Kiwix checksum saved"):
        KiwixMirrorAdapter(dirs=[tmp_path]).discover(ctx)


def test_only_a_well_formed_checksum_of_that_file_is_trusted(tmp_path: Path) -> None:
    name = "ifixit_en_all_2026-06.zim"
    (tmp_path / f"{name}.sha256").write_text("<html>Too many requests</html>")
    assert saved_checksum(name, [tmp_path]) is None
    (tmp_path / f"{name}.sha256").write_text(f"{sha(3)}  other_en_all_2026-06.zim\n")
    assert saved_checksum(name, [tmp_path]) is None
    (tmp_path / f"{name}.sha256").write_text(f"{sha(3)}  {name}\n")
    assert saved_checksum(name, [tmp_path]) == sha(3)


def test_the_listing_is_read_once_an_hour(tmp_path: Path) -> None:
    f = fetcher()
    kiwix.mirror_listing(f)
    kiwix.mirror_listing(f)
    assert f.requests.count(f"{BASE}zim/") == 1
