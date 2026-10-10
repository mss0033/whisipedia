"""``arxiv`` adapter: OAI-PMH metadata plus the PDF mirror (PLAN §6.2, D4).

**Two kinds of generations** live under ``library/science/arxiv/``:

* **Metadata generations** (``YYYY-MM-DD``), one per cycle: an OAI-PMH harvest stored as
  ``upstream/oai-arXiv-<from>-<until>.jsonl.zst`` (one JSON line per ListRecords response page:
  ``{"url", "xml"}``, the raw XML untouched) plus, when ``access.pdf_months`` > 0, the newest
  months of PDFs from the public ``arxiv-dataset`` GCS bucket (MD5 per object). ``CURRENT.txt``
  points at the newest metadata generation; the indexer refines that one.
* **PDF part generations** (``<snapshot>.pdf.<archive>-<yymm>``) from the bulk pull
  (``tools/arxiv_pull.py``), sealed one chunk at a time by ``wsp ingest`` (see
  ``wsp_cli.ingest``). Each holds ``upstream/pdf/<archive>/<yymm>/<file>.pdf`` and the chunk
  manifest ``upstream/manifests/<archive>-<yymm>.jsonl`` (``{path, size, md5}`` lines).

**OAI-PMH.** The endpoint moved in 2025 to ``https://oaipmh.arxiv.org/oai`` (``export.arxiv.org/oai2``
now 301-redirects there); ``metadataPrefix=arXiv`` (separated author names, categories, licence,
DOI), ``YYYY-MM-DD`` granularity, persistent deletions. Harvesting follows resumption tokens with at
most one request per ``access.oai_interval`` seconds (default 3, arXiv's guidance for bulk
harvesting) and retries a failed page after a pause. :meth:`plan` sets ``from=`` to the previous
metadata generation's date minus one day of overlap; without one the whole archive is harvested
(about 2,800 pages, roughly 3 hours at the polite rate).

**Documents.** A metadata generation's documents are the union of *all* harvests up to it, read
newest first so each paper's newest record wins and a deletion hides older copies (an incremental
harvest alone would otherwise look like millions of removals to the snapshot diff). ``native_id``
is the arXiv id (``2301.01234``, ``hep-th/9901001``); the text holds title, authors, categories,
abstract, comments, journal reference, DOI and licence URL; ``links`` holds the DOI and abstract
URLs. ``html_ref`` is ``arxiv-pdf:<generation>/<path>`` for the newest PDF version found in any
PDF part (or metadata) generation of the source, resolved through an SQLite index on local disk
built from the chunk manifests (and cached until the set of generations changes).

**Full text (optional, off by default).** ``access.pdf_text: true`` appends the text of the first
``access.pdf_max_pages`` (default 30) pages of the resolved PDF, extracted with ``pypdfium2`` one
page at a time (bounded memory; each page is closed before the next). This reads every PDF from
the NAS, so expect hours per million papers.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import io
import json
import logging
import re
import sqlite3
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote, urlencode
from xml.etree import ElementTree as ET

import blake3
import zstandard

from wsp_adapters._common import access, int_option, plan_missing, upstream_dir, verify_file
from wsp_adapters._streams import Throttle
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

OAI_URL = "https://oaipmh.arxiv.org/oai"
GCS_API = "https://storage.googleapis.com/storage/v1/b/arxiv-dataset/o"
GCS_MEDIA = "https://storage.googleapis.com/arxiv-dataset/"
OAI_INTERVAL = 3.0
HARVEST_RE = re.compile(
    r"^oai-arXiv-(?P<from>\d{4}-\d{2}-\d{2}|all)-(?P<until>\d{4}-\d{2}-\d{2})\.jsonl\.zst$"
)
PART_RE = re.compile(r"^(?P<snapshot>[0-9][0-9-]*)\.pdf\.(?P<chunk>[a-z-]+-\d{4})$")
PDF_RE = re.compile(r"(?:^|/)pdf/(?P<archive>[a-z-]+)/(?P<yymm>\d{4})/(?P<stem>[^/]+?)v(?P<v>\d+)\.pdf$")
_NS = {"oai": "http://www.openarchives.org/OAI/2.0/", "a": "http://arxiv.org/OAI/arXiv/"}
PDF_INDEX = "arxiv-pdfs.sqlite"


def arxiv_id_of(pdf_path: str) -> tuple[str, int] | None:
    """``pdf/arxiv/2301/2301.01234v2.pdf`` -> ``("2301.01234", 2)``; old-style archives keep their
    prefix: ``pdf/hep-th/9901/9901001v1.pdf`` -> ``("hep-th/9901001", 1)``."""
    match = PDF_RE.search(pdf_path)
    if match is None:
        return None
    archive, stem = match.group("archive"), match.group("stem")
    return (stem if archive == "arxiv" else f"{archive}/{stem}"), int(match.group("v"))


# --- OAI-PMH -------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OaiPage:
    records: list[ET.Element]
    token: str | None


def parse_page(xml: bytes | str) -> OaiPage:
    root = ET.fromstring(xml)  # noqa: S314 - arXiv OAI response
    error = root.find("oai:error", _NS)
    if error is not None:
        if error.get("code") == "noRecordsMatch":
            return OaiPage([], None)
        raise ValueError(f"OAI-PMH error {error.get('code')}: {(error.text or '').strip()}")
    listing = root.find("oai:ListRecords", _NS)
    if listing is None:
        return OaiPage([], None)
    token_el = listing.find("oai:resumptionToken", _NS)
    token = (token_el.text or "").strip() if token_el is not None else ""
    return OaiPage(listing.findall("oai:record", _NS), token or None)


def harvest(
    get: Callable[[str], bytes],
    base: str,
    *,
    from_: str | None,
    until: str,
    throttle: Throttle,
    retries: int = 3,
    pause: Callable[[float], None] = time.sleep,
) -> Iterator[tuple[str, bytes]]:
    """``(url, raw xml)`` of every ListRecords page, following resumption tokens politely."""
    query = {"verb": "ListRecords", "metadataPrefix": "arXiv", "until": until}
    if from_:
        query["from"] = from_
    url: str | None = f"{base}?{urlencode(query)}"
    while url is not None:
        data = b""
        for attempt in range(retries + 1):
            throttle.wait()
            try:
                data = get(url)
                break
            except Exception as exc:
                if attempt == retries:
                    raise
                log.warning("OAI page failed (%s); retrying", exc)
                pause(throttle.interval * 10)
        yield url, data
        token = parse_page(data).token
        url = f"{base}?verb=ListRecords&resumptionToken={quote(token, safe='')}" if token else None


def _text(el: ET.Element | None, path: str) -> str:
    if el is None:
        return ""
    found = el.find(path, _NS)
    return " ".join("".join(found.itertext()).split()) if found is not None else ""


@dataclass(frozen=True, slots=True)
class Paper:
    arxiv_id: str
    datestamp: str
    deleted: bool
    title: str = ""
    authors: tuple[str, ...] = ()
    categories: str = ""
    abstract: str = ""
    comments: str = ""
    journal_ref: str = ""
    doi: str = ""
    license: str = ""
    created: str = ""
    updated: str = ""


def parse_record(record: ET.Element) -> Paper | None:
    header = record.find("oai:header", _NS)
    if header is None:
        return None
    ident = (header.findtext("oai:identifier", "", _NS)).strip()
    arxiv_id = ident.removeprefix("oai:arXiv.org:")
    stamp = header.findtext("oai:datestamp", "", _NS).strip()
    if header.get("status") == "deleted":
        return Paper(arxiv_id, stamp, True)
    meta = record.find("oai:metadata/a:arXiv", _NS)
    if meta is None:
        return None
    authors: list[str] = []
    for author in meta.iterfind("a:authors/a:author", _NS):
        parts = [_text(author, "a:forenames"), _text(author, "a:keyname"), _text(author, "a:suffix")]
        if name := " ".join(p for p in parts if p):
            authors.append(name)
    return Paper(
        arxiv_id=_text(meta, "a:id") or arxiv_id,
        datestamp=stamp,
        deleted=False,
        title=_text(meta, "a:title"),
        authors=tuple(authors),
        categories=_text(meta, "a:categories"),
        abstract=_text(meta, "a:abstract"),
        comments=_text(meta, "a:comments"),
        journal_ref=_text(meta, "a:journal-ref"),
        doi=_text(meta, "a:doi"),
        license=_text(meta, "a:license"),
        created=_text(meta, "a:created"),
        updated=_text(meta, "a:updated"),
    )


def read_harvest(path: Path) -> Iterator[Paper]:
    """Papers of one stored harvest file, in harvest order."""
    with path.open("rb") as raw, zstandard.ZstdDecompressor().stream_reader(raw) as zr:
        for line in io.TextIOWrapper(zr, encoding="utf-8"):
            if not line.strip():
                continue
            page = cast(Mapping[str, str], json.loads(line))
            for record in parse_page(page["xml"]).records:
                paper = parse_record(record)
                if paper is not None:
                    yield paper


# --- PDFs ----------------------------------------------------------------------------------------


def _gcs_month(get: Callable[[str], bytes], yymm: str) -> list[RemoteFile]:
    files: list[RemoteFile] = []
    token: str | None = None
    while True:
        query = {
            "prefix": f"arxiv/arxiv/pdf/{yymm}/",
            "maxResults": "1000",
            "fields": "items(name,size,md5Hash),nextPageToken",
        }
        if token:
            query["pageToken"] = token
        page = cast(dict[str, Any], json.loads(get(f"{GCS_API}?{urlencode(query)}")))
        for item in cast(list[dict[str, Any]], page.get("items", [])):
            name = str(item["name"])
            if not name.endswith(".pdf"):
                continue
            md5 = base64.b64decode(str(item["md5Hash"])).hex()
            files.append(
                RemoteFile(
                    GCS_MEDIA + quote(name),
                    name.rsplit("/", 1)[-1],
                    int(item["size"]),
                    (Checksum("md5", md5),),
                )
            )
        token = page.get("nextPageToken")
        if not token:
            return files


def _gcs_months(get: Callable[[str], bytes]) -> list[str]:
    query = {"prefix": "arxiv/arxiv/pdf/", "delimiter": "/", "fields": "prefixes,nextPageToken"}
    out: list[str] = []
    token: str | None = None
    while True:
        q = dict(query, **({"pageToken": token} if token else {}))
        page = cast(dict[str, Any], json.loads(get(f"{GCS_API}?{urlencode(q)}")))
        out += [p.rstrip("/").rsplit("/", 1)[-1] for p in cast(list[str], page.get("prefixes", []))]
        token = page.get("nextPageToken")
        if not token:
            return sorted(m for m in out if re.fullmatch(r"\d{4}", m))


class PdfIndex:
    """``arxiv id -> (generation, path)`` of the newest PDF version, from every generation's manifests."""

    def __init__(self, source_dir: Path, db_path: Path) -> None:
        gens = sorted(p for p in source_dir.iterdir() if p.is_dir()) if source_dir.is_dir() else []
        signature = ",".join(g.name for g in gens)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(db_path)
        self._db.executescript(
            "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);"
            "CREATE TABLE IF NOT EXISTS pdfs (id TEXT PRIMARY KEY, version INTEGER, gen TEXT, path TEXT)"
            " WITHOUT ROWID;"
        )
        row = self._db.execute("SELECT value FROM meta WHERE key = 'signature'").fetchone()
        if row is None or row[0] != signature:
            self._db.execute("DELETE FROM pdfs")
            for gen in gens:
                self._add_generation(gen)
            self._db.execute("INSERT OR REPLACE INTO meta VALUES ('signature', ?)", (signature,))
            self._db.commit()

    def _add_generation(self, gen: Path) -> None:
        up = gen / "upstream"
        rows: list[tuple[str, int, str, str]] = []
        for manifest in sorted((up / "manifests").glob("*.jsonl")) if (up / "manifests").is_dir() else []:
            with manifest.open(encoding="utf-8") as fh:
                for line in fh:
                    with contextlib.suppress(ValueError, KeyError):
                        path = str(json.loads(line)["path"])
                        if (key := arxiv_id_of(path)) is not None:
                            rows.append((key[0], key[1], gen.name, path))
        # metadata generations hold the incremental month PDFs flat: upstream/2609.01234v1.pdf
        for pdf in up.glob("*.pdf") if up.is_dir() else ():
            key = arxiv_id_of(f"pdf/arxiv/{pdf.name[:4]}/{pdf.name}")
            if key is not None:
                rows.append((key[0], key[1], gen.name, pdf.name))
        self._db.executemany(
            "INSERT INTO pdfs VALUES (?,?,?,?) ON CONFLICT(id) DO UPDATE SET version=excluded.version,"
            " gen=excluded.gen, path=excluded.path WHERE excluded.version >= pdfs.version",
            rows,
        )

    def get(self, arxiv_id: str) -> tuple[str, str] | None:
        row = self._db.execute("SELECT gen, path FROM pdfs WHERE id = ?", (arxiv_id,)).fetchone()
        return (str(row[0]), str(row[1])) if row else None

    def close(self) -> None:
        self._db.close()


def pdf_text(path: Path, max_pages: int) -> str:
    """Plain text of the first ``max_pages`` pages (pypdfium2, one page in memory at a time)."""
    import pypdfium2  # imported on use: only the optional full-text mode needs it

    pdf = pypdfium2.PdfDocument(str(path))
    try:
        out: list[str] = []
        for i in range(min(len(pdf), max_pages)):
            page = pdf[i]
            try:
                textpage = page.get_textpage()
                out.append(textpage.get_text_range())
                textpage.close()
            finally:
                page.close()
        return "\n".join(out)
    finally:
        pdf.close()


def _digests(path: Path) -> tuple[str, str]:
    sha, b3 = hashlib.sha256(), blake3.blake3()
    with path.open("rb") as fh:
        while chunk := fh.read(8 * 1024 * 1024):
            sha.update(chunk)
            b3.update(chunk)
    return sha.hexdigest(), b3.hexdigest()


# --- adapter -------------------------------------------------------------------------------------


class ArxivAdapter:
    """``SourceAdapter`` for arXiv metadata (OAI-PMH) and PDFs (public GCS mirror)."""

    id = "arxiv"

    def __init__(self, *, interval: float | None = None, today: Callable[[], date] | None = None) -> None:
        self.interval = interval
        self.today = today or (lambda: datetime.now(UTC).date())

    def _interval(self, ctx: Ctx) -> float:
        if self.interval is not None:
            return self.interval
        value = access(ctx.source).get("oai_interval", OAI_INTERVAL)
        return max(float(value), OAI_INTERVAL) if isinstance(value, int | float) else OAI_INTERVAL

    def discover(self, ctx: Ctx) -> Iterable[ReleaseRef]:
        spec = ctx.source
        base = str(access(spec).get("oai_endpoint", OAI_URL))
        until = (self.today() - timedelta(days=1)).isoformat()
        harvest_file = RemoteFile(
            url=f"{base}?{urlencode({'verb': 'ListRecords', 'metadataPrefix': 'arXiv', 'until': until})}",
            name=f"oai-arXiv-all-{until}.jsonl.zst",
        )
        files = [harvest_file]
        months = access(spec).get("pdf_months", 2)
        if isinstance(months, int) and months > 0:
            get = ctx.fetcher.get_bytes
            for yymm in _gcs_months(get)[-months:]:
                files += _gcs_month(get, yymm)
        return [
            ReleaseRef(
                source_id=spec.id,
                version=self.today().isoformat(),
                published=None,
                files=tuple(files),
                meta={"oai": base, "until": until},
                format="oai-pmh-arXiv",
            )
        ]

    def plan(self, ctx: Ctx, have: Generation | None, want: ReleaseRef) -> FetchPlan:
        """Harvest from the previous metadata generation's date (one day of overlap)."""
        plan = plan_missing(have, want)
        start: str | None = None
        if have is not None and re.fullmatch(r"\d{4}-\d{2}-\d{2}", have.generation_id):
            start = (date.fromisoformat(have.generation_id) - timedelta(days=1)).isoformat()
        files: list[RemoteFile] = []
        for remote in plan.files:
            match = HARVEST_RE.match(remote.name)
            if match is not None and start is not None:
                until = match.group("until")
                url = remote.url + f"&from={start}"
                remote = RemoteFile(url, f"oai-arXiv-{start}-{until}.jsonl.zst")
            files.append(remote)
        return FetchPlan(release=want, files=tuple(files), previous_generation=plan.previous_generation)

    def fetch(self, ctx: Ctx, plan: FetchPlan) -> Iterable[FetchResult]:
        ctx.workdir.mkdir(parents=True, exist_ok=True)
        throttle = Throttle(self._interval(ctx))
        for remote in plan.files:
            match = HARVEST_RE.match(remote.name)
            if match is None:
                yield ctx.fetcher.download(remote, ctx.workdir / remote.name)
                continue
            yield self._harvest(ctx, remote, throttle, match)

    def _harvest(self, ctx: Ctx, remote: RemoteFile, throttle: Throttle, match: re.Match[str]) -> FetchResult:
        base = remote.url.split("?", 1)[0]
        start = match.group("from")
        dest = ctx.workdir / remote.name
        partial = dest.with_name(dest.name + ".partial")
        pages = 0
        with partial.open("wb") as raw, zstandard.ZstdCompressor(level=10).stream_writer(raw) as zw:
            for url, data in harvest(
                ctx.fetcher.get_bytes,
                base,
                from_=None if start == "all" else start,
                until=match.group("until"),
                throttle=throttle,
            ):
                line = json.dumps({"url": url, "xml": data.decode("utf-8")}, ensure_ascii=False) + "\n"
                zw.write(line.encode("utf-8"))
                pages += 1
                if pages % 100 == 0:
                    log.info("arXiv OAI: %d pages harvested", pages)
        partial.replace(dest)
        sha256, b3 = _digests(dest)
        return FetchResult(dest, dest.stat().st_size, sha256, b3)

    def verify(self, ctx: Ctx, remote: RemoteFile, local: Path) -> Verdict:
        return verify_file(remote, local)

    # --- documents -------------------------------------------------------------------------------

    def documents(self, ctx: Ctx, generation_dir: Path) -> Iterator[DocRecord]:
        spec = ctx.source
        source_dir = generation_dir.parent
        me = generation_dir.name
        harvests: list[tuple[str, Path]] = []
        gens = sorted(p for p in source_dir.iterdir() if p.is_dir() and not PART_RE.match(p.name))
        for gen in gens:
            if gen.name > me and gen != generation_dir:
                continue
            up = upstream_dir(gen)
            harvests += [(gen.name, p) for p in up.glob("oai-arXiv-*.jsonl.zst") if HARVEST_RE.match(p.name)]
        harvests.sort(key=lambda gp: (gp[0], gp[1].name), reverse=True)
        want_text = bool(access(spec).get("pdf_text", False))
        max_pages = int_option(spec, "pdf_max_pages", 30)
        ctx.workdir.mkdir(parents=True, exist_ok=True)
        pdfs = PdfIndex(source_dir, ctx.workdir / PDF_INDEX)
        seen: set[str] = set()  # ~2.7M ids, about 250 MB
        try:
            for _, path in harvests:
                for paper in read_harvest(path):  # a harvest lists each record once
                    if paper.arxiv_id in seen:
                        continue
                    seen.add(paper.arxiv_id)
                    if paper.deleted:
                        continue
                    pdf = pdfs.get(paper.arxiv_id)
                    full = ""
                    if want_text and pdf is not None:
                        try:
                            full = pdf_text(source_dir / pdf[0] / "upstream" / pdf[1], max_pages)
                        except Exception as exc:  # a broken PDF must not stop the pass
                            log.warning("PDF text of %s failed: %s", paper.arxiv_id, exc)
                    yield paper_record(paper, spec.id, spec.license, pdf, full)
        finally:
            pdfs.close()


def paper_record(
    paper: Paper, source_id: str, license_: str, pdf: tuple[str, str] | None = None, full_text: str = ""
) -> DocRecord:
    lines = [paper.title or paper.arxiv_id]
    if paper.authors:
        lines.append(", ".join(paper.authors))
    lines.append(
        f"arXiv:{paper.arxiv_id} [{paper.categories}]" if paper.categories else f"arXiv:{paper.arxiv_id}"
    )
    if paper.abstract:
        lines.append(paper.abstract)
    for label, value in (("Comments", paper.comments), ("Journal reference", paper.journal_ref),
                         ("DOI", paper.doi), ("License", paper.license)):  # fmt: skip
        if value:
            lines.append(f"{label}: {value}")
    if full_text.strip():
        lines.append(full_text.strip())
    text = "\n".join(lines)
    links = [f"https://arxiv.org/abs/{paper.arxiv_id}"]
    if paper.doi:
        links.append(f"https://doi.org/{paper.doi.split()[0]}")
    stamp = paper.updated or paper.created or paper.datestamp
    timestamp = None
    with contextlib.suppress(ValueError):
        timestamp = datetime.fromisoformat(stamp).replace(tzinfo=UTC) if stamp else None
    return DocRecord(
        doc_id=make_doc_id(source_id, paper.arxiv_id),
        source_id=source_id,
        native_id=paper.arxiv_id,
        title=paper.title or f"arXiv:{paper.arxiv_id}",
        lang="en",
        license=paper.license or license_,
        text=text,
        revision=paper.datestamp or None,
        timestamp=timestamp,
        url_native=f"https://arxiv.org/abs/{paper.arxiv_id}",
        html_ref=f"arxiv-pdf:{pdf[0]}/{pdf[1]}" if pdf else None,
        links=tuple(links),
        content_hash=content_hash(text),
    )
