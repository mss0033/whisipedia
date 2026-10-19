"""PubMed Central from NLM's bucket: partitions of PMC ids, the newest version of each article, its text
under its title and citation, its own licence (owner request 2026-10-18)."""

from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path

from adapters_testkit import make_ctx, make_spec

from wsp_adapters.pmc import PmcAdapter, article_document, newest_versions, partition_of
from wsp_core.testing.adapter_contract import FakeFetcher

B = "https://pmc-oa-opendata.s3.amazonaws.com"
INV = "inventory-reports/pmc-oa-opendata/metadata/"


def _inventory() -> bytes:
    rows = [
        f'"pmc-oa-opendata","metadata/PMC{p}.{v}.json","2026-09-08T00:48:31.000Z","{e}"'
        for p, v, e in [
            (9650000, 1, "a" * 32),
            (9650000, 2, "b" * 32),  # a newer version: it is the one fetched
            (9699999, 1, "c" * 32),
            (9700000, 1, "d" * 32),  # the next partition
            (12, 1, "e" * 32),
        ]
    ]
    return gzip.compress(("\n".join(rows) + "\n").encode())


def test_partitions_hold_50000_ids() -> None:
    assert partition_of(1) == "p0000"
    assert partition_of(9_650_000) == "p0193"
    assert partition_of(9_699_999) == "p0193"
    assert partition_of(9_700_000) == "p0194"
    rows = [(9650000, 1, "x"), (9650000, 2, "y"), (9700000, 1, "z")]
    assert newest_versions(rows, "p0193") == {9650000: (2, "y")}


def test_a_partition_release_lists_each_articles_metadata_and_text(tmp_path: Path) -> None:
    inv = _inventory()
    listing = (
        '<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
        f"<CommonPrefixes><Prefix>{INV}2026-10-17T01-00Z/</Prefix></CommonPrefixes>"
        f"<CommonPrefixes><Prefix>{INV}2026-10-18T01-00Z/</Prefix></CommonPrefixes>"
        f"<CommonPrefixes><Prefix>{INV}data/</Prefix></CommonPrefixes>"
        "</ListBucketResult>"
    ).encode()
    manifest = {
        "files": [
            {"key": f"{INV}data/one.csv.gz", "size": len(inv), "MD5checksum": hashlib.md5(inv).hexdigest()}
        ]
    }
    routes = {
        f"{B}/?list-type=2&delimiter=/&prefix={INV}": listing,
        f"{B}/{INV}2026-10-18T01-00Z/manifest.json": json.dumps(manifest).encode(),
        f"{B}/{INV}data/one.csv.gz": inv,
    }
    spec = make_spec("pmc-oa", {"adapter": "pmc-oa", "partitioned": True})
    ctx = make_ctx(spec, FakeFetcher(routes), tmp_path / "work")
    adapter = PmcAdapter()
    assert adapter.partitions(ctx) == ["p0000", "p0193", "p0194"]
    (release,) = adapter.discover_partition(ctx, "p0193")
    assert release.version == "2026-10-18.p0193"
    assert [f.name for f in release.files] == [
        "PMC9650000.2.json",
        "PMC9650000.2.txt",
        "PMC9699999.1.json",
        "PMC9699999.1.txt",
    ]
    assert release.files[0].url == f"{B}/metadata/PMC9650000.2.json"
    assert release.files[0].checksums == ()  # the inventory's MD5 is a day old; metadata changes after it
    assert release.files[1].url == f"{B}/PMC9650000.2/PMC9650000.2.txt"
    assert adapter.discover_partition(ctx, "p0100") == []
    assert list(adapter.discover(ctx)) == []  # partitioned: never the whole set at once


def test_each_article_is_a_document_with_its_licence_and_pubmed_link(tmp_path: Path) -> None:
    gen = tmp_path / "2026-10-18.p0193" / "upstream"
    gen.mkdir(parents=True)
    meta = {
        "pmcid": "PMC9650000",
        "version": 2,
        "pmid": 37471719,
        "doi": "10.1000/x",
        "title": "Heat  stroke in\nmarathon runners",
        "citation": "J Sports Med. 2022;8(1):54-64.",
        "license_code": "CC BY",
        "is_retracted": False,
        "is_manuscript": False,
    }
    (gen / "PMC9650000.2.json").write_text(json.dumps(meta))
    (gen / "PMC9650000.2.txt").write_text("Abstract\nHeat stroke is a medical emergency.\n")
    (gen / "PMC9699999.1.json").write_text(json.dumps({**meta, "pmcid": "PMC9699999"}))  # no text: left out
    spec = make_spec("pmc-oa", {"adapter": "pmc-oa"})
    (doc,) = list(PmcAdapter().documents(make_ctx(spec, FakeFetcher({}), tmp_path / "w"), gen.parent))
    assert (doc.native_id, doc.title, doc.license) == (
        "PMC9650000",
        "Heat stroke in marathon runners",
        "CC-BY-4.0",
    )
    assert doc.text.startswith(
        "Heat stroke in marathon runners\n\nJ Sports Med. 2022;8(1):54-64.\n\nAbstract"
    )
    assert doc.links == ("wsp:pubmed/37471719",)
    assert doc.url_native == "https://pmc.ncbi.nlm.nih.gov/articles/PMC9650000/"
    assert doc.meta["doi"] == "10.1000/x"
    retracted = article_document({**meta, "is_retracted": True, "license_code": "TDM"}, "text", "pmc-oa")
    assert retracted is not None
    assert retracted.title.startswith("Retracted: ")
    assert retracted.license == "LicenseRef-PMC-TDM"
    assert article_document({**meta, "pmcid": "nope"}, "text", "pmc-oa") is None
