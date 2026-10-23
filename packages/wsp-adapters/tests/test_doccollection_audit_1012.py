"""Regressions for the 2026-10-12 collections and adapters audit.

See docs/audits/2026-10-12/collections-adapters.md.
"""

from __future__ import annotations

import json
import zipfile
from dataclasses import replace
from pathlib import Path
from typing import Any, ClassVar, cast

import pytest

from wsp_adapters.doccollection.adapter import (
    CollectionConfig,
    DocCollectionAdapter,
    Extracted,
    ResolveCache,
    Survey,
    WrongBodyError,
    body_kind,
    extract_text,
    reader_title,
    screen_file,
    screen_text,
    true_suffix,
    well_formed,
    write_discovery_record,
)
from wsp_adapters.doccollection.catalog import build_catalog, write_catalog
from wsp_adapters.doccollection.discovery import DiscoverEnv, Throttle, discover_all, nga_msi
from wsp_adapters.doccollection.model import (
    Candidate,
    DocumentMeta,
    clean_title,
    display_title,
    key_stem_from_url,
    title_from_url,
)
from wsp_adapters.doccollection.policy import (
    DENY_CORE,
    CurrencyRules,
    DocFacts,
    Edition,
    Rules,
    Verdict,
    assign_currency,
    check_distribution,
    classify_content,
    classify_metadata,
    load_rules,
    parse_markings,
)
from wsp_adapters.doccollection.testing import PUBLIC_A, make_pdf
from wsp_core.banners import WITHDRAWN_BANNER, banner_for
from wsp_core.model import FetchPlan, ReleaseRef, SourceSpec, SourceStatus, Tier
from wsp_core.ports import Ctx, GenerationFinalizer
from wsp_core.testing.adapter_contract import FakeFetcher

REPO = Path(__file__).resolve().parents[3]
FILLER = " This handbook describes procedures for preparedness and response planning in communities." * 3


# --- CA-3 / CA-4: the deny core ---------------------------------------------------------------------


def test_the_deny_core_has_exactly_the_owners_content_categories() -> None:
    assert set(DENY_CORE) == {
        "explosives and demolitions",
        "weapons and munitions technical data",
        "CBRN weapons production",
        "offensive cyber tooling",
    }
    assert all(DENY_CORE.values())


@pytest.mark.parametrize(
    "title",
    [
        "The Cyber Kill Chain and Defensive Measures (CISA)",
        "Exploit code is publicly available for CVE-2024-1234; patch now",
        "The Effects of Nuclear Weapons",
        "Blast Design: Weapons Effects on Buildings (FEMA 426)",
        "Demolitions of damaged structures after a flood",
        "Police Sniper Incident After-Action Report",
        "Booby Traps in Vietnam: A History",
        "Naval Gunnery in the Civil War",
        "Drug Target Development and Validation",
        "Weaponization of Disinformation on Social Media",
        "Target Acquisition in Wildlife Telemetry",
        "Minelaying in the Baltic 1914-1918 (history)",
        "Fire Direction Center (wildland fire dispatch)",
        "Wildland fire control near homes",
        # Employment doctrine (owner decision 2026-10-12)
        "Sniper Training and Employment",
        "Tank Gunnery Training",
        "Joint Targeting Process",
        "Fire Control Systems",
        "Battle Drills for the Infantry Rifle Platoon",
        "Close Air Support",
    ],
)
def test_public_civil_and_historical_titles_are_not_denied(title: str) -> None:
    rules = Rules(allow_all=True)
    assert classify_content(title, rules).verdict is Verdict.ALLOW, title
    assert classify_metadata(DocFacts(key="k", title=title), rules).verdict is Verdict.ALLOW, title


def test_a_situation_report_and_a_weapons_history_are_not_held() -> None:
    sitrep = "Weekly Bulletin on Outbreaks\n" + "x" * 3500 + "\narmed groups plant booby traps on roads"
    assert classify_content(sitrep, Rules()).verdict is Verdict.ALLOW  # WHO 10665/370667
    history = "History of the IVY Mike test\n" + "x" * 3500 + "\nthe weaponization of the device followed"
    assert classify_content(history, Rules()).verdict is Verdict.ALLOW  # OSTI 2588086


def test_a_history_prone_noun_counts_only_next_to_a_core_hit() -> None:
    rules = Rules()
    assert classify_content("Booby Traps\n" + "x" * 3500, rules).verdict is Verdict.ALLOW
    with_core = "Booby Traps\n" + "x" * 3500 + "\nAnnex: shaped charges"
    assert str(classify_content(with_core, rules)) == "deny:safety=booby traps+shaped charges"
    # Gunnery is no longer a core noun (owner decision 2026-10-12): it adds nothing to a core hit.
    gunnery = "Gunnery\n" + "x" * 3500 + "\nAnnex: shaped charges"
    assert str(classify_content(gunnery, rules)) == "review:safety=shaped charges"


@pytest.mark.parametrize(
    ("pub", "title"),
    [
        ("ATP 3-01.18", "Stinger Team Techniques"),
        ("ATP 3-01.85", "Patriot Battalion and Battery Techniques"),
        ("ATP 3-09.32", "Multi-Service Tactics for the Joint Application of Firepower"),
        ("TC 3-09.8", "Field Artillery Certification and Qualification"),
        ("ATP 3-21.90", "Tactical Employment of Mortars"),
        ("FM 3-09", "Fire Support and Field Artillery Operations"),
        ("TC 3-20.31", "Training and Qualification, Crew"),
        ("FM 3-22.9", "Rifle Marksmanship"),
    ],
)
def test_army_fires_air_defence_and_mortar_series_are_allowed(pub: str, title: str) -> None:
    """Employment doctrine is in scope (owner decision 2026-10-12; denied by CA-3 before)."""
    apd = load_rules({"rules": "registry/filters/army-pubs-public.yaml", "default": "allow"}, repo_root=REPO)
    got = classify_metadata(DocFacts(key=f"k:{pub}", pub_number=pub, title=title), apd)
    assert got.verdict is Verdict.ALLOW, (pub, got)


def test_joint_targeting_is_allowed_by_the_joint_publications_rules() -> None:
    jp = load_rules({"rules": "registry/filters/joint-publications.yaml", "default": "allow"}, repo_root=REPO)
    got = classify_metadata(DocFacts(key="jp:JP-3-60", pub_number="JP 3-60", title="Joint Targeting"), jp)
    assert str(got) == "allow:default"


@pytest.mark.parametrize(
    ("title", "rule"),
    [
        ("Explosives and Demolitions", "safety=explosives and demolitions"),
        ("Improvised Explosive Device Construction", "safety=improvised explosive device construction"),
        ("Firing Tables for the 155mm Howitzer", "safety=firing tables"),
        ("Missile Guidance Systems", "safety=missile guidance"),
        ("Production of Nerve Agents", "safety=production of nerve agents"),
        ("Malware Development for Beginners", "safety=malware development"),
    ],
)
def test_the_remaining_deny_core_still_denies(title: str, rule: str) -> None:
    got = classify_metadata(DocFacts(key="k", title=title), Rules(allow_all=True))
    assert str(got) == f"deny:{rule}", title


# --- CA-16: owner decisions -------------------------------------------------------------------------


def test_owner_allow_never_lifts_a_deny_core_title_but_a_reasoned_override_does() -> None:
    title = "Demolition Charges"  # a later edition retitled into the core
    allowed = Rules(allow_all=True).merged({"allow_documents": ["army:x:2030"]})
    assert classify_metadata(DocFacts(key="army:x:2030", title=title), allowed).verdict is Verdict.DENY
    over = Rules(allow_all=True).merged({"override_safety": {"army:x:2030": "owner 2026-10-12: history"}})
    assert classify_metadata(DocFacts(key="army:x:2030", title=title), over).verdict is Verdict.ALLOW
    with pytest.raises(ValueError, match="reason"):
        Rules().merged({"override_safety": {"army:x:2030": ""}})


def test_an_accepted_content_hold_is_released_only_with_allow_content_review() -> None:
    held = Extracted("Planning guide\n" + "x" * 3500 + "\nAnnex: shaped charges" + FILLER)
    rules = Rules(require=("published",))
    assert screen_text(held, rules, keys=("who:1",)).decision.verdict is Verdict.REVIEW
    allowed = rules.merged({"allow_documents": ["who:1"]})
    assert screen_text(held, allowed, keys=("who:1",)).decision.verdict is Verdict.REVIEW
    released = allowed.merged({"allow_content_review": ["who:1"]})
    assert screen_text(held, released, keys=("who:1",)).decision.verdict is Verdict.ALLOW
    assert screen_text(held, released, keys=("who:2",)).decision.verdict is Verdict.REVIEW  # per edition


# --- CA-5 / CA-6 / CA-18: markings -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "rule"),
    [
        (
            "This document has not been approved for public release; distribution is unlimited.",
            "not-for-public-release",
        ),
        ("This report has not yet been approved for public release.", "not-for-public-release"),
        ("Pending approval for public release.", "not-for-public-release"),
        (
            "Distribution authorized to the Department of Defense and DoD contractors only.",
            "distribution-limited",
        ),
        ("Distribution authorized to Federal agencies only.", "distribution-limited"),
        ("Distribution is limited to U.S. Government agencies only.", "distribution-limited"),
        ("For Official Use Only", "fouo"),
        ("For official Government use only.", "fouo"),
        ("OFFICIAL-SENSITIVE", "official-sensitive"),
        ("OFFICIAL SENSITIVE: COMMERCIAL", "official-sensitive"),
        ("RESTRICTED", "restricted"),
        ("NATO RESTRICTED", "restricted"),
        ("CONFIDENTIAL\nTitle of the report", "classified"),
        ("S E C R E T\nOperations plan", "classified"),
        ("Sensitive But Unclassified (SBU)", "sbu"),
        ("(Cui) Combined Arms Mobility", "cui"),
    ],
)
def test_restricted_markings_the_audit_found_are_denied(text: str, rule: str) -> None:
    for require in (("public-release", "published"), ("A",)):
        got = screen_text(Extracted(text + FILLER), Rules(require=require)).decision
        assert str(got) == f"deny:marking={rule}", (text, require, got)


def test_a_sign_about_a_restricted_area_is_not_a_banner() -> None:
    assert "restricted" not in parse_markings("RESTRICTED AREA signage\nGuidance for facilities").restricted


def test_army_titles_are_screened_as_printed_not_title_cased() -> None:
    cand = Candidate(
        key="army:ATP-3-90.4",
        url="https://x",
        title="(Cui) Combined Arms Mobility",
        issuer="A",
        extra={"raw_title": "(CUI) COMBINED ARMS MOBILITY"},
    )
    assert classify_metadata(cand.facts, Rules(allow_all=True)).verdict is Verdict.DENY


@pytest.mark.parametrize(
    "text",
    [
        "SECRET//NOFORN\nThis document will not be declassified.",
        "FOR OFFICIAL USE ONLY\nNot declassified.",
        "SECRET//NOFORN\nReferences: declassified cable of 1962.",
    ],
)
def test_the_word_declassified_is_not_a_release_stamp(text: str) -> None:
    decl = Rules(require=("declassified", "public-release", "published"), declassified_ok=True)
    assert screen_text(Extracted(text + FILLER), decl).decision.verdict is Verdict.DENY


def test_a_release_stamp_lifts_only_the_banners_on_its_own_page() -> None:
    stamp = "Approved For Release 2006/11/27 : CIA-RDP84-00499R000900030045-6"
    same = parse_markings(f"SECRET\nCable traffic\n{stamp}")
    assert check_distribution(same, ("declassified",), declassified_ok=True).verdict is Verdict.ALLOW
    other = parse_markings(f"Cover\n{stamp}\n\f\nSECRET\nAn unstamped page")
    assert check_distribution(other, ("declassified",), declassified_ok=True).verdict is Verdict.DENY
    nara = parse_markings("CONFIDENTIAL\nDECLASSIFIED Authority NND 12345\nMemo")
    assert check_distribution(nara, (), declassified_ok=True).verdict is Verdict.ALLOW


def test_a_document_about_export_control_is_held_and_a_warning_denies() -> None:
    about = parse_markings("The Export Administration Regulations (EAR): An Overview of Policy Issues")
    assert str(check_distribution(about, ("published",))) == "review:marking=export-mention:ear"
    warned = parse_markings("This document is subject to the Export Administration Regulations.")
    assert check_distribution(warned, ("published",)).verdict is Verdict.DENY
    facts = DocFacts(key="crs:1", title="The Export Administration Regulations (EAR): An Overview")
    assert classify_metadata(facts, Rules(allow_all=True)).verdict is Verdict.ALLOW  # fetched, then held


# --- CA-19: undated editions --------------------------------------------------------------------------


def test_an_undated_newer_fetch_is_not_superseded_by_an_older_dated_edition() -> None:
    eds = [
        Edition("a:2014", "FM 5-0", "2014-05-01", "2026-01-01T00:00:00+00:00"),
        Edition("a:new", "FM 5-0", "", "2026-10-01T00:00:00+00:00"),
    ]
    labels = assign_currency(eds, CurrencyRules())
    assert labels["a:new"].currency.value == "current"
    assert labels["a:2014"].superseded_by == "a:new"
    older = [replace(eds[0], fetched_at="2026-11-01T00:00:00+00:00"), eds[1]]
    assert assign_currency(older, CurrencyRules())["a:2014"].currency.value == "current"


# --- CA-20: rule keys ---------------------------------------------------------------------------------


def test_misspelled_filter_keys_are_refused() -> None:
    with pytest.raises(ValueError, match="unknown filter key"):
        Rules().merged({"allow_documnets": ["x"]})
    with pytest.raises(ValueError, match="unknown distribution key"):
        Rules().merged({"distribution": {"require": ["A"], "read_from": "pdf"}})


def test_every_registry_doc_collection_source_loads_its_rules() -> None:
    import yaml

    data: Any = yaml.safe_load((REPO / "registry" / "sources.yaml").read_text(encoding="utf-8"))
    for raw in data["sources"]:
        access: dict[str, Any] = raw.get("access") or {}
        if access.get("adapter") != "doc-collection":
            continue
        spec = SourceSpec(
            id=raw["id"],
            name=raw["name"],
            tier=Tier.T1,
            status=SourceStatus.CANDIDATE,
            license="x",
            legal_basis="public-domain",
            domain=("x",),
            adapter="doc-collection",
            raw=raw,
        )
        CollectionConfig.from_spec(spec, REPO)  # raises on an unknown key or a missing rules file


def test_every_rules_file_is_wired_to_a_source() -> None:
    """A rules file nothing loads is where bulk decisions hid (CA-1)."""
    text = (REPO / "registry" / "sources.yaml").read_text(encoding="utf-8")
    for path in sorted((REPO / "registry" / "filters").glob("*.yaml")):
        assert f"registry/filters/{path.name}" in text, path.name


# --- CA-2: error and landing pages ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "kind"),
    [
        (b'{"error":"Internal Server Error"}', "json"),
        (b'<?xml version="1.0"?><Error><Code>AccessDenied</Code></Error>', "xml"),
        (b"<!DOCTYPE html><html><head><title>CIA</title></head><body>Home</body></html>", "html"),
        (b"Access Denied", "an error message"),
    ],
)
def test_an_error_or_landing_page_for_a_pdf_link_is_a_failed_download(
    tmp_path: Path, body: bytes, kind: str
) -> None:
    path = tmp_path / "CIA-RDP84.pdf"
    path.write_bytes(body)
    with pytest.raises(WrongBodyError, match=kind):
        true_suffix(path)


def test_genuine_text_and_office_files_still_get_their_suffix(tmp_path: Path) -> None:
    csv = tmp_path / "c.pdf"
    csv.write_bytes(b"year,count\n2024,3\n2025,4\n")
    assert true_suffix(csv).name == "c.txt"
    fake_docx = tmp_path / "form.docx"
    fake_docx.write_bytes(b"<html><title>Sign in</title></html>")
    assert well_formed(fake_docx) is not None  # no zip container
    login = tmp_path / "page.html"
    login.write_bytes(b"<html><head><title>Sign In | Agency</title></head><body>form</body></html>")
    assert "login" in (well_formed(login) or "")
    home = tmp_path / "guide.html"
    home.write_bytes(b'<html><head><link rel="canonical" href="https://www.cia.gov/"></head></html>')
    assert "home page" in (well_formed(home) or "")


# --- CA-17: archives ---------------------------------------------------------------------------------


def test_a_restricted_member_rejects_its_archive(tmp_path: Path) -> None:
    z = tmp_path / "lib.zip"
    with zipfile.ZipFile(z, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("README.txt", "Library of public guides. " * 10)
        zf.writestr("a/guide.pdf", make_pdf([PUBLIC_A]))
        zf.writestr("b/note.txt", "CUI\nControlled by: DoD\n" + "Text of the note. " * 20)
    got = screen_file(z, Rules(allow_all=True))
    assert got.decision.verdict is Verdict.DENY
    assert got.decision.rule == "marking=cui@b/note.txt"


def test_a_zip_bomb_member_holds_the_archive(tmp_path: Path) -> None:
    z = tmp_path / "bomb.zip"
    with zipfile.ZipFile(z, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("README.txt", "Library of public guides. " * 10)
        zf.writestr("big.txt", b"\0" * (8 * 1024 * 1024))
    got = screen_file(z, Rules(allow_all=True))
    assert got.decision.verdict is Verdict.REVIEW
    assert "ArchiveError" in got.decision.rule


def _docx(text: str) -> bytes:
    import io

    w = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    body = "".join(f"<w:p><w:r><w:t>{t}</w:t></w:r></w:p>" for t in text.split("\n"))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as d:
        d.writestr("word/document.xml", f'<w:document xmlns:w="{w}"><w:body>{body}</w:body></w:document>')
    return buf.getvalue()


def test_a_zip_without_a_readme_is_screened_on_its_member_documents(tmp_path: Path) -> None:
    # 3GPP publishes each specification as a zip holding one .docx: the names alone held all 3,498.
    spec = tmp_path / "38300-ib0.zip"
    with zipfile.ZipFile(spec, "w") as zf:
        zf.writestr(
            "38300-ib0.docx",
            _docx(
                "3GPP TS 38.300 V18.11.0\nNR and NG-RAN Overall Description\n"
                + "The radio access network. " * 20
            ),
        )
    assert screen_file(spec, Rules(allow_all=True)).decision.verdict is Verdict.ALLOW
    # Nothing readable inside: the hold stands.
    scans = tmp_path / "scans.zip"
    with zipfile.ZipFile(scans, "w") as zf:
        zf.writestr("page1.png", b"\x89PNG")
    got = screen_file(scans, Rules(allow_all=True))
    assert got.decision.verdict is Verdict.REVIEW
    assert got.decision.rule == "text=no-text-layer"


# --- CA-7 / CA-8 / CA-11 / CA-13: catalogs ---------------------------------------------------------------


def _spec(**access: Any) -> SourceSpec:
    raw = {
        "id": "t",
        "access": {
            "adapter": "doc-collection",
            "key_prefix": "t",
            "origins": ["a.gov"],
            "discovery": [],
            "filter": {"allow_all": True},
            **access,
        },
    }
    return SourceSpec(
        id="t",
        name="T",
        tier=Tier.T1,
        status=SourceStatus.CANDIDATE,
        license="x",
        legal_basis="public-domain",
        domain=("x",),
        adapter="doc-collection",
        raw=raw,
    )


def _meta(key: str, file: str, title: str, **kw: Any) -> DocumentMeta:
    return DocumentMeta(doc_key=key, file=file, issuer="T", title=title, sha256="s", **kw)


def test_a_document_policy_now_denies_leaves_the_index(tmp_path: Path) -> None:
    gen = tmp_path / "2026-10-12"
    (gen / "upstream").mkdir(parents=True)
    for name in ("a.pdf", "b.pdf", "c.tif"):
        (gen / "upstream" / name).write_bytes(make_pdf([PUBLIC_A]))
    write_catalog(
        gen / "meta" / "documents.jsonl",
        [
            _meta("t:a", "upstream/a.pdf", "Planning Guide"),
            _meta("t:b", "upstream/b.pdf", "Occupant Emergency Programs"),
            _meta("t:c", "upstream/c.tif", "Sheet"),
        ],
    )
    spec = _spec(filter={"allow_all": True, "deny_documents": ["t:b"]}, formats=["pdf"])
    ctx = Ctx(source=spec, fetcher=FakeFetcher(), workdir=tmp_path / "w", user_agent="t")
    docs = list(DocCollectionAdapter(throttle=False).documents(ctx, gen))
    assert [d.native_id for d in docs] == ["t:a"]  # the owner deny and the TIFF are left out


def test_carried_documents_are_excluded_withdrawn_with_a_banner_and_refreshed() -> None:
    prev = [
        _meta("t:a", "upstream/a.pdf", "Old title", currency="rescinded"),
        _meta("t:gone", "upstream/g.pdf", "Gone"),
        _meta("t:held-1", "upstream/h.pdf", "Unresolved item"),
        _meta("t:tif", "upstream/x.tif", "Sheet"),
    ]
    listing = {"t:a": Candidate(key="t:a", url="https://a.gov/a.pdf", title="New title", issuer="T")}
    out = {
        m.doc_key: m
        for m in build_catalog(
            prev,
            "g1",
            [],
            CurrencyRules(collection=None),
            listed={"t:a"},
            protected={"t:held"},
            listing=listing,
            policy=Rules(allow_all=True),
            formats=("pdf",),
        )
    }
    assert out["t:a"].title == "New title"
    assert out["t:a"].currency == "current"  # the issuer no longer says rescinded: not sticky
    assert out["t:gone"].currency == "withdrawn-from-index"
    assert banner_for(out["t:gone"].record_meta()) == WITHDRAWN_BANNER
    assert out["t:held-1"].currency == "current"  # its item's resolve failed: never inferred withdrawn
    assert out["t:tif"].excluded == "format=tif"
    historical = build_catalog(
        prev[1:2], "g1", [], CurrencyRules.from_access({"currency": "historical"}), listed=set()
    )
    assert historical[0].currency == "withdrawn-from-index"  # historical collections too


def test_the_orchestrator_path_builds_the_catalog_like_the_cli(tmp_path: Path) -> None:
    adapter = DocCollectionAdapter(throttle=False)
    assert isinstance(adapter, GenerationFinalizer)
    spec = _spec()
    ctx = Ctx(source=spec, fetcher=FakeFetcher(), workdir=tmp_path / "w", user_agent="t")
    prev_dir = tmp_path / "vault" / "g1"
    write_catalog(
        prev_dir / "meta" / "documents.jsonl", [_meta("t:old", "upstream/old.pdf", "Earlier Guide")]
    )
    new = Candidate(key="t:new", url="https://a.gov/new.pdf", title="New Guide", issuer="T", pub_number="P 1")
    survey = Survey("t")
    survey.allowed.append(new)
    write_discovery_record(ctx.workdir / "doc-collection" / "2026-10-12", [new], survey)
    local = tmp_path / new.file_name
    local.write_bytes(make_pdf([PUBLIC_A]))
    release = ReleaseRef(source_id="t", version="2026-10-12", published=None, files=(), meta={}, format="x")
    plan = FetchPlan(release=release, files=(), previous_generation="g1")
    ((relpath, catalog),) = adapter.finalize(ctx, plan, prev_dir, {new.file_name: local})
    assert relpath == "meta/documents.jsonl"
    rows = [json.loads(line) for line in catalog.read_text().splitlines()]
    assert [(r["doc_key"], r["title"], r.get("generation", "")) for r in rows] == [
        ("t:new", "New Guide", ""),
        ("t:old", "Earlier Guide", "g1"),  # an earlier document stays, with its title
    ]
    with pytest.raises(FileNotFoundError):
        adapter.finalize(ctx, replace(plan, release=replace(release, version="2026-01-01")), prev_dir, {})


# --- CA-9 / CA-10: partial listings and empty resolves -----------------------------------------------------


def test_a_page_capped_api_listing_is_a_sample_and_an_expired_token_is_incomplete() -> None:
    page = json.dumps({"recordCount": 300, "records": [{"indexId": str(i)} for i in range(100)]}).encode()
    env = DiscoverEnv(
        fetcher=FakeFetcher({"https://pubs.usgs.gov/pubs-services/publication/": page}),
        issuer="T",
        prefix="t",
        origins=("pubs.usgs.gov",),
        max_pages=1,
    )
    list(discover_all([{"kind": "api", "flavor": "usgs-pubs"}], env))
    assert env.sampled
    assert not env.incomplete
    oai = (
        b'<OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/">'
        b'<error code="badResumptionToken">expired</error></OAI-PMH>'
    )
    env2 = DiscoverEnv(fetcher=FakeFetcher({"https://oai.example.gov/": oai}), issuer="T", prefix="t")
    list(discover_all([{"kind": "oai-pmh", "url": "https://oai.example.gov/"}], env2))
    assert any("badResumptionToken" in x for x in env2.incomplete)


def test_an_empty_resolve_is_never_cached_and_a_positive_none_is(tmp_path: Path) -> None:
    cache = ResolveCache(tmp_path / "c.sqlite")
    cand = Candidate(
        key="t:a", url="https://archive.org/metadata/a/files", title="A", issuer="T", resolve="archive-org"
    )
    cache.put(cand, [])
    assert cache.get(cand) is None
    cache.put(cand, [], none="the item holds no PDF")
    got = cache.get(cand)
    assert got is not None
    assert got.docs == []
    assert got.none
    cache.close()


def test_nga_items_without_a_download_id_are_each_their_own_publication() -> None:
    items = [
        {"s3Key": "a", "pubDownloadDisplayName": "Pub. 1", "fullPubFlag": True},
        {"s3Key": "b", "pubDownloadDisplayName": "Pub. 2"},
    ]
    env = DiscoverEnv(
        fetcher=FakeFetcher({"https://n.gov/l": json.dumps(items).encode()}), issuer="T", prefix="t"
    )
    assert len(list(nga_msi({"url": "https://n.gov/l"}, env))) == 2


# --- CA-21: politeness per HTTP request ---------------------------------------------------------------


def test_the_throttle_charges_probes_and_redirect_hops() -> None:
    class Report:
        provenance: ClassVar[dict[str, Any]] = {
            "sources": [{"url": "https://a.gov/x", "redirects": [{"url": "https://a.gov/y"}]}]
        }

    class Fetch(FakeFetcher):
        def download(self, remote: Any, dest: Any) -> Any:
            return Report()

        def get_bytes(self, url: str, *, max_bytes: int = 64 * 1024 * 1024) -> bytes:
            return b""

    clock = [0.0]
    throttle = Throttle(
        Fetch(), interval=1.0, clock=lambda: clock[0], sleep=lambda s: clock.__setitem__(0, clock[0] + s)
    )
    from wsp_core.model import RemoteFile

    throttle.download(RemoteFile(url="https://a.gov/x", name="x"), Path("/dev/null"))
    assert throttle.requests["a.gov"] == 3  # probe, GET, one redirect hop
    throttle.get_bytes("https://a.gov/next")
    assert clock[0] >= 3.0  # the next request waited for all three slots


# --- titles readers see (coordinator findings, 2026-10-12) ----------------------------------------------


@pytest.mark.parametrize(
    ("raw", "shown"),
    [
        ("IS0505 IG.pdf", "IS0505 IG"),
        (
            "General Class Pool and Syllabus 2023-2027 Public Release with 6th Errata Feb 4 2026.pdf",
            "General Class Pool and Syllabus 2023-2027 Public Release with 6th Errata Feb 4 2026",
        ),
        ("Programming%20Guide%20for%20Interoperability.pdf", "Programming Guide for Interoperability"),
        ("Programming-20Guide-20for-20Interoperability", "Programming Guide for Interoperability"),
        ("RI East Greenwich 353279 1942 24000 geo", "East Greenwich, RI (1942) — 1:24,000 topographic map"),
    ],
)
def test_titles_carry_no_file_name_debris(raw: str, shown: str) -> None:
    assert display_title(raw) == shown


def test_a_fragment_title_falls_back_to_the_document_heading(tmp_path: Path) -> None:
    pdf = tmp_path / "x.pdf"
    pdf.write_bytes(make_pdf([["Incomplete or Inaccurate Chart Reproductions", "being sold on the market"]]))
    meta = _meta("noaa:x", "upstream/x.pdf", "being sold on the commercial market")
    body = "Incomplete or Inaccurate Chart Reproductions\nbeing sold on the market"
    assert reader_title(meta, pdf, body) == "Incomplete or Inaccurate Chart Reproductions"
    named = _meta("fema:is", "upstream/IS0505.pdf", "IS0505 IG.pdf")
    assert reader_title(named, pdf, "") == "IS0505 IG"  # no PDF title, no heading: the cleaned name


def test_existing_keys_keep_their_spelling_and_decoding_is_opt_in() -> None:
    url = "https://www.cisa.gov/sites/Programming%2520Guide.pdf"
    assert key_stem_from_url(url) == "Programming%20Guide"  # stable doc ids
    assert key_stem_from_url(url, decode=True) == "Programming Guide"
    assert title_from_url(url) == "Programming Guide"
    assert clean_title("Guide.pdf") == "Guide"


def test_a_public_issuer_is_not_screened_for_markings_but_still_for_content() -> None:
    """71 RFCs were rejected as classified (2026-10-17): "(TS)" is a Technical Specification in RFC
    2026 and TCP's timestamp option in RFC 9293. The RFC Editor publishes every RFC openly."""
    rfc = Extracted(
        "Transmission Control Protocol (TCP)\nThe Timestamps option (TS) is described in RFC 7323." + FILLER
    )
    assert screen_text(rfc, Rules(allow_all=True)).decision.rule == "marking=classified-portion"
    public = Rules(allow_all=True, public_issuer=True)
    assert screen_text(rfc, public).decision.verdict is Verdict.ALLOW
    harmful = Extracted(
        "Field Manual: Demolition Charges and Calculations\nshaped charges, demolition charges" + FILLER
    )
    assert screen_text(harmful, public).decision.verdict is Verdict.DENY  # the deny core still applies
    assert Rules().merged({"public_issuer": True}).public_issuer


def test_a_short_document_that_mentions_an_error_and_an_8_bit_text_are_documents(tmp_path: Path) -> None:
    """RFC 6 (1,776 bytes) says "Error" near its end and RFC 64 writes "µsec" in Latin-1: both were
    refused (an error message; binary), with 15 other early RFCs (2026-10-17)."""
    rfc6 = tmp_path / "rfc6.txt"
    rfc6.write_bytes(
        b"Network Working Note   Steve Crocker\nRFC-6   10 April 1969\n\n"
        + b"We talked about code conversion. " * 40
        + b"Error checking is done by the IMP.\n"
    )
    assert well_formed(rfc6) is None
    rfc64 = tmp_path / "rfc64.txt"
    rfc64.write_bytes(
        b"\n\n\nNetwork Working Group\nThe line takes approximately 12 \xb5sec to settle.\n" * 100
    )
    assert body_kind(rfc64) == "text"
    assert well_formed(rfc64) is None
    error = tmp_path / "e.txt"
    error.write_bytes(b"Error 404: the requested document was not found on this server.\n")
    assert well_formed(error) == "an error message, not a document"
    blob = tmp_path / "b.txt"
    blob.write_bytes(b"\x89\x01\x02\x03" * 2000)
    assert body_kind(blob) == "binary"


def test_early_rfcs_padded_with_nuls_or_titled_with_an_error_are_documents(tmp_path: Path) -> None:
    """Twelve early RFCs were still refused (2026-10-17): RFC 71 "Reallocation in Case of Input Error"
    and six more titled with an error, as error messages; RFC 734 (NUL padding between pages) and
    three more as binary; RFC 674, opening with an NLS journal tag, as XML."""
    rfc71 = tmp_path / "rfc71.txt"
    rfc71.write_bytes(
        b"\n\nNetwork Working Group                     Tjaart Schipper\n"
        b"Request for Comments #71                   25 September 1970\n\n"
        b"            Reallocation in Case of Input Error\n\n"
        + b"In case of reading a message from the IMP, the length may be lost.\n"
        * 8
    )
    assert well_formed(rfc71) is None
    short = tmp_path / "s.txt"
    short.write_bytes(b"\n\nService Unavailable\n\nThe server is down for maintenance.\n")
    assert well_formed(short) == "an error message, not a document"
    rfc734 = tmp_path / "rfc734.txt"
    page = b"SUPDUP Display Protocol\t\t\tPage 1\n" + b"The display is a terminal.\n" * 60
    rfc734.write_bytes(page + b"\x00" * 300 + page + b"\x1a")
    assert body_kind(rfc734) == "text"
    assert well_formed(rfc734) is None
    assert extract_text(rfc734, max_pages=1).text == (page + page + b"\x1a").decode()
    rfc674 = tmp_path / "rfc674.txt"
    rfc674.write_bytes(
        b"\n\n<GJOURNAL>31484.NLS;1, 12-DEC-74 04:32 XXX ;;;; Title:\n" + b"Text of the RFC.\n" * 50
    )
    assert body_kind(rfc674) == "text"
    xml = tmp_path / "x.txt"
    xml.write_bytes(b"<Error><Code>NoSuchKey</Code></Error>")
    assert body_kind(xml) == "xml"
    image = tmp_path / "i.txt"
    image.write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + bytes(range(256)) * 16)
    assert body_kind(image) == "binary"
    latin1 = tmp_path / "rfc64.txt"
    latin1.write_bytes(b"The line takes approximately 12 \xb5sec to settle.\n" * 20)
    assert "12 \u00b5sec" in extract_text(latin1, max_pages=1).text


def test_a_cia_release_keeps_its_old_banners_but_not_the_deny_core() -> None:
    """Owner decision 2026-10-22: the CIA reading room is an official release, so a page whose
    "Approved For Release" stamp the scan lost no longer withholds the whole document (78 of 9,929
    in batch b0001 were). The registry's own filter block is what is tested."""
    import yaml

    from wsp_adapters.doccollection.policy import load_rules

    repo = Path(__file__).resolve().parents[3]
    sources = yaml.safe_load((repo / "registry" / "sources.yaml").read_text(encoding="utf-8"))
    rows = cast(list[dict[str, Any]], sources["sources"] if isinstance(sources, dict) else sources)
    spec = next(s for s in rows if s["id"] == "cia-reading-room")
    rules = load_rules(cast(dict[str, Any], spec["access"]["filter"]), repo_root=repo)
    cover = Extracted("SECRET NOFORN\nROUTING AND RECORD SHEET\nSoviet grain harvest estimates" + FILLER)
    assert screen_text(cover, rules).decision.verdict is Verdict.ALLOW
    harmful = Extracted("Demolition Charges and Calculations\nshaped charges, demolition charges" + FILLER)
    assert screen_text(harmful, rules).decision.verdict is Verdict.DENY


def test_a_data_file_without_readable_text_is_what_a_data_collection_holds() -> None:
    """GeoNames' allCountries.zip (2026-10-23): one 1.6 GB table, too large to read, came back
    "no-text-layer" and was held for review although the source is a data collection."""
    nothing = Extracted("", "no-text-layer")
    assert screen_text(nothing, Rules(allow_all=True)).decision.verdict is Verdict.REVIEW
    data = Rules(allow_all=True, public_issuer=True, data_files=True)
    assert screen_text(nothing, data).decision.verdict is Verdict.ALLOW
