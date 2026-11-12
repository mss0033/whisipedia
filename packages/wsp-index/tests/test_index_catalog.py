from __future__ import annotations

import json
import random
import re
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


def test_a_cold_random_pick_opens_one_reader_when_the_catalog_counts_the_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Opening every source's reader (a few NAS round trips each) took a cold API past the start
    page's deadline (2026-10-14); the source is drawn from the catalog's counts first."""
    import json

    from wsp_index.catalog import CATALOG_FORMAT

    refinery = tmp_path / "refinery"
    _wiki(refinery)
    _bundle(refinery)
    catalog_root = tmp_path / "catalog"
    catalog_root.mkdir()
    for source_id, n in (("wikipedia-en-zim", 60), ("kiwix-bundle", 20)):
        entry: dict[str, object] = {"format": CATALOG_FORMAT, "doc_count": n, "categories": []}
        (catalog_root / f"{source_id}.json").write_text(json.dumps(entry))
    feed = SqliteChangeFeed(tmp_path / "changes.sqlite")
    # "not-refined-yet" is a registry source without data: it must cost no NAS round trip.
    cat = RefineryCatalog(
        refinery, catalog_root, feed, sources=["wikipedia-en-zim", "kiwix-bundle", "not-refined-yet"]
    )
    opened: list[str] = []
    real = refinery_module.RefineryReader

    class Counting(real):  # type: ignore[misc, valid-type]
        def __init__(self, gen_dir: Path, **kw: object) -> None:
            opened.append(gen_dir.parent.name)
            super().__init__(gen_dir, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr("wsp_index.catalog.RefineryReader", Counting)
    asked: list[str] = []

    def counting_generation(root: Path, source_id: str) -> str | None:
        asked.append(source_id)
        return refinery_module.current_generation(root, source_id)

    monkeypatch.setattr("wsp_index.catalog.current_generation", counting_generation)
    assert cat.random_doc(sources=None, rng=random.Random(3)) is not None
    assert len(opened) == 1
    assert "not-refined-yet" not in asked  # no NAS round trips for a source the catalog has not counted
    # Over many picks both sources come up, in proportion to their counts (60 : 20).
    seen = Counter(
        DocId(cat.random_doc(sources=None, rng=random.Random(i)) or "").split("/")[0] for i in range(300)
    )
    assert set(seen) == {"wsp:wikipedia-en-zim", "wsp:kiwix-bundle"}
    assert seen["wsp:wikipedia-en-zim"] > seen["wsp:kiwix-bundle"] * 2
    assert sorted(set(opened)) == ["kiwix-bundle", "wikipedia-en-zim"]  # each reader once, cached
    feed.close()


def test_a_small_change_patches_the_catalog_without_a_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """2026-11-05 deep dive: each PubMed update (14k-76k of 41.3M records) scanned the whole source
    again for its catalog, 71-150 minutes a night."""
    import json

    from wsp_index.catalog import patch_source_catalog

    refinery, cat_root = tmp_path / "refinery", tmp_path / "catalog"
    _bundle(refinery)
    path = build_source_catalog(refinery, "kiwix-bundle", "2026-07", cat_root)
    built = json.loads(path.read_text())
    assert built["scanned"]["generation_id"] == "2026-07"
    assert (built["scanned"]["doc_count"], built["drift"]) == (20, 0)
    gone = "wsp:kiwix-bundle/ifixit_en_all/Page_3"
    assert any(gone in c["docs"] for c in built["categories"])
    with RefineryWriter(refinery, "kiwix-bundle", "2026-08") as w:
        for book in ("ifixit_en_all", "wikibooks_en_all"):
            for i in range(10):
                if f"{book}/Page_{i}" != "ifixit_en_all/Page_3":
                    w.write([doc("kiwix-bundle", f"{book}/Page_{i}", f"{book} page {i}")])
        w.write([doc("kiwix-bundle", "wikibooks_en_all/Page_new", "wikibooks_en_all page new")])

    def no_scan(*_: object, **__: object) -> None:
        raise AssertionError("a patch reads no rows")

    monkeypatch.setattr(refinery_module.RefineryReader, "scan", no_scan)
    # 2 of 20 documents changed: 10%, more than the default 1%.
    assert patch_source_catalog(refinery, "kiwix-bundle", "2026-08", cat_root, 2) is None
    assert patch_source_catalog(refinery, "kiwix-bundle", "2026-08", cat_root, 2, max_drift=0.2) == path
    patched = json.loads(path.read_text())
    assert (patched["generation_id"], patched["doc_count"], patched["drift"]) == ("2026-08", 20, 2)
    listed = [d for c in patched["categories"] for d in c["docs"]]
    assert gone not in listed  # the removed document is not offered
    assert len(listed) == 19  # the new one waits for the next scan
    assert patched["scanned"] == built["scanned"]
    assert [c["count"] for c in patched["categories"]] == [c["count"] for c in built["categories"]]
    # The drift adds up from the scan, not from the last patch; a week-old scan is scanned again.
    assert patch_source_catalog(refinery, "kiwix-bundle", "2026-08", cat_root, 3, max_drift=0.2) is None
    week = built["scanned"]["at"] + 7 * 86400
    assert (
        patch_source_catalog(refinery, "kiwix-bundle", "2026-08", cat_root, 0, max_drift=0.2, now=week)
        is None
    )
    assert patch_source_catalog(
        refinery, "kiwix-bundle", "2026-08", cat_root, 0, max_drift=0.2, now=week - 60
    )
    # A catalog from before scans were recorded, or none: scanned.
    del patched["scanned"]
    path.write_text(json.dumps(patched))
    assert patch_source_catalog(refinery, "kiwix-bundle", "2026-08", cat_root, 0) is None
    path.unlink()
    assert patch_source_catalog(refinery, "kiwix-bundle", "2026-08", cat_root, 0) is None


def test_refresh_featured_reports_its_two_costs(tmp_path: Path) -> None:
    """2026-11-10: the featured re-pick after a rank took 49-172 minutes for two sources and nothing
    said whether the scan or the text lookups took them. One line per source, on the rank's path and
    on the path without one; the pick itself does not depend on being watched."""
    from wsp_index.linkrank import RANK_FILE, StaticRank, compute_static_rank
    from wsp_index.refinery import RefineryReader, generation_dir

    refinery = tmp_path / "refinery"
    _wiki(refinery)
    sid, gen = "wikipedia-en-zim", "2026-08"
    reader = RefineryReader(generation_dir(refinery, sid, gen))
    compute_static_rank([(sid, reader)], tmp_path / RANK_FILE, tmp_path / "work")
    line = re.compile(rf"featured {sid}: ranked in \d+\.\ds, ([\d,]+) lookups in \d+\.\ds(.*)")
    for rank, how in ((StaticRank(tmp_path / RANK_FILE), ""), (None, " (no rank)")):
        cfg = CatalogConfig(featured=True, featured_candidates=10, min_featured_chars=100, rank=rank)
        path = build_source_catalog(refinery, sid, gen, tmp_path / "catalog", cfg)
        said: list[str] = []
        assert refresh_featured(refinery, sid, gen, tmp_path / "catalog", cfg, progress=said.append)
        watched = path.read_bytes()
        assert len(said) == 1
        found = line.fullmatch(said[0])
        assert found is not None, said
        picked = sum(len(ids) for ids in json.loads(watched)["featured_by_lang"].values())
        assert int(found.group(1).replace(",", "")) >= picked > 0  # each one it picked was read
        assert found.group(2) == how
        assert refresh_featured(refinery, sid, gen, tmp_path / "catalog", cfg)
        assert path.read_bytes() == watched
    # A rank that knows none of the source's documents: the fallback says so, once.
    other = tmp_path / "other"
    with RefineryWriter(other, "elsewhere", "g1") as w:
        w.write([doc("elsewhere", "1", "One")])
    compute_static_rank(
        [("elsewhere", RefineryReader(generation_dir(other, "elsewhere", "g1")))],
        tmp_path / "empty" / RANK_FILE,
        tmp_path / "work",
    )
    cfg = CatalogConfig(
        featured=True,
        featured_candidates=10,
        min_featured_chars=100,
        rank=StaticRank(tmp_path / "empty" / RANK_FILE),
    )
    said = []
    assert refresh_featured(refinery, sid, gen, tmp_path / "catalog", cfg, progress=said.append)
    assert len(said) == 1
    found = line.fullmatch(said[0])
    assert found is not None, said
    assert found.group(2) == " (by links or length: the rank picked nothing)"
