"""``american-stories`` adapter: newspaper articles of Chronicling America, one document per article.

American Stories (Dell, Harvard; CC BY 4.0) is the Library of Congress's Chronicling America scans,
1770-1963, laid out again into articles: a layout model found each article's headline, byline and
text blocks on the page, and the OCR of those blocks is the article's text. Chronicling America's own
OCR is per page, columns run together; this is per article, which is what a reader looks for.

**Discover.** The dataset is a Hugging Face repository with one archive per year
(``faro_1855.tar.gz``). Its API lists the files with their sizes and SHA-256 (Git LFS); the version is
the repository's last-modified day, and the files are fetched at the repository's commit, so a
release never mixes two revisions.

**Documents.** An archive holds one JSON file per page (the newspaper, its date and edition, the
page number, the scan's address and the page's articles). Each article is a document: the title its
headline and the paper and day it ran (``Death of General Jackson — Richmond Enquirer, 1845-06-17``),
the text its headline, byline and body. Fragments under :data:`MIN_CHARS` (a line of a masthead, a
column rule read as text) are left out. ``access.index_years: [from, to]`` reads only those years:
the whole set is about 370 GB of text, so the index takes it an era at a time.
"""

from __future__ import annotations

import html
import json
import logging
import re
import tarfile
from collections.abc import Iterable, Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote

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
)
from wsp_core.ports import Ctx

log = logging.getLogger(__name__)

REPO = "dell-research-harvard/AmericanStories"
HF = "https://huggingface.co"
MIN_CHARS = 120
_ARCHIVE = re.compile(r"^faro_(\d{4})\.tar\.gz$")
_WS = re.compile(r"[ \t]+")
_BLANKS = re.compile(r"\n\s*\n+")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_FILE = re.compile(r"^\d{4}-\d{2}-\d{2}_p(\d+)_[a-z0-9]+_\d+_\d{8}(\d{2})_\d+$")


def _repo(ctx: Ctx) -> str:
    return str(access(ctx.source).get("repo") or REPO)


def year_of(name: str) -> int | None:
    m = _ARCHIVE.match(name)
    return int(m.group(1)) if m else None


def release_files(tree: Iterable[Mapping[str, Any]], repo: str, revision: str) -> list[RemoteFile]:
    """The yearly archives in a repository listing, at ``revision``, with their LFS SHA-256."""
    files: list[RemoteFile] = []
    for entry in tree:
        name = str(entry.get("path") or "")
        if entry.get("type") != "file" or year_of(name) is None:
            continue
        lfs = cast(Mapping[str, Any], entry.get("lfs") or {})
        sha = str(lfs.get("oid") or "")
        size = int(lfs.get("size") or entry.get("size") or 0) or None
        files.append(
            RemoteFile(
                url=f"{HF}/datasets/{repo}/resolve/{revision}/{quote(name)}",
                name=name,
                size=size,
                checksums=(Checksum("sha256", sha),) if len(sha) == 64 else (),
            )
        )
    return sorted(files, key=lambda f: f.name)


def paper_name(title: str) -> str:
    """A paper's catalogue title as it is cited: ``The Massachusetts spy. [volume] (Boston [Mass.])
    1770-1772`` -> ``The Massachusetts spy (Boston, Mass.)``."""
    t = re.sub(r"\s*\[volume\]", "", html.unescape(title))
    t = re.sub(r"\s+\d{4}-(?:\d{4}|current)\.?\s*$", "", t)
    t = re.sub(r"\s*\[([^\]]+)\]\s*\)", r", \1)", t)  # (Boston [Mass.]) -> (Boston, Mass.)
    t = re.sub(r"\.\s+\(", " (", t)
    return _WS.sub(" ", t).strip().rstrip(".") or "Newspaper"


def _clean(text: str) -> str:
    """OCR column text as paragraphs: a column's lines joined, a word broken at a line end mended
    (``Dwell-`` / ``ing``), blank lines between paragraphs kept."""
    paragraphs: list[str] = []
    for block in _BLANKS.split(html.unescape(text.replace("\r", ""))):
        out = ""
        for line in block.split("\n"):
            line = _WS.sub(" ", line).strip()
            if not line:
                continue
            if out.endswith("-") and out[-2:-1].isalpha() and line[:1].islower():
                out = out[:-1] + line
            else:
                out = f"{out} {line}" if out else line
        if out:
            paragraphs.append(out)
    return "\n\n".join(paragraphs)


def _headline(raw: str) -> str:
    """A headline as one line, without the OCR's leading punctuation; empty if it is not mostly letters."""
    h = _WS.sub(" ", html.unescape(raw).replace("\n", " ")).strip().lstrip(".,;:-—_'\"*|!» ").strip()
    letters = sum(c.isalpha() for c in h)
    if letters < 4 or letters * 2 < sum(not c.isspace() for c in h):  # "[NUMB. 43.", "—— | ;,"
        return ""
    return h if len(h) <= 120 else h[:117].rsplit(" ", 1)[0] + "…"


def page_url(lccn: str, date: str, edition: int, page: int) -> str:
    """The page on loc.gov (where chroniclingamerica.loc.gov/lccn/.../seq-N/ now redirects)."""
    return f"https://www.loc.gov/resource/{lccn}/{date}/ed-{edition}/?sp={page}"


def _int(value: object) -> int:
    try:
        return int(str(value))
    except ValueError:
        return 0  # "na": the dataset's mark for a value it did not read


def page_documents(
    page: Mapping[str, Any], source_id: str, license_: str, name: str = ""
) -> Iterator[DocRecord]:
    """The articles of one page's JSON (``name``: its file in the archive) as documents."""
    paper = cast(Mapping[str, Any], page.get("lccn") or {})
    lccn = str(paper.get("lccn") or "").strip()
    date = str(cast(Mapping[str, Any], page.get("edition") or {}).get("date") or "")
    if not lccn or not _DATE.match(date):
        return
    # The file name is the page's key (1809-05-02_p1_sn84024013_00414216092_1809050201_0491.json:
    # page 1, edition 01, reel frame 491); its page_number field is sometimes "na" or another page's.
    stem = Path(name).stem
    m = _FILE.match(stem)
    number = int(m.group(1)) if m else _int(page.get("page_number"))
    edition = int(m.group(2)) if m and int(m.group(2)) > 0 else 1
    paper_title = paper_name(str(paper.get("title") or ""))
    place = str(paper.get("state") or "")
    when = datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=UTC)
    url = page_url(lccn, date, edition, number) if number > 0 else None
    for art in cast(list[Mapping[str, Any]], page.get("full articles") or []):
        body = _clean(str(art.get("article") or ""))
        if len(body) < MIN_CHARS:
            continue
        aid = str(art.get("full_article_id") or "").strip()
        if not aid:
            continue
        head = _headline(str(art.get("headline") or ""))
        byline = _WS.sub(" ", str(art.get("byline") or "").replace("\n", " ")).strip()
        where = f"{paper_title}, {date}" + (f", page {number}" if number > 0 else "")
        # Two scans of one page (two reels) share a page number: the file name keeps them apart.
        native = f"{stem or f'{lccn}_{date}_p{number}'}/{aid}"
        text = "\n\n".join(x for x in (head, byline, body) if x)
        yield DocRecord(
            doc_id=make_doc_id(source_id, native),
            source_id=source_id,
            native_id=native,
            title=f"{head} — {where}" if head else where,
            lang="en",
            license=license_,
            text=text,
            timestamp=when,
            url_native=url,
            content_hash=content_hash(text),
            meta={
                k: v
                for k, v in (
                    ("newspaper", paper_title),
                    ("place", place),
                    ("date", date),
                    ("page", str(number) if number > 0 else ""),
                    ("lccn", lccn),
                )
                if v
            },
        )


def archive_pages(path: Path) -> Iterator[tuple[str, Mapping[str, Any]]]:
    """The page JSONs of a yearly archive with their names, streamed in archive order; a page that
    does not parse is logged and skipped."""
    with tarfile.open(path, "r|gz") as tar:
        for member in tar:
            if not member.isfile() or not member.name.endswith(".json"):
                continue
            fh = tar.extractfile(member)
            if fh is None:
                continue
            try:
                page = json.loads(fh.read())
            except (ValueError, UnicodeDecodeError) as exc:
                log.warning("%s: %s does not parse: %s", path.name, member.name, exc)
                continue
            if isinstance(page, dict):
                yield member.name, cast(Mapping[str, Any], page)


def index_years(spec_access: Mapping[str, object]) -> tuple[int, int] | None:
    raw = spec_access.get("index_years")
    if not isinstance(raw, list | tuple):
        return None
    years = [int(y) for y in cast(list[Any], raw)]
    if len(years) != 2 or years[0] > years[1]:
        raise ValueError(f"access.index_years must be [from, to], got {raw!r}")
    return years[0], years[1]


class AmericanStoriesAdapter:
    """``SourceAdapter`` for the American Stories dataset (one archive per year)."""

    id = "american-stories"

    def discover(self, ctx: Ctx) -> Iterable[ReleaseRef]:
        repo = _repo(ctx)
        info = json.loads(ctx.fetcher.get_bytes(f"{HF}/api/datasets/{repo}", max_bytes=8 * 1024 * 1024))
        revision = str(info.get("sha") or "")
        modified = str(info.get("lastModified") or "")[:10]
        if not revision or not _DATE.match(modified):
            log.warning("%s: %s gave no revision or date", ctx.source.id, repo)
            return []
        tree = json.loads(
            ctx.fetcher.get_bytes(f"{HF}/api/datasets/{repo}/tree/{revision}", max_bytes=8 * 1024 * 1024)
        )
        files = release_files(cast(list[Mapping[str, Any]], tree), repo, revision)
        if not files:
            log.warning("%s: no yearly archives in %s", ctx.source.id, repo)
            return []
        return [
            ReleaseRef(
                ctx.source.id, modified, None, tuple(files), format="faro-tar", license=ctx.source.license
            )
        ]

    def plan(self, ctx: Ctx, have: Generation | None, want: ReleaseRef) -> FetchPlan:
        return plan_missing(have, want)

    def fetch(self, ctx: Ctx, plan: FetchPlan) -> Iterable[FetchResult]:
        return fetch_all(ctx, plan)

    def verify(self, ctx: Ctx, remote: RemoteFile, local: Path) -> Verdict:
        return verify_file(remote, local)

    def documents(self, ctx: Ctx, generation_dir: Path) -> Iterator[DocRecord]:
        spec = ctx.source
        years = index_years(access(spec))
        archives = sorted(upstream_dir(generation_dir).glob("faro_*.tar.gz"))
        for path in archives:
            year = year_of(path.name)
            if year is None or (years is not None and not years[0] <= year <= years[1]):
                continue
            for name, page in archive_pages(path):
                seen: set[str] = set()  # per page: a year holds millions of articles
                for doc in page_documents(page, spec.id, spec.license, name):
                    if doc.native_id not in seen:
                        seen.add(doc.native_id)
                        yield doc
