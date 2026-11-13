"""``low_confidence`` and the search log tell the truth, the slow-search line keeps both retrievals'
stages, and a retry shares the run in flight (2026-11-10 trawl: search-index-15, -07,
api-perf-security-06, with the changes its review required)."""

import json
import logging
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, cast

import httpx
import pytest

from wsp_api import create_app, searching
from wsp_api.gaps import JsonlQueryLog
from wsp_api.schemas import SearchResults
from wsp_api.services import ApiSettings
from wsp_api.testing import local_client
from wsp_api.testing.browse import browse_services
from wsp_core.model import SearchHit


@dataclass(frozen=True)
class _Result:
    hits: tuple[SearchHit, ...]
    low_confidence: bool = False
    scope: object = None
    exhaustive: bool | None = True
    unmatched: bool = False
    doubted: bool = False
    timings_ms: dict[str, float] = field(default_factory=dict[str, float])


class _Oracle:
    """An Oracle answering every query with ``hits``; ``doubt``: as for a query no document matches."""

    def __init__(
        self,
        hits: Sequence[SearchHit],
        *,
        doubt: bool = False,
        pin: SearchHit | None = None,
        reranker_doubts: bool = False,
    ) -> None:
        self.hits, self.doubt, self.pin, self.reranker_doubts = tuple(hits), doubt, pin, reranker_doubts
        self.calls: list[tuple[str, ...] | None] = []
        self.started, self.release = threading.Event(), threading.Event()
        self.release.set()

    def result(self, query: str, *, sources: Sequence[str] | None = None, depth: int = 0) -> _Result:
        self.calls.append(None if sources is None else tuple(sources))
        self.started.set()
        assert self.release.wait(10)
        stages = (
            {"total": 7.0, "rerank": 3.0, "cached": 1.0} if sources else {"total": 15800.0, "rerank": 9.0}
        )
        return _Result(
            self.hits, self.doubt, unmatched=self.doubt, doubted=self.reranker_doubts, timings_ms=stages
        )

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
        return self.pin


class _Speller:
    def correct(self, query: str, *, only: object = None) -> str | None:
        return None

    def unknown(self, query: str) -> list[str]:
        return []


def _held(n: int = 3) -> list[SearchHit]:
    """Hits of the browse library (so their kinds and pages exist)."""
    index = browse_services().index
    return list(index.search("the", limit=n))


def _client(index: object, **kw: Any) -> httpx.Client:
    services = replace(browse_services(), index=cast(Any, index), **kw)
    return local_client(create_app(services))


def _search(c: httpx.Client, **params: object) -> SearchResults:
    r = c.get("/v1/search", params=cast(Any, params))
    assert r.status_code == 200, r.text
    return SearchResults.model_validate(r.json())


def _lines(path: Path) -> list[dict[str, object]]:
    return [json.loads(x) for x in path.read_text().splitlines()]


def test_nonsense_is_low_confidence_and_marked_in_the_search_log(tmp_path: Path) -> None:
    log = JsonlQueryLog(tmp_path / "queries.jsonl")
    with _client(_Oracle(_held(), doubt=True), queries=log) as c:
        res = _search(c, q="qwertyuiopasdf")
    assert res.low_confidence
    assert res.hits  # the neighbours are still shown
    assert res.total == len(res.hits)  # the size of the list shown, as ever
    assert _lines(log.path) == [
        {
            "at": _lines(log.path)[0]["at"],
            "q": "qwertyuiopasdf",
            "total": res.total,
            "low": True,
            "unmatched": True,
        }
    ]


def test_a_term_the_library_lacks_that_the_reranker_doubts_too_is_still_a_gap(tmp_path: Path) -> None:
    # No word matched AND the reranker's best score is under the bar: this was low, and a gap for the
    # miner, before the new rule existed. It is not marked, so the miner still counts it.
    log = JsonlQueryLog(tmp_path / "queries.jsonl")
    with _client(_Oracle(_held(), doubt=True, reranker_doubts=True), queries=log) as c:
        assert _search(c, q="zorglubium").low_confidence
    (line,) = _lines(log.path)
    assert line["low"] is True
    assert "unmatched" not in line


def test_an_ordinary_search_is_logged_as_before(tmp_path: Path) -> None:
    log = JsonlQueryLog(tmp_path / "queries.jsonl")
    with _client(_Oracle(_held()), queries=log) as c:
        assert not _search(c, q="the").low_confidence
    assert set(_lines(log.path)[0]) == {"at", "q", "total", "low"}  # no new key on ordinary lines


def test_low_confidence_never_stands_over_a_page_the_api_pinned(tmp_path: Path) -> None:
    # A doc id matches no word of any document: the retrieval doubts, the API answers. The Navigator
    # would say "the library may not hold a good answer" above the very page asked for.
    held = _held()
    target = held[0].doc_id
    log = JsonlQueryLog(tmp_path / "queries.jsonl")
    with _client(_Oracle(held[1:], doubt=True), queries=log) as c:
        res = _search(c, q=str(target))
        assert res.navigate_to == target
        assert res.hits[0].doc_id == target
        assert not res.low_confidence
        # the same retrieval with nothing pinned is still doubted
        assert _search(c, q="qwertyuiopasdf").low_confidence
    first, second = _lines(log.path)
    assert (first["low"], "unmatched" in first) == (False, False)  # answered: neither low nor a gap
    assert (second["low"], second.get("unmatched")) == (True, True)


def test_low_confidence_never_stands_over_the_corrected_title(tmp_path: Path) -> None:
    held = _held()
    oracle = _Oracle(held[1:], doubt=True, pin=held[0])
    with _client(oracle, speller=cast(Any, _Speller())) as c:
        res = _search(c, q="qwertyuiopasdf")
    assert res.hits[0].doc_id == held[0].doc_id
    assert not res.low_confidence


def test_a_pin_outside_the_kind_asked_does_not_lift_the_doubt() -> None:
    held = _held(20)
    with _client(_Oracle(held)) as c:
        kinds = {h.doc_id: h.kind for h in _search(c, q="the", limit=20).hits}
    target = held[0].doc_id
    other = next(k for k in kinds.values() if k != kinds[target])
    with _client(_Oracle([h for h in held if kinds[h.doc_id] == other], doubt=True)) as c:
        res = _search(c, q=str(target), kind=other)
    assert target not in [h.doc_id for h in res.hits]  # the pin is not of this kind: not placed
    assert res.low_confidence


def test_a_kind_search_reports_the_stages_of_both_retrievals(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A kind-filtered search whose mixed retrieval took 15.8 s logged the kind retrieval's stages
    # alone: the second assignment replaced the first.
    held = _held(20)
    with _client(_Oracle(held)) as c:
        kind = _search(c, q="the").hits[0].kind
    oracle = _Oracle(held)
    with _client(oracle) as c, caplog.at_level(logging.WARNING, logger="wsp.api.search"):
        monkeypatch.setattr(searching, "SLOW_SEARCH_MS", 0.0)  # every search is logged as slow
        res = _search(c, q="the", kind=kind)
        plain = _search(c, q="the")
    assert oracle.calls[0] is None
    assert oracle.calls[1] is not None  # the mixed retrieval, then the kind's
    t = res.timings_ms
    assert (t["oracle.total"], t["oracle.rerank"]) == (15800.0, 9.0)  # the mixed retrieval's
    assert (t["oracle.kind.total"], t["oracle.kind.rerank"]) == (7.0, 3.0)  # the kind's
    assert t["oracle.kind.cached"] == 1.0
    assert "oracle.cached" not in t
    assert {"retrieval", "kind_retrieval", "did_you_mean"} <= set(t)
    assert not [k for k in plain.timings_ms if k.startswith("oracle.kind.")]  # no kind: one retrieval
    line = next(r.getMessage() for r in caplog.records if "slow search" in r.getMessage())
    assert "'oracle.total': 15800.0" in line
    assert "'oracle.kind.total': 7.0" in line


def test_a_retry_while_the_first_search_runs_shares_its_retrieval() -> None:
    # A request that times out leaves its run going; the retry waits on that run, it does not start
    # a second one beside it (search-index-07: the sharing exists; this is the missing proof).
    oracle = _Oracle(_held())
    oracle.release.clear()
    settings = ApiSettings(search_timeout_seconds=0.05)
    services = replace(browse_services(), index=cast(Any, oracle))
    with local_client(create_app(services, settings)) as c:
        first = c.get("/v1/search", params={"q": "the"})
        assert first.status_code == 503  # timed out; the run goes on
        assert oracle.started.is_set()
        answers: list[httpx.Response] = []
        # the retry asks for another page size: the retrieval is the same
        retry = threading.Thread(
            target=lambda: answers.append(c.get("/v1/search", params={"q": "the", "limit": 3}))
        )
        retry.start()
        retry.join(10)  # it times out like the first, its worker waiting on the run in flight
        assert [a.status_code for a in answers] == [503]
        assert oracle.release.wait(0) is False
        oracle.release.set()
        assert len(oracle.calls) == 1  # one run for both requests
        done = c.get("/v1/search", params={"q": "the", "limit": 3})  # the same fake: no cache of its own
        assert done.status_code == 200
