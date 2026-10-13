from __future__ import annotations

import random
import time
from collections import Counter
from datetime import date
from pathlib import Path

import pytest

from wsp_core.ids import DocId
from wsp_core.model import ChangeKind, ChangeRecord
from wsp_core.ports import Catalog, PortalSource
from wsp_index import RefineryWriter, SqliteChangeFeed
from wsp_index import refinery as refinery_module
from wsp_index.catalog import (
    CatalogConfig,
    InitialCategories,
    RefineryCatalog,
    build_source_catalog,
    parse_date_page,
    refresh_featured,
)
from wsp_index_helpers import doc

DATE_PAGE = """October 10 is the 283rd day of the year.
Events
Pre-1600
732 \u2013 Battle of Tours: Charles Martel defeats the invading forces.
1911 \u2013 The Wuchang Uprising leads to the end of the Qing dynasty.
* 1970 \u2013 Fiji becomes independent from the United Kingdom.
Births
1813 \u2013 Giuseppe Verdi, Italian composer (d. 1901)
"""


def _wiki(root: Path) -> None:
    with RefineryWriter(root, "wikipedia-en-zim", "2026-08", row_group_rows=8, docs_per_file=40) as w:
        long = "word " * 600
        for i in range(60):
            links = tuple(f"wsp:wikipedia-en-zim/Topic_{j}" for j in range(i % 5))
            w.write([doc("wikipedia-en-zim", f"Topic_{i}", f"Topic {i}", long, links=links)])
        w.write(
            [
                doc(
                    "wikipedia-en-zim",
                    "List_of_topics",
                    "List of topics",
                    long,
                    links=("wsp:wikipedia-en-zim/Topic_1",),
                )
            ]
        )
        w.write([doc("wikipedia-en-zim", "October_10", "October 10", DATE_PAGE)])


def _bundle(root: Path) -> None:
    with RefineryWriter(root, "kiwix-bundle", "2026-07") as w:
        for book in ("ifixit_en_all", "wikibooks_en_all"):
            for i in range(10):
                w.write([doc("kiwix-bundle", f"{book}/Page_{i}", f"{book} page {i}")])


def test_parse_date_page() -> None:
    assert parse_date_page(DATE_PAGE) == [
        (732, "Battle of Tours: Charles Martel defeats the invading forces."),
        (1911, "The Wuchang Uprising leads to the end of the Qing dynasty."),
        (1970, "Fiji becomes independent from the United Kingdom."),
    ]


def test_build_and_serve(tmp_path: Path) -> None:
    refinery, cat_root = tmp_path / "refinery", tmp_path / "catalog"
    _wiki(refinery)
    _bundle(refinery)
    cfg = CatalogConfig(featured=True, featured_candidates=10, min_featured_chars=100)
    build_source_catalog(refinery, "wikipedia-en-zim", "2026-08", cat_root, cfg)
    build_source_catalog(refinery, "kiwix-bundle", "2026-07", cat_root)
    feed = SqliteChangeFeed(tmp_path / "changes.sqlite")
    feed.publish(
        ChangeRecord(DocId(f"wsp:wikipedia-en-zim/Topic_{i}"), ChangeKind.ADDED, "2026-08", None, "h")
        for i in range(5)
    )
    feed.publish([ChangeRecord(DocId("wsp:kiwix-bundle/x/y"), ChangeKind.ADDED, "2026-07", None, "h")])
    cat = RefineryCatalog(refinery, cat_root, feed, featured_sources=["wikipedia-en-zim"])
    assert isinstance(cat, Catalog)
    assert isinstance(cat, PortalSource)

    assert cat.doc_count("wikipedia-en-zim") == 62
    assert cat.doc_count("nope") is None

    # Featured: well-linked articles, never lists or date pages, stable for a day.
    pick = cat.featured(date(2026, 10, 10))
    assert pick is not None
    assert pick == cat.featured(date(2026, 10, 10))
    assert pick.removeprefix("wsp:wikipedia-en-zim/") in {f"Topic_{i}" for i in range(4)}
    picks = {cat.featured(date(2026, 1, 1).replace(day=d)) for d in range(1, 29)}
    assert len(picks) > 1

    otd = cat.on_this_day(date(2026, 10, 10), limit=2)
    assert len(otd) == 2
    assert otd[0].year < otd[1].year
    assert otd[0].doc_id == "wsp:wikipedia-en-zim/October_10"
    assert cat.on_this_day(date(2026, 10, 11)) == ()

    # Directory: a bundle splits by book, a single-book source A-Z.
    books = cat.categories("kiwix-bundle")
    assert [(c.slug, c.name, c.count) for c in books] == [
        ("ifixit-en-all", "Ifixit", 10),
        ("wikibooks-en-all", "Wikibooks", 10),
    ]
    listed = cat.category_docs("kiwix-bundle", "ifixit-en-all", limit=3)
    assert listed == [f"wsp:kiwix-bundle/ifixit_en_all/Page_{i}" for i in range(3)]
    letters = {c.slug for c in cat.categories("wikipedia-en-zim")}
    assert {"t", "l", "o"} <= letters

    # Random: uniform over the source's documents, via Parquet footers.
    rng = random.Random(1)
    seen = Counter(cat.random_doc(sources=["kiwix-bundle"], rng=rng) for _ in range(400))
    assert len(seen) == 20
    assert min(seen.values()) > 5
    assert cat.random_doc(sources=["nope"], rng=rng) is None

    # Recent: newest first from the feed tail, optionally per source.
    recent, cursor = cat.recent(limit=3)
    assert recent[0].doc_id == "wsp:kiwix-bundle/x/y"
    assert cursor
    only, _ = cat.recent(limit=10, sources=["wikipedia-en-zim"])
    assert len(only) == 5
    assert only[0].doc_id.endswith("Topic_4")

    started = time.perf_counter()
    for _ in range(50):
        cat.random_doc(sources=None, rng=rng)
        cat.featured(date(2026, 10, 10))
        cat.categories("wikipedia-en-zim")
    assert (time.perf_counter() - started) / 50 < 0.05
    feed.close()


def test_explicit_provider_and_reload(tmp_path: Path) -> None:
    refinery, cat_root = tmp_path / "refinery", tmp_path / "catalog"
    _bundle(refinery)
    feed = SqliteChangeFeed(tmp_path / "changes.sqlite")
    cat = RefineryCatalog(refinery, cat_root, feed, reload_seconds=0)
    assert cat.categories("kiwix-bundle") == ()
    build_source_catalog(
        refinery, "kiwix-bundle", "2026-07", cat_root, CatalogConfig(providers=[InitialCategories()])
    )
    assert {c.slug for c in cat.categories("kiwix-bundle")} == {"i", "w"}
    assert cat.featured(date(2026, 1, 1)) is None
    feed.close()


def test_random_picks_on_the_nas_map_the_id_index_locally(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On a network filesystem a reader without a map root loads each source's whole id index into
    memory (~20 bytes a document, read over the network): the API grew to 7 GB picking random pages
    for the start page (2026-10-13). With the map root it is memory-mapped from local disk."""
    refinery = tmp_path / "refinery"
    _wiki(refinery)

    def cifs(_path: Path) -> str:
        return "cifs"

    monkeypatch.setattr(refinery_module, "filesystem_type", cifs)
    feed = SqliteChangeFeed(tmp_path / "changes.sqlite")
    on_nas = RefineryCatalog(refinery, tmp_path / "catalog", feed)
    assert on_nas.random_doc(sources=None, rng=random.Random(1)) is not None
    reader = on_nas._reader("wikipedia-en-zim")  # pyright: ignore[reportPrivateUsage]
    assert reader is not None
    assert reader.id_index_bytes > 0  # in memory: what the fix avoids
    maps = tmp_path / "maps"
    mapped = RefineryCatalog(refinery, tmp_path / "catalog", feed, map_root=maps)
    assert mapped.random_doc(sources=None, rng=random.Random(1)) is not None
    reader = mapped._reader("wikipedia-en-zim")  # pyright: ignore[reportPrivateUsage]
    assert reader is not None
    assert reader.id_index_bytes == 0
    assert any(maps.rglob("*.npy"))


def test_catalog_builders_map_the_id_index_locally_on_the_nas(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The featured pick does thousands of point lookups: each one read a whole ``_ids.parquet`` row
    group over the NAS when the builder's reader had no local map (2026-10-13 review)."""
    refinery = tmp_path / "refinery"
    _wiki(refinery)

    def cifs(_path: Path) -> str:
        return "cifs"

    monkeypatch.setattr(refinery_module, "filesystem_type", cifs)
    cfg = CatalogConfig(featured=True, featured_candidates=10, min_featured_chars=100)
    maps = tmp_path / "maps"
    build_source_catalog(refinery, "wikipedia-en-zim", "2026-08", tmp_path / "catalog", cfg, map_root=maps)
    assert any(maps.rglob("*.npy"))
    assert refresh_featured(refinery, "wikipedia-en-zim", "2026-08", tmp_path / "catalog", cfg, map_root=maps)
    assert not any((tmp_path / "refinery").rglob("*.npy"))  # never beside the generation on the NAS


def test_random_picks_ask_the_nas_for_each_source_once_in_a_while(tmp_path: Path) -> None:
    """Finding a source's current generation is three metadata round trips on the NAS, and listing
    the Refinery a directory walk: once per source per reload period, not per pick."""
    refinery = tmp_path / "refinery"
    _wiki(refinery)
    _bundle(refinery)
    feed = SqliteChangeFeed(tmp_path / "changes.sqlite")
    clock = {"t": 100.0}
    cat = RefineryCatalog(
        refinery,
        tmp_path / "catalog",
        feed,
        sources=["wikipedia-en-zim", "kiwix-bundle"],
        reload_seconds=5,
        monotonic=lambda: clock["t"],
    )
    calls = {"n": 0}

    def counting(root: Path, source_id: str) -> str | None:
        calls["n"] += 1
        return refinery_module.current_generation(root, source_id)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("wsp_index.catalog.current_generation", counting)
        for i in range(5):
            assert cat.random_doc(sources=None, rng=random.Random(i)) is not None
        assert calls["n"] == 2  # one per registered source
        assert cat._known_sources() == ["kiwix-bundle", "wikipedia-en-zim"]  # pyright: ignore[reportPrivateUsage]
        clock["t"] += 6
        assert cat.random_doc(sources=None, rng=random.Random(9)) is not None
        assert calls["n"] == 4
