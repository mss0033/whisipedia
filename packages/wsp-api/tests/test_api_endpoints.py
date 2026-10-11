from datetime import UTC, datetime

import httpx

from wsp_api import Services, create_app
from wsp_api.addresses import document_href
from wsp_api.schemas import (
    ChangePage,
    Document,
    GenerationPage,
    Health,
    HistoryPage,
    SearchResults,
    SourceInfo,
    SourcePage,
    Suggestions,
    SystemStatus,
)
from wsp_api.testing import local_client
from wsp_api.testing.fakes import SAMPLE_GEN_1, SAMPLE_GEN_2, sample_services

TURING = "wsp:wikipedia-en-zim/A/Alan_Turing"


def test_health(client: httpx.Client) -> None:
    body = Health.model_validate(client.get("/v1/health").json())
    assert body.status == "ok"
    assert body.sources == 3


def test_sources_paginate_with_opaque_cursor(client: httpx.Client) -> None:
    first = SourcePage.model_validate(client.get("/v1/sources", params={"limit": 2}).json())
    assert [s.id for s in first.items] == ["gutenberg", "rfc"]
    assert first.next_cursor is not None
    second = SourcePage.model_validate(
        client.get("/v1/sources", params={"limit": 2, "cursor": first.next_cursor}).json()
    )
    assert [s.id for s in second.items] == ["wikipedia-en-zim"]
    assert second.next_cursor is None


def test_source_detail_reports_current_generation(client: httpx.Client) -> None:
    src = SourceInfo.model_validate(client.get("/v1/sources/wikipedia-en-zim").json())
    assert src.current_generation == SAMPLE_GEN_2
    assert src.address == "wsp://wikipedia-en-zim"
    assert src.tier == "T1"


def test_generations_newest_first_and_current_flag(client: httpx.Client) -> None:
    page = GenerationPage.model_validate(client.get("/v1/sources/rfc/generations").json())
    assert [(g.generation_id, g.status, g.current) for g in page.items] == [
        (SAMPLE_GEN_2, "degraded", False),
        (SAMPLE_GEN_1, "sealed", True),
    ]


def test_document_current_and_at_generation(client: httpx.Client) -> None:
    now = Document.model_validate(client.get(document_href(TURING)).json())
    old = Document.model_validate(client.get(document_href(TURING), params={"at": SAMPLE_GEN_1}).json())
    assert now.native_id == "A/Alan_Turing"
    assert now.address == "wsp://wikipedia-en-zim/A/Alan_Turing"
    assert "father of theoretical computer science" in now.text
    assert "father" not in old.text
    assert now.content_hash != old.content_hash


def test_document_before_first_generation_is_404(client: httpx.Client) -> None:
    r = client.get(document_href(TURING), params={"at": "2000-01-01"})
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "document_not_found"


def test_document_history_oldest_first(client: httpx.Client) -> None:
    hist = HistoryPage.model_validate(client.get(document_href(TURING) + "/history").json())
    assert [v.generation_id for v in hist.items] == [SAMPLE_GEN_1, SAMPLE_GEN_2]
    assert hist.doc_id == TURING


def test_doc_id_with_reserved_characters_round_trips(services: Services) -> None:
    from dataclasses import replace

    from wsp_core.ids import make_doc_id

    store = services.documents
    base = store.get(make_doc_id("rfc", "9110"))
    assert base is not None
    odd = replace(base, doc_id=make_doc_id("rfc", "a b?c#d%e"), native_id="a b?c#d%e")
    store.put(SAMPLE_GEN_2, odd)  # type: ignore[attr-defined]
    with local_client(create_app(services)) as c:
        r = c.get(document_href(odd.doc_id))
        assert r.status_code == 200, r.text
        assert r.json()["native_id"] == "a b?c#d%e"


def test_search_paging_and_source_filter(client: httpx.Client) -> None:
    res = SearchResults.model_validate(client.get("/v1/search", params={"q": "computer", "limit": 1}).json())
    assert [h.doc_id for h in res.hits] == [TURING]
    assert res.hits[0].address == "wsp://wikipedia-en-zim/A/Alan_Turing"
    only_rfc = SearchResults.model_validate(
        client.get("/v1/search", params={"q": "http caching", "sources": "rfc,gutenberg"}).json()
    )
    assert {h.source_id for h in only_rfc.hits} == {"rfc"}
    none = SearchResults.model_validate(
        client.get("/v1/search", params=[("q", "http"), ("sources", "gutenberg")]).json()
    )
    assert none.hits == []
    assert none.next_offset is None


def test_search_next_offset() -> None:
    with local_client(create_app(sample_services())) as c:
        page = SearchResults.model_validate(c.get("/v1/search", params={"q": "the", "limit": 1}).json())
        assert page.next_offset == 1


def test_suggest(client: httpx.Client) -> None:
    body = Suggestions.model_validate(client.get("/v1/suggest", params={"q": "al"}).json())
    assert body.suggestions == ["Alan Turing"]


def test_changes_cursor_continues(client: httpx.Client) -> None:
    first = ChangePage.model_validate(client.get("/v1/changes", params={"limit": 3}).json())
    assert [c.kind for c in first.items] == ["added"] * 3
    rest = ChangePage.model_validate(client.get("/v1/changes", params={"since": first.cursor}).json())
    assert [c.kind for c in rest.items] == ["added", "modified"]
    assert rest.items[-1].old_hash is not None
    empty = ChangePage.model_validate(client.get("/v1/changes", params={"since": rest.cursor}).json())
    assert empty.items == []
    assert empty.cursor == rest.cursor


def test_system_monitor(services: Services) -> None:
    clock_services = Services(
        index=services.index,
        documents=services.documents,
        changes=services.changes,
        vault=services.vault,
        ledger=services.ledger,
        sources=services.sources,
        clock=lambda: datetime(2026, 10, 9, tzinfo=UTC),
    )
    with local_client(create_app(clock_services)) as c:
        status = SystemStatus.model_validate(c.get("/v1/system", params={"ledger_tail": 2}).json())
    fresh = {f.source_id: f for f in status.freshness}
    assert not fresh["wikipedia-en-zim"].stale  # sealed 8 days ago, cadence P1M
    assert fresh["rfc"].stale  # current generation sealed 2026-09-01, older than P1M
    assert fresh["gutenberg"].stale is True
    assert [(d.source_id, d.generation_id) for d in status.degraded] == [("rfc", SAMPLE_GEN_2)]
    assert [e.kind for e in status.ledger_tail] == ["release.state", "scrub.mismatch"]
    assert status.ledger_ok


def test_system_flags_broken_ledger() -> None:
    services = sample_services()
    services.ledger.tamper(1, state="forged")  # type: ignore[attr-defined]
    with local_client(create_app(services)) as c:
        assert c.get("/v1/system").json()["ledger_ok"] is False


def test_summarize_is_gone(client: httpx.Client) -> None:
    """No generative features (ADR-0008): the old summary endpoint does not exist."""
    r = client.post("/v1/summarize", json={"query": "x", "doc_ids": [TURING]})
    assert r.status_code in (404, 405)
    assert "/v1/summarize" not in client.get("/v1/openapi.json").json()["paths"]
