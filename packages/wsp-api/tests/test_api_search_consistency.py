"""Search (audit 2026-10-11 #7, #9): one retrieval per query; totals and facets counted in it."""

import threading
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, ClassVar, cast

import httpx
import pytest

from wsp_api import create_app
from wsp_api.schemas import SearchResults
from wsp_api.testing import local_client
from wsp_api.testing.browse import browse_services
from wsp_api.testing.fakes import InMemoryDocumentStore, InMemoryLexicalIndex, sample_services
from wsp_core.ids import DocId
from wsp_core.model import DocRecord, SearchHit


class CountingIndex:
    """Counts every search the API runs."""

    def __init__(self, inner: InMemoryLexicalIndex, delay: float = 0.0) -> None:
        self.inner = inner
        self.searches: list[tuple[str, int, int]] = []
        self.delay = delay
        self.lock = threading.Lock()

    def search(
        self, query: str, *, limit: int = 10, offset: int = 0, sources: Sequence[str] | None = None
    ) -> Sequence[SearchHit]:
        with self.lock:
            self.searches.append((query, limit, offset))
        time.sleep(self.delay)
        return self.inner.search(query, limit=limit, offset=offset, sources=sources)

    def suggest(self, prefix: str, *, limit: int = 10) -> Sequence[str]:
        return self.inner.suggest(prefix, limit=limit)


@dataclass(frozen=True)
class _Result:
    hits: tuple[SearchHit, ...]
    low_confidence: bool = False
    scope: object = None


class OracleLike(CountingIndex):
    """Has ``result(q, sources, depth)`` like ``wsp_oracle.OracleIndex``: one ranked run per query."""

    def result(self, query: str, *, sources: Sequence[str] | None = None, depth: int = 0) -> _Result:
        return _Result(tuple(self.search(query, limit=depth, sources=sources)))


def _client(index: object, **kw: Any) -> httpx.Client:
    services = replace(browse_services(), index=cast(Any, index), **kw)
    return local_client(create_app(services))


def _search(c: httpx.Client, **params: object) -> SearchResults:
    r = c.get("/v1/search", params=cast(Any, params))
    assert r.status_code == 200, r.text
    return SearchResults.model_validate(r.json())


@pytest.mark.parametrize("oracle", [False, True])
def test_every_facet_is_exactly_what_filtering_by_it_shows(oracle: bool) -> None:
    base = browse_services().index
    index = OracleLike(cast(InMemoryLexicalIndex, base)) if oracle else CountingIndex(cast(Any, base))
    with _client(index) as c:
        res = _search(c, q="the", limit=100)
        assert res.total == len(res.hits)
        assert sum(f.count for f in res.facets) == res.total
        assert all(f.count > 0 for f in res.facets)  # no "Dictionary (0)"
        index.searches.clear()
        for facet in res.facets:
            only = _search(c, q="the", kind=facet.kind.value, limit=100)
            assert only.total == facet.count == len(only.hits)
            assert {self_kind.kind for self_kind in only.hits} == {facet.kind}
            assert only.facets == res.facets  # facets ignore the filter
        assert len(index.searches) == len(res.facets)  # one retrieval per request, no per-kind windows


def test_one_retrieval_per_page_and_pages_tile_the_results() -> None:
    index = CountingIndex(cast(InMemoryLexicalIndex, browse_services().index))
    with _client(index) as c:
        full = _search(c, q="the", limit=100)
        index.searches.clear()
        paged = [
            h.doc_id for off in range(0, full.total, 2) for h in _search(c, q="the", limit=2, offset=off).hits
        ]
        assert paged == [h.doc_id for h in full.hits]
        assert len(index.searches) == len(range(0, full.total, 2))


def test_offsets_beyond_the_ranked_window_are_refused(client: httpx.Client) -> None:
    assert client.get("/v1/search", params={"q": "the", "offset": 200}).status_code == 200
    r = client.get("/v1/search", params={"q": "the", "offset": 201})
    assert r.status_code == 422
    assert client.get("/v1/search", params={"q": "the", "offset": 10_000}).status_code == 422


class _Speller:
    KNOWN: ClassVar[set[str]] = {"the", "turing", "vaccine"}

    def correct(self, query: str, *, only: Iterable[str] | None = None) -> str | None:
        fixed = query.replace("vacine", "vaccine")
        return None if fixed == query else fixed

    def unknown(self, query: str) -> list[str]:
        return [w for w in query.split() if w not in self.KNOWN]


def test_did_you_mean_asks_the_vocabulary_not_the_index() -> None:
    index = CountingIndex(cast(InMemoryLexicalIndex, sample_services().index))
    services = replace(sample_services(), index=cast(Any, index), speller=_Speller())
    with local_client(create_app(services)) as c:
        res = _search(c, q="vacine the zzqxv blorf")
        assert res.did_you_mean is None or "vaccine" in res.did_you_mean
        # the retrieval, plus at most one count for the correction: no search per unknown word
        assert len(index.searches) <= 2
        assert all(q != w for q, _, _ in index.searches for w in ("zzqxv", "blorf", "vacine"))


def test_concurrent_identical_searches_share_one_retrieval() -> None:
    index = CountingIndex(cast(InMemoryLexicalIndex, sample_services().index), delay=0.3)
    services = replace(sample_services(), index=cast(Any, index))
    app = create_app(services)
    with local_client(app) as c:
        out: list[int] = []
        threads = [
            threading.Thread(
                target=lambda: out.append(c.get("/v1/search", params={"q": "turing"}).status_code)
            )
            for _ in range(8)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    assert out == [200] * 8
    assert len([s for s in index.searches if s[0] == "turing"]) < 8


class ProjectingStore(InMemoryDocumentStore):
    """A store with projected reads (``DocumentFields``); full reads are counted."""

    def __init__(self, inner: InMemoryDocumentStore) -> None:
        super().__init__()
        self.inner = inner
        self.full_reads = 0

    def get(self, doc_id: DocId, *, at_generation: str | None = None) -> DocRecord | None:
        self.full_reads += 1
        return self.inner.get(doc_id, at_generation=at_generation)

    def history(self, doc_id: DocId) -> Sequence[tuple[str, str]]:
        return self.inner.history(doc_id)

    def fields(self, doc_id: DocId, columns: Sequence[str]) -> Mapping[str, object] | None:
        doc = self.inner.get(doc_id)
        return None if doc is None else {c: getattr(doc, c) for c in ("doc_id", *columns)}


def test_lists_use_projected_reads_not_article_text() -> None:
    services = browse_services()
    store = ProjectingStore(cast(InMemoryDocumentStore, services.documents))
    with local_client(create_app(replace(services, documents=store))) as c:
        res = _search(c, q="the rfc 9110", limit=20)
        assert res.hits
        assert any(h.license for h in res.hits)  # hit metadata still filled in
        assert c.get("/v1/changes/recent").status_code == 200
        assert c.get("/go/rfc/9110").status_code == 200
        assert c.get("/v1/random", params={"seed": 1}).status_code == 200
    assert store.full_reads == 0
