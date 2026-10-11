"""``wsp collections`` (fetch + dry run) and ``wsp ingest`` of a document-collection generation."""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from wsp_adapters.doccollection import DocCollectionAdapter
from wsp_adapters.doccollection.adapter import CATALOG
from wsp_adapters.doccollection.catalog import read_catalog_file
from wsp_adapters.doccollection.testing import PUBLIC_A, RESTRICTED_C, make_pdf, s3_listing
from wsp_cli.doccollections import (
    CollectionFetcher,
    apply_review_decision,
    collection_spec,
    dry_run,
    render_dry_run,
)
from wsp_cli.ingest import Ingestor, VersionStatus
from wsp_core.model import SourceSpec, SourceStatus, Tier
from wsp_core.ports import Ctx
from wsp_core.testing.adapter_contract import FakeFetcher
from wsp_vault.ledger import JsonlLedger
from wsp_vault.signing import Ed25519Signer
from wsp_vault.vault import FileVault

BUCKET = "https://bucket.example.gov"
PREFIX = "pubs/"
GOOD = make_pdf([PUBLIC_A, ["Chapter 1"]])
RESTRICTED = make_pdf([RESTRICTED_C])
UNMARKED = make_pdf([["PLANNING HANDBOOK", "Contents"]])


def spec(objects: list[tuple[str, bytes]]) -> SourceSpec:
    del objects
    return SourceSpec(
        id="test-pubs",
        name="Test publications",
        tier=Tier.T1,
        status=SourceStatus.CANDIDATE,
        license="LicenseRef-US-Gov-PD",
        legal_basis="public-domain",
        domain=("preparedness",),
        adapter="doc-collection",
        raw={
            "id": "test-pubs",
            "access": {
                "adapter": "doc-collection",
                "issuer": "Test Agency",
                "key_prefix": "test",
                "origins": ["bucket.example.gov"],
                "discovery": [{"kind": "s3-listing", "bucket_url": BUCKET, "prefix": PREFIX}],
                "filter": {"allow_all": True, "distribution": {"require": ["A"]}},
                "currency": {"historical_before": "1990-01-01"},
            },
        },
    )


def routes(objects: list[tuple[str, bytes]]) -> dict[str, bytes]:
    out = {f"{BUCKET}/": s3_listing(PREFIX, objects)}
    out.update({f"{BUCKET}/{PREFIX}{k}": v for k, v in objects})
    return out


OBJECTS = [("FM-5-0_2022.pdf", GOOD), ("restricted.pdf", RESTRICTED), ("unmarked.pdf", UNMARKED)]


@pytest.fixture
def vault(tmp_path: Path) -> FileVault:
    signer = Ed25519Signer.generate()
    root = tmp_path / "vault"
    return FileVault(
        root, signer, domains={"test-pubs": "preparedness"}, ledger=JsonlLedger(root / "ledger", signer)
    )


def runner(vault: FileVault, objects: list[tuple[str, bytes]], tmp_path: Path) -> CollectionFetcher:
    return CollectionFetcher(
        vault.root,
        FakeFetcher(routes(objects)),
        keys={},
        user_agent="test",
        workdir=tmp_path / "work",
        out=io.StringIO(),
        throttle=False,
    )


def test_fetch_screens_quarantines_and_ingest_seals(vault: FileVault, tmp_path: Path) -> None:
    s = spec(OBJECTS)
    report = runner(vault, OBJECTS, tmp_path).run(s, version="2026-10-10")
    assert [d.cand.title for d in report.fetched] == ["FM 5 0 2022"]
    assert report.rejected == [("test:restricted.pdf", "deny:marking=distribution-C")]
    assert report.alerts
    assert "restricted.pdf" in report.alerts[0]
    assert len(report.held) == 1
    assert report.held[0]["rule"] == "review:marking=missing-A"
    incoming = vault.root / "_incoming"
    assert Path(report.held[0]["quarantined"]).is_file()
    assert not list((incoming / "test-pubs" / "2026-10-10").glob("*restricted*"))  # deleted, not kept
    plan = json.loads((incoming / "_plans" / "targets.d" / "test-pubs-2026-10-10.json").read_text())
    assert [t["meta"].get("relpath") for t in plan] == [None, CATALOG]
    catalog = read_catalog_file(incoming / "test-pubs" / "2026-10-10" / CATALOG)
    assert [m.distribution for m in catalog] == ["A"]
    assert "Approved for public release" in catalog[0].distribution_text

    ingest = Ingestor(vault, [s], out=io.StringIO(), settle_seconds=0)
    result = ingest.run(source_id="test-pubs")
    (vp,) = result.versions
    assert vp.status is VersionStatus.INGESTED
    assert vp.promoted
    gen_dir = vault.generation_path("test-pubs", "2026-10-10")
    assert (gen_dir / CATALOG).is_file()
    ctx = Ctx(source=s, fetcher=FakeFetcher(), workdir=tmp_path / "w2", user_agent="t")
    (doc,) = list(DocCollectionAdapter(throttle=False).documents(ctx, gen_dir))
    assert doc.meta["distribution"] == "A"
    assert doc.native_id == "test:FM-5-0_2022.pdf"

    again = runner(vault, OBJECTS, tmp_path).run(s, version="2026-11-10")
    assert again.fetched == []
    assert again.skipped == 1
    assert not (incoming / "_plans" / "targets.d" / "test-pubs-2026-11-10.json").exists()


def test_dry_run_counts_and_renders(tmp_path: Path) -> None:
    s = spec(OBJECTS)
    results = dry_run(
        [s],
        FakeFetcher(routes(OBJECTS)),
        keys={},
        workdir=tmp_path,
        user_agent="t",
        max_pages=1,
        resolve_sample=0,
        head_samples=0,
    )
    (r,) = results
    assert (r.discovered, r.allowed, r.denied, r.review) == (3, 3, 0, 0)
    assert r.known_bytes == len(GOOD) + len(RESTRICTED) + len(UNMARKED)
    out = io.StringIO()
    render_dry_run(results, out)
    assert "test-pubs" in out.getvalue()
    assert "TOTAL" in out.getvalue()


def test_git_mirror_sources_are_surveyed_as_bundles() -> None:
    s = SourceSpec(
        id="frus",
        name="FRUS",
        tier=Tier.T1,
        status=SourceStatus.CANDIDATE,
        license="LicenseRef-US-Gov-PD",
        legal_basis="public-domain",
        domain=("defense",),
        adapter="git-mirror",
        raw={
            "id": "frus",
            "access": {"adapter": "git-mirror", "endpoints": ["https://github.com/HistoryAtState/frus"]},
        },
    )
    coll = collection_spec(s)
    assert coll is not None
    access: Any = coll.raw["access"]
    assert access["discovery"] == [{"kind": "git", "repos": ["https://github.com/HistoryAtState/frus"]}]


def test_review_decisions_are_remembered_in_the_rules_file(tmp_path: Path) -> None:
    doc = {
        "doc_review": {
            "source_id": "army-pubs-public",
            "documents": [{"key": "army:ATP-9-99", "pub_number": "ATP 9-99"}],
        }
    }
    path = apply_review_decision(tmp_path, doc, accept=True)
    assert path is not None
    assert yaml.safe_load(path.read_text())["allow_documents"] == ["ATP 9-99"]
    apply_review_decision(
        tmp_path,
        {
            "doc_review": {
                "source_id": "army-pubs-public",
                "documents": [{"key": "k", "pub_number": "TC 9-1"}],
            }
        },
        accept=False,
    )
    data = yaml.safe_load(path.read_text())
    assert data["deny_documents"] == ["TC 9-1"]
    assert data["allow_documents"] == ["ATP 9-99"]


def test_api_keys_come_from_local_config_and_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    from wsp_cli.doccollections import api_keys

    monkeypatch.setenv("WSP_API_KEY_CONGRESS", "env-key")
    keys = api_keys({"curator": {"api_keys": {"govinfo_key": "cfg-key", "empty_key": ""}}})
    assert keys == {"govinfo": "cfg-key", "congress": "env-key"}
