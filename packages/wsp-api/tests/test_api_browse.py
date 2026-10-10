"""Navigator-facing resources: portal, directory, random, recent changes, assets, gaps, rich documents."""

import json
from collections.abc import Iterator, Sequence
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pytest

from wsp_api import create_app
from wsp_api.addresses import document_href
from wsp_api.gaps import JsonlGapLog
from wsp_api.htmlrewrite import rewrite_html
from wsp_api.schemas import (
    DirectoryPage,
    DocRef,
    Document,
    HistoryPage,
    Portal,
    RecentChanges,
    SearchResults,
    SummaryCitation,
    SummaryToken,
)
from wsp_api.summaries import CitationSplitter
from wsp_api.testing import local_client
from wsp_api.testing.browse import ENIGMA, PIXEL_PNG, CategorizedCatalog, InMemoryGapLog, browse_services
from wsp_api.testing.fakes import SAMPLE_GEN_2, sample_services
from wsp_core.ids import DocId, make_doc_id
from wsp_core.model import DayEvent, Gap

TURING = "wsp:wikipedia-en-zim/A/Alan_Turing"


@pytest.fixture
def browse() -> Iterator[httpx.Client]:
    with local_client(create_app(browse_services())) as c:
        yield c


# --- documents ---------------------------------------------------------------------------------


def test_html_document_is_rewritten_into_the_offline_web(browse: httpx.Client) -> None:
    doc = Document.model_validate(browse.get(document_href(ENIGMA)).json())
    assert doc.format == "html"
    assert doc.html is not None
    assert 'href="wsp://wikipedia-en-zim/A/Alan_Turing"' in doc.html
    assert 'href="wsp://wikipedia-en-zim/A/Bombe#history"' in doc.html
    assert 'href="https://example.org/enigma"' in doc.html  # external: left for the client to flag
    assert f'src="/v1/assets/wikipedia-en-zim/{SAMPLE_GEN_2}/I/enigma.png"' in doc.html
    assert "srcset" not in doc.html
    assert doc.generation_id == SAMPLE_GEN_2
    assert doc.source_name == "Wikipedia English (sample)"
    assert doc.kind == "encyclopedia"
    assert [(link.href, link.target) for link in doc.links] == [
        (TURING, TURING),
        ("wsp:wikipedia-en-zim/A/Bombe", None),
        ("https://example.org/enigma", None),
    ]


def test_document_vault_location_and_history_flag(browse: httpx.Client) -> None:
    doc = Document.model_validate(browse.get(document_href(TURING)).json())
    assert doc.has_history
    assert doc.format == "text"
    assert doc.vault is not None
    assert doc.vault.generation_id == SAMPLE_GEN_2
    assert doc.vault.path == f"wikipedia-en-zim/{SAMPLE_GEN_2}"  # relative, never an absolute host path
    assert doc.vault.manifest == f"wikipedia-en-zim/{SAMPLE_GEN_2}/MANIFEST.json"
    rfc = Document.model_validate(browse.get(document_href("wsp:rfc/9110")).json())
    assert not rfc.has_history


def test_history_carries_revision_and_kind(browse: httpx.Client) -> None:
    hist = HistoryPage.model_validate(browse.get(document_href(TURING) + "/history").json())
    assert [(v.kind, v.revision) for v in hist.items] == [("added", None), ("modified", "2")]


def test_native_id_ending_in_history_does_not_collide() -> None:
    services = sample_services()
    base = services.documents.get(make_doc_id("rfc", "9110"))
    assert base is not None
    odd = replace(base, doc_id=make_doc_id("rfc", "notes/history"), native_id="notes/history")
    services.documents.put(SAMPLE_GEN_2, odd)  # type: ignore[attr-defined]
    with local_client(create_app(services)) as c:
        doc = c.get(document_href(odd.doc_id))
        assert doc.status_code == 200, doc.text
        assert doc.json()["native_id"] == "notes/history"
        hist = c.get(document_href(odd.doc_id) + "/history")
        assert hist.json()["doc_id"] == "wsp:rfc/notes/history"


def test_rewrite_keeps_markup_and_entities() -> None:
    src = '<p class="x">A &amp; B &#169;<!-- c --><a href="./Foo%20Bar?x#y">f</a><br/></p>'
    out = rewrite_html(src, source_id="wiki", generation_id="g", native_id="A/Page")
    assert out.startswith('<p class="x">A &amp; B &#169;<!-- c -->')
    assert '<a href="wsp://wiki/A/Foo%20Bar#y">' in out
    assert out.endswith("<br/></p>")
    assert (
        rewrite_html('<a href="../../x">', source_id="w", generation_id="g", native_id="A")
        == '<a href="../../x">'
    )


# --- assets ------------------------------------------------------------------------------------


def test_asset_route_serves_images_only(browse: httpx.Client) -> None:
    r = browse.get(f"/v1/assets/wikipedia-en-zim/{SAMPLE_GEN_2}/I/enigma.png")
    assert r.status_code == 200
    assert r.content == PIXEL_PNG
    assert r.headers["content-type"] == "image/png"
    assert "immutable" in r.headers["cache-control"]
    csp = r.headers.get_list("content-security-policy")
    assert csp[0].startswith("default-src 'self'")
    assert any("sandbox" in p for p in csp)
    for path in ("I/evil.js", "A/Enigma_machine", "I/missing.png"):
        bad = browse.get(f"/v1/assets/wikipedia-en-zim/{SAMPLE_GEN_2}/{path}")
        assert bad.status_code == 404
        assert bad.json()["error"]["code"] == "asset_not_found"
    assert browse.get(f"/v1/assets/nope/{SAMPLE_GEN_2}/x.png").json()["error"]["code"] == "source_not_found"


# --- search ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("q", "target"),
    [
        ("rfc 9110", "wsp:rfc/9110"),
        ("RFC9110", "wsp:rfc/9110"),
        ("wikipedia Alan Turing", TURING),
        ("wiki alan turing", TURING),
        ("gutenberg 84", "wsp:gutenberg/84"),
        ("wsp://rfc/9110", "wsp:rfc/9110"),
        ("rfc about http caching", None),
        ("Alan Turing", None),  # no site named: a results page, not a jump
        ("rfc 1", None),
    ],
)
def test_navigational_queries(browse: httpx.Client, q: str, target: str | None) -> None:
    res = SearchResults.model_validate(browse.get("/v1/search", params={"q": q}).json())
    assert res.navigate_to == target
    assert (res.intent == "navigate") == (target is not None)


def test_search_hits_are_enriched_with_highlights_and_facets(browse: httpx.Client) -> None:
    res = SearchResults.model_validate(browse.get("/v1/search", params={"q": "vaccines immune"}).json())
    hit = res.hits[0]
    assert hit.source_name
    assert hit.kind == "encyclopedia"
    assert hit.license == "CC-BY-SA-4.0"
    marked = [hit.snippet[a:b] for a, b in hit.highlights]
    assert {m.casefold() for m in marked} >= {"vaccines", "immune"}
    assert res.total >= 1
    assert [f.kind for f in res.facets] == ["encyclopedia"]


def test_search_kind_filter_keeps_facets_of_all_kinds(browse: httpx.Client) -> None:
    res = SearchResults.model_validate(browse.get("/v1/search", params={"q": "the", "kind": "papers"}).json())
    assert {h.source_id for h in res.hits} == {"rfc"}
    assert len(res.facets) > 1
    none = SearchResults.model_validate(browse.get("/v1/search", params={"q": "the", "kind": "maps"}).json())
    assert none.hits == []
    assert none.low_confidence


def test_highlights_are_code_points(browse: httpx.Client) -> None:
    from wsp_api.search import highlights

    snippet = "😀 Vaccines and vaccine"
    assert [snippet[a:b] for a, b in highlights(snippet, ["vaccine"])] == ["Vaccines", "vaccine"]
    assert highlights(snippet, ["vaccine"])[0] == (2, 10)


# --- portal, directory, random, recent -----------------------------------------------------------


def test_portal(browse: httpx.Client) -> None:
    portal = Portal.model_validate(browse.get("/v1/portal").json())
    assert portal.featured is not None
    assert portal.featured.doc_id.startswith("wsp:wikipedia-en-zim/")
    assert portal.featured.blurb
    assert portal.newest[0].doc_id == ENIGMA
    assert portal.newest[0].title == "Enigma machine"
    assert portal.random is not None
    assert {s.id for s in portal.sites} == {"gutenberg", "rfc", "wikipedia-en-zim"}
    assert set(portal.random_sources) == {"gutenberg", "rfc", "wikipedia-en-zim"}
    again = Portal.model_validate(browse.get("/v1/portal").json())
    assert again.featured == portal.featured  # deterministic for the day


class _PortalCatalog(CategorizedCatalog):
    """A catalog with precomputed start-page picks (``wsp_core.ports.PortalSource``)."""

    def featured(self, day: date) -> DocId | None:
        return DocId(ENIGMA)

    def on_this_day(self, day: date, *, limit: int = 5) -> Sequence[DayEvent]:
        return [DayEvent(1912, "Turing is born (not really today).", DocId(TURING))][:limit]


def test_portal_uses_precomputed_picks() -> None:
    base = browse_services()
    assert isinstance(base.catalog, CategorizedCatalog)
    services = replace(base, catalog=_PortalCatalog(base.changes, base.catalog.category_map))
    with local_client(create_app(services)) as c:
        portal = Portal.model_validate(c.get("/v1/portal").json())
    assert portal.featured is not None
    assert portal.featured.doc_id == ENIGMA
    assert [(e.year, e.doc_id) for e in portal.on_this_day] == [(1912, TURING)]


def test_directory_tree(browse: httpx.Client) -> None:
    top = DirectoryPage.model_validate(browse.get("/v1/directory").json())
    assert [c.path for c in top.children] == ["books", "encyclopedia", "standards"]
    enc = DirectoryPage.model_validate(browse.get("/v1/directory", params={"path": "encyclopedia"}).json())
    assert [(c.path, c.count) for c in enc.children] == [("encyclopedia/wikipedia-en-zim", 3)]
    site = DirectoryPage.model_validate(
        browse.get("/v1/directory", params={"path": "encyclopedia/wikipedia-en-zim"}).json()
    )
    assert [c.name for c in site.children] == ["Computing", "Medicine"]
    assert site.entries[0].address == "wsp://wikipedia-en-zim"
    cat = DirectoryPage.model_validate(
        browse.get("/v1/directory", params={"path": "encyclopedia/wikipedia-en-zim/computing"}).json()
    )
    assert [e.title for e in cat.entries] == ["Alan Turing", "Enigma machine"]
    assert [t.name for t in cat.trail] == ["Encyclopedia", "Wikipedia English (sample)", "Computing"]
    for bad in ("nope", "books/rfc", "encyclopedia/wikipedia-en-zim/nope", "a/b/c/d"):
        r = browse.get("/v1/directory", params={"path": bad})
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "category_not_found"


def test_random(browse: httpx.Client) -> None:
    one = DocRef.model_validate(browse.get("/v1/random", params={"seed": 7}).json())
    assert one == DocRef.model_validate(browse.get("/v1/random", params={"seed": 7}).json())
    rfc = DocRef.model_validate(browse.get("/v1/random", params={"source": "rfc"}).json())
    assert rfc.doc_id == "wsp:rfc/9110"
    assert browse.get("/v1/random", params={"source": "nope"}).status_code == 404


def test_random_from_empty_library_is_404() -> None:
    services = sample_services()
    empty = replace(services, changes=type(services.changes)())
    with local_client(create_app(empty)) as c:
        r = c.get("/v1/random")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "empty"


def test_recent_changes_newest_first_with_resumable_cursor(browse: httpx.Client) -> None:
    recent = RecentChanges.model_validate(browse.get("/v1/changes/recent", params={"limit": 2}).json())
    assert [c.doc_id for c in recent.items] == [ENIGMA, TURING]
    assert recent.items[0].address == "wsp://wikipedia-en-zim/A/Enigma_machine"
    assert recent.cursor is not None
    tail = browse.get("/v1/changes", params={"since": recent.cursor}).json()
    assert tail["items"] == []
    only_rfc = RecentChanges.model_validate(browse.get("/v1/changes/recent", params={"source": "rfc"}).json())
    assert {c.source_id for c in only_rfc.items} == {"rfc"}


# --- gaps --------------------------------------------------------------------------------------


def test_gap_reports_are_recorded() -> None:
    services = browse_services()
    with local_client(create_app(services)) as c:
        r = c.post("/v1/gaps", json={"href": "https://example.org/x", "from_doc_id": TURING})
    assert r.status_code == 204
    gaps = services.gaps
    assert isinstance(gaps, InMemoryGapLog)
    assert [(g.href, g.from_doc_id) for g in gaps.gaps] == [("https://example.org/x", TURING)]


def test_gap_reports_need_a_log_and_a_local_client() -> None:
    with local_client(create_app(sample_services())) as c:
        r = c.post("/v1/gaps", json={"href": "x"})
    assert (r.status_code, r.json()["error"]["code"]) == (501, "not_configured")
    transport = httpx.ASGITransport(app=create_app(browse_services()), client=("192.0.2.5", 1))
    remote = httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8737")

    import asyncio

    async def post() -> httpx.Response:
        async with remote:
            return await remote.post("/v1/gaps", json={"href": "x"})

    r = asyncio.run(post())
    assert (r.status_code, r.json()["error"]["code"]) == (403, "local_only")


def test_jsonl_gap_log(tmp_path: Path) -> None:
    log = JsonlGapLog(tmp_path / "index" / "gaps.jsonl")
    log.record(Gap(at=datetime(2026, 10, 9, tzinfo=UTC), href="https://example.org/ü"))
    log.record(Gap(at=datetime(2026, 10, 9, tzinfo=UTC), href="wsp://rfc/1", from_doc_id="wsp:rfc/2"))
    lines = [json.loads(x) for x in log.path.read_text(encoding="utf-8").splitlines()]
    assert lines[0]["href"] == "https://example.org/ü"
    assert lines[1]["from_doc_id"] == "wsp:rfc/2"


# --- summaries ---------------------------------------------------------------------------------


def test_citation_markers_split_across_chunks() -> None:
    cite = SummaryCitation(index=1, doc_id="wsp:a/b", title="B", address="wsp://a/b")
    sp = CitationSplitter({1: cite})
    events = sp.feed("Cats purr [") + sp.feed("1] and [9] dogs [") + sp.flush()
    assert events == [
        SummaryToken(text="Cats purr "),
        cite,
        SummaryToken(text=" and [9] dogs "),
        SummaryToken(text="["),
    ]
