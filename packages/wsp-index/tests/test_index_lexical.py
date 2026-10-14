from __future__ import annotations

import json
import time
from collections import Counter
from collections.abc import Iterator, Sequence
from dataclasses import replace
from pathlib import Path

import pytest

from wsp_core.ids import make_doc_id
from wsp_core.model import DocRecord, SearchHit
from wsp_index import (
    ParquetDocumentStore,
    RefineryReader,
    RefineryWriter,
    SqliteChangeFeed,
    TantivyConfig,
    TantivyIndex,
    refresh,
)
from wsp_index.lexical import SchemaMismatchError
from wsp_index.refinery import generation_dir
from wsp_index.titles import IndexedSource, TitleSources
from wsp_index_helpers import SnippetIndex, doc, set_rank

SMALL = TantivyConfig(writer_heap_bytes=20_000_000, writer_threads=1)


@pytest.fixture
def index(tmp_path: Path) -> Iterator[TantivyIndex]:
    idx = SnippetIndex(tmp_path / "ix", SMALL)
    yield idx
    idx.close()


def ids(hits: Sequence[SearchHit]) -> list[str]:
    return [h.doc_id for h in hits]


def test_uncommitted_writes_invisible_and_rollback(index: TantivyIndex) -> None:
    index.upsert([doc("s", "a", "Alpha", "zebra")])
    assert index.search("zebra") == []
    index.rollback()
    index.commit()
    assert index.search("zebra") == []
    assert index.suggest("alp") == []


def test_auto_commit(tmp_path: Path) -> None:
    cfg = TantivyConfig(writer_heap_bytes=20_000_000, writer_threads=1, commit_every=2)
    with TantivyIndex(tmp_path / "ix", cfg) as idx:
        idx.upsert([doc("s", "a", "A", "zebra")])
        assert idx.search("zebra") == []
        idx.upsert([doc("s", "b", "B", "zebra")])
        assert len(idx.search("zebra")) == 2


def test_persistence_across_reopen(tmp_path: Path) -> None:
    with TantivyIndex(tmp_path / "ix", SMALL) as idx:
        idx.upsert([doc("s", "a", "Alpha Centauri", "nearest star system")])
    with TantivyIndex(tmp_path / "ix", SMALL) as idx:
        assert ids(idx.search("star")) == [make_doc_id("s", "a")]
        assert idx.suggest("alpha c") == ["Alpha Centauri"]
        assert idx.num_docs == 1


def test_overlay_persisted_on_close(tmp_path: Path) -> None:
    idx = TantivyIndex(tmp_path / "ix", SMALL)
    idx.upsert([doc("s", "a", "Alpha")])
    idx.commit()
    idx.upsert([doc("s", "b", "Alpine")])
    idx.commit()  # small change: goes to the in-memory overlay
    idx.close()
    with TantivyIndex(tmp_path / "ix", SMALL) as again:
        assert sorted(again.suggest("alp")) == ["Alpha", "Alpine"]


def test_schema_mismatch(tmp_path: Path) -> None:
    TantivyIndex(tmp_path / "ix", SMALL).close()
    with pytest.raises(SchemaMismatchError):
        TantivyIndex(tmp_path / "ix", TantivyConfig(languages=("en", "fr")))


def test_english_stemming_and_accent_folding(index: TantivyIndex) -> None:
    index.upsert([doc("s", "a", "Runner", "She runs every morning to the café.")])
    index.commit()
    assert ids(index.search("running")) == [make_doc_id("s", "a")]
    assert ids(index.search("cafe")) == [make_doc_id("s", "a")]


def test_languages_routed_to_own_analyzers(tmp_path: Path) -> None:
    cfg = TantivyConfig(languages=("en", "fr"), writer_heap_bytes=20_000_000, writer_threads=1)
    with TantivyIndex(tmp_path / "ix", cfg) as idx:
        idx.upsert(
            [
                doc("s", "fr", "Chevaux", "Les chevaux galopaient dans les prairies.", lang="fr-FR"),
                doc("s", "de", "Pferde", "Die Pferde laufen.", lang="de"),
            ]
        )
        idx.commit()
        assert idx.route("fr-FR") == "fr"
        assert idx.route("de") == "any"
        assert ids(idx.search("galoper")) == [make_doc_id("s", "fr")]  # French stemmer
        assert ids(idx.search("pferde")) == [make_doc_id("s", "de")]  # fallback, unstemmed
        hit = idx.search("pferde")[0]
        assert hit.title == "Pferde"


def test_snippet_highlights_and_escapes(index: TantivyIndex) -> None:
    index.upsert([doc("s", "a", "Tags", "Use <script> tags with care; the script element runs code.")])
    index.commit()
    snippet = index.search("script")[0].snippet
    assert "<b>script</b>" in snippet
    assert "<script>" not in snippet.replace("<b>script</b>", "")


def test_title_only_match_gets_lead_snippet(index: TantivyIndex) -> None:
    index.upsert([doc("s", "a", "Quokka", "A small marsupial from Western Australia.")])
    index.commit()
    assert index.search("quokka")[0].snippet.startswith("A small marsupial")


def test_lookup(index: TantivyIndex) -> None:
    index.upsert([doc("s", "a", "Alpha")])
    index.commit()
    hit = index.lookup(make_doc_id("s", "a"))
    assert hit is not None
    assert (hit.title, hit.source_id) == ("Alpha", "s")
    assert index.lookup(make_doc_id("s", "zz")) is None


def test_suggest_priors_and_aliases(index: TantivyIndex) -> None:
    paris = replace(doc("s", "p", "Paris"), aliases=("City of Light",))
    index.upsert([paris, doc("s", "pc", "Paris Commune"), doc("s", "ph", "Paris Hilton")])
    index.commit()
    assert index.suggest("paris") == ["Paris", "Paris Hilton", "Paris Commune"]  # shorter first
    assert index.suggest("city of") == ["Paris"]  # the document's own aliases join at once
    set_rank(index, {make_doc_id("s", "pc"): 0.9})  # priors are the static rank's features
    assert index.suggest("paris")[0] == "Paris Commune"


def test_suggest_rebuild_path_matches_overlay_path(tmp_path: Path) -> None:
    docs = [doc("s", f"n{i}", f"Topic {i:03d}") for i in range(60)]
    overlay = TantivyConfig(writer_heap_bytes=20_000_000, writer_threads=1, suggest_rebuild_threshold=10_000)
    rebuild = TantivyConfig(writer_heap_bytes=20_000_000, writer_threads=1, suggest_rebuild_threshold=5)
    results: list[list[str]] = []
    for name, cfg in (("a", overlay), ("b", rebuild)):
        with TantivyIndex(tmp_path / name, cfg) as idx:
            idx.upsert(docs[:30])
            idx.commit()
            idx.upsert(docs[30:])
            idx.delete([make_doc_id("s", "n1"), make_doc_id("s", "n40")])
            idx.commit()
            results.append(list(idx.suggest("topic 0", limit=100)))
    assert results[0] == results[1]
    assert "Topic 001" not in results[0]
    assert "Topic 040" not in results[0]
    assert len(results[0]) == 58


def _gen(root: Path, gen: str, docs: list[DocRecord]) -> RefineryReader:
    with RefineryWriter(root, "enc", gen) as w:
        w.write(docs)
    return RefineryReader(generation_dir(root, "enc", gen))


def test_incremental_refresh_with_tombstones(tmp_path: Path) -> None:
    root = tmp_path / "refinery"
    g1 = _gen(
        root,
        "2026-01",
        [
            doc("enc", "a", "Apple", "red fruit"),
            doc("enc", "b", "Banana", "yellow fruit"),
            doc("enc", "c", "Cherry"),
        ],
    )
    g2 = _gen(
        root,
        "2026-02",
        [
            doc("enc", "a", "Apple", "green fruit"),
            doc("enc", "c", "Cherry"),
            doc("enc", "d", "Date", "brown fruit"),
        ],
    )
    feed = SqliteChangeFeed(tmp_path / "feed.sqlite")
    with TantivyIndex(tmp_path / "ix", SMALL) as idx:
        s1 = refresh(idx, feed, None, g1)
        assert (s1.upserted, s1.deleted) == (3, 0)
        s2 = refresh(idx, feed, g1, g2)
        assert (s2.upserted, s2.deleted) == (2, 1)
        assert sorted(ids(idx.search("fruit", limit=10))) == [make_doc_id("enc", x) for x in ("a", "d")]
        assert idx.search("red") == []
        assert idx.search("banana") == []
        assert "Banana" not in idx.suggest("b")
        assert idx.num_docs == 3
        changes, _ = feed.since(s1.cursor)
        assert {(c.doc_id, c.kind.value) for c in changes} == {
            (make_doc_id("enc", "a"), "modified"),
            (make_doc_id("enc", "b"), "removed"),
            (make_doc_id("enc", "d"), "added"),
        }
    feed.close()
    store = ParquetDocumentStore(root)
    assert store.get(make_doc_id("enc", "b")) is None  # tombstoned at head
    old = store.get(make_doc_id("enc", "b"), at_generation="2026-01")
    assert old is not None
    assert old.text == "yellow fruit"


def test_stopwords_do_not_outrank_content_words(index: TantivyIndex) -> None:
    # Function words reach BM25 (wsp_index.text.drop_stopwords); IDF discounts them in any corpus
    # where they are common, which the filler pages make this one.
    filler = [doc("s", f"f{i}", f"Page {i}", f"how do people work on topic {i}") for i in range(40)]
    index.upsert(
        [
            *filler,
            doc("s", "how", "How Doctors Think", "how how how doctors think about how things work"),
            doc("s", "vax", "Vaccine", "a vaccine trains the immune system to recognise a pathogen"),
        ]
    )
    index.commit()
    assert ids(index.search("how do vaccines work"))[0] == make_doc_id("s", "vax")
    assert ids(index.search("how"))  # an all-stopword query still searches


def test_search_hits_matches_search_without_snippets(index: TantivyIndex) -> None:
    index.upsert([doc("s", str(i), f"Zebra {i}", "zebra stripes " * (i + 1)) for i in range(5)])
    index.commit()
    full, bare = index.search("zebra", limit=5), index.search_hits("zebra", limit=5)
    assert ids(full) == ids(bare)
    assert all(h.snippet for h in full)
    assert not any(h.snippet for h in bare)


def test_reader_picks_up_trie_rebuilt_by_another_process(tmp_path: Path) -> None:
    writer = TantivyIndex(tmp_path / "ix", SMALL)
    writer.upsert([doc("s", "a", "Aardvark", "x")])
    writer.commit()
    writer.rebuild_suggest()
    reader = TantivyIndex(tmp_path / "ix", SMALL)
    assert reader.suggest("Zeb") == []
    writer.upsert([doc("s", "z", "Zebra", "y")])
    writer.rebuild_suggest()
    reader._trie_checked = 0.0  # pyright: ignore[reportPrivateUsage]  # skip the 5 s throttle
    assert reader.suggest("Zeb") == ["Zebra"]
    reader.close()
    writer.close()


def test_source_priors_lift_curated_sources(tmp_path: Path) -> None:
    docs = [
        doc("dict", "insulinized", "insulin", "insulin insulin insulin insulin"),
        doc(
            "enc", "Insulin", "Insulin hormone", "Insulin is a peptide hormone that regulates blood glucose."
        ),
    ]

    def top(name: str, priors: dict[str, float]) -> str:
        ix = TantivyIndex(tmp_path / name, replace(SMALL, source_priors=priors))
        ix.upsert(docs)
        ix.commit()
        first = ix.search("insulin")[0].source_id
        ix.close()
        return first

    assert top("plain", {}) == "dict"
    assert top("prior", {"enc": 50.0}) == "enc"


def test_an_index_built_with_older_analyzers_is_refused(tmp_path: Path) -> None:
    # An index analyzed by older rules would silently miss matches (rank-oracle audit #14).
    TantivyIndex(tmp_path / "ix", SMALL).close()
    import sqlite3

    db = sqlite3.connect(tmp_path / "ix" / "meta.sqlite")
    old = json.dumps({"languages": sorted(SMALL.languages), "store_text": SMALL.store_text})
    db.execute("UPDATE meta SET value=? WHERE key='schema'", (old,))
    db.commit()
    db.close()
    with pytest.raises(SchemaMismatchError):
        TantivyIndex(tmp_path / "ix", SMALL)


def test_snippets_come_from_the_document_store_when_text_is_not_stored(tmp_path: Path) -> None:
    from wsp_index import ParquetDocumentStore, RefineryWriter

    docs = [doc("s", "a", "Insulin", "Insulin is a peptide hormone made by beta cells of the pancreas.")]
    with RefineryWriter(tmp_path / "refinery", "s", "g1") as w:
        w.write(docs)
    store = ParquetDocumentStore(tmp_path / "refinery")
    with TantivyIndex(tmp_path / "ix", SMALL, texts=store.texts) as idx:
        idx.upsert(docs)
        idx.commit()
        (hit,) = idx.search("hormone", limit=1)
        assert "<b>hormone</b>" in hit.snippet
        stored = idx._index.searcher().doc(idx._index.searcher().search(idx.parse("hormone"), 1).hits[0][1])  # pyright: ignore[reportPrivateUsage, reportOptionalMemberAccess, reportArgumentType]
        assert stored.get_first("text_en") is None  # the body text is not stored in Tantivy


def test_without_a_text_source_hits_have_no_snippet(tmp_path: Path) -> None:
    with TantivyIndex(tmp_path / "ix", SMALL) as idx:
        idx.upsert([doc("s", "a", "Insulin", "Insulin is a peptide hormone.")])
        idx.commit()
        (hit,) = idx.search("hormone", limit=1)
        assert hit.snippet == ""
        assert hit.title == "Insulin"


def test_snippets_are_cut_on_demand_for_ranked_hits(index: TantivyIndex) -> None:
    from wsp_index.hybrid import HybridSearcher

    index.upsert([doc("s", str(i), f"Tide {i}", f"tables of tide heights for port {i}") for i in range(30)])
    index.commit()
    ranked = HybridSearcher(index, snippet_free=True).search("tide heights", limit=30)
    assert len(ranked) == 30
    assert all(h.snippet == "" for h in ranked)  # a window ranked without snippets
    page = [h.doc_id for h in ranked[:3]]
    cut = index.snippets("tide heights", [*page, "wsp:s/missing"])
    assert set(cut) == set(page)
    assert all("<b>tide</b>" in v for v in cut.values())
    assert index.snippets("tide", []) == {}


BILINGUAL = TantivyConfig(languages=("en", "de"), writer_heap_bytes=50_000_000, writer_threads=1)


def test_every_word_matches_in_any_language_are_found(tmp_path: Path) -> None:
    # Audit 2026-10-12 #1: the query has no German function words, so it was read as English, and the
    # English tiers alone (enough matches) answered: the German page about the query was never searched.
    mauer = make_doc_id("s", "mauer")
    with TantivyIndex(tmp_path / "ix", BILINGUAL) as idx:
        idx.upsert(
            [doc("s", f"b{i}", f"Berliner {i}", "a berliner doughnut recipe") for i in range(250)]
            + [
                doc(
                    "s",
                    "mauer",
                    "Berliner Mauer",
                    "Die Berliner Mauer war ein Grenzbefestigungssystem.",
                    lang="de",
                )
            ]
        )
        idx.commit()
        assert ids(idx.search("Berliner Mauer", limit=10))[0] == mauer  # the only every-word match
        assert mauer in ids(idx.search_hits("Berliner Mauer", limit=200))
        # Enough English documents matching every word (the old query-language tier) change nothing.
        idx.upsert(
            [doc("s", f"w{i}", f"Walls {i}", "berliner mauer " + "english text " * 50) for i in range(40)]
        )
        idx.commit()
        assert mauer in ids(idx.search("Berliner Mauer", limit=41))
        assert mauer in ids(idx.search_window("Berliner Mauer", limit=200, partial=False).hits)
        hit = idx.lookup(mauer)
        assert hit is not None
        assert hit.lang == "de"


def test_a_few_every_word_matches_rank_among_the_any_word_ones(tmp_path: Path) -> None:
    with TantivyIndex(tmp_path / "ix", BILINGUAL) as idx:
        idx.upsert(
            [
                doc("s", "both", "Metformin side effects", "metformin side effects and dosing", lang="en"),
                doc("s", "one", "Metformin", "metformin is a drug", lang="en"),
                doc("s", "de", "Metformin Nebenwirkungen", "metformin side effects deutsch", lang="de"),
            ]
        )
        idx.commit()
        window = idx.search_window("metformin side effects", limit=10)
        assert window.hits[0].doc_id == make_doc_id("s", "both")
        assert {h.doc_id for h in window.hits} == {make_doc_id("s", n) for n in ("both", "one", "de")}
        assert window.exhaustive


def _weak_and_strong(idx: TantivyIndex) -> None:
    """35 documents matching every word weakly, 40 matching one word strongly (audit 2026-10-12 #6)."""
    idx.upsert(
        [doc("s", f"every{i:02d}", f"Note {i}", "alpha beta " + "filler " * 300) for i in range(35)]
        + [doc("s", f"some{i:02d}", "alpha alpha", "alpha alpha alpha") for i in range(40)]
    )
    idx.commit()


def test_pages_of_any_size_tile_one_list(index: TantivyIndex) -> None:
    _weak_and_strong(index)
    full = ids(index.search_hits("alpha beta", limit=100))
    assert len(full) == 75
    assert all(d.startswith("wsp:s/every") for d in full[:35])  # every-word matches lead
    for size in (7, 10, 30):
        paged = [
            d
            for off in range(0, 80, size)
            for d in ids(index.search_hits("alpha beta", limit=size, offset=off))
        ]
        assert paged == full  # no page repeats a document, none is skipped
    assert ids(index.search("alpha beta", limit=10, offset=30)) == full[30:40]


def test_a_window_may_end_with_its_every_word_lead(index: TantivyIndex) -> None:
    # Audit 2026-10-12 #7: a 200-hit window ran the every-language any-word query (seconds on the full
    # library) whenever fewer than 200 documents matched every word.
    from unittest import mock

    _weak_and_strong(index)
    with mock.patch.object(index, "_top", wraps=index._top) as top:  # pyright: ignore[reportPrivateUsage]
        lead = index.search_window("alpha beta", limit=200, partial=False)
        assert top.call_count == 1  # the every-word query only
        assert len(lead.hits) == 35
        assert not lead.exhaustive
        full = index.search_window("alpha beta", limit=200)
        assert top.call_count == 3
        assert len(full.hits) == 75
        assert full.exhaustive
        assert [h.doc_id for h in full.hits[:35]] == [h.doc_id for h in lead.hits]


def test_the_commit_stamp_changes_with_every_commit(index: TantivyIndex) -> None:
    before = index.commit_stamp()
    index.upsert([doc("s", "a", "Alpha", "zebra")])
    assert index.commit_stamp() == before  # buffered, not committed
    index.commit()
    assert index.commit_stamp() != before


def test_suggest_titles_are_shared_by_the_sources(tmp_path: Path) -> None:
    # Unranked titles (a rebuild ranks last) were taken in reverse doc-id order until the key budget ran
    # out: one source's titles, the one sorting last (every completion of "dog" was a Wiktionary phrase).
    sources: list[IndexedSource] = []
    docs = {
        "books": [doc("books", f"b{i:03d}", f"Huckleberry {i}") for i in range(100)],
        "words": [doc("words", f"w{i:03d}", f"Dog phrase {i}") for i in range(100)],
        "encyc": [doc("encyc", f"e{i:03d}", f"Ranked {i}") for i in range(100)],
    }
    for sid, items in docs.items():
        with RefineryWriter(tmp_path / "ref", sid, "g1") as w:
            w.write(items)
        sources.append(IndexedSource(sid, RefineryReader(generation_dir(tmp_path / "ref", sid, "g1"))))
    cfg = replace(SMALL, suggest_max_keys=60, suggest_open_rebuild_titles=0)
    with TantivyIndex(tmp_path / "ix", cfg, titles=TitleSources(sources)) as idx:
        for items in docs.values():
            idx.upsert(items)
        set_rank(idx, {make_doc_id("encyc", f"e{i:03d}"): (1.0 + i) / 101 for i in range(100)})
        entries = idx._suggest_entries()  # pyright: ignore[reportPrivateUsage]
        assert len(entries) == 60
        by_source = Counter(e.doc_id.split("/")[0] for e in entries)
        # half for ranked titles, best first; the other half shared by the three sources
        assert [e.doc_id for e in entries[:30]] == [
            make_doc_id("encyc", f"e{i:03d}") for i in range(99, 69, -1)
        ]
        assert by_source == {"wsp:encyc": 40, "wsp:books": 10, "wsp:words": 10}
        assert idx.suggest("huckle")
        assert idx.suggest("dog")


def test_a_query_reads_each_stored_document_once() -> None:
    from wsp_index.lexical import _DocMemo  # pyright: ignore[reportPrivateUsage]

    class Address:
        def __init__(self, seg: int, doc: int) -> None:
            self.segment_ord, self.doc = seg, doc

    class Searcher:
        reads = 0
        num_docs = 7

        def doc(self, address: Address) -> str:
            Searcher.reads += 1
            return f"doc {address.segment_ord}/{address.doc}"

    memo = _DocMemo(Searcher())  # type: ignore[arg-type]
    assert memo.doc(Address(0, 1)) == memo.doc(Address(0, 1)) == "doc 0/1"  # type: ignore[arg-type]
    assert memo.doc(Address(1, 1)) == "doc 1/1"  # type: ignore[arg-type]
    assert Searcher.reads == 2
    assert memo.num_docs == 7  # everything else goes to the searcher


def test_a_new_rank_replaces_every_prior(index: TantivyIndex) -> None:
    index.upsert([doc("s", "p", "Paris"), doc("s", "pc", "Paris Commune"), doc("t", "ph", "Paris Hilton")])
    index.commit()
    set_rank(index, {make_doc_id("s", "p"): 0.9})
    assert index.suggest("paris")[0] == "Paris"
    set_rank(index, {make_doc_id("s", "pc"): 0.5, make_doc_id("t", "ph"): 0.2})  # p has no rank now
    assert index.suggest("paris") == ["Paris Commune", "Paris Hilton", "Paris"]


def test_a_suggest_rebuild_is_due_only_for_a_stale_or_old_trie(tmp_path: Path) -> None:
    """A full rebuild reads every title of the library; a few new titles on a fresh trie wait."""
    from dataclasses import replace as replace_cfg

    # suggest_open_rebuild_titles=0: not "small", so commits keep an overlay instead of rebuilding.
    cfg = replace_cfg(
        SMALL, suggest_rebuild_threshold=3, suggest_rebuild_seconds=3600, suggest_open_rebuild_titles=0
    )
    with TantivyIndex(tmp_path / "ix", cfg) as idx:  # an empty trie is written on open
        idx.upsert([doc("s", "a", "Aardvark", "x")])
        idx.rebuild_suggest()
        assert not idx.suggest_rebuild_due()  # fresh and clean
        idx.upsert([doc("s", "b", "Bison", "y")])
        idx.commit()
        assert not idx.suggest_rebuild_due()  # one pending title on a fresh trie: not worth the scan
        assert idx.suggest("Bis") == ["Bison"]  # the overlay serves it meanwhile
        assert idx.suggest_rebuild_due(now=time.time() + 3601)  # dirty and old: due
        # An overlay past its threshold is folded by the commit itself (unchanged behaviour).
        idx.upsert([doc("s", str(i), f"Camel {i}", "z") for i in range(4)])
        idx.commit()
        assert not idx.suggest_rebuild_due()
        assert idx.suggest("Cam", limit=1) == ["Camel 0"]


def test_a_trie_left_dirty_by_another_process_is_aged_not_rebuilt(tmp_path: Path) -> None:
    """Release steps are separate processes: a small overlay one step leaves behind (persisted as
    the dirty flag) must not make the next step's check say "due" on a fresh trie."""
    from dataclasses import replace as replace_cfg

    cfg = replace_cfg(
        SMALL, suggest_rebuild_threshold=3, suggest_rebuild_seconds=3600, suggest_open_rebuild_titles=0
    )
    with TantivyIndex(tmp_path / "ix", cfg) as idx:
        idx.upsert([doc("s", "a", "Aardvark", "x")])
        idx.rebuild_suggest()
        idx.upsert([doc("s", "b", "Bison", "y")])
        idx.commit()
        assert not idx.suggest_rebuild_due()
    with TantivyIndex(tmp_path / "ix", cfg) as idx:  # the next step
        assert not idx.suggest_rebuild_due()
        assert idx.suggest_rebuild_due(now=time.time() + 3601)  # still dirty: due once old
        idx.upsert([doc("s", "c", "Camel", "z")])
        idx.commit()
        assert not idx.suggest_rebuild_due()
        assert idx.suggest("Cam") == ["Camel"]
        idx.rebuild_suggest()
        assert idx.suggest("Bis") == ["Bison"]  # the earlier process's title joins at the rebuild
