"""``pmc-oa`` adapter: the PMC Article Datasets (PubMed Central open access and author manuscripts),
the full text of each article, from NLM's own cloud bucket.

Since 2026 NLM publishes the datasets only as the public bucket ``pmc-oa-opendata`` (the FTP bulk
packages were removed in August 2026). Each article version is a prefix ``PMC<id>.<version>/`` with
its JATS XML, the plain text extracted from it, a JSON of its metadata (title, citation, licence,
PMID, DOI, retraction) and, licence permitting, its PDF and figures. A daily inventory lists every
metadata JSON with its MD5.

**Partitions** (ADR-0010). About 8 million article versions are too many for one release: a
partition is a range of :data:`PARTITION_SIZE` PMC ids (``p0000`` is PMC1-PMC49999), fetched and sealed
on its own (``wsp bulk fetch --source pmc-oa --partition p0193``). Its release is the newest version of
each article in the range: the metadata JSON (checksummed by the inventory) and the text. The XML,
PDFs and figures are not fetched (several terabytes; the text is what search and the reader need).
Neither file is pinned to a checksum: the inventory's MD5 is a day old, and NLM rewrites metadata
after it (2026-10-19: 2,536 of p0273's 22,889 metadata files had changed since the morning's
inventory, and every one was refused). The download is over HTTPS, and a file that does not parse is
left out of the documents.

**Documents.** One per article, keyed by its PMC id (``PMC10000000``): the title and citation above
the text, the article's own licence (``license_code``: CC BY, CC0, ... or TDM for an author
manuscript), linked to its PubMed record (``wsp:pubmed/<pmid>``) and to its page on PMC. A retracted
article says so in its title.

The inventory is read once a day into the adapter's work directory (``<workdir>/inventory/<date>``,
about 250 MB) and reused by every partition fetched that day.
"""

from __future__ import annotations

import csv
import gzip
import io
import json
import logging
import re
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Iterator, Mapping
from pathlib import Path
from typing import Any, cast

from wsp_adapters._common import access, fetch_all, plan_missing, upstream_dir, verify_file
from wsp_core.ids import content_hash, make_doc_id
from wsp_core.model import (
    Checksum,
    DocRecord,
    FetchPlan,
    FetchResult,
    Generation,
    ReleaseRef,
    RemoteFile,
    Verdict,
    VerdictStatus,
)
from wsp_core.ports import Ctx

log = logging.getLogger(__name__)

BUCKET = "https://pmc-oa-opendata.s3.amazonaws.com"
INVENTORY = "inventory-reports/pmc-oa-opendata/metadata/"
PARTITION_SIZE = 50_000
_S3 = "{http://s3.amazonaws.com/doc/2006-03-01/}"
_META_KEY = re.compile(r"^metadata/PMC(\d+)\.(\d+)\.json$")
_PARTITION = re.compile(r"^p(\d{4})$")
_DAY = re.compile(r"^(\d{4}-\d{2}-\d{2})T")
# license_code -> SPDX (the codes are Creative Commons short names, or TDM for text-mining-only).
LICENSES = {
    "CC0": "CC0-1.0",
    "CC BY": "CC-BY-4.0",
    "CC BY-SA": "CC-BY-SA-4.0",
    "CC BY-ND": "CC-BY-ND-4.0",
    "CC BY-NC": "CC-BY-NC-4.0",
    "CC BY-NC-SA": "CC-BY-NC-SA-4.0",
    "CC BY-NC-ND": "CC-BY-NC-ND-4.0",
    "TDM": "LicenseRef-PMC-TDM",
}


def partition_of(pmcid: int) -> str:
    return f"p{pmcid // PARTITION_SIZE:04d}"


def _bucket(ctx: Ctx) -> str:
    return str(access(ctx.source).get("bucket") or BUCKET).rstrip("/")


def latest_inventory(ctx: Ctx) -> str:
    """The newest inventory's prefix (``.../metadata/2026-10-18T01-00Z/``)."""
    url = f"{_bucket(ctx)}/?list-type=2&delimiter=/&prefix={INVENTORY}"
    root = ET.fromstring(ctx.fetcher.get_bytes(url))  # noqa: S314 - NLM's own S3 listing over HTTPS
    dated = [
        p for p in (e.text or "" for e in root.iter(f"{_S3}Prefix")) if _DAY.match(p.removeprefix(INVENTORY))
    ]
    if not dated:
        raise ValueError(f"{ctx.source.id}: no inventory under {INVENTORY}")
    return max(dated)


def inventory_files(ctx: Ctx, prefix: str) -> list[RemoteFile]:
    manifest = json.loads(ctx.fetcher.get_bytes(f"{_bucket(ctx)}/{prefix}manifest.json"))
    return [
        RemoteFile(
            url=f"{_bucket(ctx)}/{f['key']}",
            name=str(f["key"]).rsplit("/", 1)[-1],
            size=int(f["size"]),
            checksums=(Checksum("md5", str(f["MD5checksum"]).lower()),),
        )
        for f in cast(list[Mapping[str, Any]], manifest.get("files") or [])
    ]


def inventory_rows(paths: Iterable[Path]) -> Iterator[tuple[int, int, str]]:
    """``(pmcid, version, etag)`` of every metadata JSON the inventory files list."""
    for path in paths:
        with gzip.open(path, "rt", encoding="utf-8", newline="") as fh:
            for row in csv.reader(fh):
                if len(row) < 4:
                    continue
                m = _META_KEY.match(row[1])
                if m:
                    yield int(m.group(1)), int(m.group(2)), row[3].strip('"').lower()


def newest_versions(rows: Iterable[tuple[int, int, str]], partition: str) -> dict[int, tuple[int, str]]:
    """Each article of ``partition`` at its newest version: ``pmcid -> (version, etag)``."""
    out: dict[int, tuple[int, str]] = {}
    for pmcid, version, etag in rows:
        if partition_of(pmcid) == partition and version > out.get(pmcid, (0, ""))[0]:
            out[pmcid] = (version, etag)
    return out


def release_files(bucket: str, articles: Mapping[int, tuple[int, str]]) -> list[RemoteFile]:
    files: list[RemoteFile] = []
    for pmcid in sorted(articles):
        version, _ = articles[pmcid]  # its inventory MD5 is a day old: not pinned
        stem = f"PMC{pmcid}.{version}"
        files.append(RemoteFile(f"{bucket}/metadata/{stem}.json", f"{stem}.json", None, ()))
        files.append(RemoteFile(f"{bucket}/{stem}/{stem}.txt", f"{stem}.txt", None, ()))
    return files


def article_document(meta: Mapping[str, Any], body: str, source_id: str) -> DocRecord | None:
    """One article: its title and citation above its text, its own licence."""
    pmcid = str(meta.get("pmcid") or "")
    if not re.fullmatch(r"PMC\d+", pmcid) or not body.strip():
        return None
    title = " ".join(str(meta.get("title") or pmcid).split())
    if meta.get("is_retracted") is True:
        title = f"Retracted: {title}"
    citation = " ".join(str(meta.get("citation") or "").split())
    code = str(meta.get("license_code") or "").strip()
    text = "\n\n".join(x for x in (title, citation, body.strip()) if x)
    pmid = str(meta.get("pmid") or "")
    return DocRecord(
        doc_id=make_doc_id(source_id, pmcid),
        source_id=source_id,
        native_id=pmcid,
        title=title,
        lang="en",
        license=LICENSES.get(
            code, f"LicenseRef-PMC-{code.replace(' ', '-')}" if code else "LicenseRef-per-item"
        ),
        text=text,
        url_native=f"https://pmc.ncbi.nlm.nih.gov/articles/{pmcid}/",
        links=(f"wsp:pubmed/{pmid}",) if pmid.isdigit() else (),
        content_hash=content_hash(text),
        meta={
            k: v
            for k, v in (
                ("citation", citation),
                ("pmid", pmid if pmid.isdigit() else ""),
                ("doi", str(meta.get("doi") or "")),
                ("license_code", code),
                ("version", str(meta.get("version") or "")),
                ("manuscript", "yes" if meta.get("is_manuscript") is True else ""),
                ("retracted", "yes" if meta.get("is_retracted") is True else ""),
            )
            if v
        },
    )


class PmcAdapter:
    """``SourceAdapter`` for the PMC Article Datasets, one partition of PMC ids per release."""

    id = "pmc-oa"

    def _inventory(self, ctx: Ctx) -> tuple[str, list[Path]]:
        prefix = latest_inventory(ctx)
        day = _DAY.match(prefix.removeprefix(INVENTORY))
        assert day is not None
        here = ctx.workdir / "inventory" / day.group(1)
        here.mkdir(parents=True, exist_ok=True)
        paths: list[Path] = []
        for remote in inventory_files(ctx, prefix):
            dest = here / remote.name
            if not (dest.is_file() and verify_file(remote, dest).status is VerdictStatus.OK):
                ctx.fetcher.download(remote, dest)
            paths.append(dest)
        return day.group(1), paths

    def discover_partition(self, ctx: Ctx, partition: str) -> list[ReleaseRef]:
        """The release of one partition (``p0193``): its articles at their newest versions."""
        if not _PARTITION.match(partition):
            raise ValueError(f"{ctx.source.id}: partition must look like p0193, got {partition!r}")
        day, paths = self._inventory(ctx)
        articles = newest_versions(inventory_rows(paths), partition)
        if not articles:
            return []
        files = release_files(_bucket(ctx), articles)
        return [
            ReleaseRef(
                ctx.source.id,
                f"{day}.{partition}",
                None,
                tuple(files),
                format="pmc-text",
                license=ctx.source.license,
            )
        ]

    def partitions(self, ctx: Ctx) -> list[str]:
        """Every partition the newest inventory holds articles in, in order."""
        _, paths = self._inventory(ctx)
        return sorted({partition_of(pmcid) for pmcid, _, _ in inventory_rows(paths)})

    def discover(self, ctx: Ctx) -> Iterable[ReleaseRef]:
        return []  # partitioned (ADR-0010): `wsp bulk fetch --source pmc-oa --partition p0193`

    def plan(self, ctx: Ctx, have: Generation | None, want: ReleaseRef) -> FetchPlan:
        return plan_missing(have, want)

    def fetch(self, ctx: Ctx, plan: FetchPlan) -> Iterable[FetchResult]:
        return fetch_all(ctx, plan)

    def verify(self, ctx: Ctx, remote: RemoteFile, local: Path) -> Verdict:
        return verify_file(remote, local)

    def documents(self, ctx: Ctx, generation_dir: Path) -> Iterator[DocRecord]:
        up = upstream_dir(generation_dir)
        for meta_path in sorted(up.glob("PMC*.json")):
            text_path = meta_path.with_suffix(".txt")
            if not text_path.is_file():
                continue
            try:
                meta = cast(Mapping[str, Any], json.loads(meta_path.read_bytes()))
                body = io.TextIOWrapper(text_path.open("rb"), encoding="utf-8", errors="replace").read()
            except (OSError, ValueError) as exc:
                log.warning("%s: %s unreadable: %s", ctx.source.id, meta_path.name, exc)
                continue
            doc = article_document(meta, body, ctx.source.id)
            if doc is not None:
                yield doc
