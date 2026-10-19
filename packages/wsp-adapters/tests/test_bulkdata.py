"""Research datasets kept whole (OpenAlex, Chronicling America's page OCR): a partition each entity or
state, every file with its upstream checksum, no documents yet (owner request 2026-10-18)."""

from __future__ import annotations

import json
from pathlib import Path

from adapters_testkit import make_ctx, make_spec

from wsp_adapters.bulkdata import ChroniclingAmericaOcrAdapter, OpenAlexAdapter
from wsp_core.testing.adapter_contract import FakeFetcher

B = "https://openalex.s3.amazonaws.com"
NS = 'xmlns="http://s3.amazonaws.com/doc/2006-03-01/"'


def test_an_openalex_entity_is_a_partition_with_md5s(tmp_path: Path) -> None:
    prefixes = (
        f"<ListBucketResult {NS}>"
        "<CommonPrefixes><Prefix>data/parquet/topics/</Prefix></CommonPrefixes>"
        "<CommonPrefixes><Prefix>data/parquet/works/</Prefix></CommonPrefixes></ListBucketResult>"
    ).encode()
    manifest = {
        "date": "2026-10-14",
        "files": [
            {"url": "s3://openalex/data/parquet/topics/updated_date=2026-10-05/part_0000.parquet",
             "meta": {"content_length": 110582, "record_count": 4516}},
            {"url": "s3://openalex/data/parquet/topics/updated_date=2026-10-05/part_0001.parquet",
             "meta": {"content_length": 99, "record_count": 1}},
        ],
    }  # fmt: skip
    keys = (
        f"<ListBucketResult {NS}><IsTruncated>false</IsTruncated>"
        "<Contents><Key>data/parquet/topics/updated_date=2026-10-05/part_0000.parquet</Key>"
        f"<ETag>&quot;{'a' * 32}&quot;</ETag></Contents>"
        "<Contents><Key>data/parquet/topics/updated_date=2026-10-05/part_0001.parquet</Key>"
        "<ETag>&quot;abc-3&quot;</ETag></Contents></ListBucketResult>"  # multipart: not an MD5
    ).encode()
    routes = {
        f"{B}/?list-type=2&delimiter=/&prefix=data/parquet/": prefixes,
        f"{B}/data/parquet/topics/manifest.json": json.dumps(manifest).encode(),
        f"{B}/?list-type=2&prefix=data%2Fparquet%2Ftopics%2F": keys,
    }
    ctx = make_ctx(
        make_spec("openalex", {"adapter": "openalex", "partitioned": True}), FakeFetcher(routes), tmp_path
    )
    adapter = OpenAlexAdapter()
    assert adapter.partitions(ctx) == ["topics", "works"]
    (r,) = adapter.discover_partition(ctx, "topics")
    assert r.version == "2026-10-14.topics"
    assert [(f.name, f.size) for f in r.files] == [
        ("2026-10-05_part_0000.parquet", 110582),
        ("2026-10-05_part_0001.parquet", 99),
    ]
    assert r.files[0].checksums[0].hexdigest == "a" * 32
    assert r.files[1].checksums == ()
    assert list(adapter.documents(ctx, tmp_path)) == []


def test_chronicling_america_ocr_by_state_with_sha256(tmp_path: Path) -> None:
    def batch(name: str, day: str) -> dict[str, object]:
        return {
            "archive_name": f"{name}.tar.bz2",
            "url": f"https://chroniclingamerica.loc.gov/data/ocr/{name}.tar.bz2",
            "sha256": "F" * 64,
            "sha1": "e" * 40,
            "size": 768843638,
            "archive_created": f"{day}T13:56:34+00:00",
        }

    listing = [batch("ak_albatross_ver01", "2019-07-30"), batch("ak_beluga_ver01", "2024-05-05"),
               batch("az_saguaro_ver02", "2020-01-01")]  # fmt: skip
    routes = {"https://chroniclingamerica.loc.gov/data/ocr/ocr.json": json.dumps(listing).encode()}
    spec = make_spec("chronicling-america", {"adapter": "loc-ocr-batches", "partitioned": True})
    fetcher = FakeFetcher(routes)
    ctx = make_ctx(spec, fetcher, tmp_path)
    adapter = ChroniclingAmericaOcrAdapter()
    assert adapter.partitions(ctx) == ["ak", "az"]
    (r,) = adapter.discover_partition(ctx, "ak")
    assert r.version == "2024-05-05.ak"  # its newest batch
    assert [f.name for f in r.files] == ["ak_albatross_ver01.tar.bz2", "ak_beluga_ver01.tar.bz2"]
    assert r.files[0].checksums[0].algorithm == "sha256"
    assert r.files[0].checksums[0].hexdigest == "f" * 64
    assert adapter.discover_partition(ctx, "wy") == []
    # Read once a day, reused by every state after the first (read once per state, it drew HTTP 429).
    assert fetcher.requests.count("https://chroniclingamerica.loc.gov/data/ocr/ocr.json") == 1
