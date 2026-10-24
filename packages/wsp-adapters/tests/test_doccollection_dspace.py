"""``dspace-sitemap`` discovery (WHO IRIS, NPS Calhoun, FAO): sitemaps, item pages, the item cache.

The pages are synthetic but shaped like the real ones (fetched 2026-10-24): Highwire ``citation_*``
tags, no Dublin Core tags, and the Angular transfer state with the item, its bundle list and the
ORIGINAL bundle's first page of bitstreams.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, cast

from wsp_adapters.doccollection.discovery import (
    DiscoverEnv,
    discover_all,
    parse_dspace_item,
)
from wsp_adapters.doccollection.policy import Rules
from wsp_core.robots import parse as parse_robots
from wsp_core.testing.adapter_contract import FakeFetcher

BASE = "https://iris.example.int"
API = f"{BASE}/server/api/core"


def item_page(
    uuid: str,
    handle: str,
    title: str,
    files: Sequence[tuple[str, str, int]] = (("b1", "doc.pdf", 1000),),
    *,
    metadata: Mapping[str, Sequence[str]] | None = None,
    total: int | None = None,
    others: Sequence[tuple[str, str]] = (),
    escaped: bool = True,
) -> bytes:
    """An item page: ``files`` are the ORIGINAL bundle's ``(uuid, name, size)`` in order; ``others``
    are ``(uuid, handle)`` of further items the page renders (FAO shows related items)."""
    md = {"dc.title": [title], **(metadata or {})}
    item_href = f"{API}/items/{uuid}"
    objects: dict[str, Any] = {
        item_href: {
            "data": {
                "type": "item",
                "uuid": uuid,
                "handle": handle,
                "metadata": {k: [{"value": v} for v in vs] for k, vs in md.items()},
            }
        },
        f"{item_href}/bundles?size=9999": {
            "data": {
                "type": {"value": "paginated-list"},
                "pageInfo": {"totalElements": 2},
                "_links": {
                    "page": [{"href": f"{API}/bundles/t-{uuid}"}, {"href": f"{API}/bundles/o-{uuid}"}]
                },
            }
        },
        f"{API}/bundles/t-{uuid}": {"data": {"type": "bundle", "_name": "THUMBNAIL"}},
        f"{API}/bundles/o-{uuid}": {"data": {"type": "bundle", "_name": "ORIGINAL"}},
        f"{API}/bundles/o-{uuid}/bitstreams?page=0&size=5": {
            "data": {
                "type": {"value": "paginated-list"},
                "pageInfo": {"elementsPerPage": 5, "totalElements": len(files) if total is None else total},
                "_links": {"page": [{"href": f"{API}/bitstreams/{b}"} for b, _n, _s in files]},
            }
        },
    }
    for b, name, size in files:
        objects[f"{API}/bitstreams/{b}"] = {
            "data": {
                "type": "bitstream",
                "uuid": b,
                "bundleName": "ORIGINAL",
                "sizeBytes": size,
                "metadata": {"dc.title": [{"value": name}]},
            }
        }
    for other, other_handle in others:
        objects[f"{API}/items/{other}"] = {
            "data": {"type": "item", "uuid": other, "handle": other_handle, "metadata": {}}
        }
    state = json.dumps({"NGRX_STATE": {"core": {"cache/object": objects}}})
    if escaped:  # Angular before 16
        state = state.replace("&", "&a;").replace('"', "&q;").replace("'", "&s;").replace("<", "&l;")
    pdf = f"{BASE}/bitstreams/{files[0][0]}/download" if files else ""
    return (
        "<!DOCTYPE html><html><head>"
        '<meta name="Generator" content="DSpace 7.6">'
        f'<meta name="citation_title" content="{title}">'
        '<meta name="citation_language" content="English">'
        f'<meta name="citation_abstract_html_url" content="{BASE}/handle/{handle}">'
        + (f'<meta name="citation_pdf_url" content="{pdf}">' if pdf else "")
        + "</head><body><ds-app>...</ds-app>"
        f'<script id="dspace-angular-state" type="application/json">{state}</script></body></html>'
    ).encode()


SHELL = b"<!DOCTYPE html><html><head><title>DSpace</title></head><body><ds-app></ds-app></body></html>"


def sitemap_index(*maps: str) -> bytes:
    rows = "".join(f"<sitemap><loc>{m}</loc><lastmod>2026-10-24T02:23:54Z</lastmod></sitemap>" for m in maps)
    return f'<?xml version="1.0"?><sitemapindex xmlns="x">{rows}</sitemapindex>'.encode()


def sitemap(rows: Sequence[tuple[str, str]]) -> bytes:
    body = "".join(
        f"<url><loc>{loc}</loc>" + (f"<lastmod>{lm}</lastmod>" if lm else "") + "</url>" for loc, lm in rows
    )
    return f'<?xml version="1.0"?><urlset xmlns="x">{body}</urlset>'.encode()


U1 = "11111111-1111-1111-1111-111111111111"
U2 = "22222222-2222-2222-2222-222222222222"
U3 = "33333333-3333-3333-3333-333333333333"


def routes() -> dict[str, bytes]:
    return {
        f"{BASE}/sitemap_index.xml": sitemap_index(f"{BASE}/sitemap0.xml", f"{BASE}/sitemap1.xml"),
        f"{BASE}/sitemap0.xml": sitemap(
            [
                (f"{BASE}/communities/c0", ""),  # not an item
                (f"{BASE}/items/{U1}", "2025-10-21T13:09:29Z"),
            ]
        ),
        f"{BASE}/sitemap1.xml": sitemap(
            [(f"{BASE}/items/{U2}", "2025-10-22T00:00:00Z"), (f"{BASE}/items/{U3}", "2025-10-20T00:00:00Z")]
        ),
        f"{BASE}/items/{U1}": item_page(
            U1,
            "10665/102539",
            "Guidelines for drinking-water quality",
            [("e356", "gdwq_eng.pdf", 1506070), ("t1", "gdwq.txt", 10), ("f7fc", "gdwq_ger.pdf", 825219)],
            metadata={
                "dc.rights": ["CC BY-NC-SA 3.0 IGO"],
                "dc.subject.mesh": ["Drinking Water", "Water Quality"],
                "dc.subject.meshqualifier": ["standards"],
                "dc.date.issued": ["2022-03-21"],
                "dc.language.iso": ["en"],
                "dc.identifier.govdoc": ["WHO/HEP/2022.1"],
            },
        ),
        f"{BASE}/items/{U2}": item_page(U2, "10665/2", "Polio eradication: progress report"),
        f"{BASE}/items/{U3}": item_page(
            U3,
            "10665/3",
            "Emergency triage assessment and treatment (ETAT)",
            [("c3", "etat.pdf", 500)],
            metadata={"dc.language": ["French"]},
        ),
    }


def requests(env: DiscoverEnv) -> list[str]:
    return cast(FakeFetcher, env.fetcher).requests


def env_for(rts: Mapping[str, bytes], tmp: Path | None = None, **kw: Any) -> DiscoverEnv:
    return DiscoverEnv(
        fetcher=FakeFetcher(rts),
        issuer="World Health Organization",
        prefix="who",
        origins=("iris.example.int",),
        rules=Rules(),
        cache_dir=tmp,
        **kw,
    )


TOPICS = ['"guidelines for drinking-water quality"', '"emergency triage assessment and treatment"']


def test_items_from_sitemaps_keep_the_dspace7_keys_urls_rights_and_subjects(tmp_path: Path) -> None:
    env = env_for(routes(), tmp_path)
    cfg = {"kind": "dspace-sitemap", "base": BASE, "topics": TOPICS}
    cands = {c.key: c for c in discover_all([cfg], env)}
    # The off-topic item is left out; the third file (index 2, after a .txt) keeps its index, as the
    # dspace7 resolver numbered it (WHO's 10665/102539 is held as who:10665-102539 and -1 that way).
    assert sorted(cands) == ["who:10665-102539", "who:10665-102539-2", "who:10665-3"]
    first = cands["who:10665-102539"]
    assert first.url == f"{API}/bitstreams/e356/content"  # the endpoint robots.txt allows, as before
    assert (first.size, first.rights, first.edition_date) == (1506070, "CC BY-NC-SA 3.0 IGO", "2022-03-21")
    assert first.subjects == ("Drinking Water", "Water Quality")  # not the MeSH qualifiers
    assert (first.pub_number, first.landing_url) == ("WHO/HEP/2022.1", f"{BASE}/handle/10665/102539")
    assert cands["who:10665-102539-2"].url == f"{API}/bitstreams/f7fc/content"
    assert cands["who:10665-3"].language == "fr"  # "French", spelled out as FAO and WHO do
    assert env.totals["dspace-sitemap"] == 3
    assert not env.incomplete
    assert any("topical selection" in s for s in env.sampled)  # absence from it proves nothing


def test_read_pages_are_kept_and_read_again_only_when_their_sitemap_date_moves(tmp_path: Path) -> None:
    rts = routes()
    cfg = {"kind": "dspace-sitemap", "base": BASE}
    first = env_for(rts, tmp_path)
    assert len(list(discover_all([cfg], first))) == 4
    assert not first.sampled  # every page read, no topics: a complete listing

    again = env_for(rts, tmp_path)
    assert len(list(discover_all([cfg], again))) == 4
    fetched = requests(again)
    assert not [u for u in fetched if "/items/" in u]  # three sitemap requests, no item page

    rts[f"{BASE}/sitemap1.xml"] = sitemap(
        [(f"{BASE}/items/{U2}", "2026-01-01T00:00:00Z"), (f"{BASE}/items/{U3}", "2025-10-20T00:00:00Z")]
    )
    moved = env_for(rts, tmp_path)
    list(discover_all([cfg], moved))
    assert [u for u in requests(moved) if "/items/" in u] == [f"{BASE}/items/{U2}"]


def test_a_run_reads_max_items_pages_newest_first_and_is_a_sample_until_all_are_read(tmp_path: Path) -> None:
    rts = routes()
    cfg = {"kind": "dspace-sitemap", "base": BASE, "max_items": 1}
    env = env_for(rts, tmp_path)
    keys = [c.key for c in discover_all([cfg], env)]
    assert keys == ["who:10665-2"]  # U2 has the newest <lastmod>
    assert not env.incomplete  # what was read is fetched
    assert any("2 of 3 item pages not read yet" in s for s in env.sampled)
    env = env_for(rts, tmp_path)
    assert sorted(c.key for c in discover_all([cfg], env)) == [
        "who:10665-102539",
        "who:10665-102539-2",
        "who:10665-2",
    ]
    env = env_for(rts, tmp_path)
    assert len(list(discover_all([cfg], env))) == 4
    assert not env.sampled


def test_a_dry_run_reads_a_few_sitemaps_and_pages() -> None:
    env = env_for(routes(), max_pages=1)
    list(discover_all([{"kind": "dspace-sitemap", "base": BASE}], env))
    assert any("read 1 of 2 sitemaps" in s for s in env.sampled)
    item_requests = [u for u in requests(env) if "/items/" in u]
    assert len(item_requests) == 1  # the one item of sitemap0 (five a max_page at most)


def test_client_side_shells_stop_the_reading_and_leave_the_listing_incomplete() -> None:
    # NPS Calhoun answers every item page with an empty Angular shell (2026-10-24): no metadata, so
    # nothing can be selected; reading 74,000 of them at 10 s would be eight days for nothing.
    uuids = [f"{i:08d}-0000-0000-0000-000000000000" for i in range(30)]
    rts = {
        f"{BASE}/sitemap_index.xml": sitemap_index(f"{BASE}/sitemap0.xml"),
        f"{BASE}/sitemap0.xml": sitemap([(f"{BASE}/entities/publication/{u}", "") for u in uuids]),
        **{f"{BASE}/entities/publication/{u}": SHELL for u in uuids},
    }
    env = env_for(rts)
    assert list(discover_all([{"kind": "dspace-sitemap", "base": BASE}], env)) == []
    assert len([u for u in requests(env) if "/entities/" in u]) == 10
    assert any("10 of 10 item pages" in s for s in env.incomplete)
    # A dry run reads too few pages to reach the stop: all of them failing still says so.
    env = env_for(rts, max_pages=1)
    assert list(discover_all([{"kind": "dspace-sitemap", "base": BASE}], env)) == []
    assert any("5 of 5 item pages" in s for s in env.incomplete)


def test_the_page_item_is_chosen_by_its_url_never_guessed() -> None:
    fao = "https://openknowledge.fao.org"
    page = item_page(U1, "20.500.14283/ca8818en", "Fall armyworm", others=[(U2, "20.500.14283/cb6516en")])
    rec = parse_dspace_item(page.decode(), f"{fao}/handle/20.500.14283/ca8818en")
    assert rec is not None
    assert rec["uuid"] == U1
    assert parse_dspace_item(page.decode(), f"{fao}/handle/20.500.14283/zz0000en") is None
    assert parse_dspace_item(page.decode(), f"{BASE}/items/{U3}") is None
    assert parse_dspace_item(SHELL.decode(), f"{BASE}/items/{U1}") is None


def test_angular_state_without_escapes_is_read_too() -> None:
    page = item_page(U1, "10665/9", "Mass casualty management", escaped=False)
    rec = parse_dspace_item(page.decode(), f"{BASE}/items/{U1}")
    assert rec is not None
    assert rec["files"] == [[0, "b1", "doc.pdf", 1000]]


def test_an_item_with_more_files_than_its_page_shows_keeps_the_listing_a_sample() -> None:
    rts = routes()
    rts[f"{BASE}/items/{U2}"] = item_page(U2, "10665/2", "Polio", [("p1", "a.pdf", 1)], total=7)
    env = env_for(rts)
    list(discover_all([{"kind": "dspace-sitemap", "base": BASE}], env))
    assert any("1 item(s) list more files" in s for s in env.sampled)


def test_a_listed_page_that_is_gone_is_remembered_until_its_date_moves(tmp_path: Path) -> None:
    rts = routes()
    del rts[f"{BASE}/items/{U2}"]
    env = env_for(rts, tmp_path)
    assert len(list(discover_all([{"kind": "dspace-sitemap", "base": BASE}], env))) == 3
    assert not env.incomplete
    env = env_for(rts, tmp_path)
    list(discover_all([{"kind": "dspace-sitemap", "base": BASE}], env))
    assert f"{BASE}/items/{U2}" not in requests(env)


def test_cover_page_repositories_give_no_size() -> None:
    env = env_for(routes())
    cfg = {"kind": "dspace-sitemap", "base": BASE, "cover_page": True}
    sizes = {c.size for c in discover_all([cfg], env)}
    assert sizes == {None}


def test_match_selects_item_urls_before_any_page_is_read() -> None:
    fao = "https://openknowledge.fao.org"
    rts = {
        f"{fao}/sitemap_index.xml": sitemap_index("http://openknowledge.fao.org/sitemap0.xml"),
        f"{fao}/sitemap0.xml": sitemap(
            [(f"{fao}/handle/20.500.14283/ca8818en", ""), (f"{fao}/handle/20.500.14283/ca8818fr", "")]
        ),
        f"{fao}/handle/20.500.14283/ca8818en": item_page(U1, "20.500.14283/ca8818en", "Fall armyworm"),
    }
    env = DiscoverEnv(
        fetcher=FakeFetcher(rts), issuer="FAO", prefix="fao", origins=("openknowledge.fao.org",)
    )
    cfg = {"kind": "dspace-sitemap", "base": fao, "match": r"(?:en|\d+e)$"}
    (cand,) = list(discover_all([cfg], env))
    assert cand.key == "fao:20.500.14283-ca8818en"
    assert not [u for u in requests(env) if u.endswith("fr")]


def test_topics_are_phrases_and_words_with_or() -> None:
    env = env_for(routes())
    cfg = {"kind": "dspace-sitemap", "base": BASE, "topics": ['polio "progress report" OR "no such phrase"']}
    assert [c.key for c in discover_all([cfg], env)] == ["who:10665-2"]
    env = env_for(routes())
    cfg = {
        "kind": "dspace-sitemap",
        "base": BASE,
        "topics": ["water quality"],
    }  # both words, subjects included
    assert {c.key for c in discover_all([cfg], env)} == {"who:10665-102539", "who:10665-102539-2"}


def test_dspace_stock_robots_allows_what_the_discovery_reads() -> None:
    robots = parse_robots(
        b"User-agent: *\nDisallow: /search\nDisallow: /entities/*/full\nDisallow: /items/*/full\n"
        b"Crawl-delay: 10\nDisallow: /browse\nAllow: /server/api/core/mapping\n"
        b"Allow: /server/api/core/bitstreams\nDisallow: /server/api/*\nDisallow: /server/api\n"
        b"Disallow: /server/oai/*\n"
    )
    assert robots is not None
    ua = "Whisipedia/0.1 (personal offline archive)"
    for url in (
        f"{BASE}/sitemap_index.xml",
        f"{BASE}/sitemap3.xml",
        f"{BASE}/items/{U1}",
        f"{BASE}/entities/publication/{U1}",
        f"{BASE}/handle/10665/1861",
        f"{API}/bitstreams/e356/content",
    ):
        assert robots.can_fetch(ua, url), url
    for url in (
        f"{API}/items/{U1}/bundles",
        f"{BASE}/server/api/discover/search/objects",
        f"{BASE}/items/{U1}/full",
    ):
        assert not robots.can_fetch(ua, url), url
    assert robots.crawl_delay(ua) == 10


def restricted_page(uuid: str) -> bytes:
    """The page of an item the public may not see: the site frame, and the item's request refused."""
    request = {
        "request": {"href": f"{API}/items/{uuid}?embed=owningCollection%2FparentCommunity"},
        "state": "Error",
        "response": {"statusCode": 401, "errorMessage": "Authentication is required"},
    }
    state = {"NGRX_STATE": {"core": {"cache/object": {}, "data/request": {"client/1": request}}}}
    return (
        '<html><head><meta name="Generator" content="DSpace 7.6"></head><body><ds-app></ds-app>'
        f'<script id="dspace-angular-state" type="application/json">{json.dumps(state)}</script>'
        "</body></html>"
    ).encode()


def test_a_restricted_item_is_an_answer_kept_until_its_date_moves(tmp_path: Path) -> None:
    # WHO IRIS lists items edited the day before whose item request answers 401 (2026-10-24): not a
    # failure of the site, so they neither count towards the stop nor make the listing incomplete.
    rts = routes()
    rts[f"{BASE}/items/{U2}"] = restricted_page(U2)
    assert parse_dspace_item(rts[f"{BASE}/items/{U2}"].decode(), f"{BASE}/items/{U2}") == {
        "v": 1,
        "gone": True,
        "status": 401,
    }
    env = env_for(rts, tmp_path)
    assert "who:10665-2" not in {c.key for c in discover_all([{"kind": "dspace-sitemap", "base": BASE}], env)}
    assert not env.incomplete
    assert not env.sampled
    env = env_for(rts, tmp_path)
    list(discover_all([{"kind": "dspace-sitemap", "base": BASE}], env))
    assert f"{BASE}/items/{U2}" not in requests(env)
