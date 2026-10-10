"""Sync and async clients against the real app on in-memory fakes (in-process, no sockets)."""

import asyncio
from collections.abc import Iterator

import pytest

from wsp_api import create_app
from wsp_api.testing import local_async_client, local_client
from wsp_api.testing.browse import InMemoryGapLog, browse_services
from wsp_api.testing.fakes import SAMPLE_GEN_1, SAMPLE_GEN_2, sample_services
from wsp_sdk import AsyncWspClient, NotConfiguredError, NotFoundError, WspApiError, WspClient
from wsp_sdk.models import DocKind

TURING = "wsp:wikipedia-en-zim/A/Alan_Turing"


@pytest.fixture
def wsp() -> Iterator[WspClient]:
    app = create_app(sample_services())
    with local_client(app) as http, WspClient(http=http) as client:
        yield client


def test_health_and_sources(wsp: WspClient) -> None:
    assert wsp.health().sources == 3
    assert [s.id for s in wsp.iter_sources(page_size=1)] == ["gutenberg", "rfc", "wikipedia-en-zim"]
    assert wsp.source("rfc").current_generation == SAMPLE_GEN_1
    assert [g.generation_id for g in wsp.iter_generations("rfc", page_size=1)] == [SAMPLE_GEN_2, SAMPLE_GEN_1]


def test_documents(wsp: WspClient) -> None:
    doc = wsp.document(TURING)
    assert doc.title == "Alan Turing"
    assert wsp.document(TURING, at=SAMPLE_GEN_1).content_hash != doc.content_hash
    assert [v.generation_id for v in wsp.iter_history(TURING, page_size=1)] == [SAMPLE_GEN_1, SAMPLE_GEN_2]


def test_errors_are_typed(wsp: WspClient) -> None:
    with pytest.raises(NotFoundError) as nf:
        wsp.document("wsp:rfc/0")
    assert nf.value.code == "document_not_found"
    assert nf.value.status == 404
    with pytest.raises(WspApiError) as bad:
        wsp.document("rfc-9110")
    assert bad.value.code == "invalid_doc_id"


def test_search_iterates_pages(wsp: WspClient) -> None:
    page = wsp.search("the", limit=1)
    assert page.next_offset == 1
    titles = [h.title for h in wsp.iter_search("the", page_size=1)]
    assert len(titles) == len(set(titles)) >= 2
    assert len(list(wsp.iter_search("the", page_size=1, max_results=1))) == 1
    assert {h.source_id for h in wsp.iter_search("http", sources=["rfc"])} == {"rfc"}
    assert wsp.suggest("frank").suggestions == ["Frankenstein; Or, The Modern Prometheus"]


def test_change_feed_iteration_and_stream(wsp: WspClient) -> None:
    pages = list(wsp.iter_changes(page_size=2))
    assert [len(p.items) for p in pages] == [2, 2, 1]
    assert wsp.changes(since=pages[-1].cursor).items == []
    streamed = list(wsp.stream_changes(since="3", max_seconds=0.05))
    assert [s.change.kind for s in streamed] == ["added", "modified"]
    assert [s.cursor for s in streamed] == [None, "5"]
    with pytest.raises(WspApiError) as exc:
        list(wsp.stream_changes(since="bogus"))
    assert exc.value.code == "invalid_cursor"


def test_system_summarize_resolve(wsp: WspClient) -> None:
    status = wsp.system(ledger_tail=1)
    assert len(status.ledger_tail) == 1
    assert status.ledger_ok
    events = list(wsp.summarize("who", [TURING]))
    cites = [e for e in events if e.type == "citation"]
    assert [c.doc_id for c in cites] == [TURING]  # type: ignore[union-attr]
    assert events[0].type == "token"
    assert events[-1].type == "done"
    assert wsp.resolve("wsp://rfc/9110").href == "/v1/documents/wsp%3Arfc%2F9110"
    assert wsp.resolve("wsp://search?q=turing").href == "/v1/search?q=turing"


def test_navigator_resources() -> None:
    services = browse_services()
    with local_client(create_app(services)) as http, WspClient(http=http) as wsp:
        assert wsp.portal().sites
        assert wsp.directory("encyclopedia").children[0].path == "encyclopedia/wikipedia-en-zim"
        assert wsp.random(source="rfc").doc_id == "wsp:rfc/9110"
        assert wsp.recent_changes(limit=1).items[0].title == "Enigma machine"
        assert {h.source_id for h in wsp.search("the", kind=DocKind.PAPERS).hits} == {"rfc"}
        wsp.report_gap("https://example.org/", from_doc_id=TURING)
    assert isinstance(services.gaps, InMemoryGapLog)
    assert services.gaps.gaps[0].href == "https://example.org/"


def test_doc_ids_with_reserved_characters_are_escaped(wsp: WspClient) -> None:
    with pytest.raises(NotFoundError) as exc:
        wsp.document("wsp:rfc/what?is#this 100%")
    assert "what?is#this 100%" in exc.value.message


def test_summarize_not_configured() -> None:
    app = create_app(sample_services(with_generator=False))
    with local_client(app) as http, WspClient(http=http) as wsp, pytest.raises(NotConfiguredError):
        list(wsp.summarize("q", [TURING]))


def test_async_client_mirrors_sync() -> None:
    async def run() -> None:
        app = create_app(sample_services())
        async with local_async_client(app) as http, AsyncWspClient(http=http) as wsp:
            assert (await wsp.health()).sources == 3
            assert [s.id async for s in wsp.iter_sources(page_size=2)] == [
                "gutenberg",
                "rfc",
                "wikipedia-en-zim",
            ]
            assert (await wsp.source("rfc")).id == "rfc"
            assert [g.generation_id async for g in wsp.iter_generations("rfc")] == [
                SAMPLE_GEN_2,
                SAMPLE_GEN_1,
            ]
            assert (await wsp.document(TURING)).native_id == "A/Alan_Turing"
            assert len([v async for v in wsp.iter_history(TURING)]) == 2
            assert [h.doc_id async for h in wsp.iter_search("computer")] == [TURING]
            assert (await wsp.suggest("al")).suggestions == ["Alan Turing"]
            assert [len(p.items) async for p in wsp.iter_changes(page_size=4)] == [4, 1]
            streamed = [s async for s in wsp.stream_changes(since="4", max_seconds=0.05)]
            assert [s.cursor for s in streamed] == ["5"]
            assert (await wsp.system()).ledger_ok
            assert [c.type async for c in wsp.summarize("q", [TURING])][-1] == "done"
            assert (await wsp.resolve("home")).kind == "home"
            assert (await wsp.portal()).featured is not None
            assert (await wsp.directory()).name == "Top"
            assert (await wsp.random(seed=1)).doc_id
            assert (await wsp.recent_changes(limit=1)).cursor == "5"
            with pytest.raises(NotFoundError):
                await wsp.source("nope")

    asyncio.run(run())


def test_owned_clients_close() -> None:
    WspClient("http://127.0.0.1:1").close()
    asyncio.run(AsyncWspClient("http://127.0.0.1:1").aclose())
