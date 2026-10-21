"""``doc-collection`` adapter: discovery kinds, policy stages, catalog and the shared contract."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from adapters_testkit import make_ctx, make_spec, pages_pdf
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
            [["SHAPED CHARGES AND DEMOLITIONS", "Approved for public release; distribution is unlimited."]]
        )
    )
    assert str(screen_file(weapon, rules).decision) == "deny:safety=shaped charges"
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
      <a href="/pubs/tm9-1300.html">TM 9-1300</a>
      <a href="/files/direct.pdf">Download PDF</a>
      <a href="http://www.x.gov/files/plain-http.pdf">insecure</a>
    </body></html>"""
    detail = b'<a href="/files/FM5-0_2022.pdf">PDF</a>'
    routes = {"https://www.x.gov/index": index, "https://www.x.gov/pubs/fm5-0.html": detail}
    rules = Rules(deny_series_prefix=("TM 9-",))
    env = env_for(routes, origins=("www.x.gov",), rules=rules)
    cfg = {
        "kind": "index-page",
        "url": "https://www.x.gov/index",
        "follow": r"/pubs/",
        "filter_follow": True,
        "pub_number": r"((?:FM|TM) \d+-\d+)",
    }
    cands = list(discover_all([cfg], env))
    fetcher = env.fetcher
    assert isinstance(fetcher, FakeFetcher)
    assert "https://www.x.gov/pubs/tm9-1300.html" not in fetcher.requests
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


def test_rows_with_a_url_rewrite_need_no_detail_page() -> None:
    """The RFC index: one row per RFC linking its info page; the text file is fetched directly."""
    table = b"""<table>
      <tr><td><a href="/info/rfc9293/"><span>RFC</span> <span>9293</span></a></td>
          <td>Transmission Control Protocol (TCP)</td><td>August 2022</td></tr>
      <tr><td><a href="/about/">About</a></td><td>not an RFC</td></tr>
    </table>"""
    env = env_for({"https://www.rfc-editor.org/rfc-index.html": table}, origins=("www.rfc-editor.org",))
    cfg = {
        "kind": "index-page",
        "url": "https://www.rfc-editor.org/rfc-index.html",
        "rows": "tr",
        "columns": {"pub_number": 0, "title": 1},
        "url_rewrite": [r"/info/rfc(\d+)/$", r"/rfc/rfc\1.txt"],
    }
    (cand,) = list(discover_all([cfg], env))
    assert cand.url == "https://www.rfc-editor.org/rfc/rfc9293.txt"
    assert cand.landing_url == "https://www.rfc-editor.org/info/rfc9293/"
    assert (cand.pub_number, cand.title) == ("RFC 9293", "Transmission Control Protocol (TCP)")
    assert cand.key == "t:RFC-9293"


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


def test_archive_org_volumes_of_one_work_get_their_volume_in_the_title() -> None:
    # 2026-10-16 field exam: Appian's Roman History, v.1 and v.2, share one archive.org title.
    items = [
        {
            "identifier": f"romanhistoryofap0{n}appi",
            "title": "The Roman history of Appian of Alexandria;",
            "volume": f"v.{n}",
            "date": "1899",
        }
        for n in (1, 2)
    ]
    scrape = {"items": [*items, {"identifier": "single", "title": "A single book", "volume": "v.1 of 1"}]}
    routes = {"https://archive.org/services/search/v1/scrape": json.dumps({**scrape, "total": 3}).encode()}
    cfg = {"kind": "archive-org", "query": "identifier:x", "provenance": "pd", "title_volume": True}
    titles = [c.title for c in discover_all([cfg], env_for(routes, origins=("archive.org",)))]
    assert titles == [
        "The Roman history of Appian of Alexandria, v.1",
        "The Roman history of Appian of Alexandria, v.2",
        "A single book, v.1 of 1",
    ]
    plain = [
        c.title
        for c in discover_all([{**cfg, "title_volume": False}], env_for(routes, origins=("archive.org",)))
    ]
    assert plain[0] == "The Roman history of Appian of Alexandria;"


def test_archive_org_buckets_are_stable_whatever_the_listing_order() -> None:
    idents = [f"CIA-RDP{i:05d}" for i in range(40)]

    def batches(order: list[str], partition: str | None) -> dict[str, str]:
        pages = [order[:20], order[20:]]
        first = {"items": [{"identifier": i, "title": i} for i in pages[0]], "total": 40, "cursor": "c2"}
        second = {"items": [{"identifier": i, "title": i} for i in pages[1]], "total": 40}
        routes = {
            "https://archive.org/services/search/v1/scrape?q=collection%3Ax&fields=identifier%2Ctitle%2Cdate"
            "%2Citem_size%2Csubject%2Ccreator%2Cdescription&count=20": json.dumps(first).encode(),
            "https://archive.org/services/search/v1/scrape?q=collection%3Ax&fields=identifier%2Ctitle%2Cdate"
            "%2Citem_size%2Csubject%2Ccreator%2Cdescription&count=20&cursor=c2": json.dumps(second).encode(),
        }
        env = env_for(routes, origins=("archive.org",), partition=partition)
        cfg = {
            "kind": "archive-org",
            "query": "collection:x",
            "provenance": "mirror",
            "page_size": 20,
            "buckets": 4,
        }
        return {c.landing_url.rsplit("/", 1)[-1]: c.partition for c in discover_all([cfg], env)}

    everything = batches(idents, None)
    assert len(everything) == 40
    assert set(everything.values()) <= {"b0001", "b0002", "b0003", "b0004"}
    assert batches(list(reversed(idents)), None) == everything  # new items shifting pages move nothing
    b2 = batches(idents, "b0002")  # a batch run lists every page and keeps only its own items
    assert b2 == {i: p for i, p in everything.items() if p == "b0002"}
    assert b2


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
    # A repository that adds a cover page to what it serves (NPS Calhoun, 2026-10-21): the stored
    # file's size and MD5 are not the download's.
    env = env_for(routes, origins=("iris.example.int",), max_pages=1)
    cfg = {"kind": "api", "flavor": "dspace7", "base": base, "queries": ["water"], "cover_page": True}
    (cand,) = list(discover_all([cfg], env))
    (doc,) = resolve(cand, env)
    assert (doc.size, doc.md5, doc.url) == (None, None, f"{base}/server/api/core/bitstreams/s1/content")


def test_openstax_lists_each_live_book_with_its_pdf() -> None:
    def book(slug: str, state: str, pdf: str) -> dict[str, object]:
        return {
            "meta": {
                "slug": slug,
                "locale": "en",
                "html_url": f"https://openstax.org/details/books/{slug}",
                "first_published_at": "2025-02-18T15:26:43-06:00",
            },
            "title": slug.replace("-", " ").title(),
            "book_state": state,
            "high_resolution_pdf_url": pdf,
        }

    page = {
        "meta": {"total_count": 3},
        "items": [
            book("college-physics", "live", "https://assets.openstax.org/media/documents/Physics-WEB.pdf"),
            book("new-book", "coming_soon", ""),
            book("old-stats", "retired", "https://assets.openstax.org/media/documents/Stats-WEB.pdf"),
        ],
    }
    env = env_for(
        {"https://openstax.org/apps/cms/api/v2/pages/": json.dumps(page).encode()},
        origins=("openstax.org", "assets.openstax.org"),
    )
    got = list(discover_all([{"kind": "api", "flavor": "openstax"}], env))
    assert [(c.title, c.url.rsplit("/", 1)[-1], c.edition_date) for c in got] == [
        ("College Physics", "Physics-WEB.pdf", "2025-02-18"),
        ("Old Stats", "Stats-WEB.pdf", "2025-02-18"),
    ]
    assert got[0].landing_url == "https://openstax.org/details/books/college-physics"
    assert not env.incomplete


def test_git_lists_an_organisations_repositories_a_hundred_a_page() -> None:
    def repo(name: str, archived: bool = False) -> dict[str, object]:
        return {
            "name": name,
            "description": name,
            "default_branch": "master",
            "pushed_at": "2026-09-01T00:00:00Z",
            "size": 1500,
            "archived": archived,
            "license": {"spdx_id": "CC0-1.0"},
        }

    page1 = [repo(f"author-{i}_book-{i}") for i in range(99)] + [repo("tools")]
    page2 = [repo("jane-austen_emma"), repo("old_gone", archived=True)]
    routes = {
        "https://api.github.com/orgs/standardebooks/repos?per_page=100&page=1": json.dumps(page1).encode(),
        "https://api.github.com/orgs/standardebooks/repos?per_page=100&page=2": json.dumps(page2).encode(),
    }
    env = env_for(routes, origins=("github.com", "api.github.com", "codeload.github.com"))
    cfg = {"kind": "git", "org": "standardebooks", "match": "^[a-z0-9-]+_[a-z0-9-]+", "snapshot": True}
    got = list(discover_all([cfg], env))
    assert len(got) == 100  # 99 + emma; "tools" does not match, an archived repository is left out
    assert got[-1].url == "https://github.com/standardebooks/jane-austen_emma.git"
    assert got[-1].extra["default_branch"] == "master"
    assert got[-1].extra["snapshot"] == "1"


def test_a_published_csv_lists_documents_newest_html_first() -> None:
    csv_text = (
        "number,url,sha1,latestPubDate,title,latestPDF,latestHTML\n"
        "R48566,reports/R48566.json,d2,2026-10-16,The Motion to Recommit in the House,"
        "files/a.pdf,files/a.html\n"
        "IF1,reports/IF1.json,f2,2026-10-15,Only a PDF,files/b.pdf,\n"
        ",reports/x.json,f3,2026-10-14,No number,files/c.pdf,\n"
    )
    url = "https://www.everycrsreport.com/reports.csv"
    env = env_for({url: csv_text.encode()}, origins=("www.everycrsreport.com",))
    cfg = {
        "kind": "csv",
        "url": url,
        "base": "https://www.everycrsreport.com/",
        "key": "number",
        "file": ["latestHTML", "latestPDF"],
        "title": "title",
        "date": "latestPubDate",
    }
    got = list(discover_all([cfg], env))
    assert [(c.pub_number, c.url.rsplit("/", 1)[-1], c.edition_date) for c in got] == [
        ("R48566", "a.html", "2026-10-16"),
        ("IF1", "b.pdf", "2026-10-15"),
    ]
    assert got[0].title == "The Motion to Recommit in the House"


def test_osti_records_without_full_text_are_skipped_in_json_and_xml() -> None:
    def link(oid: str, *rels: str) -> list[dict[str, str]]:
        return [{"rel": r, "href": f"https://www.osti.gov/{r}/{oid}"} for r in rels]

    api = "https://www.osti.gov/api/v1/records?"
    page1 = json.dumps(
        [
            {"osti_id": "1", "title": "Has text", "links": link("1", "citation", "fulltext")},
            {"osti_id": "2", "title": "Citation only", "links": link("2", "citation")},
            {"osti_id": "3", "title": "No links listed"},
        ]
    ).encode()
    xml = (
        b'<?xml version="1.0"?><records>'
        b'<record><osti_id>4</osti_id><title>X text</title><links><link rel="citation" href="c"/>'
        b'<link rel="fulltext" href="f"/></links></record>'
        b'<record><osti_id>5</osti_id><title>X citation only</title><links><link rel="citation" href="c"/>'
        b"</links></record></records>"
    )
    for body, want in ((page1, ["t:OSTI-1", "t:OSTI-3"]), (xml, ["t:OSTI-4"])):
        env = env_for(
            {api + "rows=100&page=1&q=x": body, api + "rows=100&page=2&q=x": b"[]"}, origins=("www.osti.gov",)
        )
        got = list(discover_all([{"kind": "api", "flavor": "osti", "query": "x"}], env))
        assert [c.key for c in got] == want
        assert "osti: 1 record(s) without full text at OSTI were skipped" in env.notes


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


def test_s3_listing_keeps_the_newest_of_a_dated_series() -> None:
    """OpenHistoricalMap keeps a planet a day (planet-YYMMDD_HHMM.osm.pbf): only the newest is fetched."""
    days = [
        ("planet-211204_0800.osm.pbf", b"a"),
        ("planet-261019_0800.osm.pbf", b"bb"),
        ("planet-251231_0800.osm.pbf", b"c"),
    ]
    env = env_for({f"{BUCKET}/": s3_listing("planet/", days)}, origins=("bucket.example.gov",))
    cfg = {
        "kind": "s3-listing",
        "bucket_url": BUCKET,
        "prefix": "planet/",
        "key_pattern": r"\.osm\.pbf$",
        "newest": 1,
    }
    (cand,) = list(discover_all([cfg], env))
    assert cand.url.endswith("planet-261019_0800.osm.pbf")
    # The bucket lists but refuses reads; the same keys come from its CDN host (2026-10-21).
    env = env_for({f"{BUCKET}/": s3_listing("planet/", days)}, origins=("bucket.example.gov",))
    (cand,) = list(discover_all([{**cfg, "download_url": "https://planet.example.org/"}], env))
    assert cand.url == "https://planet.example.org/planet/planet-261019_0800.osm.pbf"


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


def test_resolve_cache_survives_a_restart_and_misses_on_changed_listings(tmp_path: Path) -> None:
    from wsp_adapters.doccollection.adapter import ResolveCache, Resolved

    now = [1000.0]
    cand = Candidate(
        key="t:a", url="https://archive.org/metadata/a/files", title="A", issuer="T", resolve="archive-org"
    )
    doc = cand.with_url("https://archive.org/download/a/a.pdf", size=5, md5="abc")
    first = ResolveCache(tmp_path / "c.sqlite", ttl=100, clock=lambda: now[0])
    first.put(cand, [doc])
    first.close()  # a restart
    again = ResolveCache(tmp_path / "c.sqlite", ttl=100, clock=lambda: now[0])
    assert again.get(cand) == Resolved([doc])
    assert again.get(replace(cand, title="A, revised")) is None  # the listing changed
    now[0] += 101
    assert again.get(cand) is None  # expired: a later cycle resolves again
    again.close()


def test_concurrent_requests_overlap_but_start_an_interval_apart() -> None:
    import itertools
    import threading as th
    import time

    lock = th.Lock()
    active = [0, 0]  # in flight now, most at once

    class Slow(FakeFetcher):
        def get_bytes(self, url: str, *, max_bytes: int = 64 * 1024 * 1024) -> bytes:
            with lock:
                active[0] += 1
                active[1] = max(active[1], active[0])
            time.sleep(0.25)  # a slow round trip
            with lock:
                active[0] -= 1
            return b"{}"

    slots: list[float] = []

    def sleep(delay: float) -> None:
        with lock:
            slots.append(time.monotonic() + delay)  # the start this request reserved
        time.sleep(delay)

    t = Throttle(Slow({}), interval=0.05, sleep=sleep)
    workers = [th.Thread(target=t.get_bytes, args=(f"https://a.gov/{i}",)) for i in range(6)]
    for w in workers:
        w.start()
    for w in workers:
        w.join()
    slots.sort()
    # Reserved starts are an interval apart (each computed under the throttle's lock); ignore the tiny
    # jitter between computing a slot and reading the clock here.
    assert all(b - a >= 0.05 - 0.01 for a, b in itertools.pairwise(slots))
    assert active[1] >= 2  # requests overlapped: one did not wait for the previous to finish
    assert t.requests["a.gov"] == 6


def test_a_complete_archive_listing_is_cached_and_a_partial_one_is_not(tmp_path: Path) -> None:
    from wsp_core.errors import FetchError

    base = "https://archive.org/services/search/v1/scrape?q=collection%3Ax&fields=identifier%2Ctitle%2Cdate%2Citem_size%2Csubject%2Ccreator%2Cdescription&count=2"
    page1 = {
        "items": [{"identifier": "a", "title": "A"}, {"identifier": "b", "title": "B"}],
        "total": 3,
        "cursor": "c2",
    }
    page2 = {"items": [{"identifier": "c", "title": "C"}], "total": 3}
    routes = {base: json.dumps(page1).encode(), base + "&cursor=c2": json.dumps(page2).encode()}
    cfg = {
        "kind": "archive-org",
        "query": "collection:x",
        "provenance": "mirror",
        "page_size": 2,
        "buckets": 2,
    }

    class Down(FakeFetcher):
        def get_bytes(self, url: str, *, max_bytes: int = 64 * 1024 * 1024) -> bytes:
            raise FetchError("ReadTimeout")

    # A listing that fails on page 2: incomplete, and nothing cached.
    half = {base: routes[base]}
    env = DiscoverEnv(
        fetcher=FakeFetcher(half), issuer="T", prefix="t", origins=("archive.org",), cache_dir=tmp_path
    )
    assert len(list(discover_all([cfg], env))) == 2
    assert env.incomplete
    assert not list(tmp_path.glob("archive-listing-*"))
    # A complete listing is kept; the next run reads it without a request.
    env = DiscoverEnv(
        fetcher=FakeFetcher(routes), issuer="T", prefix="t", origins=("archive.org",), cache_dir=tmp_path
    )
    first = sorted(c.key for c in discover_all([cfg], env))
    assert first == ["t:a", "t:b", "t:c"]
    assert not env.incomplete
    assert len(list(tmp_path.glob("archive-listing-*.jsonl.gz"))) == 1
    env = DiscoverEnv(fetcher=Down({}), issuer="T", prefix="t", origins=("archive.org",), cache_dir=tmp_path)
    assert sorted(c.key for c in discover_all([cfg], env)) == first
    assert not env.incomplete


def test_xml_answers_with_forbidden_control_characters_still_parse() -> None:
    from wsp_adapters.doccollection.discovery import xml_root

    root = xml_root(b"<records><record><title>Shelter\x0b design \x01guide</title></record></records>")
    assert root.findtext("record/title") == "Shelter design guide"


def test_a_gatsby_site_is_read_from_page_data_with_chapters_named_by_book() -> None:
    category = b'<a href="/book-a">Book A</a> <a href="/book-b">Book B</a>'
    shell = b"<html><body><div id=___gatsby></div></body></html>"

    def page_data(book: str) -> bytes:
        html = (
            f'<a href="https://files.example.mil/dl.aspx?docid={book}-epub">{book} EPUB file download</a>'
            f'<a href="https://files.example.mil/dl.aspx?docid={book}-1">Front Matter</a>'
            f'<a href="https://files.example.mil/dl.aspx?docid={book}-2">Chapter One</a>'
        )
        return json.dumps({"result": {"data": {"page": {"body": html}}}}).encode()

    routes = {
        "https://site.example.mil/cat": category,
        "https://site.example.mil/page-data/cat/page-data.json": b"{}",
        "https://site.example.mil/book-a": shell,
        "https://site.example.mil/book-b": shell,
        "https://site.example.mil/page-data/book-a/page-data.json": page_data("Book A"),
        "https://site.example.mil/page-data/book-b/page-data.json": page_data("Book B"),
    }
    env = env_for(routes, origins=("site.example.mil", "files.example.mil"))
    cfg = {
        "kind": "index-page",
        "gatsby": True,
        "url": "https://site.example.mil/cat",
        "follow": r"site\.example\.mil/book-",
        "depth": 1,
        "documents": r"files\.example\.mil/dl\.aspx\?docid=",
        "documents_skip_text": r"\b(EPUB|MOBI)\b",
        "key_from": "text",
    }
    found = list(discover_all([cfg], env))
    assert sorted(c.title for c in found) == [
        "Book A: Chapter One",
        "Book A: Front Matter",
        "Book B: Chapter One",
        "Book B: Front Matter",
    ]
    assert len({c.key for c in found}) == 4  # "Front Matter" twice, but never one key
    assert not env.incomplete


def test_latest_dated_finds_the_newest_published_release() -> None:
    from wsp_core.errors import NotFoundError
    from wsp_core.model import RemoteHead

    published = {
        "https://dl.example.mil/Library_July_2026.zip",
        "https://dl.example.mil/Library_April_2026.zip",
    }

    class Heads(FakeFetcher):
        def head(self, url: str) -> RemoteHead:
            if url not in published:
                raise NotFoundError(url)
            return RemoteHead(url=url, size=123)

    env = DiscoverEnv(fetcher=Heads({}), issuer="DISA", prefix="stig", origins=("dl.example.mil",))
    cfg = {
        "kind": "latest-dated",
        "url": "https://dl.example.mil/Library_{Month}_{YYYY}.zip",
        "months": [1, 4, 7, 10],
        "today": "2026-10-12",  # October not out yet: July is the newest
        "series": "Library",
        "title": "Library",
    }
    (cand,) = list(discover_all([cfg], env))
    assert cand.url == "https://dl.example.mil/Library_July_2026.zip"
    assert (cand.key, cand.edition_date, cand.size) == ("stig:Library-2026-07", "2026-07-01", 123)
    assert cand.title == "Library (July 2026)"


def test_a_zip_is_screened_by_its_readme_and_member_names(tmp_path: Path) -> None:
    import zipfile

    from wsp_adapters.doccollection.adapter import extract_text, well_formed

    z = tmp_path / "lib.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("README.txt", "Security Technical Implementation Guides, approved for public release.")
        zf.writestr("U_Windows_10_V3R1_STIG.zip", b"PK")
    got = extract_text(z, max_pages=3)
    assert got.problem == ""
    assert "approved for public release" in got.text
    assert "U Windows 10 V3R1 STIG.zip" in got.text
    assert well_formed(z) is None
    bad = tmp_path / "bad.zip"
    bad.write_bytes(b"<html>")
    assert well_formed(bad) == "not a zip archive"


def test_archive_items_can_prefer_the_ocr_text_pdf() -> None:
    files = {
        "result": [
            {
                "name": "1.pdf",
                "source": "original",
                "format": "Image Container PDF",
                "size": "100",
                "md5": "AA",
            },
            {
                "name": "1_text.pdf",
                "source": "derivative",
                "format": "Additional Text PDF",
                "size": "210",
                "md5": "BB",
            },
        ]
    }
    routes = {"https://archive.org/metadata/doc1/files": json.dumps(files).encode()}
    env = env_for(routes, origins=("archive.org",))
    base = Candidate(
        key="t:doc1",
        url="https://archive.org/metadata/doc1/files",
        title="Doc",
        issuer="T",
        landing_url="https://archive.org/details/doc1",
        resolve="archive-org",
    )
    (orig,) = resolve(base, env)
    assert orig.url.endswith("/1.pdf")
    assert orig.md5 == "aa"
    (ocr,) = resolve(replace(base, extra={"prefer": "text-pdf"}), env)
    assert ocr.url.endswith("/1_text.pdf")
    assert (ocr.size, ocr.md5) == (210, "bb")


def test_pdf_dates() -> None:
    from wsp_adapters.doccollection.adapter import pdf_date

    assert pdf_date("D:20150709142233-04'00'") == "2015-07-09"
    assert pdf_date("D:201507") == "2015-07"
    assert pdf_date("2015") == "2015"
    assert pdf_date("D:20151399") == "2015"
    assert pdf_date("garbage") == ""


def test_words_hyphenated_across_a_pdf_line_are_joined() -> None:
    # pdfium's U+FFFE line-break hyphen split "following" in Bowditch (2026-10-16 field exam).
    from wsp_adapters.doccollection.adapter import _LINE_HYPHEN  # pyright: ignore[reportPrivateUsage]

    text = "the fol￾\r\nlowing method; posi￾tion; avail￾ \navable; a real-hyphen word"
    assert _LINE_HYPHEN.sub("", text) == "the following method; position; availavable; a real-hyphen word"


def _pages_pdf(n: int) -> bytes:
    return pages_pdf([f"Page {i + 1}" for i in range(n)])


def test_a_pdf_is_reopened_every_few_pages_and_read_within_a_time_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FEMA's CAD-drawing PDF grew the nightly refine past its memory ceiling (2026-10-17): pdfium keeps
    every parsed page until the document closes, and some pages take seconds each."""
    import pypdfium2

    from wsp_adapters.doccollection import adapter as dc

    path = tmp_path / "drawings.pdf"
    path.write_bytes(_pages_pdf(25))
    opened: list[str] = []
    real = pypdfium2.PdfDocument

    def counting(src: str) -> pypdfium2.PdfDocument:
        opened.append(src)
        return real(src)

    monkeypatch.setattr(pypdfium2, "PdfDocument", counting)
    text = dc.pdf_text(path, 100)
    assert [p for p in text.split("\n\f\n")] == [f"Page {i}" for i in range(1, 26)]
    assert len(opened) == 3  # pages 1-10, 11-20, 21-25

    ticks = iter(range(1000))
    late = dc.pdf_text(path, 100, seconds=4.5, clock=lambda: float(next(ticks)))
    pages = late.split("\n\f\n")
    assert pages[:-1] == [f"Page {i}" for i in range(1, 5)]  # the clock passes 4.5 s before page 5
    assert pages[-1] == "[Pages 5-25 of this PDF were not read: its drawings take too long to read.]"


def test_data_files_keep_their_extension_and_a_list_item_can_name_one(tmp_path: Path) -> None:
    """2026-10-20: GHCN-Daily's .tar.gz was stored as .pdf and failed the PDF check; GEBCO's URL ends
    in "/zip/" and needs its extension named."""
    env = DiscoverEnv(fetcher=FakeFetcher({}), issuer="NOAA", prefix="ghcn", origins=("www.ncei.noaa.gov",))
    cfg = {
        "kind": "list",
        "items": [
            {
                "pub_number": "ghcnd_all",
                "url": "https://www.ncei.noaa.gov/pub/data/ghcn/daily/ghcnd_all.tar.gz",
            },
            {
                "pub_number": "grid",
                "url": "https://www.ncei.noaa.gov/open_download/grid/zip/",
                "suffix": ".zip",
            },
            {"pub_number": "odd", "url": "https://www.ncei.noaa.gov/x/y/", "suffix": "../../etc"},
            {"pub_number": "planet", "url": "https://www.ncei.noaa.gov/planet-260101.osm.pbf"},
        ],
    }
    names = [c.file_name for c in discover_all([cfg], env)]
    assert names[0].endswith(".gz")
    assert names[1].endswith(".zip")
    assert names[2].endswith(".pdf")  # a suffix that is not an extension is ignored
    assert names[3].endswith(".pbf")
