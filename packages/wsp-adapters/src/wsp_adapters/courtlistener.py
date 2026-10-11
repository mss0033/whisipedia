"""``courtlistener`` adapter: Free Law Project bulk data (case-law opinions) (PLAN §6.2, ``http-files``).

**Discover.** Lists the public S3 bucket (``ListObjectsV2`` under ``bulk-data/``, following
continuation tokens). Files are ``<table>-<YYYY-MM-DD>.csv.bz2`` plus ``load-bulk-data-<date>.sh``;
a release is a date whose ``opinions`` file exists, version ``YYYY-MM-DD``. ``access.tables``
limits which tables are fetched (default: every table of that date). The S3 ``ETag`` is the MD5 of
a single-part upload; multipart ETags (``...-N``) are not digests, so those files are unverifiable.

**CSV dialect.** The files are PostgreSQL ``COPY ... WITH (FORMAT csv, ESCAPE '\\', HEADER)``:
quotes inside fields are ``\\"`` rather than doubled, and one field can hold a whole opinion
(megabytes), so the reader uses ``escapechar='\\\\'``, ``doublequote=False`` and the largest field
size limit.

**Documents.** One per opinion (``native_id`` = opinion id). The text is the first non-empty of
``html_with_citations``, ``html_columbia``, ``html_lawbox``, ``xml_harvard``, ``html_anon_2020``,
``html`` (rendered to text) and ``plain_text``, under a header with the case name, court, date
filed, docket number and judges. Those come from ``opinion-clusters``, ``dockets`` and ``courts``
when present: before the opinions pass, the cluster and docket tables are streamed once into an
SQLite lookup **on local disk** in ``ctx.workdir`` (never the NAS). Only dockets referenced by a
cluster are kept (a bitset of docket ids filters the tens of millions of PACER docket rows), and
the lookup is reused while the input files are unchanged. Memory stays bounded: one CSV row at a
time plus SQLite's page cache. HTML-to-text runs in worker processes (``access.workers``) in
batches of rows; bz2 is decoded in parallel (:func:`wsp_adapters._streams.open_bz2`).
"""

from __future__ import annotations

import csv
import logging
import re
import sqlite3
import sys
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import quote
from xml.etree import ElementTree as ET

from wsp_adapters._common import (
    access,
    fetch_all,
    int_option,
    ordered_parallel_map,
    plan_missing,
    str_list,
    upstream_dir,
    verify_file,
    workers_for,
)
from wsp_adapters._streams import Bitset, open_bz2, text_lines
from wsp_adapters.html_text import html_to_text
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
)
from wsp_core.ports import Ctx

log = logging.getLogger(__name__)

DEFAULT_ENDPOINT = "https://com-courtlistener-storage.s3-us-west-2.amazonaws.com/bulk-data/"
FILE_RE = re.compile(r"^(?P<table>[a-z0-9-]+?)-(?P<date>\d{4}-\d{2}-\d{2})\.(?:csv\.bz2|sh)$")
_MD5_RE = re.compile(r"^[0-9a-fA-F]{32}$")
TEXT_FIELDS = ("html_with_citations", "html_columbia", "html_lawbox", "xml_harvard", "html_anon_2020", "html")
OPINION_TYPES = {
    "015unamimous": "unanimous",
    "025plurality": "plurality",
    "030concurrence": "concurrence",
    "035concurrenceinpart": "concurrence in part",
    "040dissent": "dissent",
    "050addendum": "addendum",
    "060remittitur": "remittitur",
    "070rehearing": "rehearing",
    "080onthemerits": "on the merits",
    "090onmotiontostrike": "on motion to strike",
}
_S3_NS = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}
_BATCH = 200
_LOOKUP_VERSION = "1"


def csv_rows(fh: Iterable[str]) -> Iterator[dict[str, str]]:
    """Rows of a CourtListener CSV (PostgreSQL ``ESCAPE '\\'`` dialect, huge fields)."""
    csv.field_size_limit(min(sys.maxsize, 2**31 - 1))
    yield from csv.DictReader(fh, escapechar="\\", doublequote=False)


def table_of(name: str) -> str | None:
    match = FILE_RE.match(name)
    return match.group("table") if match else None


# --- discovery -----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class S3Object:
    key: str
    size: int
    etag: str


def parse_s3_listing(data: bytes) -> tuple[list[S3Object], str | None]:
    """Objects and the continuation token of one ``ListObjectsV2`` page."""
    root = ET.fromstring(data)  # noqa: S314 - public S3 listing
    objects: list[S3Object] = []
    for item in root.iterfind("s3:Contents", _S3_NS):
        size = item.findtext("s3:Size", "0", _S3_NS)
        objects.append(
            S3Object(
                key=item.findtext("s3:Key", "", _S3_NS),
                size=int(size) if size.isdigit() else 0,
                etag=item.findtext("s3:ETag", "", _S3_NS).strip('"'),
            )
        )
    truncated = root.findtext("s3:IsTruncated", "false", _S3_NS) == "true"
    token = root.findtext("s3:NextContinuationToken", None, _S3_NS) if truncated else None
    return objects, token


class CourtListenerAdapter:
    """``SourceAdapter`` for CourtListener's bulk CSVs."""

    id = "courtlistener"

    def discover(self, ctx: Ctx) -> Iterable[ReleaseRef]:
        spec = ctx.source
        endpoint = (str_list(access(spec).get("endpoints")) or (DEFAULT_ENDPOINT,))[0]
        bucket, _, prefix = endpoint.rstrip("/").rpartition("/")
        tables = set(str_list(access(spec).get("tables")))
        by_date: dict[str, list[RemoteFile]] = {}
        token: str | None = None
        while True:
            query = f"?list-type=2&prefix={prefix}/"
            if token:
                query += f"&continuation-token={quote(token, safe='')}"
            objects, token = parse_s3_listing(ctx.fetcher.get_bytes(f"{bucket}/{query}"))
            for obj in objects:
                name = obj.key.rsplit("/", 1)[-1]
                match = FILE_RE.match(name)
                if match is None or (tables and match.group("table") not in tables | {"load-bulk-data"}):
                    continue
                checksums = (Checksum("md5", obj.etag.lower()),) if _MD5_RE.match(obj.etag) else ()
                by_date.setdefault(match.group("date"), []).append(
                    RemoteFile(f"{bucket}/{obj.key}", name, obj.size or None, checksums)
                )
            if token is None:
                break
        releases: list[ReleaseRef] = []
        for date in sorted(by_date, reverse=True):
            files = tuple(sorted(by_date[date], key=lambda f: f.name))
            if not any(table_of(f.name) == "opinions" for f in files):
                continue
            releases.append(
                ReleaseRef(
                    source_id=spec.id,
                    version=date,
                    published=None,
                    files=files,
                    meta={"tables": ",".join(t for f in files if (t := table_of(f.name)))},
                    format="courtlistener-csv",
                )
            )
            if len(releases) >= int_option(spec, "max_releases", 1):
                break
        return releases

    def plan(self, ctx: Ctx, have: Generation | None, want: ReleaseRef) -> FetchPlan:
        return plan_missing(have, want)

    def fetch(self, ctx: Ctx, plan: FetchPlan) -> Iterable[FetchResult]:
        return fetch_all(ctx, plan)

    def verify(self, ctx: Ctx, remote: RemoteFile, local: Path) -> Verdict:
        return verify_file(remote, local)

    # --- documents -------------------------------------------------------------------------------

    def documents(self, ctx: Ctx, generation_dir: Path) -> Iterator[DocRecord]:
        spec = ctx.source
        workers = workers_for(spec)
        files: dict[str, Path] = {}
        for path in upstream_dir(generation_dir).iterdir():
            if path.name.endswith(".csv.bz2") and (table := table_of(path.name)):
                files[table] = path
        opinions = files.get("opinions")
        if opinions is None:
            return
        courts = load_courts(files["courts"]) if "courts" in files else {}
        lookup: Lookup | None = None
        if "opinion-clusters" in files:
            ctx.workdir.mkdir(parents=True, exist_ok=True)
            path = build_lookup(
                ctx.workdir / "courtlistener-lookup.sqlite",
                files["opinion-clusters"],
                files.get("dockets"),
                workers=workers,
            )
            lookup = Lookup(path)
        try:
            batches = _opinion_batches(opinions, lookup, courts, spec.id, spec.license, workers)
            for docs in ordered_parallel_map(render_batch, batches, workers):
                yield from docs
        finally:
            if lookup is not None:
                lookup.close()


# --- metadata lookup (local SQLite) --------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CaseMeta:
    case_name: str = ""
    date_filed: str = ""
    judges: str = ""
    slug: str = ""
    precedential_status: str = ""
    court: str = ""
    docket_number: str = ""


def load_courts(path: Path) -> dict[str, str]:
    """``court id -> full name`` (a few thousand rows)."""
    with open_bz2(path) as fh:
        rows = csv_rows(text_lines(fh))
        return {r["id"]: r.get("full_name") or r.get("short_name") or r["id"] for r in rows}


def _signature(*paths: Path | None) -> str:
    return ";".join(f"{p.name}:{p.stat().st_size}" for p in paths if p is not None) + f";v{_LOOKUP_VERSION}"


def build_lookup(db_path: Path, clusters: Path, dockets: Path | None, *, workers: int = 1) -> Path:
    """Stream clusters (and the dockets they reference) into ``db_path``; reuse it if up to date."""
    signature = _signature(clusters, dockets)
    if db_path.is_file():
        try:
            with sqlite3.connect(db_path) as db:
                row = db.execute("SELECT value FROM meta WHERE key = 'signature'").fetchone()
            if row is not None and row[0] == signature:
                return db_path
        except sqlite3.DatabaseError:
            pass
        db_path.unlink()
    partial = db_path.with_name(db_path.name + ".partial")
    partial.unlink(missing_ok=True)
    db = sqlite3.connect(partial)
    try:
        db.executescript(
            "PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF;"
            "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);"
            "CREATE TABLE clusters (id INTEGER PRIMARY KEY, docket_id INTEGER, case_name TEXT,"
            " date_filed TEXT, judges TEXT, slug TEXT, status TEXT);"
            "CREATE TABLE dockets (id INTEGER PRIMARY KEY, court_id TEXT, docket_number TEXT);"
        )
        wanted = Bitset()
        batch: list[tuple[object, ...]] = []
        with open_bz2(clusters, workers) as fh:
            for r in csv_rows(text_lines(fh)):
                docket = int(r["docket_id"]) if r.get("docket_id", "").isdigit() else None
                if docket is not None:
                    wanted.add(docket)
                name = r.get("case_name") or r.get("case_name_short") or r.get("case_name_full") or ""
                batch.append(
                    (int(r["id"]), docket, name, r.get("date_filed", ""), r.get("judges", ""),
                     r.get("slug", ""), r.get("precedential_status", ""))
                )  # fmt: skip
                if len(batch) >= 10_000:
                    db.executemany("INSERT OR REPLACE INTO clusters VALUES (?,?,?,?,?,?,?)", batch)
                    batch.clear()
        db.executemany("INSERT OR REPLACE INTO clusters VALUES (?,?,?,?,?,?,?)", batch)
        batch.clear()
        if dockets is not None:
            with open_bz2(dockets, workers) as fh:
                for r in csv_rows(text_lines(fh)):
                    if not r.get("id", "").isdigit() or int(r["id"]) not in wanted:
                        continue
                    batch.append((int(r["id"]), r.get("court_id", ""), r.get("docket_number", "")))
                    if len(batch) >= 10_000:
                        db.executemany("INSERT OR REPLACE INTO dockets VALUES (?,?,?)", batch)
                        batch.clear()
            db.executemany("INSERT OR REPLACE INTO dockets VALUES (?,?,?)", batch)
        db.execute("INSERT INTO meta VALUES ('signature', ?)", (signature,))
        db.commit()
    finally:
        db.close()
    partial.replace(db_path)
    return db_path


class Lookup:
    """Read side of the cluster/docket lookup."""

    def __init__(self, path: Path) -> None:
        self._db = sqlite3.connect(path)

    def case(self, cluster_id: str, courts: Mapping[str, str]) -> CaseMeta:
        if not cluster_id.isdigit():
            return CaseMeta()
        row = self._db.execute(
            "SELECT c.case_name, c.date_filed, c.judges, c.slug, c.status, d.court_id, d.docket_number"
            " FROM clusters c LEFT JOIN dockets d ON d.id = c.docket_id WHERE c.id = ?",
            (int(cluster_id),),
        ).fetchone()
        if row is None:
            return CaseMeta()
        name, filed, judges, slug, status, court, number = (str(v) if v is not None else "" for v in row)
        return CaseMeta(name, filed, judges, slug, status, courts.get(court, court), number)

    def close(self) -> None:
        self._db.close()


# --- opinions --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Opinion:
    opinion_id: str
    cluster_id: str
    kind: str
    author: str
    modified: str
    body: str
    is_markup: bool
    case: CaseMeta


@dataclass(frozen=True, slots=True)
class _Batch:
    source_id: str
    license: str
    rows: tuple[_Opinion, ...]


def best_body(row: Mapping[str, str]) -> tuple[str, bool]:
    """The richest text field of an opinion row and whether it is HTML/XML markup."""
    for field in TEXT_FIELDS:
        value = row.get(field) or ""
        if value.strip():
            return value, True
    return row.get("plain_text") or "", False


def _opinion_batches(
    path: Path,
    lookup: Lookup | None,
    courts: Mapping[str, str],
    source_id: str,
    license_: str,
    workers: int,
) -> Iterator[_Batch]:
    rows: list[_Opinion] = []
    with open_bz2(path, workers) as fh:
        for r in csv_rows(text_lines(fh)):
            if not r.get("id", "").isdigit():
                continue
            body, markup = best_body(r)
            cluster = r.get("cluster_id", "")
            case = lookup.case(cluster, courts) if lookup is not None else CaseMeta()
            rows.append(
                _Opinion(r["id"], cluster, r.get("type", ""), r.get("author_str", ""),
                         r.get("date_modified", ""), body, markup, case)
            )  # fmt: skip
            if len(rows) >= _BATCH:
                yield _Batch(source_id, license_, tuple(rows))
                rows = []
    if rows:
        yield _Batch(source_id, license_, tuple(rows))


def _timestamp(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("+00", "+00:00") if value.endswith("+00") else value)
    except ValueError:
        return None


def render_batch(batch: _Batch) -> list[DocRecord]:
    """Worker: opinion rows -> documents (HTML/XML to text is the expensive part)."""
    return [opinion_record(o, batch.source_id, batch.license) for o in batch.rows]


def opinion_record(o: _Opinion, source_id: str, license_: str) -> DocRecord:
    body = html_to_text(o.body) if o.is_markup else o.body.strip()
    case = o.case
    kind = OPINION_TYPES.get(o.kind, "")
    title = case.case_name or f"Opinion {o.opinion_id}"
    if kind:
        title += f" ({kind}{', ' + o.author if o.author else ''})"
    header = [case.case_name or title]
    facts = [
        case.court,
        f"Filed {case.date_filed}" if case.date_filed else "",
        f"Docket {case.docket_number}" if case.docket_number else "",
    ]
    if any(facts):
        header.append(". ".join(f for f in facts if f))
    if case.judges:
        header.append(f"Judges: {case.judges}")
    if o.author:
        header.append(f"Opinion by {o.author}")
    text = "\n".join([*header, body]).strip()
    url = None
    if o.cluster_id:
        url = f"https://www.courtlistener.com/opinion/{o.cluster_id}/{case.slug or 'opinion'}/"
    stamp = _timestamp(o.modified)
    return DocRecord(
        doc_id=make_doc_id(source_id, o.opinion_id),
        source_id=source_id,
        native_id=o.opinion_id,
        title=title,
        lang="en",
        license=license_,
        text=text,
        revision=stamp.isoformat() if stamp else None,
        timestamp=stamp,
        url_native=url,
        content_hash=content_hash(text),
    )
