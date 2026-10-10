"""Contract suite for ``LexicalIndex`` (PLAN §9.3, §12.2).

Subclass and provide an ``index`` fixture returning an empty index::

    class TestMyIndex(LexicalIndexContract):
        @pytest.fixture
        def index(self, tmp_path): return MyIndex(tmp_path)

Semantics pinned here: writes become visible after ``commit()``; ``upsert`` replaces by ``doc_id``;
``sources=[]`` matches nothing while ``sources=None`` means all; queries are plain language and
never raise on stray syntax; ``suggest`` matches title prefixes case-insensitively.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest

from wsp_core.ids import content_hash, make_doc_id
from wsp_core.model import DocRecord, SearchHit
from wsp_core.ports import LexicalIndex


def make_doc(source_id: str, native_id: str, title: str, text: str, *, lang: str = "en") -> DocRecord:
    return DocRecord(
        doc_id=make_doc_id(source_id, native_id),
        source_id=source_id,
        native_id=native_id,
        title=title,
        lang=lang,
        license="CC0-1.0",
        text=text,
        content_hash=content_hash(text),
    )


CORPUS: tuple[DocRecord, ...] = (
    make_doc("encyclo", "turing", "Alan Turing", "Alan Turing worked on mathematics and computer science."),
    make_doc("encyclo", "lovelace", "Ada Lovelace", "Ada Lovelace wrote the first published algorithm."),
    make_doc("encyclo", "enigma", "Enigma machine", "Enigma was broken by Polish mathematicians."),
    make_doc("encyclo", "lisbon", "Lisbon", "Lisbon is the capital of Portugal, on the Tagus estuary."),
    make_doc("rfc", "9110", "HTTP Semantics", "This document describes HTTP semantics, caching and methods."),
    make_doc("rfc", "9111", "HTTP Caching", "HTTP caching stores responses for reuse by clients."),
    make_doc("books", "frankenstein", "Frankenstein", "A novel by Mary Shelley about a created being."),
    make_doc("books", "mathematics", "Mathematics for All", "An introduction to mathematics for readers."),
)


def _ids(hits: Sequence[SearchHit]) -> list[str]:
    return [h.doc_id for h in hits]


class LexicalIndexContract:
    @pytest.fixture
    def index(self, tmp_path: Path, request: pytest.FixtureRequest) -> LexicalIndex:
        """Build an empty instance under ``tmp_path``; clean up via ``request.addfinalizer``."""
        raise NotImplementedError("subclass must provide an `index` fixture")

    @pytest.fixture
    def loaded(self, index: LexicalIndex) -> LexicalIndex:
        assert index.upsert(CORPUS) == len(CORPUS)
        index.commit()
        return index

    def test_satisfies_protocol(self, index: LexicalIndex) -> None:
        assert isinstance(index, LexicalIndex)

    def test_finds_committed_document(self, loaded: LexicalIndex) -> None:
        hits = loaded.search("lovelace algorithm")
        assert hits
        assert hits[0].doc_id == make_doc_id("encyclo", "lovelace")
        assert hits[0].title == "Ada Lovelace"
        assert hits[0].source_id == "encyclo"
        assert hits[0].score > 0

    def test_scores_are_descending(self, loaded: LexicalIndex) -> None:
        scores = [h.score for h in loaded.search("mathematics", limit=10)]
        assert scores == sorted(scores, reverse=True)

    def test_title_match_outranks_single_body_match(self, loaded: LexicalIndex) -> None:
        hits = loaded.search("mathematics")
        assert hits[0].doc_id == make_doc_id("books", "mathematics")

    def test_snippet_mentions_query_term(self, loaded: LexicalIndex) -> None:
        hits = loaded.search("estuary")
        assert hits
        assert "estuary" in hits[0].snippet.lower()

    def test_no_match_returns_empty(self, loaded: LexicalIndex) -> None:
        assert list(loaded.search("xylophonezzz")) == []

    def test_blank_query_returns_empty(self, loaded: LexicalIndex) -> None:
        assert list(loaded.search("")) == []
        assert list(loaded.search("   ")) == []

    @pytest.mark.parametrize(
        "query", ['title:"unbalanced', "(((", "a AND", "-", "^2", "http://x/y?z=1", "foo:bar", "*", "~"]
    )
    def test_query_syntax_never_raises(self, loaded: LexicalIndex, query: str) -> None:
        loaded.search(query)

    def test_sources_filter(self, loaded: LexicalIndex) -> None:
        all_hits = loaded.search("http caching", limit=20)
        assert {h.source_id for h in all_hits} == {"rfc"}
        assert list(loaded.search("http caching", sources=["books"])) == []
        mixed = loaded.search("mathematics", limit=20, sources=["encyclo"])
        assert mixed
        assert all(h.source_id == "encyclo" for h in mixed)
        both = loaded.search("mathematics", limit=20, sources=["encyclo", "books"])
        assert {h.source_id for h in both} == {"encyclo", "books"}

    def test_empty_sources_matches_nothing(self, loaded: LexicalIndex) -> None:
        assert list(loaded.search("mathematics", sources=[])) == []

    def test_pagination(self, index: LexicalIndex) -> None:
        docs = [make_doc("pages", f"p{i}", f"Page {i}", "common " * (i + 1)) for i in range(12)]
        index.upsert(docs)
        index.commit()
        full = _ids(index.search("common", limit=12))
        assert len(full) == 12
        pages = [_ids(index.search("common", limit=5, offset=o)) for o in (0, 5, 10)]
        assert [len(p) for p in pages] == [5, 5, 2]
        assert pages[0] + pages[1] + pages[2] == full
        assert list(index.search("common", limit=5, offset=12)) == []

    def test_non_positive_limit_returns_empty(self, loaded: LexicalIndex) -> None:
        assert list(loaded.search("mathematics", limit=0)) == []

    def test_upsert_replaces_by_doc_id(self, loaded: LexicalIndex) -> None:
        loaded.upsert([make_doc("encyclo", "lisbon", "Lisbon", "Lisbon has trams and fado.")])
        loaded.commit()
        assert list(loaded.search("estuary")) == []
        hits = loaded.search("fado")
        assert _ids(hits) == [make_doc_id("encyclo", "lisbon")]
        assert len(_ids(loaded.search("lisbon", limit=20))) == 1

    def test_repeated_upsert_in_one_batch_keeps_last(self, index: LexicalIndex) -> None:
        index.upsert([make_doc("s", "x", "X", "first version alpha")])
        index.upsert([make_doc("s", "x", "X", "second version beta")])
        index.commit()
        assert list(index.search("alpha")) == []
        assert _ids(index.search("beta")) == [make_doc_id("s", "x")]

    def test_delete(self, loaded: LexicalIndex) -> None:
        loaded.delete([make_doc_id("encyclo", "lisbon"), make_doc_id("encyclo", "does-not-exist")])
        loaded.commit()
        assert list(loaded.search("lisbon")) == []
        assert loaded.search("lovelace")

    def test_delete_then_reinsert(self, loaded: LexicalIndex) -> None:
        doc_id = make_doc_id("encyclo", "lisbon")
        loaded.delete([doc_id])
        loaded.commit()
        loaded.upsert([make_doc("encyclo", "lisbon", "Lisbon", "Back again with tagus")])
        loaded.commit()
        assert _ids(loaded.search("tagus")) == [doc_id]

    def test_suggest_prefix_case_insensitive(self, loaded: LexicalIndex) -> None:
        assert "Alan Turing" in loaded.suggest("ala")
        assert "Alan Turing" in loaded.suggest("ALAN T")
        assert "HTTP Caching" in loaded.suggest("http")

    def test_suggest_limit_and_empty(self, loaded: LexicalIndex) -> None:
        assert len(loaded.suggest("http", limit=1)) == 1
        assert list(loaded.suggest("")) == []
        assert list(loaded.suggest("zzzz")) == []

    def test_suggest_follows_updates(self, loaded: LexicalIndex) -> None:
        loaded.delete([make_doc_id("encyclo", "lisbon")])
        loaded.upsert([make_doc("encyclo", "lima", "Lima", "Capital of Peru.")])
        loaded.commit()
        assert "Lisbon" not in loaded.suggest("li")
        assert "Lima" in loaded.suggest("li")
