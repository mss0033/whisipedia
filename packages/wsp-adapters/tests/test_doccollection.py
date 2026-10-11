"""``doc-collection`` adapter: discovery kinds, policy stages, catalog and the shared contract."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from adapters_testkit import make_ctx, make_spec
from doc_fixtures import EXPORT_WARNING, PUBLIC_A, RESTRICTED_C, make_pdf, md5, s3_listing

from wsp_adapters import available_adapters, load_adapter
from wsp_adapters.doccollection import DocCollectionAdapter
from wsp_adapters.doccollection.adapter import CATALOG, read_catalog, screen_file
from wsp_adapters.doccollection.catalog import FetchedDoc, build_catalog, write_catalog
from wsp_adapters.doccollection.discovery import (
    DiscoverEnv,
    DiscoveryError,
    Throttle,
    discover_all,
    origin_ok,
    resolve,
)
from wsp_adapters.doccollection.model import Candidate, DocumentMeta
from wsp_adapters.doccollection.policy import CurrencyRules, Rules, Verdict
from wsp_core.banners import HISTORICAL_BANNER, banner_for
from wsp_core.ids import make_doc_id
from wsp_core.model import Checksum, Generation, GenerationStatus, VerdictStatus
from wsp_core.ports import Ctx, GenerationDependencies, SourceAdapter
from wsp_core.testing.adapter_contract import FakeFetcher, SourceAdapterContract

BUCKET = "https://bucket.example.gov"
PREFIX = "pubs/"
DOC_A = make_pdf([PUBLIC_A, ["Chapter 1 Fundamentals of planning"]])
DOC_B = make_pdf([["NAVIGATION RULES HANDBOOK", "Approved for public release; distribution is unlimited."]])
DOC_C = make_pdf([RESTRICTED_C])
OBJECTS = [("FM-5-0_2022.pdf", DOC_A), ("NavRules_2024.pdf", DOC_B)]


def s3_spec(**access: Any) -> Any:
    return make_spec(
        "test-pubs",
        {
            "adapter": "doc-collection",
            "issuer": "Test Agency",
            "key_prefix": "test",
            "origins": ["bucket.example.gov"],
            "discovery": [
                {"kind": "s3-listing", "bucket_url": BUCKET, "prefix": PREFIX, "key_pattern": r"\.pdf$"}
            ],
            "filter": {"allow_all": True, "distribution": {"require": ["A"]}},
            **access,
        },
        license_="LicenseRef-US-Gov-PD",
    )


def s3_routes(objects: list[tuple[str, bytes]] = OBJECTS) -> dict[str, bytes]:
    routes = {f"{BUCKET}/": s3_listing(PREFIX, objects)}
    routes.update({f"{BUCKET}/{PREFIX}{k}": v for k, v in objects})
    return routes


def unthrottled() -> DocCollectionAdapter:
    return DocCollectionAdapter(throttle=False)


@pytest.fixture
def catalog_generation(tmp_path: Path) -> Path:
    """A sealed-looking generation: two documents and the catalog (one historical)."""
    gen = tmp_path / "lib" / "2026-10-10"
    (gen / "upstream").mkdir(parents=True)
    (gen / "upstream" / "a.pdf").write_bytes(DOC_A)
    (gen / "upstream" / "b.pdf").write_bytes(DOC_B)
    docs = [
        DocumentMeta(
            doc_key="test:FM-5-0:2022-11-04",
            file="upstream/a.pdf",
            issuer="Test Agency",
            title="Planning and Orders Production",
            pub_number="FM 5-0",
            edition_date="2022-11-04",
            distribution="A",
        ),
        DocumentMeta(
            doc_key="test:FB-1",
            file="upstream/b.pdf",
            issuer="Test Agency",
            title="Old Bulletin",
            currency="historical",
            historical=True,
            banner=HISTORICAL_BANNER,
        ),
    ]
    write_catalog(gen / CATALOG, docs)
    return gen


class TestDocCollectionContract(SourceAdapterContract):
    @pytest.fixture
    def adapter(self) -> SourceAdapter:
        return unthrottled()

    @pytest.fixture
    def ctx(self, tmp_path: Path) -> Ctx:
        return make_ctx(s3_spec(), FakeFetcher(s3_routes()), tmp_path / "work")

    @pytest.fixture
    def generation_dir(self, catalog_generation: Path) -> Path:
        return catalog_generation


def test_registered_and_loadable() -> None:
    assert "doc-collection" in available_adapters()
    assert isinstance(load_adapter("doc-collection"), DocCollectionAdapter)


def test_discover_writes_an_audit_and_keeps_only_allowed(tmp_path: Path) -> None:
    spec = s3_spec(filter={"deny_keywords": ["navrules"], "default": "allow"})
    ctx = make_ctx(spec, FakeFetcher(s3_routes()), tmp_path / "work")
    (release,) = unthrottled().discover(ctx)
    assert len(release.files) == 1
    assert "FM-5-0" in release.files[0].name
    assert release.meta["denied"] == "1"
    assert release.meta["allowed"] == "1"
    audits = list((tmp_path / "work" / "doc-collection").rglob("candidates.jsonl"))
    rows = [json.loads(line) for line in audits[0].read_text().splitlines()]
    assert sorted(r["decision"] for r in rows) == ["allow:default", "deny:keyword=navrules"]
    assert all(f.checksums and f.checksums[0].algorithm == "md5" for f in release.files)


def test_nothing_allowed_means_no_release(tmp_path: Path) -> None:
    spec = s3_spec(filter={"default": "review"})
    ctx = make_ctx(spec, FakeFetcher(s3_routes()), tmp_path / "work")
    assert list(unthrottled().discover(ctx)) == []


def test_plan_skips_documents_already_held(tmp_path: Path) -> None:
    ctx = make_ctx(s3_spec(), FakeFetcher(s3_routes()), tmp_path / "work")
    adapter = unthrottled()
    (release,) = adapter.discover(ctx)
    first = release.files[0]
    from pathlib import PurePosixPath

    from wsp_core.model import Artifact

    have = Generation(
        "test-pubs",
        "2026-07-01",
        GenerationStatus.SEALED,
        artifacts=(
            Artifact(
                "test-pubs", "2026-07-01", PurePosixPath(f"upstream/{first.name}"), first.size or 0, "x", "y"
            ),
        ),
    )
    plan = adapter.plan(ctx, have, release)
    assert first.name not in {f.name for f in plan.files}
    assert plan.previous_generation == "2026-07-01"


def test_verify_rejects_restricted_and_export_controlled_files(tmp_path: Path) -> None:
    objects = [("restricted.pdf", DOC_C), ("export.pdf", make_pdf([EXPORT_WARNING]))]
    ctx = make_ctx(s3_spec(), FakeFetcher(s3_routes(objects)), tmp_path / "work")
    adapter = unthrottled()
    (release,) = adapter.discover(ctx)
    results = list(adapter.fetch(ctx, adapter.plan(ctx, None, release)))
    for remote, result in zip(release.files, results, strict=True):
        verdict = adapter.verify(ctx, remote, result.path)
        assert verdict.status is VerdictStatus.REJECTED  # ADR-0010: dropped, the release goes on
        assert verdict.withheld
        assert not verdict.ok
        assert "policy deny:marking=" in verdict.detail


def test_verify_holds_documents_that_need_review(tmp_path: Path) -> None:
    objects = [("unmarked.pdf", make_pdf([["Planning handbook", "Contents"]]))]
    ctx = make_ctx(s3_spec(), FakeFetcher(s3_routes(objects)), tmp_path / "work")
    adapter = unthrottled()
    (release,) = adapter.discover(ctx)
    (result,) = adapter.fetch(ctx, adapter.plan(ctx, None, release))
    verdict = adapter.verify(ctx, release.files[0], result.path)
    assert verdict.status is VerdictStatus.HELD
    assert verdict.withheld
    assert verdict.detail == "policy review:marking=missing-A"


def test_screen_is_the_policy_alone_without_checksums(tmp_path: Path) -> None:
    """``DocumentScreen``: the orchestrator calls ``screen`` even when the fetcher verified digests."""
    objects = [("a.pdf", make_pdf([PUBLIC_A])), ("c.pdf", make_pdf([RESTRICTED_C])), ("x.pdf", b"<html>")]
    ctx = make_ctx(s3_spec(), FakeFetcher(s3_routes(objects)), tmp_path / "work")
    adapter = unthrottled()
    (release,) = adapter.discover(ctx)
    results = {r.path.name: r.path for r in adapter.fetch(ctx, adapter.plan(ctx, None, release))}
    by_name = {f.name: f for f in release.files}
    statuses: dict[str, VerdictStatus] = {}
    for name, path in results.items():
        wrong = replace(by_name[name], checksums=(Checksum("md5", "0" * 32),))  # screen ignores digests
        statuses[name.rsplit("-", 1)[0].removeprefix("test__")] = adapter.screen(ctx, wrong, path).status
    assert statuses == {
        "a.pdf": VerdictStatus.OK,
        "c.pdf": VerdictStatus.REJECTED,
        "x.pdf": VerdictStatus.MISMATCH,
    }


def test_a_file_that_is_not_a_pdf_is_still_a_mismatch(tmp_path: Path) -> None:
    objects = [("broken.pdf", b"<html>error page</html>")]
    ctx = make_ctx(s3_spec(), FakeFetcher(s3_routes(objects)), tmp_path / "work")
    adapter = unthrottled()
    (release,) = adapter.discover(ctx)
    (result,) = adapter.fetch(ctx, adapter.plan(ctx, None, release))
    assert adapter.verify(ctx, release.files[0], result.path).status is VerdictStatus.MISMATCH


def test_depends_on_names_the_generations_the_catalog_points_at(tmp_path: Path) -> None:
    ctx = make_ctx(s3_spec(), FakeFetcher({}), tmp_path / "work")
    gen = tmp_path / "lib" / "2026-10-11.ca"
    write_catalog(
        gen / CATALOG,
        [
            DocumentMeta(doc_key="k:1", file="upstream/a.pdf", issuer="x", title="a"),
            DocumentMeta(
                doc_key="k:2", file="upstream/b.pdf", issuer="x", title="b", generation="2025-10-11.ca"
            ),
            DocumentMeta(
                doc_key="k:3", file="upstream/c.pdf", issuer="x", title="c", generation="2026-10-11.ca"
            ),
        ],
    )
    adapter = unthrottled()
    assert isinstance(adapter, GenerationDependencies)
    assert adapter.depends_on(ctx, gen) == {"2025-10-11.ca"}
    assert adapter.depends_on(ctx, tmp_path / "lib" / "no-catalog") == frozenset()


def test_screen_file_stages(tmp_path: Path) -> None:
    rules = Rules(require=("A",))
    ok = tmp_path / "a.pdf"
    ok.write_bytes(DOC_A)
    screen = screen_file(ok, rules)
    assert screen.decision.verdict is Verdict.ALLOW
    assert screen.marking.statement == "A"
    assert "Approved for public release" in screen.marking.text
    held = tmp_path / "b.pdf"
    held.write_bytes(make_pdf([["Planning handbook", "Contents"]]))
    assert str(screen_file(held, rules).decision) == "review:marking=missing-A"
    weapon = tmp_path / "w.pdf"
    weapon.write_bytes(
        make_pdf(
            [["SNIPER TRAINING AND EMPLOYMENT", "Approved for public release; distribution is unlimited."]]
        )
    )
    assert str(screen_file(weapon, rules).decision) == "deny:safety=sniper"
    bad = tmp_path / "x.pdf"
    bad.write_bytes(b"<html>not a pdf</html>")
    from wsp_adapters.doccollection.adapter import well_formed

    assert well_formed(bad) == "not a PDF (missing %PDF- header)"


def test_documents_carry_meta_and_banner(catalog_generation: Path, tmp_path: Path) -> None:
    ctx = make_ctx(s3_spec(), FakeFetcher(), tmp_path / "work")
    docs = {d.native_id: d for d in unthrottled().documents(ctx, catalog_generation)}
    fm = docs["test:FM-5-0:2022-11-04"]
    assert fm.doc_id == make_doc_id("test-pubs", "test:FM-5-0:2022-11-04")
    assert fm.revision == "2022-11-04"
    assert fm.meta["pub_number"] == "FM 5-0"
    assert fm.meta["distribution"] == "A"
    assert "Fundamentals of planning" in fm.text
    assert fm.aliases == ("FM 5-0",)
    old = docs["test:FB-1"]
    assert old.meta["currency"] == "historical"
    assert banner_for(old.meta) == HISTORICAL_BANNER
    assert HISTORICAL_BANNER in old.text.splitlines()[:5]
    assert banner_for(fm.meta) is None


def test_documents_resolve_bytes_in_earlier_generations(catalog_generation: Path, tmp_path: Path) -> None:
    later = catalog_generation.parent / "2027-01-10"
    later.mkdir()
    write_catalog(
        later / CATALOG, [replace(m, generation="2026-10-10") for m in read_catalog(catalog_generation)]
    )
    ctx = make_ctx(s3_spec(), FakeFetcher(), tmp_path / "work")
    docs = list(unthrottled().documents(ctx, later))
    assert len(docs) == 2
    assert docs[0].html_ref == "file:2026-10-10/upstream/a.pdf"


def test_catalog_merge_supersedes_and_withdraws() -> None:
    prev = [
        DocumentMeta(
            doc_key="t:FM-5-0:2014",
            file="upstream/old.pdf",
            issuer="T",
            title="FM 5-0",
            pub_number="FM 5-0",
            edition_date="2014-03-01",
        ),
        DocumentMeta(
            doc_key="t:FM-9-9:2001",
            file="upstream/gone.pdf",
            issuer="T",
            title="FM 9-9",
            pub_number="FM 9-9",
            edition_date="2001-01-01",
        ),
    ]
    new = Candidate(
        key="t:FM-5-0:2022",
        url="https://x/new.pdf",
        title="FM 5-0",
        issuer="T",
        pub_number="FM 5-0",
        edition_date="2022-11-04",
    )
    fetched = [
        FetchedDoc(cand=new, file="upstream/new.pdf", sha256="ab", bytes=3, decision="allow:series=FM 5-0")
    ]
    cat = {
        m.doc_key: m
        for m in build_catalog(prev, "2026-07-01", fetched, CurrencyRules(), listed={"t:FM-5-0:2022"})
    }
    assert cat["t:FM-5-0:2022"].currency == "current"
    assert cat["t:FM-5-0:2022"].generation == ""
    assert cat["t:FM-5-0:2014"].currency == "superseded"
    assert cat["t:FM-5-0:2014"].superseded_by == "t:FM-5-0:2022"
    assert cat["t:FM-5-0:2014"].generation == "2026-07-01"
    assert cat["t:FM-9-9:2001"].currency == "withdrawn-from-index"


# --- discoverers ---------------------------------------------------------------------------------


def env_for(
    routes: Mapping[str, bytes], *, origins: tuple[str, ...] = (), rules: Rules | None = None, **kw: Any
) -> DiscoverEnv:
    return DiscoverEnv(
        fetcher=FakeFetcher(routes), issuer="T", prefix="t", origins=origins, rules=rules or Rules(), **kw
    )


def test_throttle_spaces_requests_per_host() -> None:
    now = [0.0]
    slept: list[float] = []

    def sleep(s: float) -> None:
        slept.append(s)
        now[0] += s

    t = Throttle(
        FakeFetcher({"https://a.gov/1": b"x", "https://a.gov/2": b"y", "https://b.gov/1": b"z"}),
        interval=1.0,
        per_host={"a.gov": 10},
        clock=lambda: now[0],
        sleep=sleep,
    )
    t.get_bytes("https://a.gov/1")
    t.get_bytes("https://b.gov/1")
    t.get_bytes("https://a.gov/2")
    assert slept == [10.0]
    assert t.requests == {"a.gov": 2, "b.gov": 1}


def test_index_page_follow_never_opens_denied_series() -> None:
    index = b"""<html><body>
      <a href="/pubs/fm5-0.html">FM 5-0</a>
      <a href="/pubs/fm3-09.html">FM 3-09</a>
      <a href="/files/direct.pdf">Download PDF</a>
      <a href="http://www.x.gov/files/plain-http.pdf">insecure</a>
    </body></html>"""
    detail = b'<a href="/files/FM5-0_2022.pdf">PDF</a>'
    routes = {"https://www.x.gov/index": index, "https://www.x.gov/pubs/fm5-0.html": detail}
    rules = Rules(deny_series_prefix=("FM 3-09",))
    env = env_for(routes, origins=("www.x.gov",), rules=rules)
    cfg = {
        "kind": "index-page",
        "url": "https://www.x.gov/index",
        "follow": r"/pubs/",
        "filter_follow": True,
        "pub_number": r"(FM \d+-\d+)",
    }
    cands = list(discover_all([cfg], env))
    fetcher = env.fetcher
    assert isinstance(fetcher, FakeFetcher)
    assert "https://www.x.gov/pubs/fm3-09.html" not in fetcher.requests
    urls = {c.url: c for c in cands}
    assert set(urls) == {"https://www.x.gov/files/direct.pdf", "https://www.x.gov/files/FM5-0_2022.pdf"}
    assert urls["https://www.x.gov/files/FM5-0_2022.pdf"].title == "FM 5-0"
    assert urls["https://www.x.gov/files/direct.pdf"].title == "direct"


def test_rows_then_detail_page() -> None:
    table = b"""<table><tr><th>No</th></tr>
      <tr><td><a href="Details.aspx?PUB_ID=1">FM 5-0</a></td><td>ACTIVE</td><td>11/04/2022</td>
      <td>PLANNING AND ORDERS PRODUCTION</td></tr>
    </table>"""
    detail = b'<a href="../../epubs/DR_pubs/DR_a/ARN1-FM_5-0-000-WEB-1.pdf">PDF</a>'
    routes = {
        "https://apd.mil/PubForm/FM.aspx": table,
        "https://apd.mil/PubForm/Details.aspx?PUB_ID=1": detail,
    }
    env = env_for(routes, origins=("apd.mil",))
    cfg = {
        "kind": "index-page",
        "url": "https://apd.mil/PubForm/FM.aspx",
        "rows": "tr",
        "columns": {"pub_number": 0, "status": 1, "date": 2, "title": 3},
        "detail_documents": r"epubs/DR_pubs/.+\.pdf$",
    }
    (cand,) = list(discover_all([cfg], env))
    assert (cand.pub_number, cand.edition_date, cand.status, cand.title) == (
        "FM 5-0",
        "2022-11-04",
        "ACTIVE",
        "Planning And Orders Production",
    )
    assert cand.key == "t:FM-5-0:2022-11-04"
    (doc,) = resolve(cand, env)
    assert doc.url == "https://apd.mil/epubs/DR_pubs/DR_a/ARN1-FM_5-0-000-WEB-1.pdf"


def test_nga_api_prefers_full_publications() -> None:
    items = [
        {
            "pubDownloadId": 1,
            "fullPubFlag": True,
            "pubDownloadDisplayName": "Pub. 9 - Bowditch",
            "sectionDisplayName": "Pub. 9 Volume 1",
            "s3Key": "1/SFH/vol1.pdf",
            "fileSize": 100,
            "filenameBase": "vol1",
            "sectionLastModified": "2019-09-20T14:02:18Z",
        },
        {
            "pubDownloadId": 1,
            "fullPubFlag": False,
            "pubDownloadDisplayName": "Pub. 9 - Bowditch",
            "sectionDisplayName": "Chapter 1",
            "s3Key": "1/SFH/ch1.pdf",
            "fileSize": 10,
            "filenameBase": "ch1",
        },
        {
            "pubDownloadId": 2,
            "fullPubFlag": False,
            "pubDownloadDisplayName": "Pub. 105 - Atlas",
            "sectionDisplayName": "Pub. 105: Cover",
            "s3Key": "2/SFH/105cover.pdf",
            "fileSize": 5,
            "filenameBase": "105cover",
        },
    ]
    url = "https://msi.nga.mil/api/publications/stored-pubs?output=json"
    env = env_for({url: json.dumps(items).encode()}, origins=("msi.nga.mil",))
    cands = list(discover_all([{"kind": "api", "flavor": "nga-msi", "url": url}], env))
    assert [c.size for c in cands] == [100, 5]
    assert cands[0].pub_number == "Pub. 9"
    assert cands[0].url.startswith("https://msi.nga.mil/api/publications/download?key=1%2FSFH%2Fvol1.pdf")


def test_archive_org_records_provenance_and_resolves_the_original_pdf() -> None:
    scrape = {
        "items": [
            {
                "identifier": "farmersbulletin1",
                "title": "Farmers' bulletin no. 1",
                "date": "1922",
                "item_size": 999,
            }
        ],
        "total": 1,
    }
    files = {
        "result": [
            {"name": "fb1_djvu.txt", "source": "derivative"},
            {"name": "fb1.pdf", "source": "original", "size": "321", "md5": "ABC"},
        ]
    }
    routes = {
        "https://archive.org/services/search/v1/scrape": json.dumps(scrape).encode(),
        "https://archive.org/metadata/farmersbulletin1/files": json.dumps(files).encode(),
    }
    env = env_for(routes, origins=("archive.org",))
    cfg = {
        "kind": "archive-org",
        "query": "collection:x",
        "provenance": "agency upload",
        "pub_number": r"(farmers'? bulletin\s*no\.?\s*\d+)",
    }
    (cand,) = list(discover_all([cfg], env))
    assert cand.extra["provenance"] == "agency upload"
    assert cand.pub_number == "Farmers' bulletin no. 1"
    assert env.totals["archive-org"] == 1
    (doc,) = resolve(cand, env)
    assert doc.url == "https://archive.org/download/farmersbulletin1/fb1.pdf"
    assert (doc.size, doc.md5) == (321, "abc")


def test_govinfo_needs_a_key_unless_demo_is_allowed() -> None:
    env = env_for({}, origins=("www.govinfo.gov",))
    list(discover_all([{"kind": "api", "flavor": "govinfo", "collection": "GOVPUB"}], env))
    assert any("API key 'govinfo' not configured" in n for n in env.notes)
    page = {
        "count": 2,
        "packages": [
            {
                "packageId": "GOVPUB-HS5-PURL-gpo1",
                "title": "National Response Framework",
                "dateIssued": "2019-10-28",
            },
            {"packageId": "GOVPUB-D101-PURL-gpo2", "title": "Other"},
        ],
        "nextPage": None,
    }
    url = "https://api.govinfo.gov/collections/GOVPUB/1990-01-01T00:00:00Z"
    env2 = env_for({url: json.dumps(page).encode()}, origins=("www.govinfo.gov",), keys={"govinfo": "k"})
    (cand,) = list(
        discover_all([{"kind": "api", "flavor": "govinfo", "package_prefix": ["GOVPUB-HS5"]}], env2)
    )
    assert cand.url == "https://www.govinfo.gov/content/pkg/GOVPUB-HS5-PURL-gpo1/pdf/GOVPUB-HS5-PURL-gpo1.pdf"
    assert env2.totals["govinfo:GOVPUB"] == 2


def test_dspace7_search_and_bitstreams() -> None:
    base = "https://iris.example.int"
    search = {
        "_embedded": {
            "searchResult": {
                "page": {"totalElements": 1},
                "_embedded": {
                    "objects": [
                        {
                            "_embedded": {
                                "indexableObject": {
                                    "uuid": "u1",
                                    "handle": "10665/1",
                                    "name": "Guidelines for drinking-water quality",
                                    "metadata": {
                                        "dc.date.issued": [{"value": "2022"}],
                                        "dc.rights": [{"value": "CC BY-NC-SA 3.0 IGO"}],
                                    },
                                }
                            }
                        }
                    ]
                },
            }
        }
    }
    bundles = {
        "_embedded": {
            "bundles": [
                {"name": "THUMBNAIL"},
                {
                    "name": "ORIGINAL",
                    "_links": {"bitstreams": {"href": f"{base}/server/api/core/bundles/b1/bitstreams"}},
                },
            ]
        }
    }
    streams = {
        "_embedded": {
            "bitstreams": [
                {
                    "name": "gdwq.pdf",
                    "sizeBytes": 1234,
                    "checkSum": {"checkSumAlgorithm": "MD5", "value": "FF"},
                    "_links": {"content": {"href": f"{base}/server/api/core/bitstreams/s1/content"}},
                }
            ]
        }
    }
    routes = {
        f"{base}/server/api/discover/search/objects": json.dumps(search).encode(),
        f"{base}/server/api/core/items/u1/bundles": json.dumps(bundles).encode(),
        f"{base}/server/api/core/bundles/b1/bitstreams": json.dumps(streams).encode(),
    }
    env = env_for(routes, origins=("iris.example.int",), max_pages=1)
    (cand,) = list(
        discover_all([{"kind": "api", "flavor": "dspace7", "base": base, "queries": ["water"]}], env)
    )
    assert cand.rights == "CC BY-NC-SA 3.0 IGO"
    assert cand.edition_date == "2022"
    (doc,) = resolve(cand, env)
    assert (doc.size, doc.md5, doc.url) == (1234, "ff", f"{base}/server/api/core/bitstreams/s1/content")


def test_unknown_kinds_and_flavors_fail_loudly() -> None:
    with pytest.raises(DiscoveryError):
        list(discover_all([{"kind": "carrier-pigeon"}], env_for({})))
    env = env_for({})
    list(discover_all([{"kind": "api", "flavor": "nope"}], env))
    assert any("unknown api flavor" in n for n in env.notes)


def test_origin_policy_is_https_and_allow_listed() -> None:
    assert origin_ok("https://www.cisa.gov/x.pdf", ("www.cisa.gov",))
    assert not origin_ok("http://www.cisa.gov/x.pdf", ("www.cisa.gov",))
    assert not origin_ok("https://evil.example/x.pdf", ("www.cisa.gov",))
    assert origin_ok("https://dl.dod.cyber.mil/x.zip", ("cyber.mil",))


def test_s3_listing_partitions_and_md5() -> None:
    listing = s3_listing(
        "HistoricalTopo/PDF/", [("AK/AK_Anchorage_1950.pdf", b"a"), ("CA/CA_Yosemite_1898.pdf", b"bb")]
    )
    env = env_for({f"{BUCKET}/": listing}, origins=("bucket.example.gov",))
    cfg = {
        "kind": "s3-listing",
        "bucket_url": BUCKET,
        "prefix": "HistoricalTopo/PDF/",
        "partition": "HistoricalTopo/PDF/([A-Z]{2})/",
    }
    cands = list(discover_all([cfg], env))
    assert [(c.partition, c.size, c.md5) for c in cands] == [("ak", 1, md5(b"a")), ("ca", 2, md5(b"bb"))]
    assert cands[1].edition_date == "1898"


def test_one_partition_lists_one_state_prefix() -> None:
    listing = s3_listing("HistoricalTopo/PDF/CA/", [("CA_Yosemite_1898.pdf", b"bb")])
    env = env_for({f"{BUCKET}/": listing}, origins=("bucket.example.gov",), partition="ca")
    cfg = {
        "kind": "s3-listing",
        "bucket_url": BUCKET,
        "prefix": "HistoricalTopo/PDF/",
        "partition": "HistoricalTopo/PDF/([A-Z]{2})/",
        "partition_in_prefix": True,
    }
    (cand,) = list(discover_all([cfg], env))
    assert cand.partition == "ca"
    fetcher = env.fetcher
    assert isinstance(fetcher, FakeFetcher)
    assert "prefix=HistoricalTopo%2FPDF%2FCA%2F" in fetcher.requests[0]


def test_candidate_round_trip_and_file_names_are_flat() -> None:
    c = Candidate(
        key="army:FM-5-0:2022-11-04",
        url="https://a/b/FM5.pdf?x=1",
        title="T",
        issuer="I",
        mirrors=("https://m/FM5.pdf",),
    )
    assert Candidate.from_json(c.to_json()) == c
    assert "/" not in c.file_name
    assert c.file_name.endswith(".pdf")
    assert c.file_name.startswith("army__FM-5-0__2022-11-04-")
