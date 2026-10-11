from __future__ import annotations

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
from wsp_index_helpers import doc

SMALL = TantivyConfig(writer_heap_bytes=20_000_000, writer_threads=1)


@pytest.fixture
def index(tmp_path: Path) -> Iterator[TantivyIndex]:
    idx = TantivyIndex(tmp_path / "ix", SMALL)
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
    index.upsert([doc("s", "p", "Paris"), doc("s", "pc", "Paris Commune"), doc("s", "ph", "Paris Hilton")])
    index.commit()
    assert index.suggest("paris") == ["Paris", "Paris Hilton", "Paris Commune"]  # shorter first
    index.set_priors({make_doc_id("s", "pc"): 5.0})
    index.set_aliases({make_doc_id("s", "p"): ["City of Light"]})
    index.commit()
    assert index.suggest("paris")[0] == "Paris Commune"
    assert index.suggest("city of") == ["Paris"]


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
