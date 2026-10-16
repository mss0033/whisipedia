"""Totals after duplicates collapse, the result cache's freshness, and title queries across sites
(audit 2026-10-12 #2, #10, F10; the "huckleberry finn" report of the same day)."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import replace
from pathlib import Path

import pytest
from oracle_helpers import OverlapReranker

from wsp_core.ids import DocId, make_doc_id
from wsp_core.model import DocRecord, SearchHit
from wsp_index import TantivyConfig, TantivyIndex
from wsp_oracle.pipeline import OracleIndex, OracleSearcher
from wsp_oracle.router import Router


class _Editions:
    """A lexical index with 10,000 matches whose titles repeat in pairs (two records of each)."""

    def __init__(self) -> None:
        self.stamp = 1
        self.runs = 0

    def search(
        self, query: str, *, limit: int = 10, offset: int = 0, sources: object = None
    ) -> list[SearchHit]:
        self.runs += 1
        hits = [
            SearchHit(DocId(f"wsp:ol/{i:05}"), 10_000.0 - i, f"Title {i // 2}", "", "ol")
            for i in range(offset, min(offset + limit, 10_000))
        ]
        return hits

    def commit_stamp(self) -> int:
        return self.stamp


def _pairs(ids: Sequence[str]) -> dict[str, str]:
    """Content hashes: the two records of a title hold the same text."""
    return {i: f"text of {int(i.rsplit('/', 1)[1]) // 2}" for i in ids}


def test_a_list_shortened_by_collapsing_is_not_complete() -> None:
    # Audit 2026-10-12 #2: 200 candidates collapsed to 100 read as "every match": an exact total of 100
    # out of 10,000, and the cache never ran a deeper request.
    lex = _Editions()
    index = OracleIndex(OracleSearcher(lex, content_hashes=_pairs))  # type: ignore[arg-type]
    res = index.result("anything", depth=200)
    assert len(res.hits) == 100  # the duplicates collapsed
    assert not res.exhaustive
    runs = lex.runs
    deeper = index.result("anything", depth=400)
    assert lex.runs > runs  # not taken for exhaustive: the deeper request ran
    assert len(deeper.hits) == 200


def test_a_short_list_from_retrievers_that_ran_out_is_complete() -> None:
    class _Few(_Editions):
        def search(
            self, query: str, *, limit: int = 10, offset: int = 0, sources: object = None
        ) -> list[SearchHit]:
            return super().search(query, limit=limit, offset=offset)[: max(0, 5 - offset)]

    res = OracleIndex(OracleSearcher(_Few())).result("anything", depth=200)  # type: ignore[arg-type]
    assert len(res.hits) == 5
    assert res.exhaustive


def test_the_cache_is_cleared_when_the_lexical_index_commits() -> None:
    # Audit 2026-10-12 #10: a commit by another process (``wsp index build``) left cached result lists
    # in place for up to ten minutes; removed documents were listed and new ones missing.
    lex = _Editions()
    now = [0.0]
    index = OracleIndex(OracleSearcher(lex), refresh_seconds=5.0, clock=lambda: now[0])  # type: ignore[arg-type]
    index.result("anything")
    runs = lex.runs
    now[0] = 10.0
    index.result("anything")
    assert lex.runs == runs  # nothing committed: served from the cache
    lex.stamp += 1
    index.result("anything")
    assert lex.runs == runs  # not checked again yet
    now[0] = 20.0
    index.result("anything")
    assert lex.runs > runs


SMALL = TantivyConfig(languages=("en",), writer_heap_bytes=30_000_000, writer_threads=1)


def _doc(source: str, native: str, title: str, text: str, lang: str = "en") -> DocRecord:
    return DocRecord(
        doc_id=make_doc_id(source, native),
        source_id=source,
        native_id=native,
        title=title,
        lang=lang,
        license="CC0-1.0",
        text=text,
    )


BOOK = make_doc_id("gutenberg", "76")
TRANSLATION = make_doc_id("gutenberg", "66159")


@pytest.fixture
def library(tmp_path: Path) -> Iterator[TantivyIndex]:
    """Many short records titled like the query (BM25 favours them) beside the book's long text."""
    river = "the river raft and the widow and the island " * 400
    docs = [
        _doc("ol", f"OL{i}W", "Huckleberry Finn", f"Huckleberry Finn by Mark Twain, edition {i}")
        for i in range(150)
    ]
    docs += [_doc("se", f"users/{i}", "User Huckleberry Finn", "Huckleberry Finn, a user") for i in range(80)]
    docs += [
        _doc(
            "gutenberg",
            "76",
            "Adventures of Huckleberry Finn",
            f"Adventures of Huckleberry Finn. {river} Huck Finn",
        ),
        _doc(
            "gutenberg",
            "66159",
            "Huckleberry Finn kalandjai",
            "Huckleberry Finn kalandjai. a folyó",
            lang="hu",
        ),
        *(
            _doc(
                "gutenberg",
                f"71{i:02}",
                f"Adventures of Huckleberry Finn, Chapters {i} to {i + 4}",
                f"Huckleberry Finn {river}",
            )
            for i in range(1, 30, 5)
        ),
    ]
    idx = TantivyIndex(tmp_path / "lexical", SMALL)
    idx.upsert(docs)
    idx.commit()
    yield idx
    idx.close()


def _searcher(idx: TantivyIndex) -> OracleSearcher:
    return OracleSearcher(
        idx,
        router=Router.from_sources(["ol", "se", "gutenberg"]),
        reranker=OverlapReranker(),
        resolve=idx.lookup,
    )


def test_a_title_query_finds_the_book_across_all_sites(library: TantivyIndex) -> None:
    res = _searcher(library).run("huckleberry finn")
    page = [h.doc_id for h in res.hits[:10]]
    assert BOOK in page  # the book is on the first page, not crowded out by records named like it
    assert {h.source_id for h in res.hits[:10]} >= {"ol", "se", "gutenberg"}
    assert page.index(BOOK) < (page.index(TRANSLATION) if TRANSLATION in page else 10)


class _SaturatedReranker:
    """Like the cross-encoder on title matches: near 1.0 for all, the translation a hair ahead."""

    def rerank(self, query: str, hits: Sequence[SearchHit]) -> Sequence[SearchHit]:
        scores = {TRANSLATION: 0.9955, BOOK: 0.9633}
        return sorted((replace(h, score=scores.get(h.doc_id, 0.9)) for h in hits), key=lambda h: -h.score)


def test_within_a_site_the_query_language_edition_leads(library: TantivyIndex) -> None:
    searcher = _searcher(library)
    searcher.reranker = _SaturatedReranker()
    ids = [h.doc_id for h in searcher.run("huckleberry finn", sources=["gutenberg"]).hits]
    assert ids[0] == BOOK  # the English edition before the Hungarian translation
    assert TRANSLATION in ids[:10]  # which is still found


def test_a_question_holding_a_title_word_for_word_leads_with_that_page(tmp_path: Path) -> None:
    # 2026-10-16 field exam: "Incident Command System span of control" listed CIMS, NIMS and AIIMS
    # (short pages full of the words) above the article Incident Command System.
    span = "span of control incident command system " * 6
    docs = [
        _doc("wiki", "en/CIMS", "Coordinated Incident Management System", f"{span} New Zealand"),
        _doc("wiki", "en/NIMS", "National Incident Management System", f"{span} United States"),
        _doc(
            "wiki",
            "en/ICS",
            "Incident Command System",
            "A standard command structure. " * 80 + "Span of control",
        ),
        _doc("wiki", "en/Control", "Control", f"{span} control theory"),
        _doc("wiki", "en/Water", "Water", "water " * 30),
    ]
    idx = TantivyIndex(tmp_path / "lexical", SMALL)
    idx.upsert(docs)
    idx.commit()
    searcher = OracleSearcher(
        idx, router=Router.from_sources(["wiki"]), reranker=OverlapReranker(), resolve=idx.lookup
    )
    res = searcher.run("Incident Command System span of control")
    assert res.hits[0].doc_id == make_doc_id("wiki", "en/ICS")
    # The longest held title wins; one-word titles, the whole query and page-name queries never.
    route = searcher.router.route("history of the roman republic and its army")
    hits = [
        SearchHit(DocId(f"wsp:wiki/{t}"), 1.0, t, "", "wiki") for t in ("Rome", "Roman republic", "Republic")
    ]
    assert [h.title for h in searcher._embedded_title(route, hits, set())] == ["Roman republic"]  # pyright: ignore[reportPrivateUsage]
    water = [SearchHit(DocId("wsp:wiki/Water"), 1.0, "Water", "", "wiki")]
    assert searcher._embedded_title(searcher.router.route("how to purify water"), water, set()) == []  # pyright: ignore[reportPrivateUsage]
    lookup = searcher.router.route("roman republic")
    assert searcher._embedded_title(lookup, hits, set()) == []  # pyright: ignore[reportPrivateUsage]
    idx.close()
