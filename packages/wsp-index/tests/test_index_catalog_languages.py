"""Catalog per language (rank-oracle #13): initials in every script, featured articles ranked inside
their language, categories listing their best-ranked pages, inlinks sampled across all parts."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from wsp_index import RefineryReader, RefineryWriter, SqliteChangeFeed
from wsp_index.catalog import CatalogConfig, RefineryCatalog, build_source_catalog, initial_of
from wsp_index.linkrank import StaticRank, compute_static_rank
from wsp_index.refinery import generation_dir
from wsp_index_helpers import doc

LONG = "word " * 600


def test_initials_in_every_script() -> None:
    assert initial_of("!!! (band)") == ("b", "B")  # leading punctuation skipped
    assert initial_of("...") == ("other", "Other")
    assert initial_of("Émile Zola") == ("e", "E")
    assert initial_of("1984") == ("0-9", "0-9")
    assert initial_of("Берлин") == ("u0411", "Б")  # Cyrillic
    assert initial_of("Λόγος") == ("u039b", "Λ")  # Greek
    assert initial_of("東京") == ("han", "Han")
    assert initial_of("ひらがな") == ("kana", "Kana")
    assert initial_of("한국") == ("u1112", "ᄒ")  # the leading consonant of the syllable


def test_featured_per_language_and_ranked_directory(tmp_path: Path) -> None:
    """Interlinked Cebuano stubs never become the English featured article; a category lists its
    best-ranked pages, not the alphabetically first punctuation titles."""
    stubs = [
        doc(
            "wikipedia-x",
            f"ceb/S{i}",
            f"S{i}",
            LONG,
            lang="ceb",
            links=tuple(f"wsp:wikipedia-x/ceb/S{j}" for j in range(30) if j != i),
        )
        for i in range(30)
    ]
    en = [
        doc("wikipedia-x", f"en/P{i}", f"!!! {i}", LONG, links=("wsp:wikipedia-x/en/Hub",)) for i in range(6)
    ]
    refinery = tmp_path / "refinery"
    with RefineryWriter(refinery, "wikipedia-x", "g1") as w:
        w.write([*stubs, *en, doc("wikipedia-x", "en/Hub", "Hub", LONG)])
    reader = RefineryReader(generation_dir(refinery, "wikipedia-x", "g1"))
    compute_static_rank([("wikipedia-x", reader)], tmp_path / "rank.bin", tmp_path / "work")
    rank = StaticRank(tmp_path / "rank.bin")
    cfg = CatalogConfig(
        featured=True, featured_candidates=1, min_featured_chars=100, rank=rank, per_category=1
    )
    path = build_source_catalog(refinery, "wikipedia-x", "g1", tmp_path / "catalog", cfg)
    data = json.loads(path.read_text())
    assert data["featured"] == ["wsp:wikipedia-x/en/Hub"]  # English, the default language
    assert set(data["featured_by_lang"]) == {"ceb", "en"}
    feed = SqliteChangeFeed(tmp_path / "f.sqlite")
    cat = RefineryCatalog(refinery, tmp_path / "catalog", feed)
    assert cat.featured(date(2026, 1, 1)) == "wsp:wikipedia-x/en/Hub"
    assert str(cat.featured(date(2026, 1, 1), lang="ceb")).startswith("wsp:wikipedia-x/ceb/")
    english = next(c for c in data["categories"] if c["slug"] == "en")
    assert english["docs"] == ["wsp:wikipedia-x/en/Hub"]  # the best-ranked page, not "!!! 0"
    feed.close()


def test_inlinks_are_counted_across_all_parts(tmp_path: Path) -> None:
    """Without a rank, the most-linked page wins even when its linkers are stored last."""
    pages = [doc("w-x", f"A{i:03d}", f"A{i}", LONG, links=("wsp:w-x/Early",)) for i in range(40)]
    late = [doc("w-x", f"Z{i:03d}", f"Z{i}", LONG, links=("wsp:w-x/Late",)) for i in range(200)]
    refinery = tmp_path / "refinery"
    with RefineryWriter(refinery, "w-x", "g1", docs_per_file=40, row_group_rows=10) as w:
        w.write([*pages, *late, doc("w-x", "Early", "Early", LONG), doc("w-x", "Late", "Late", LONG)])
    cfg = CatalogConfig(featured=True, featured_candidates=1, min_featured_chars=100, max_link_docs=60)
    path = build_source_catalog(refinery, "w-x", "g1", tmp_path / "catalog", cfg)
    assert json.loads(path.read_text())["featured"] == ["wsp:w-x/Late"]
