"""Large research datasets kept whole for later use (owner request 2026-10-18): fetched and sealed,
partition by partition (ADR-0010, ``wsp bulk fetch --partition``), read into documents later.

* ``openalex`` (OurResearch's OpenAlex snapshot, CC0): the Parquet snapshot in the public bucket
  ``openalex``, one partition per entity (``works`` 744 GB and 465 million records, ``authors``,
  ``sources``, ``institutions``, ``topics``, ...). Each entity's ``manifest.json`` lists its files with
  their sizes; the bucket listing gives their MD5 (a single-part ETag). The version is the manifest's
  date.
* ``chronicling-america`` (the Library of Congress's page OCR of Chronicling America, public domain):
  the 3,010 OCR batch archives (2.47 TB) listed by ``ocr.json`` with their SHA-256, one partition per
  state (the batch's first word: ``ak_albatross_ver01`` is Alaska's). American Stories already reads
  the same scans as articles; the page OCR keeps every column it left out.

Neither yields documents yet (``documents`` is empty, and the registry keeps them ``index: false``):
465 million works or 23 million pages would outgrow the local index disk; a reader of them is next.
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Iterator, Mapping
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlencode

from wsp_adapters._common import access, fetch_all, plan_missing, verify_file
from wsp_core.model import (
    Checksum,
    DocRecord,
    FetchPlan,
    FetchResult,
    Generation,
    ReleaseRef,
    RemoteFile,
    Verdict,
)
from wsp_core.ports import Ctx

OPENALEX = "https://openalex.s3.amazonaws.com"
OPENALEX_PREFIX = "data/parquet/"
LOC_OCR = "https://chroniclingamerica.loc.gov/data/ocr/ocr.json"
_S3 = "{http://s3.amazonaws.com/doc/2006-03-01/}"
_PARTITION = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _check(partition: str) -> str:
    if not _PARTITION.match(partition):
        raise ValueError(f"partition must be lowercase letters, digits and -, got {partition!r}")
    return partition


class _DataOnly:
    """The parts of ``SourceAdapter`` both share: plan, fetch, verify; no documents yet."""

    def discover(self, ctx: Ctx) -> Iterable[ReleaseRef]:
        return []  # partitioned (ADR-0010): `wsp bulk fetch --source S --partition P`

    def plan(self, ctx: Ctx, have: Generation | None, want: ReleaseRef) -> FetchPlan:
        return plan_missing(have, want)

    def fetch(self, ctx: Ctx, plan: FetchPlan) -> Iterable[FetchResult]:
        return fetch_all(ctx, plan)

    def verify(self, ctx: Ctx, remote: RemoteFile, local: Path) -> Verdict:
        return verify_file(remote, local)

    def documents(self, ctx: Ctx, generation_dir: Path) -> Iterator[DocRecord]:
        return iter(())


# --- OpenAlex ------------------------------------------------------------------------------------------


def _s3_etags(ctx: Ctx, bucket: str, prefix: str) -> dict[str, str]:
    """Every key under ``prefix`` with its MD5, where the ETag is one (a single-part upload)."""
    out: dict[str, str] = {}
    token = ""
    while True:
        params = {"list-type": "2", "prefix": prefix, **({"continuation-token": token} if token else {})}
        root = ET.fromstring(ctx.fetcher.get_bytes(f"{bucket}/?{urlencode(params)}"))  # noqa: S314 - HTTPS S3
        for c in root.iter(f"{_S3}Contents"):
            key, etag = c.findtext(f"{_S3}Key") or "", (c.findtext(f"{_S3}ETag") or "").strip('"').lower()
            if re.fullmatch(r"[0-9a-f]{32}", etag):
                out[key] = etag
        token = root.findtext(f"{_S3}NextContinuationToken") or ""
        if root.findtext(f"{_S3}IsTruncated") != "true" or not token:
            return out


def openalex_files(manifest: Mapping[str, Any], bucket: str, etags: Mapping[str, str]) -> list[RemoteFile]:
    """An entity's files: ``s3://openalex/data/parquet/works/updated_date=2025-11-06/part_0054.parquet``
    is named ``2025-11-06_part_0054.parquet`` (every name in a release is unique)."""
    files: list[RemoteFile] = []
    for entry in cast(list[Mapping[str, Any]], manifest.get("files") or manifest.get("entries") or []):
        key = str(entry.get("url") or "").removeprefix("s3://openalex/")
        if not key.endswith(".parquet"):
            continue
        part = key.rsplit("/", 2)
        day = part[-2].removeprefix("updated_date=") if len(part) == 3 else ""
        name = f"{day}_{part[-1]}" if day else part[-1]
        meta = cast(Mapping[str, Any], entry.get("meta") or {})
        md5 = etags.get(key)
        files.append(
            RemoteFile(
                url=f"{bucket}/{key}",
                name=name,
                size=int(meta["content_length"]) if meta.get("content_length") is not None else None,
                checksums=(Checksum("md5", md5),) if md5 else (),
            )
        )
    return files


class OpenAlexAdapter(_DataOnly):
    """``SourceAdapter`` for the OpenAlex Parquet snapshot, one entity per partition."""

    id = "openalex"

    def partitions(self, ctx: Ctx) -> list[str]:
        bucket = str(access(ctx.source).get("bucket") or OPENALEX).rstrip("/")
        root = ET.fromstring(  # noqa: S314 - HTTPS S3 listing
            ctx.fetcher.get_bytes(f"{bucket}/?list-type=2&delimiter=/&prefix={OPENALEX_PREFIX}")
        )
        names = [(e.text or "").removeprefix(OPENALEX_PREFIX).strip("/") for e in root.iter(f"{_S3}Prefix")]
        return sorted(n for n in names if n and _PARTITION.match(n))

    def discover_partition(self, ctx: Ctx, partition: str) -> list[ReleaseRef]:
        bucket = str(access(ctx.source).get("bucket") or OPENALEX).rstrip("/")
        prefix = f"{OPENALEX_PREFIX}{_check(partition)}/"
        manifest = cast(
            Mapping[str, Any], json.loads(ctx.fetcher.get_bytes(f"{bucket}/{prefix}manifest.json"))
        )
        day = str(manifest.get("date") or "")
        if not _DATE.match(day):
            raise ValueError(f"{ctx.source.id}: {partition} manifest has no date")
        files = openalex_files(manifest, bucket, _s3_etags(ctx, bucket, prefix))
        if not files:
            return []
        return [
            ReleaseRef(
                ctx.source.id, f"{day}.{partition}", None, tuple(files), format="parquet", license="CC0-1.0"
            )
        ]


# --- Chronicling America page OCR ----------------------------------------------------------------------


def ocr_batches(listing: Iterable[Mapping[str, Any]]) -> Iterator[tuple[str, RemoteFile, str]]:
    """``(state, archive, created day)`` of every OCR batch in ``ocr.json``."""
    for b in listing:
        name, url = str(b.get("archive_name") or ""), str(b.get("url") or "")
        if not name.endswith(".tar.bz2") or not url.startswith("https://"):
            continue
        sums = [Checksum(a, str(b[a]).lower()) for a in ("sha256", "sha1") if b.get(a)]
        size = b.get("size")
        state = name.split("_", 1)[0].lower()
        yield (
            state,
            RemoteFile(url, name, int(size) if size else None, tuple(sums)),
            str(b.get("archive_created") or "")[:10],
        )


class ChroniclingAmericaOcrAdapter(_DataOnly):
    """``SourceAdapter`` for Chronicling America's OCR batch archives, one state per partition."""

    id = "loc-ocr-batches"

    def _batches(self, ctx: Ctx) -> list[tuple[str, RemoteFile, str]]:
        url = str(access(ctx.source).get("listing") or LOC_OCR)
        listing = json.loads(ctx.fetcher.get_bytes(url, max_bytes=64 * 1024 * 1024))
        return list(ocr_batches(cast(list[Mapping[str, Any]], listing)))

    def partitions(self, ctx: Ctx) -> list[str]:
        return sorted({state for state, _, _ in self._batches(ctx) if _PARTITION.match(state)})

    def discover_partition(self, ctx: Ctx, partition: str) -> list[ReleaseRef]:
        mine = [(f, day) for state, f, day in self._batches(ctx) if state == _check(partition)]
        if not mine:
            return []
        newest = (
            max(day for _, day in mine if _DATE.match(day)) if any(_DATE.match(d) for _, d in mine) else ""
        )
        version = f"{newest or 'undated'}.{partition}"
        files = tuple(sorted((f for f, _ in mine), key=lambda f: f.name))
        return [
            ReleaseRef(ctx.source.id, version, None, files, format="ndnp-ocr", license=ctx.source.license)
        ]
