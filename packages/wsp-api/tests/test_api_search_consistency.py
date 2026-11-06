"""Search (audit 2026-10-11 #7, #9): one retrieval per query; totals and facets counted in it."""

import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
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
from wsp_core.model import DocRecord, SearchHit, SourceSpec, SourceStatus, Tier


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
    exhaustive: bool | None = None


class OracleLike(CountingIndex):
    """Has ``result(q, sources, depth)`` like ``wsp_oracle.OracleIndex``: one ranked run per query."""

    def result(self, query: str, *, sources: Sequence[str] | None = None, depth: int = 0) -> _Result:
        return _Result(tuple(self.search(query, limit=depth, sources=sources)))


class CollapsingOracle(CountingIndex):
    """Collapses its candidates to a few and says whether they are every match (audit 2026-10-12 F10)."""

    exhaustive = False

    def result(self, query: str, *, sources: Sequence[str] | None = None, depth: int = 0) -> _Result:
        hits = tuple(self.search(query, limit=depth, sources=sources))[:2]
        return _Result(hits, exhaustive=self.exhaustive)


@pytest.mark.parametrize("exhaustive", [False, True])
def test_the_total_is_exact_only_when_the_oracle_says_so(exhaustive: bool) -> None:
    index = CollapsingOracle(cast(InMemoryLexicalIndex, browse_services().index))
    index.exhaustive = exhaustive
    with _client(index) as c:
        res = _search(c, q="the", limit=10)
        assert res.total == 2
        assert res.total_is_estimate is not exhaustive  # a short list is not proof that nothing is left


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
        # each filter is also searched in its own sources (2026-10-17 field exam: "Books" for "bread"
        # showed 2 of the mixed top 200 while Gutenberg holds hundreds): the mixed list and the kind's
        assert len(index.searches) <= 2 * len(res.facets)


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


def test_a_kind_is_searched_in_its_own_sources() -> None:
    """The mixed list is the top of everything; a kind filter reaches past it (2026-10-17 field exam:
    "Books" for "bread" showed 2 Wikisource pages of the top 200, Gutenberg holds hundreds)."""
    full = OracleLike(cast(InMemoryLexicalIndex, browse_services().index))
    every = _search(_client(full), q="the", limit=100)
    index = CollapsingOracle(cast(InMemoryLexicalIndex, browse_services().index))  # a mixed list of 2
    with _client(index) as c:
        mixed = _search(c, q="the", limit=100)
        beyond = [f.kind for f in every.facets if f.kind not in {m.kind for m in mixed.facets}]
        assert beyond  # kinds the short mixed list does not reach
        for kind in beyond:
            only = _search(c, q="the", kind=kind.value, limit=100)
            assert only.hits
            assert {h.kind for h in only.hits} == {kind}


def test_a_document_the_query_cites_comes_first() -> None:
    """RFCs citing RFC 791 ranked above it, and a question naming 5 U.S.C. 552 did not show the
    section (2026-10-17 field exam): a cited document the library holds is the first result."""
    services = browse_services()
    rfc = DocRecord(
        doc_id=DocId("wsp:rfc/rfc:RFC-4242"),
        source_id="rfc",
        native_id="rfc:RFC-4242",
        title="Information Refresh Time Option",
        lang="en",
        license="LicenseRef-IETF-Trust",
        text="Refresh.",
        content_hash="x",
    )
    assert isinstance(services.documents, InMemoryDocumentStore)
    services.documents.put(next(iter(services.vault.generations("rfc"))).generation_id, rfc)
    with local_client(create_app(services)) as c:
        res = _search(c, q="the refresh option of RFC 4242", limit=5)
        plain = _search(c, q="the", limit=100)
    assert res.hits[0].doc_id == "wsp:rfc/rfc:RFC-4242"
    assert res.hits[0].title == "Information Refresh Time Option"
    assert "wsp:rfc/rfc:RFC-4242" not in {h.doc_id for h in plain.hits}


# --- one copy of a page, rare-word corrections, the corrected title (2026-11-05 deep dive) ----------


def _spec(sid: str, access: Mapping[str, object] | None = None) -> SourceSpec:
    return SourceSpec(
        id=sid,
        name=sid,
        tier=Tier.T1,
        status=SourceStatus.ACTIVE,
        license="CC-BY-SA-4.0",
        legal_basis="open-license",
        domain=("dictionary",),
        raw={"access": dict(access or {})},
    )


TWIN_SOURCES = (
    _spec(
        "wiktionary-en",
        {
            "rendition": [{"source": "kiwix-wiktionary-en-all"}],
            "copies": [{"source": "kiwix-wiktionary-en-all", "match": "native"}],
        },
    ),
    _spec("kiwix-wiktionary-en-all"),
    _spec("simple", {"copies": [{"source": "wiki", "match": "native", "prefix": "en/"}]}),
    _spec("wiki"),
)


class FixedOracle:
    """Answers every query with the same ranked hits (an Oracle's ``result``)."""

    def __init__(self, hits: Sequence[SearchHit], pin: SearchHit | None = None) -> None:
        self.hits = tuple(hits)
        self.pin = pin
        self.asked: list[str | None] = []

    def result(self, query: str, *, sources: Sequence[str] | None = None, depth: int = 0) -> _Result:
        return _Result(self.hits, exhaustive=True)

    def search(
        self, query: str, *, limit: int = 10, offset: int = 0, sources: Sequence[str] | None = None
    ) -> Sequence[SearchHit]:
        return self.hits[offset : offset + limit]

    def suggest(self, prefix: str, *, limit: int = 10) -> Sequence[str]:
        return []

    def correction_pin(
        self,
        query: str,
        hits: Sequence[SearchHit],
        corrected: Callable[[], str | None],
        sources: Sequence[str] | None = None,
    ) -> SearchHit | None:
        self.asked.append(corrected())
        return self.pin


def _h(source: str, native: str, title: str = "x", lang: str = "en") -> SearchHit:
    return SearchHit(DocId(f"wsp:{source}/{native}"), 1.0, title, "", source, lang)


def test_a_page_held_by_two_sources_that_render_alike_is_listed_once() -> None:
    hits = [
        _h("kiwix-wiktionary-en-all", "photosynthesis"),
        _h("wiki", "en/Photosynthesis"),
        _h("wiktionary-en", "photosynthesis"),  # the same entry: it renders with the archive's page
        _h("simple", "Photosynthesis"),  # another text of the article: kept
        _h("wiktionary-en", "chlorophyll"),
        _h("kiwix-wiktionary-en-all", "chlorophyll"),
    ]
    services = replace(sample_services(sources=TWIN_SOURCES), index=cast(Any, FixedOracle(hits)))
    with local_client(create_app(services)) as c:
        res = _search(c, q="photosynthesis")
    assert [h.doc_id for h in res.hits] == [
        "wsp:kiwix-wiktionary-en-all/photosynthesis",
        "wsp:wiki/en/Photosynthesis",
        "wsp:simple/Photosynthesis",
        "wsp:wiktionary-en/chlorophyll",
    ]
    assert res.total == 4


class _Rare(_Speller):
    """Knows "einstien" (an iFixit user's name) as a thousand times rarer than "einstein"."""

    def correct(self, query: str, *, only: Iterable[str] | None = None) -> str | None:
        if only is not None and "einstien" not in only:
            return None
        fixed = query.replace("Einstien", "Einstein")
        return None if fixed == query else fixed

    def unknown(self, query: str) -> list[str]:
        return []

    def rare(self, query: str) -> list[str]:
        return ["einstien"] if "Einstien" in query else []


def test_a_typo_the_library_shares_is_corrected_unless_a_page_is_titled_so() -> None:
    many = [_h("wiki", f"en/Page_{i}", f"Albert Einstein {i}") for i in range(8)]
    services = replace(
        sample_services(sources=TWIN_SOURCES), index=cast(Any, FixedOracle(many)), speller=_Rare()
    )
    with local_client(create_app(services)) as c:
        assert _search(c, q="Albert Einstien").did_you_mean == "Albert Einstein"
    titled = [_h("wiki", "en/Albert_Einstien", "Albert Einstien"), *many]
    services = replace(services, index=cast(Any, FixedOracle(titled)))
    with local_client(create_app(services)) as c:
        assert _search(c, q="Albert Einstien").did_you_mean is None  # a page has that name


def test_the_corrected_title_leads_every_page_alike() -> None:
    french = [_h("wiki", f"fr/Page_{i}", f"Newton {i}", "fr") for i in range(6)]
    english = _h("wiki", "en/Isaac_Newton", "Isaac Newton")
    oracle = FixedOracle(french, pin=english)

    class _Newton(_Speller):
        def correct(self, query: str, *, only: Iterable[str] | None = None) -> str | None:
            return "Isaac Newton" if "Newtonn" in query else None

        def unknown(self, query: str) -> list[str]:
            return ["newtonn"]

    services = replace(sample_services(sources=TWIN_SOURCES), index=cast(Any, oracle), speller=_Newton())
    with local_client(create_app(services)) as c:
        first = _search(c, q="Isaac Newtonn", limit=3)
        second = _search(c, q="Isaac Newtonn", limit=3, offset=3)
    assert first.hits[0].doc_id == "wsp:wiki/en/Isaac_Newton"
    assert first.total == second.total == 7
    paged = [h.doc_id for h in (*first.hits, *second.hits)]
    assert len(set(paged)) == 6  # one list: the pin is not shown twice nor a hit skipped
    assert oracle.asked[0] == "Isaac Newton"
