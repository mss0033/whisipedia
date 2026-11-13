"""A query no document matches is low confidence whatever the dense stage finds, and a result from
the cache says so (2026-11-10 trawl, search-index-15 and api-perf-security-06)."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from dataclasses import replace
from pathlib import Path

import pytest
from oracle_helpers import CORPUS

from wsp_core.model import SearchHit
from wsp_index import HashingEmbedder, TantivyConfig, TantivyIndex
from wsp_oracle.pipeline import OracleIndex, OracleSearcher, Scope
from wsp_oracle.router import Route, Router

SMALL = TantivyConfig(writer_heap_bytes=20_000_000, writer_threads=1)
NEIGHBOURS = ("wsp:encyclo/Lisbon", "wsp:encyclo/Alligator", "wsp:encyclo/Crocodile")


class _Neighbours:
    """Dense retrieval as it is: the nearest documents for any text, nonsense included."""

    dimensions = 8

    def search_docs(
        self, vector: object, *, limit: int = 50, allow: Callable[[str], bool] | None = None
    ) -> list[tuple[str, float, str]]:
        return [(d, 0.8 - i / 10, f"{d}#0") for i, d in enumerate(NEIGHBOURS)][:limit]


class _Sure:
    """A reranker that clears the confidence bar for whatever it is shown."""

    def rerank(self, query: str, hits: Sequence[SearchHit]) -> Sequence[SearchHit]:
        return [replace(h, score=0.9) for h in hits]


@pytest.fixture
def searcher(tmp_path: Path) -> Iterator[OracleSearcher]:
    idx = TantivyIndex(tmp_path / "lexical", SMALL)
    idx.upsert(CORPUS)
    idx.commit()
    yield OracleSearcher(
        idx,
        router=Router.from_sources(["encyclo", "dictionary", "rfc"]),
        embedder=HashingEmbedder(8),
        vectors=_Neighbours(),  # type: ignore[arg-type]
        reranker=_Sure(),  # type: ignore[arg-type]
        resolve=idx.lookup,
    )
    idx.close()


def test_a_query_no_document_matches_is_low_confidence_whatever_dense_finds(searcher: OracleSearcher) -> None:
    res = searcher.run("qwertyuiopasdf")
    assert {h.doc_id for h in res.hits} == set(NEIGHBOURS)  # the neighbours are still returned
    assert res.confidence == 0.9  # and the reranker is sure of them
    assert res.low_confidence
    assert res.unmatched
    # a word some document holds: matched, and as confident as the reranker
    held = searcher.run("alligator")
    assert not held.unmatched
    assert not held.low_confidence
    # one word of two is enough to be matched (the any-word query runs when few hold every word)
    half = searcher.run("alligator qwertyuiopasdf")
    assert not half.unmatched
    assert not half.low_confidence


def test_a_dense_only_route_is_not_marked_unmatched(
    searcher: OracleSearcher, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A route with no text for BM25 says nothing about the words: only the reranker's score decides.
    scoped = searcher.scoped

    def dense_only(query: str, sources: Sequence[str] | None) -> tuple[Route, Scope | None]:
        route, scope = scoped(query, sources)
        return replace(route, lexical=""), scope

    monkeypatch.setattr(searcher, "scoped", dense_only)
    res = searcher.run("qwertyuiopasdf")
    assert res.hits
    assert not res.unmatched
    assert not res.low_confidence


def test_a_cached_result_says_so(searcher: OracleSearcher) -> None:
    index = OracleIndex(searcher)
    first = index.result("alligator")
    assert "cached" not in first.timings_ms
    again = index.result("alligator")
    assert again.timings_ms["cached"] == 1.0
    stages = {k: v for k, v in again.timings_ms.items() if k != "cached"}
    assert stages == dict(first.timings_ms)  # the stages of the run that made it, marked as such
    assert again.hits == first.hits
    assert "cached" not in index.result("crocodile").timings_ms
    assert index.result("alligator").timings_ms["cached"] == 1.0  # the entry itself is not rewritten


def test_a_name_in_no_text_that_is_a_redirect_is_matched(tmp_path: Path) -> None:
    # A redirect's name lives in the suggest trie, not in any text: "acetaminophen" matches no word
    # of any document and the alias pin answers it. That is no nonsense query.
    from wsp_core.ids import make_doc_id
    from wsp_core.model import DocRecord

    docs = [
        DocRecord(
            doc_id=make_doc_id("wiki", "en/Paracetamol"),
            source_id="wiki",
            native_id="en/Paracetamol",
            title="Paracetamol",
            lang="en",
            license="CC0-1.0",
            text="Paracetamol is a medication used to treat fever and pain.",
            aliases=("Acetaminophen",),
        )
    ]
    idx = TantivyIndex(
        tmp_path / "lexical",
        TantivyConfig(languages=("en",), writer_heap_bytes=20_000_000, writer_threads=1, store_text=True),
    )
    idx.upsert(docs)
    idx.commit()
    searcher = OracleSearcher(
        idx,
        router=Router.from_sources(["wiki"]),
        embedder=HashingEmbedder(8),
        vectors=_Neighbours(),  # type: ignore[arg-type]
        reranker=_Sure(),  # type: ignore[arg-type]
        resolve=idx.lookup,
    )
    res = searcher.run("acetaminophen")
    assert res.hits[0].doc_id == make_doc_id("wiki", "en/Paracetamol")
    assert not res.unmatched
    assert not res.low_confidence
    idx.close()


def test_the_older_doubt_is_told_apart_from_the_new_rule(searcher: OracleSearcher) -> None:
    sure = searcher.run("qwertyuiopasdf")
    assert (sure.low_confidence, sure.unmatched, sure.doubted) == (True, True, False)
    searcher.low_confidence = 0.95  # the reranker's 0.9 is under the bar now
    both = searcher.run("qwertyuiopasdf")
    assert (both.low_confidence, both.unmatched, both.doubted) == (True, True, True)
    held = searcher.run("alligator")
    assert (held.low_confidence, held.unmatched, held.doubted) == (True, False, True)
