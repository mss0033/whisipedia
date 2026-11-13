"""``GET /v1/search?lang=``: the reader's language reaches the index, and only when one is sent
(2026-11-10 trawl, search-index-16)."""

import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from typing import Any, cast

from wsp_api import create_app
from wsp_api.searching import Searcher, reader_key
from wsp_api.testing import local_client
from wsp_api.testing.fakes import sample_services
from wsp_api.views import Library
from wsp_core.ids import DocId
from wsp_core.model import SearchHit


@dataclass(frozen=True)
class _Result:
    hits: tuple[SearchHit, ...]
    low_confidence: bool = False
    scope: object = None
    exhaustive: bool | None = True


def _hits(lang: str) -> tuple[SearchHit, ...]:
    return tuple(
        SearchHit(DocId(f"wsp:wiki/{lang}/Page_{i}"), 1.0, f"Page {i}", "", "wiki", lang) for i in range(3)
    )


class _Reading:
    """An Oracle that records the reader's language of every call (``"-"``: none was passed)."""

    def __init__(
        self, started: threading.Event | None = None, release: threading.Event | None = None
    ) -> None:
        self.results: list[str] = []
        self.pins: list[str] = []
        self.started, self.release = started, release
        self.lock = threading.Lock()

    def result(
        self, query: str, *, sources: Sequence[str] | None = None, depth: int = 0, **said: str
    ) -> _Result:
        with self.lock:
            self.results.append(said.get("lang", "-"))
        if self.started is not None and self.release is not None:
            self.started.set()
            self.release.wait(5)
        return _Result(_hits("fr"))

    def search(
        self, query: str, *, limit: int = 10, offset: int = 0, sources: Sequence[str] | None = None
    ) -> Sequence[SearchHit]:
        return _hits("fr")[offset : offset + limit]

    def suggest(self, prefix: str, *, limit: int = 10) -> Sequence[str]:
        return []

    def correction_pin(
        self,
        query: str,
        hits: Sequence[SearchHit],
        corrected: Callable[[], str | None],
        sources: Sequence[str] | None = None,
        **said: str,
    ) -> SearchHit | None:
        self.pins.append(said.get("lang", "-"))
        return None

    @staticmethod
    def reader_key(lang: str | None) -> str:
        code = lang.lower().split("-")[0] if lang else "en"
        return code


class _Speller:
    def correct(self, query: str, *, only: object = None) -> str | None:
        return None

    def unknown(self, query: str) -> list[str]:
        return []


def test_search_takes_an_optional_reader_language() -> None:
    index = _Reading()
    services = replace(sample_services(), index=cast(Any, index), speller=cast(Any, _Speller()))
    with local_client(create_app(services)) as c:
        assert c.get("/v1/search", params={"q": "golem"}).status_code == 200
        assert c.get("/v1/search", params={"q": "golem", "lang": "de-AT"}).status_code == 200
        assert (
            c.get("/v1/search", params={"q": "golem", "lang": "pt_BR", "kind": "encyclopedia"}).status_code
            == 200
        )
        for bad in ("d", "de-", "de AT", "x" * 36, "12"):
            r = c.get("/v1/search", params={"q": "golem", "lang": bad})
            assert r.status_code == 422, bad
    # none sent: the index is called as before the parameter existed; sent: as it was sent, to both
    # retrievals of a kind search
    assert index.results[:2] == ["-", "de-AT"]
    assert set(index.results[2:]) == {"pt_BR"}
    assert len(index.results) == 4
    assert index.pins == ["-", "de-AT", "pt_BR"]


def test_a_plain_lexical_index_is_never_passed_the_language() -> None:
    # A plain lexical index (no ``result``) takes no ``lang``: a request that sends one still answers.
    with local_client(create_app(sample_services())) as c:
        assert c.get("/v1/search", params={"q": "turing", "lang": "de"}).status_code == 200


def test_runs_in_flight_are_shared_by_the_readers_the_index_serves_alike() -> None:
    # The in-flight key must take the tags the Oracle's cache takes for one reader, or "en" and no
    # lang would run the same retrieval twice side by side.
    started, release = threading.Event(), threading.Event()
    index = _Reading(started, release)
    searcher = Searcher(Library(replace(sample_services(), index=cast(Any, index))))
    out: list[int] = []
    first = threading.Thread(target=lambda: out.append(len(searcher.retrieval("golem", None).hits)))
    first.start()
    assert started.wait(5)
    same = threading.Thread(target=lambda: out.append(len(searcher.retrieval("golem", None, "en-GB").hits)))
    same.start()
    other = threading.Thread(target=lambda: out.append(len(searcher.retrieval("golem", None, "de").hits)))
    other.start()
    deadline = threading.Event()
    while len(index.results) < 2 and not deadline.wait(0.01):
        pass
    deadline.wait(0.3)  # time for the "en-GB" call to reach the run in flight (late: a spurious failure)
    release.set()
    for t in (first, same, other):
        t.join(5)
    assert out == [3, 3, 3]
    assert sorted(index.results) == ["-", "de"]  # "en-GB" waited on the run without a lang
    # an index that keeps no such key: the primary subtag, and nothing for no lang
    assert reader_key(object(), None) == ""
    assert reader_key(object(), "PT_br") == "pt"
