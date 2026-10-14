"""Titles, aliases and suggest priors read from where they live, not copied into a database (ADR-0011)."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest
import tantivy

from wsp_core.ids import make_doc_id
from wsp_index import TantivyConfig, TantivyIndex
from wsp_index import lexical as lexical_module
from wsp_index.lexical import TANTIVY_POINTER, point_tantivy, tantivy_dir
from wsp_index.refinery import RefineryReader, RefineryWriter, generation_dir
from wsp_index.titles import IndexedSource, TitleSources, water_fill
from wsp_index_helpers import doc, set_rank
from wsp_vault.ratelimit import RateLimiter

SMALL = TantivyConfig(writer_heap_bytes=20_000_000, writer_threads=1)


def _redirects(path: Path, rows: list[tuple[str, str]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as db:
        db.execute("CREATE TABLE redirects (target TEXT NOT NULL, alias TEXT NOT NULL)")
        db.executemany("INSERT INTO redirects VALUES (?, ?)", rows)
        db.commit()
    return path


@pytest.fixture
def wiki(tmp_path: Path) -> IndexedSource:
    docs = [
        replace(doc("wiki", "Paris", "Paris"), aliases=("Lutetia",)),
        doc("wiki", "Rome", "Rome"),
        doc("wiki", "Bern", "Bern"),
    ]
    with RefineryWriter(tmp_path / "ref", "wiki", "g1") as w:
        w.write(docs)
    table = _redirects(
        tmp_path / "work" / "wiki" / "g1" / "redirects.sqlite",
        [("Paris", "City of Light"), ("Rome", "Eternal City"), ("Nowhere", "Lost"), ("Bern", "  ")],
    )
    return IndexedSource("wiki", RefineryReader(generation_dir(tmp_path / "ref", "wiki", "g1")), (table,))


def test_aliases_come_from_the_refinery_and_the_redirects(wiki: IndexedSource) -> None:
    got = sorted(TitleSources([wiki]).iter_aliases())
    assert got == sorted(
        [
            (make_doc_id("wiki", "Paris"), "Lutetia"),
            (make_doc_id("wiki", "Paris"), "City of Light"),
            (make_doc_id("wiki", "Rome"), "Eternal City"),
            (make_doc_id("wiki", "Nowhere"), "Lost"),  # a redirect to a missing page is still an alias
        ]
    )


def test_suggest_entries_join_redirect_aliases(wiki: IndexedSource) -> None:
    entries = {e.doc_id: e for e in TitleSources([wiki]).suggest_entries(None, budget=100)}
    assert set(entries) == {make_doc_id("wiki", n) for n in ("Paris", "Rome", "Bern")}
    assert entries[make_doc_id("wiki", "Paris")].aliases == ("Lutetia", "City of Light")
    assert entries[make_doc_id("wiki", "Rome")].aliases == ("Eternal City",)
    assert entries[make_doc_id("wiki", "Bern")].aliases == ()


def test_full_suggest_rebuild_offers_redirects(wiki: IndexedSource, tmp_path: Path) -> None:
    cfg = replace(SMALL, suggest_open_rebuild_titles=0)
    with TantivyIndex(tmp_path / "ix", cfg, titles=TitleSources([wiki])) as idx:
        idx.upsert(list(wiki.reader.records()))
        set_rank(idx, {make_doc_id("wiki", "Rome"): 0.5})
        assert idx.suggest("eternal") == ["Rome"]
        assert idx.suggest("city of") == ["Paris"]


def test_water_fill_shares_equally_and_gives_leftovers_on() -> None:
    assert water_fill({"a": 100, "b": 100, "c": 100}, 60) == {"a": 20, "b": 20, "c": 20}
    assert water_fill({"a": 5, "b": 100, "c": 100}, 60) == {"a": 5, "b": 28, "c": 27}
    assert water_fill({"a": 5, "b": 0}, 60) == {"a": 5, "b": 0}
    assert sum(water_fill({str(i): 7 for i in range(9)}, 50).values()) == 50


def test_search_ranks_and_caps_from_fast_columns_without_reading_documents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = replace(SMALL, rank_depth=10)  # a window of 10 of the 45 matches: the cap has work to do
    with TantivyIndex(tmp_path / "ix", cfg) as idx:
        idx.upsert([doc("big", f"b{i}", f"Lantern {i}", "lantern oil") for i in range(40)])
        idx.upsert([doc("small", f"s{i}", f"Lantern note {i}", "lantern wick") for i in range(5)])
        idx.commit()
        set_rank(idx, {make_doc_id("small", "s3"): 1.0})
        reads = {"n": 0}
        real = tantivy.Searcher.doc

        def counting(self: tantivy.Searcher, address: tantivy.DocAddress) -> tantivy.Document:
            reads["n"] += 1
            return real(self, address)

        monkeypatch.setattr(tantivy.Searcher, "doc", counting)
        window = idx.search_window("lantern", limit=10, per_source=3)
        assert reads["n"] == 10  # the ten hits returned, not the window's sources or the rank head
        assert sum(h.source_id == "small" for h in window.hits) >= 3  # the cap made room
        # The rank boost's features, read by the doc_key column, match a lookup by doc id.
        rank = idx.static_rank()
        assert rank is not None
        searcher = idx._index.searcher()  # pyright: ignore[reportPrivateUsage]
        addrs = [a for _, a in searcher.search(tantivy.Query.all_query(), 50).hits]
        before = reads["n"]
        feats = idx._rank_features(searcher, rank, addrs)  # pyright: ignore[reportPrivateUsage]
        assert reads["n"] == before
        ids = [str(real(searcher, a).get_first("doc_id")) for a in addrs]
        assert feats.tolist() == rank.features(ids).tolist()
        assert feats.max() == 1.0


def test_a_reader_learns_source_numbers_given_after_it_opened(tmp_path: Path) -> None:
    with TantivyIndex(tmp_path / "ix", SMALL) as writer:
        writer.upsert([doc("a", "1", "Alpha one")])
        writer.commit()
        reader = TantivyIndex(tmp_path / "ix", SMALL)
        writer.upsert([doc("b", "1", "Alpha two")])
        writer.commit()
        reader._index.reload()  # pyright: ignore[reportPrivateUsage]
        hits = reader.search_window("alpha", limit=2, per_source=1).hits
        assert sorted(h.source_id for h in hits) == ["a", "b"]
        reader.close()


def test_has_source_present_and_delete_source(tmp_path: Path) -> None:
    with TantivyIndex(tmp_path / "ix", SMALL) as idx:
        idx.upsert([doc("a", "1", "One"), doc("a", "2", "Two"), doc("b", "1", "Three")])
        idx.commit()
        assert idx.has_source("a")
        assert not idx.has_source("zzz")
        ids = [make_doc_id("a", "1"), make_doc_id("a", "9"), make_doc_id("b", "1")]
        assert idx.present(ids) == {make_doc_id("a", "1"), make_doc_id("b", "1")}
        idx.delete_source("a")
        idx.commit()
        assert not idx.has_source("a")
        assert idx.present(ids) == {make_doc_id("b", "1")}
        assert idx.lookup(make_doc_id("b", "1")) is not None


def test_the_tantivy_index_can_live_elsewhere(tmp_path: Path) -> None:
    lexical = tmp_path / "lexical"
    lexical.mkdir()
    assert tantivy_dir(lexical) == lexical / "tantivy"
    elsewhere = tmp_path / "nas" / "tantivy-g2"
    point_tantivy(lexical, elsewhere)
    with pytest.raises(RuntimeError, match="not mounted"):  # the NAS is not there: never an empty index
        TantivyIndex(lexical, SMALL)
    (tmp_path / "nas").mkdir()
    assert (lexical / TANTIVY_POINTER).read_text(encoding="utf-8").strip() == str(elsewhere)
    with TantivyIndex(lexical, SMALL) as idx:
        idx.upsert([doc("a", "1", "Far away")])
        idx.commit()
        assert idx.tantivy_dir == elsewhere
    assert (elsewhere / "meta.json").is_file()
    assert not (lexical / "tantivy").exists()
    with pytest.raises(ValueError, match="absolute"):
        point_tantivy(lexical, Path("relative/dir"))
    stray = tmp_path / "nas" / "stray"
    stray.mkdir()
    (stray / "not-an-index").write_text("x")
    point_tantivy(lexical, stray)
    with pytest.raises(RuntimeError, match="holds no Tantivy index"):
        TantivyIndex(lexical, SMALL)


def test_lookup_answers_from_a_cache_until_the_index_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lookup is a stored-document read (on the NAS, round trips); list pages do hundreds."""
    monkeypatch.setattr(lexical_module, "LOOKUP_STAMP_EVERY", 0.0)
    with TantivyIndex(tmp_path / "ix", SMALL) as idx:
        idx.upsert([doc("a", "1", "First title")])
        idx.commit()
        reads = {"n": 0}
        real = tantivy.Searcher.doc

        def counting(self: tantivy.Searcher, address: tantivy.DocAddress) -> tantivy.Document:
            reads["n"] += 1
            return real(self, address)

        monkeypatch.setattr(tantivy.Searcher, "doc", counting)
        did = make_doc_id("a", "1")
        for _ in range(3):
            hit = idx.lookup(did)
            assert hit is not None
            assert hit.title == "First title"
        assert idx.lookup(make_doc_id("a", "9")) is None
        assert idx.lookup(make_doc_id("a", "9")) is None
        assert reads["n"] == 1  # misses are remembered too
        idx.upsert([doc("a", "1", "Second title")])
        idx.commit()  # meta.json changes: the cache is dropped
        hit = idx.lookup(did)
        assert hit is not None
        assert hit.title == "Second title"
        assert reads["n"] == 2


def test_suggest_scans_are_paced_by_bytes(wiki: IndexedSource) -> None:
    clock = {"t": 0.0}
    slept: list[float] = []

    def sleep(s: float) -> None:
        slept.append(s)
        clock["t"] += s

    limiter = RateLimiter(1000.0, burst_seconds=0.0, monotonic=lambda: clock["t"], sleep=sleep)
    total = sum(b.nbytes for b in wiki.reader.scan(["doc_id", "title", "aliases"]))
    assert total > 0
    TitleSources([wiki], limiter=limiter).suggest_entries(None, budget=100)
    assert sum(slept) == pytest.approx(total / 1000.0)


def test_a_shared_window_runs_the_query_once_unless_a_source_crowds_it_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cold query costs seconds on the live index: the window is asked wider once, and only a source
    that still crowds the ``n`` best of that wider window has the query run again without it."""
    from wsp_index.lexical import DIVERSE_WIDTH

    calls = {"n": 0}
    real = cast(Callable[..., object], tantivy.Searcher.search)

    def counting(self: tantivy.Searcher, *args: object, **kwargs: object) -> object:
        calls["n"] += 1
        return real(self, *args, **kwargs)

    monkeypatch.setattr(tantivy.Searcher, "search", counting)
    with TantivyIndex(tmp_path / "ix", SMALL) as idx:
        idx.upsert([doc("big", f"b{i}", f"Lantern {i}", "lantern oil") for i in range(12)])
        idx.upsert([doc("small", f"s{i}", f"Lantern {i}", "lantern oil") for i in range(3)])
        idx.commit()
        calls["n"] = 0  # the commit's suggest rebuild searched too
        window = idx.search_window("lantern", limit=10, per_source=3)
        assert calls["n"] == 1  # 15 matches fit the wider window of 10 * DIVERSE_WIDTH: one run
        assert sum(h.source_id == "small" for h in window.hits) == 3
        assert len(window.hits) == 10
    with TantivyIndex(tmp_path / "ix2", SMALL) as idx:
        n = 10 * DIVERSE_WIDTH + 20
        idx.upsert([doc("big", f"b{i}", f"Lantern {i}", "lantern oil") for i in range(n)])
        idx.upsert([doc("small", f"s{i}", f"Lantern note number {i}", "lantern wick") for i in range(3)])
        idx.commit()
        calls["n"] = 0
        window = idx.search_window("lantern", limit=10, per_source=3)
        assert calls["n"] == 2  # big filled the wider window: run again without it
        assert sum(h.source_id == "small" for h in window.hits) == 3
        assert sum(h.source_id == "big" for h in window.hits) == 7
