"""Partitioned collections end to end (ADR-0010): fetch per partition, ingest, index the union, retention."""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from wsp_adapters.doccollection import DocCollectionAdapter
from wsp_adapters.doccollection.adapter import CATALOG
from wsp_adapters.doccollection.catalog import read_catalog_file
from wsp_adapters.doccollection.testing import PUBLIC_A, make_pdf, s3_listing
from wsp_cli.doccollections import CollectionFetcher, withheld_review_row
from wsp_cli.indexer import Indexer
from wsp_cli.ingest import Ingestor, VersionStatus
from wsp_core.ids import make_doc_id
from wsp_core.model import SourceSpec, SourceStatus, Tier
from wsp_core.testing.adapter_contract import FakeFetcher
from wsp_index import ParquetDocumentStore, SqliteChangeFeed, TantivyIndex
from wsp_index.refinery import current_generation, part_dir, set_members
from wsp_vault.ledger import JsonlLedger
from wsp_vault.signing import Ed25519Signer
from wsp_vault.vault import FileVault

BUCKET = "https://bucket.example.gov"
PREFIX = "maps/"
SID = "test-topo"


def sheet(title: str) -> bytes:
    return make_pdf([[title.upper(), *PUBLIC_A[1:2]], ["Quadrangle sheet"]])


def spec() -> SourceSpec:
    return SourceSpec(
        id=SID,
        name="Test topographic maps",
        tier=Tier.T2,
        status=SourceStatus.CANDIDATE,
        license="LicenseRef-US-Gov-PD",
        legal_basis="public-domain",
        domain=("maps",),
        adapter="doc-collection",
        raw={
            "id": SID,
            "access": {
                "adapter": "doc-collection",
                "issuer": "Test Survey",
                "key_prefix": "topo",
                "partitioned": True,
                "origins": ["bucket.example.gov"],
                "discovery": [
                    {
                        "kind": "s3-listing",
                        "bucket_url": BUCKET,
                        "prefix": PREFIX,
                        "key_pattern": r"\.pdf$",
                        "partition": "maps/([A-Z]{2})/",
                        "partition_in_prefix": True,
                    }
                ],
                "filter": {"allow_all": True},
                "currency": {"collection": "historical"},
            },
        },
    )


@pytest.fixture
def vault(tmp_path: Path) -> FileVault:
    signer = Ed25519Signer.generate()
    root = tmp_path / "vault"
    return FileVault(
        root, signer, domains={SID: "maps"}, partitioned=[SID], ledger=JsonlLedger(root / "ledger", signer)
    )


def fetch(vault: FileVault, tmp_path: Path, state: str, version: str, sheets: dict[str, bytes]) -> None:
    prefix = f"{PREFIX}{state.upper()}/"
    routes = {f"{BUCKET}/": s3_listing(prefix, list(sheets.items()))}
    routes.update({f"{BUCKET}/{prefix}{k}": v for k, v in sheets.items()})
    runner = CollectionFetcher(
        vault.root,
        FakeFetcher(routes),
        keys={},
        user_agent="test",
        workdir=tmp_path / "work",
        out=io.StringIO(),
        throttle=False,
    )
    runner.run(spec(), version=version, partition=state)
    result = Ingestor(vault, [spec()], out=io.StringIO(), settle_seconds=0).run(source_id=SID)
    assert {vp.generation_id: vp.status for vp in result.versions}[version] is VersionStatus.INGESTED


def indexer(vault: FileVault, tmp_path: Path) -> Indexer:
    root = tmp_path / "index"
    return Indexer(
        vault,
        [spec()],
        {"doc-collection": DocCollectionAdapter(throttle=False)},
        root,
        TantivyIndex(root / "lexical"),
        SqliteChangeFeed(root / "changes.sqlite"),
    )


def native_ids(vault: FileVault, gid: str) -> list[str]:
    return sorted(m.doc_key for m in read_catalog_file(vault.generation_path(SID, gid) / CATALOG))


def test_partitions_are_kept_promoted_and_indexed_together(vault: FileVault, tmp_path: Path) -> None:
    fetch(vault, tmp_path, "ak", "2026-10-03.ak", {"AK_Anchorage_1950.pdf": sheet("Anchorage")})
    fetch(vault, tmp_path, "ca", "2026-10-02.ca", {"CA_Yosemite_1898.pdf": sheet("Yosemite")})
    # an older partition generation ingested after a newer one of another partition is still promoted
    assert [g.generation_id for g in vault.current_set(SID)] == ["2026-10-02.ca", "2026-10-03.ak"]
    assert native_ids(vault, "2026-10-02.ca") == ["topo:CA_Yosemite_1898.pdf"]  # no other partition

    ix = indexer(vault, tmp_path)
    try:
        (report,) = ix.build([SID])
        assert report.skipped is None
        assert report.indexed is not None
        assert ".set" in report.indexed
        refinery = tmp_path / "index" / "refinery"
        assert set_members(refinery / SID / report.indexed) == ("2026-10-02.ca", "2026-10-03.ak")
        assert part_dir(refinery, SID, "2026-10-03.ak").is_dir()
        assert current_generation(refinery, SID) == report.indexed
        hits = {h.doc_id for h in ix.index.search("Yosemite")} | {
            h.doc_id for h in ix.index.search("Anchorage")
        }
        assert hits == {
            make_doc_id(SID, "topo:CA_Yosemite_1898.pdf"),
            make_doc_id(SID, "topo:AK_Anchorage_1950.pdf"),
        }
    finally:
        ix.index.close()
        ix.feed.close()

    # a new cycle of one partition: the others stay; its catalog carries only its own documents
    fetch(
        vault,
        tmp_path,
        "ak",
        "2026-11-01.ak",
        {"AK_Anchorage_1950.pdf": sheet("Anchorage"), "AK_Juneau_1951.pdf": sheet("Juneau")},
    )
    assert [g.generation_id for g in vault.current_set(SID)] == ["2026-10-02.ca", "2026-11-01.ak"]
    ak = read_catalog_file(vault.generation_path(SID, "2026-11-01.ak") / CATALOG)
    assert sorted(m.doc_key for m in ak) == ["topo:AK_Anchorage_1950.pdf", "topo:AK_Juneau_1951.pdf"]
    assert {m.generation for m in ak} == {"", "2026-10-03.ak"}  # unchanged sheet stays where it was sealed
    assert all(m.currency != "withdrawn-from-index" for m in ak)
    adapter = DocCollectionAdapter(throttle=False)
    deps = adapter.depends_on(ix.ctx(spec(), "x"), vault.generation_path(SID, "2026-11-01.ak"))
    assert deps == {"2026-10-03.ak"}

    ix = indexer(vault, tmp_path)
    try:
        (report,) = ix.build([SID])
        assert (report.upserted, report.deleted) == (1, 0)  # only Juneau is new
        store = ParquetDocumentStore(tmp_path / "index" / "refinery")
        for native in ("topo:CA_Yosemite_1898.pdf", "topo:AK_Anchorage_1950.pdf", "topo:AK_Juneau_1951.pdf"):
            assert store.get(make_doc_id(SID, native)) is not None
    finally:
        ix.index.close()
        ix.feed.close()


def test_fetch_carries_over_only_its_own_partition(vault: FileVault, tmp_path: Path) -> None:
    fetch(vault, tmp_path, "ak", "2026-10-01.ak", {"AK_Anchorage_1950.pdf": sheet("Anchorage")})
    runner = CollectionFetcher(
        vault.root, FakeFetcher({}), keys={}, user_agent="t", workdir=tmp_path / "w", throttle=False
    )
    assert runner.previous_catalog(spec(), "ca") == (None, [])
    gen, catalog = runner.previous_catalog(spec(), "ak")
    assert gen == "2026-10-01.ak"
    assert [m.doc_key for m in catalog] == ["topo:AK_Anchorage_1950.pdf"]


def test_withheld_review_row_reads_the_key_from_the_file_name() -> None:
    row = withheld_review_row("topo__AK_Mine_1950.pdf-1a2b3c4d.pdf", "https://x", "policy review:safety=mine")
    assert row["key"] == "topo:AK_Mine_1950.pdf"
    assert row["rule"] == "review:safety=mine"
