"""``pubmed`` adapter: the PubMed annual baseline plus daily update files (PLAN §6.2, ``http-files``).

**Discover.** Lists ``access.endpoints`` (default the NCBI ``baseline/`` and ``updatefiles/`` folders)
for ``pubmed<YY>n<NNNN>.xml.gz``. The release is the newest baseline year with every update file of
that year: version ``pubmed26`` (baseline only) or ``pubmed26-u1452`` (through update file 1452).
Each file's MD5 comes from its ``<file>.md5`` sidecar (``MD5(name)= hex``); NCBI asks for at most
three requests per second without an API key, so sidecars are fetched at that pace.

**Documents.** Files are parsed with ``xml.etree.ElementTree.iterparse`` and every finished
``PubmedArticle`` / ``PubmedBookArticle`` element is cleared, so memory does not grow with the file.
A PMID appears in the baseline and again in every update file that revises it; update files also
carry ``DeleteCitation`` lists. Files are therefore read **newest first** and a PMID is emitted only
the first time it is seen (its newest version); a deletion marks the PMID as seen without emitting
it, so older copies are skipped. "Seen" is a bitset indexed by PMID (about 5 MB for 40M PMIDs),
which keeps the pass bounded in memory. Files are parsed in worker processes (``access.workers``,
default 4) and their deletions are applied before their articles, which matches NLM's semantics.

Each record: title, authors, journal and year, the structured abstract (``LABEL: text``
paragraphs), MeSH headings and keywords as text lines; ``links`` holds the DOI, PMC and MeSH
descriptor URLs (MeSH terms are *not* aliases: ``Humans`` on 20M records would swamp title
autocomplete). ``native_id`` is the PMID, ``revision`` the ``DateRevised``.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import IO
from xml.etree import ElementTree as ET

from wsp_adapters._common import (
    IncompleteGenerationError,
    access,
    default_workers,
    fetch_all,
    int_option,
    ordered_parallel_map,
    plan_missing,
    str_list,
    upstream_dir,
    verify_file,
)
from wsp_adapters._streams import Bitset, Throttle, listing_links, open_any
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

DEFAULT_ENDPOINTS = (
    "https://ftp.ncbi.nlm.nih.gov/pubmed/baseline/",
    "https://ftp.ncbi.nlm.nih.gov/pubmed/updatefiles/",
)
FILE_RE = re.compile(r"^pubmed(?P<yy>\d{2})n(?P<num>\d{4})\.xml\.gz$")
_MD5_RE = re.compile(r"=\s*([0-9a-fA-F]{32})")
NCBI_INTERVAL = 0.34  # seconds between requests: NCBI's 3 requests/second without an API key

# ISO 639-2 (PubMed <Language>) -> BCP 47 for the most common languages; others pass through.
_LANGS = {
    "eng": "en", "ger": "de", "fre": "fr", "spa": "es", "ita": "it", "jpn": "ja", "rus": "ru",
    "chi": "zh", "por": "pt", "pol": "pl", "dut": "nl", "swe": "sv", "dan": "da", "nor": "no",
    "fin": "fi", "cze": "cs", "hun": "hu", "kor": "ko", "tur": "tr", "gre": "el", "heb": "he",
}  # fmt: skip


def file_key(name: str) -> tuple[str, int] | None:
    """``pubmed26n0042.xml.gz`` -> ``("26", 42)``; None for other names."""
    match = FILE_RE.match(name)
    return (match.group("yy"), int(match.group("num"))) if match else None


GENERATION_RE = re.compile(r"^pubmed(?P<yy>\d{2})(?:-u(?P<upto>\d{4,}))?$")


def content_set(generation_dir: Path) -> dict[int, Path]:
    """Every file of a generation's release, by number: ``{1: .../pubmed26n0001.xml.gz, ...}``."""
    return {n: path for n, (_, path) in content_members(generation_dir).items()}


def content_members(generation_dir: Path) -> dict[int, tuple[str, Path]]:
    """Every file of a generation's release, by file number, with the generation that holds it.

    ``plan`` fetches only the files the previous generation does not hold, so ``pubmed26-u1671``
    stores update files 1335-1671 and reads the baseline (1-1334) from ``pubmed26``. The content set
    is therefore the union of this generation and the earlier generations of the same baseline year
    (``pubmed26``, ``pubmed26-uNNNN`` with ``NNNN`` at most this one's), a file number's newest copy
    winning. It must be exactly the files ``1..N`` (``N`` = the generation's update number, or the
    last baseline file): a missing baseline or update file raises :class:`IncompleteGenerationError`
    instead of refining a partial release, whose diff would delete every document it lacks.

    A generation directory whose name is not a PubMed release id (tests, hand-made generations)
    is read on its own, under the same completeness rule.
    """
    me = GENERATION_RE.match(generation_dir.name)
    chain = [generation_dir]
    upto = 0
    if me is not None:
        upto = int(me.group("upto") or 0)
        siblings = sorted(
            (
                (int(m.group("upto") or 0), p)
                for p in generation_dir.parent.iterdir()
                if p.is_dir() and p != generation_dir and (m := GENERATION_RE.match(p.name))
                if m.group("yy") == me.group("yy") and int(m.group("upto") or 0) <= upto
            ),
            key=lambda up: (up[0], up[1].name),
        )
        chain = [p for _, p in siblings] + [generation_dir]
    files: dict[int, tuple[str, Path]] = {}
    for gen in chain:  # oldest first: a later generation's copy of a number replaces an earlier one
        up = upstream_dir(gen)
        if not up.is_dir():
            continue
        for path in up.iterdir():
            key = file_key(path.name)
            if key is not None and (me is None or key[0] == me.group("yy")):
                files[key[1]] = (gen.name, path)
    if 1 not in files:
        raise IncompleteGenerationError(
            f"{generation_dir.name}: no baseline file (pubmed<YY>n0001) in it or its earlier generations"
        )
    last = max(files)
    if upto and last != upto:
        raise IncompleteGenerationError(f"{generation_dir.name}: update files end at {last}, not {upto}")
    missing = [n for n in range(1, last + 1) if n not in files]
    if missing:
        shown = ", ".join(f"{n:04d}" for n in missing[:10])
        raise IncompleteGenerationError(
            f"{generation_dir.name}: {len(missing)} file(s) missing from the content set: {shown}"
        )
    return files


def parse_md5(text: str) -> str | None:
    """``MD5(pubmed26n0001.xml.gz)= 0123...`` -> the hex digest."""
    match = _MD5_RE.search(text)
    return match.group(1).lower() if match else None


class PubmedAdapter:
    """``SourceAdapter`` for the NLM PubMed baseline and update files."""

    id = "pubmed"

    def __init__(self, *, interval: float = NCBI_INTERVAL) -> None:
        self.interval = interval

    # --- discovery -------------------------------------------------------------------------------

    def discover(self, ctx: Ctx) -> Iterable[ReleaseRef]:
        endpoints = str_list(access(ctx.source).get("endpoints")) or DEFAULT_ENDPOINTS
        throttle = Throttle(self.interval)
        found: dict[str, tuple[int, str]] = {}  # name -> (endpoint index, URL); the first endpoint wins
        for i, base in enumerate(endpoints):
            base = base if base.endswith("/") else base + "/"
            throttle.wait()
            for url in listing_links(ctx.fetcher.get_bytes(base, max_bytes=16 * 1024 * 1024), base):
                name = url.rsplit("/", 1)[-1]
                if file_key(name) is not None:
                    found.setdefault(name, (i, url))
        keys = {n: k for n in found if (k := file_key(n)) is not None}
        if not keys:
            return []
        year = max(k[0] for k in keys.values())
        names = sorted((n for n in keys if keys[n][0] == year), key=lambda n: keys[n][1])
        files: list[RemoteFile] = []
        for name in names:
            throttle.wait()
            url = found[name][1]
            digest = parse_md5(ctx.fetcher.get_bytes(url + ".md5", max_bytes=4096).decode("ascii", "replace"))
            checksums = (Checksum("md5", digest),) if digest else ()
            files.append(RemoteFile(url=url, name=name, checksums=checksums))
        baseline = [n for n in names if found[n][0] == 0]
        updates = [n for n in names if found[n][0] > 0]
        version = f"pubmed{year}"
        if updates:
            version += f"-u{keys[updates[-1]][1]:04d}"
        return [
            ReleaseRef(
                source_id=ctx.source.id,
                version=version,
                published=None,
                files=tuple(files),
                meta={"baseline_files": str(len(baseline)), "update_files": str(len(updates))},
                format="pubmed-xml",
            )
        ]

    def plan(self, ctx: Ctx, have: Generation | None, want: ReleaseRef) -> FetchPlan:
        return plan_missing(have, want)

    def fetch(self, ctx: Ctx, plan: FetchPlan) -> Iterable[FetchResult]:
        return fetch_all(ctx, plan)

    def verify(self, ctx: Ctx, remote: RemoteFile, local: Path) -> Verdict:
        return verify_file(remote, local)

    # --- documents -------------------------------------------------------------------------------

    def depends_on(self, ctx: Ctx, generation_dir: Path) -> frozenset[str]:
        """Earlier generations holding files of this generation's content set (``GenerationDependencies``)."""
        return frozenset(gen for gen, _ in content_members(generation_dir).values()) - {generation_dir.name}

    def documents(self, ctx: Ctx, generation_dir: Path) -> Iterator[DocRecord]:
        spec = ctx.source
        workers = min(int_option(spec, "workers", 4), default_workers())
        files = [p for _, p in sorted(content_set(generation_dir).items(), reverse=True)]
        tasks = (_FileTask(p, spec.id, spec.license) for p in files)
        seen = Bitset()
        for result in ordered_parallel_map(parse_file, tasks, workers, window=workers + 1):
            for pmid in result.deleted:
                seen.add(pmid)
            for pmid, doc in result.docs:
                if pmid not in seen:
                    seen.add(pmid)
                    yield doc


@dataclass(frozen=True, slots=True)
class _FileTask:
    path: Path
    source_id: str
    license: str


@dataclass(frozen=True, slots=True)
class FileResult:
    docs: tuple[tuple[int, DocRecord], ...]
    deleted: tuple[int, ...]


def parse_file(task: _FileTask) -> FileResult:
    """Worker: every article of one PubMed XML file plus its ``DeleteCitation`` PMIDs."""
    with open_any(task.path) as fh:
        docs, deleted = parse_stream(fh, task.source_id, task.license)
    return FileResult(tuple(docs), tuple(deleted))


def parse_stream(
    fh: IO[bytes], source_id: str, license_: str
) -> tuple[list[tuple[int, DocRecord]], list[int]]:
    """Articles (as ``(pmid, doc)``) and deleted PMIDs of one XML stream, in file order."""
    docs: list[tuple[int, DocRecord]] = []
    deleted: list[int] = []
    context: Iterator[tuple[str, ET.Element]] = ET.iterparse(fh, events=("start", "end"))  # noqa: S314
    root: ET.Element | None = None
    for event, elem in context:
        if event == "start":
            if root is None:
                root = elem
            continue
        if elem.tag in ("PubmedArticle", "PubmedBookArticle"):
            doc = article_record(elem, source_id, license_)
            if doc is not None:
                docs.append((int(doc.native_id), doc))
            elem.clear()
            if root is not None:
                root.clear()
        elif elem.tag == "DeleteCitation":
            deleted.extend(int(p.text) for p in elem.iter("PMID") if p.text and p.text.strip().isdigit())
            elem.clear()
    return docs, deleted


def _text(elem: ET.Element | None) -> str:
    """All text inside ``elem`` (keeps ``<i>``/``<sup>`` content), whitespace collapsed."""
    if elem is None:
        return ""
    return " ".join("".join(elem.itertext()).split())


def _date(elem: ET.Element | None) -> datetime | None:
    if elem is None:
        return None
    try:
        return datetime(
            int(elem.findtext("Year", "0")),
            int(elem.findtext("Month", "1")),
            int(elem.findtext("Day", "1")),
            tzinfo=UTC,
        )
    except ValueError:
        return None


def _year(article: ET.Element | None) -> str:
    if article is None:
        return ""
    pub = article.find("Journal/JournalIssue/PubDate")
    if pub is not None:
        year = pub.findtext("Year") or (pub.findtext("MedlineDate") or "")[:4]
        if year.strip():
            return year.strip()
    stamp = article.find("ArticleDate")
    return (stamp.findtext("Year") or "") if stamp is not None else ""


def _authors(container: ET.Element | None) -> list[str]:
    out: list[str] = []
    if container is None:
        return out
    for author in container.iter("Author"):
        collective = author.findtext("CollectiveName")
        if collective:
            out.append(collective.strip())
            continue
        last, fore = author.findtext("LastName", ""), author.findtext("ForeName", "")
        name = f"{fore} {last}".strip()
        if name:
            out.append(name)
    return out


def _abstract(container: ET.Element | None) -> list[str]:
    if container is None:
        return []
    out: list[str] = []
    for part in container.iter("AbstractText"):
        text = _text(part)
        if not text:
            continue
        label = part.get("Label")
        out.append(f"{label.strip().upper()}: {text}" if label else text)
    return out


def article_record(elem: ET.Element, source_id: str, license_: str) -> DocRecord | None:
    """Render one ``PubmedArticle`` or ``PubmedBookArticle`` as a document."""
    is_book = elem.tag == "PubmedBookArticle"
    cite = elem.find("BookDocument" if is_book else "MedlineCitation")
    if cite is None:
        return None
    pmid = (cite.findtext("PMID") or "").strip()
    if not pmid.isdigit():
        return None
    article = cite if is_book else cite.find("Article")
    title = _text(article.find("ArticleTitle")) if article is not None else ""
    if not title and article is not None:
        title = _text(article.find("VernacularTitle")) or _text(article.find("Book/BookTitle"))
    title = title or f"PMID {pmid}"
    journal = _text(article.find("Journal/Title")) if article is not None and not is_book else ""
    year = _year(article) if not is_book else (cite.findtext("Book/PubDate/Year") or "")
    authors = _authors(article.find("AuthorList") if article is not None else None)
    abstract = _abstract(article.find("Abstract") if article is not None else cite.find("Abstract"))
    mesh: list[str] = []
    mesh_links: list[str] = []
    for heading in cite.iter("MeshHeading"):
        descriptor = heading.find("DescriptorName")
        if descriptor is None or not descriptor.text:
            continue
        qualifiers = [q.text.strip() for q in heading.iter("QualifierName") if q.text]
        mesh.append(descriptor.text.strip() + (f" ({', '.join(qualifiers)})" if qualifiers else ""))
        if ui := descriptor.get("UI"):
            mesh_links.append(f"https://id.nlm.nih.gov/mesh/{ui}")
    keywords = [_text(k) for k in cite.iter("Keyword") if _text(k)]
    # The article's own ids only: ``elem.iter`` would also walk ReferenceList/Reference/ArticleIdList,
    # where the cited papers' DOIs live, and the last one would win (audit 2026-10-11 H1).
    own = elem.findall(
        "PubmedBookData/ArticleIdList/ArticleId" if is_book else "PubmedData/ArticleIdList/ArticleId"
    )
    ids: dict[str, str] = {}
    for a in own:
        ids.setdefault(a.get("IdType", ""), (a.text or "").strip())
    doi = ids.get("doi") or next(
        ((e.text or "").strip() for e in cite.iter("ELocationID") if e.get("EIdType") == "doi"), ""
    )
    lines = [title]
    if authors:
        lines.append(", ".join(authors))
    venue = ". ".join(x for x in (journal, year) if x)
    if venue:
        lines.append(venue)
    lines.extend(abstract)
    if mesh:
        lines.append("MeSH terms: " + "; ".join(mesh))
    if keywords:
        lines.append("Keywords: " + "; ".join(keywords))
    text = "\n".join(lines)
    links: list[str] = []
    if doi:
        links.append(f"https://doi.org/{doi}")
    if pmc := ids.get("pmc"):
        links.append(f"https://pmc.ncbi.nlm.nih.gov/articles/{pmc}/")
    links.extend(mesh_links)
    language = (article.findtext("Language") if article is not None else None) or "eng"
    revised = _date(cite.find("DateRevised"))
    return DocRecord(
        doc_id=make_doc_id(source_id, pmid),
        source_id=source_id,
        native_id=pmid,
        title=title,
        lang=_LANGS.get(language.strip().lower(), language.strip().lower()),
        license=license_,
        text=text,
        revision=revised.date().isoformat() if revised else None,
        timestamp=revised,
        url_native=f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
        links=tuple(dict.fromkeys(links)),
        content_hash=content_hash(text),
    )
