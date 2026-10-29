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
)
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
    # srcset candidates go through the asset route too (a raw srcset would bypass the rewrite)
    assert f'srcset="/v1/assets/wikipedia-en-zim/{SAMPLE_GEN_2}/I/enigma-2x.png 2x"' in doc.html
    assert doc.generation_id == SAMPLE_GEN_2
    assert doc.source_name == "Wikipedia English (sample)"
    assert doc.kind == "encyclopedia"
    assert [(link.href, link.target) for link in doc.links] == [
        (TURING, TURING),
        # A link inside the page's own archive is taken as held (no lookup per link: 1-5 s on a cold
        # archive); the Navigator's POST /v1/links/missing marks it when the archive lacks it.
        ("wsp:wikipedia-en-zim/A/Bombe", "wsp:wikipedia-en-zim/A/Bombe"),
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
    for path in ("I/evil.js", "I/s.css", "A/Enigma_machine", "I/missing.png"):
        bad = browse.get(f"/v1/assets/wikipedia-en-zim/{SAMPLE_GEN_2}/{path}")
        assert bad.status_code == 404
        assert bad.json()["error"]["code"] == "asset_not_found"
    assert browse.get(f"/v1/assets/nope/{SAMPLE_GEN_2}/x.png").json()["error"]["code"] == "source_not_found"


def test_asset_route_sends_an_ebook_as_a_download(browse: httpx.Client) -> None:
    """The Gutenberg ZIM's cover page offers each book's EPUB (2026-10-16 field exam: the link opened
    "not found"): it is saved, never shown in the page."""
    r = browse.get(f"/v1/assets/wikipedia-en-zim/{SAMPLE_GEN_2}/Enigma%27s%20Book.11.epub")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/epub+zip"
    assert r.headers["content-disposition"] == "attachment; filename*=UTF-8''Enigma%27s%20Book.11.epub"
    assert any("sandbox" in p for p in r.headers.get_list("content-security-policy"))


def test_asset_route_sends_any_other_file_a_page_links_to_as_a_download(browse: httpx.Client) -> None:
    """Sheet music, recordings and data files in an archive (2026-10-17 trawl: dead links)."""
    r = browse.get(f"/v1/assets/wikipedia-en-zim/{SAMPLE_GEN_2}/I/tune.mid")
    assert r.status_code == 200
    assert r.content == b"MThd"
    assert r.headers["content-disposition"] == "attachment; filename*=UTF-8''tune.mid"
    assert r.headers["x-content-type-options"] == "nosniff"


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
    for facet in res.facets:  # each facet's count is what filtering by it shows (E2E-5)
        only = SearchResults.model_validate(
            browse.get("/v1/search", params={"q": "the", "kind": facet.kind.value}).json()
        )
        assert only.total == facet.count
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
    sites_first = [c for c in portal.newest if not c.doc_id or c.address == f"wsp://{c.source_id}"]
    assert portal.newest[: len(sites_first)] == sites_first  # recently sealed sites lead What's New
    docs = portal.newest[len(sites_first) :]
    assert docs[0].doc_id == ENIGMA
    assert docs[0].title == "Enigma machine"
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


def test_rewrite_drops_inline_styles() -> None:
    page = (
        '<head><style>p { color: red }</style></head><p style="x" class="c">Hi <b STYLE=\'y\'>there</b></p>'
    )
    out = rewrite_html(page, source_id="s", generation_id="g", native_id="A/P")
    assert out == '<head></head><p class="c">Hi <b>there</b></p>'


def test_planned_sources_with_nothing_downloaded_are_not_listed() -> None:
    from wsp_api.testing.fakes import SAMPLE_SOURCES

    planned = replace(SAMPLE_SOURCES[0], id="arxiv", name="arXiv", domain=("papers",))
    services = replace(sample_services(), sources=(*SAMPLE_SOURCES, planned))
    with local_client(create_app(services)) as c:
        portal = Portal.model_validate(c.get("/v1/portal").json())
        assert "arxiv" not in {s.id for s in portal.sites}
        top = DirectoryPage.model_validate(c.get("/v1/directory").json())
        assert "papers" not in {ch.path for ch in top.children}  # a category holding only planned sites
        assert c.get("/v1/sources/arxiv").status_code == 200  # its page still opens and says so


def test_zim_listing_pages_are_never_results() -> None:
    from wsp_api.searching import Searcher
    from wsp_api.views import Library
    from wsp_core.model import SearchHit

    searcher = Searcher(Library(sample_services()))
    tag_page = SearchHit(DocId("wsp:wikipedia-en-zim/se_en_all/questions/tagged/first-aid_page=3"), 1.0, "x")
    question = SearchHit(DocId("wsp:wikipedia-en-zim/se_en_all/questions/13068/first-aid"), 1.0, "x")
    other_site = SearchHit(DocId("wsp:gutenberg/questions/tagged/x"), 1.0, "x")
    assert searcher.is_listing(tag_page)
    assert not searcher.is_listing(question)
    assert not searcher.is_listing(other_site)  # not a ZIM source: its ids mean something else


def test_a_search_says_where_its_time_went(browse: httpx.Client) -> None:
    res = SearchResults.model_validate(browse.get("/v1/search", params={"q": "vaccines immune"}).json())
    assert {"retrieval", "page_snippets", "hit_fields", "navigate", "did_you_mean"} <= set(res.timings_ms)
    assert all(v >= 0 for v in res.timings_ms.values())
    assert sum(v for k, v in res.timings_ms.items() if not k.startswith("oracle.")) <= res.took_ms + 1


def test_recent_changes_skip_uncaptured_player_stubs_and_carry_the_seal_date() -> None:
    """Field exam 2026-10-14: the newest What's New item was a web archive's empty YouTube placeholder,
    and the list had no dates."""
    from wsp_core.model import ChangeKind, ChangeRecord

    services = browse_services()
    stubs = [
        make_doc_id("wikipedia-en-zim", f"A/youtube.fuzzy.replayweb.page/embed/v{i}") for i in range(40)
    ]  # more than four times the page: the tail is read further back
    services.changes.publish([ChangeRecord(s, ChangeKind.ADDED, SAMPLE_GEN_2, None, "h") for s in stubs])
    with local_client(create_app(services)) as c:
        recent = RecentChanges.model_validate(c.get("/v1/changes/recent", params={"limit": 2}).json())
    assert [x.doc_id for x in recent.items] == [ENIGMA, make_doc_id("wikipedia-en-zim", "A/Alan_Turing")]
    sealed = {g.generation_id: g.sealed_at for g in services.vault.generations("wikipedia-en-zim")}
    assert recent.items[0].added_at == sealed[SAMPLE_GEN_2]


def test_a_document_asked_without_its_trailing_slash_is_answered() -> None:
    """`xkcd.com/` typed or linked relatively reaches the API as `xkcd.com`: no 404 to retry."""
    from wsp_api.testing.fakes import InMemoryDocumentStore
    from wsp_core.ids import content_hash
    from wsp_core.model import DocRecord

    services = browse_services()
    front = DocRecord(
        doc_id=make_doc_id("wikipedia-en-zim", "A/Front/"),
        source_id="wikipedia-en-zim",
        native_id="A/Front/",
        title="Front page",
        lang="en",
        license="CC-BY-SA-4.0",
        text="The newest comic.",
        content_hash=content_hash("The newest comic."),
    )
    assert isinstance(services.documents, InMemoryDocumentStore)
    services.documents.put(SAMPLE_GEN_2, front)
    with local_client(create_app(services)) as c:
        r = c.get(document_href(make_doc_id("wikipedia-en-zim", "A/Front")))
        missing = c.get(document_href(make_doc_id("wikipedia-en-zim", "A/Nowhere")))
    assert r.status_code == 200
    assert r.json()["doc_id"] == "wsp:wikipedia-en-zim/A/Front/"
    assert (missing.status_code, missing.json()["error"]["code"]) == (404, "document_not_found")


def test_outside_route_names_the_library_copy_or_says_none(browse: httpx.Client) -> None:
    """The "not in library" page asks first (2026-10-17): an RFC Editor link opens the RFC."""
    r = browse.get("/v1/outside", params={"url": "https://www.rfc-editor.org/rfc/rfc9110.html"})
    assert r.status_code == 200
    assert r.json() == {
        "url": "https://www.rfc-editor.org/rfc/rfc9110.html",
        "address": "wsp://rfc/rfc:RFC-9110",  # the document collection's key (rfc:RFC-<n>)
        "source_id": "rfc",
    }
    # the sample's Gutenberg source has no current generation: not held
    assert browse.get("/v1/outside", params={"url": "https://www.gutenberg.org/ebooks/84"}).status_code == 404
    miss = browse.get("/v1/outside", params={"url": "https://example.org/nothing"})
    assert miss.status_code == 404
    assert miss.json()["error"]["code"] == "not_held"


def test_an_rfc_asked_by_its_number_is_answered() -> None:
    """RFCs are keyed `rfc:RFC-9110`; links in an RFC's text and typed addresses say `wsp:rfc/9110`
    and every RFC-to-RFC link was a 404 (2026-10-17 trawl)."""
    from wsp_api.testing.fakes import InMemoryDocumentStore
    from wsp_core.ids import content_hash
    from wsp_core.model import DocRecord

    services = browse_services()
    rfc = DocRecord(
        doc_id=make_doc_id("rfc", "rfc:RFC-4242"),
        source_id="rfc",
        native_id="rfc:RFC-4242",
        title="HTTP Semantics",
        lang="en",
        license="LicenseRef-IETF-Trust",
        text="The Hypertext Transfer Protocol.",
        content_hash=content_hash("The Hypertext Transfer Protocol."),
    )
    assert isinstance(services.documents, InMemoryDocumentStore)
    services.documents.put(SAMPLE_GEN_2, rfc)
    with local_client(create_app(services)) as c:
        found = [c.get(document_href(make_doc_id("rfc", n))) for n in ("4242", "04242")]
        missing = c.get(document_href(make_doc_id("rfc", "9999")))
    assert [r.json()["doc_id"] for r in found] == ["wsp:rfc/rfc:RFC-4242"] * 2
    assert (missing.status_code, missing.json()["error"]["code"]) == (404, "document_not_found")


def test_random_skips_a_crawls_captures_of_site_machinery(monkeypatch: pytest.MonkeyPatch) -> None:
    """2026-10-19 audit: half the random pages of the Cloudflare Learning Center were its crawler's
    captures of Cloudflare challenges (.../cdn-cgi/challenge-platform/...), which open as Not Found."""
    from wsp_api.app import library_of

    app = create_app(sample_services())
    lib = library_of(app)
    draws = iter(
        [DocId("wsp:rfc/challenges.example.com/cdn-cgi/challenge-platform/x")] * 3
        + [DocId("wsp:rfc/youtube.fuzzy.replayweb.page/embed/x")]
        + [DocId("wsp:rfc/9110")]
    )

    def random_doc(**_: object) -> DocId:
        return next(draws)

    def title(did: str) -> str:
        return "a title"

    monkeypatch.setattr(lib.catalog, "random_doc", random_doc)
    monkeypatch.setattr(lib, "title", title)
    with local_client(app) as c:
        assert c.get("/v1/random", params={"source": "rfc"}).json()["doc_id"] == "wsp:rfc/9110"


def test_pages_of_a_navigational_query_slice_one_list(browse: httpx.Client) -> None:
    """2026-10-26 review #112: the pin was placed on page 1 only, so a hit was skipped (or shown twice)."""
    q = "wikipedia Alan Turing"
    first = SearchResults.model_validate(browse.get("/v1/search", params={"q": q, "limit": 1}).json())
    second = SearchResults.model_validate(
        browse.get("/v1/search", params={"q": q, "limit": 1, "offset": 1}).json()
    )
    both = SearchResults.model_validate(browse.get("/v1/search", params={"q": q, "limit": 2}).json())
    assert [h.doc_id for h in [*first.hits, *second.hits]] == [h.doc_id for h in both.hits]
    assert second.navigate_to is None  # only page 1 says "go straight there"
