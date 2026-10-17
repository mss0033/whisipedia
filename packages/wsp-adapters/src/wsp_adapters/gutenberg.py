"""``gutenberg`` adapter: Project Gutenberg's bulk feeds (PLAN §6.2).

The raw rsync mirror is too slow (about 150 KB/s), so the library takes the official feeds from
``https://www.gutenberg.org/cache/epub/feeds/``:

* ``pg_catalog.csv.gz``: one row per book (``Text#, Type, Issued, Title, Language, Authors,
  Subjects, LoCC, Bookshelves``);
* ``rdf-files.tar.bz2``: one RDF/XML file per book (``cache/epub/<id>/pg<id>.rdf``) with the
  summary (``marc520``), alternative titles and author Wikipedia pages;
* ``txt-files.tar.zip``: a ZIP holding a single ~30 GB tar of plain texts
  (``cache/epub/<id>/pg<id>.txt``).

**Discover.** The release is the three feed files, version = today's date (UTC) or the newest
``Last-Modified`` when the fetcher supports ``head()``. Gutenberg publishes no checksums for the
feeds, so the files are *unverifiable*; :meth:`verify` still detects truncation by size.

**Documents** (one per book, ``native_id`` = the ebook number). The catalog and the RDF summaries
are loaded first (~75k books, tens of MB). The texts are then **streamed** out of the ZIP's tar
member (``zipfile`` -> ``tarfile`` in stream mode; nothing is extracted to disk) and the Project
Gutenberg header and licence footer are cut at the ``*** START OF``/``*** END OF`` markers. Books
without a plain text (audio books, some collections) still get a catalog-only document.

**Reading copy.** When the Kiwix Gutenberg ZIM is in the vault (``access.reading_copy``, default
``gutenberg-zim``, found next to this source as ``<domain>/<reading_copy>/CURRENT.txt``), each book
links to its ZIM page: the ZIM names book pages ``<title>.<id>``, so one pass over the ZIM's paths
builds ``id -> path``, cached as JSON in ``ctx.workdir``. ``links`` then holds
``wsp:gutenberg-zim/<path>``, the Navigator's illustrated EPUB/HTML copy of the same book. Author
pages are named the same way, ``<author>.<author id>``, and author ids overlap ebook numbers: only a
number with a ``<title>_cover.<id>`` page is a book.
"""

from __future__ import annotations

import contextlib
import csv
import gzip
import json
import logging
import re
import tarfile
import zipfile
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from xml.etree import ElementTree as ET

from wsp_adapters._common import (
    LAST_MODIFIED_META,
    access,
    fetch_all,
    files_across_generations,
    head_release,
    head_version,
    holder_of,
    plan_by_head,
    str_list,
    upstream_dir,
    verify_file,
)
from wsp_adapters._streams import open_bz2
from wsp_core.ids import InvalidDocIdError, content_hash, make_doc_id
from wsp_core.model import (
    BYLINE_META,
    DocRecord,
    FetchPlan,
    FetchResult,
    Generation,
    ReleaseRef,
    RemoteFile,
    SourceSpec,
    Verdict,
)
from wsp_core.ports import Ctx

log = logging.getLogger(__name__)

FEEDS_URL = "https://www.gutenberg.org/cache/epub/feeds/"
FEED_FILES = ("pg_catalog.csv.gz", "rdf-files.tar.bz2", "txt-files.tar.zip")
CATALOG = "pg_catalog.csv.gz"
RDF = "rdf-files.tar.bz2"
TEXTS = "txt-files.tar.zip"
_TXT_RE = re.compile(r"(?:^|/)pg(\d+)\.txt$")
_RDF_RE = re.compile(r"(?:^|/)pg(\d+)\.rdf$")
_ZIM_BOOK_RE = re.compile(r"\.(\d+)$")
_ZIM_PATHS_RULE = 2  # in the cache key: a cache built by an earlier rule is built again
_START_RE = re.compile(r"^\*\*\* ?START OF (THE|THIS) PROJECT GUTENBERG.*$", re.MULTILINE | re.IGNORECASE)
_END_RE = re.compile(r"^\*\*\* ?END OF (THE|THIS) PROJECT GUTENBERG.*$", re.MULTILINE | re.IGNORECASE)
_NS = {
    "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "dcterms": "http://purl.org/dc/terms/",
    "pgterms": "http://www.gutenberg.org/2009/pgterms/",
}


@dataclass
class Book:
    """Catalog metadata of one ebook."""

    number: int
    title: str
    kind: str = "Text"
    issued: str = ""
    languages: tuple[str, ...] = ()
    authors: str = ""
    subjects: str = ""
    locc: str = ""
    bookshelves: str = ""
    summary: str = ""
    alternatives: tuple[str, ...] = ()
    author_pages: tuple[str, ...] = field(default=())


def strip_boilerplate(text: str) -> str:
    """The book between the ``*** START OF`` and ``*** END OF`` markers (all of it if absent)."""
    start = _START_RE.search(text)
    if start is not None:
        text = text[start.end() :]
    end = _END_RE.search(text)
    if end is not None:
        text = text[: end.start()]
    return text.strip()


_ROLE_RE = re.compile(r"\[([^\]]+)\]\s*$")
_DATES_RE = re.compile(r"^(?:\d|-|\?|active|fl\.|ca\.|BCE|CE|AD|BC|\s)+", re.IGNORECASE)
_BYLINE_AUTHORS = 3


def byline(authors: str) -> str:
    """The authors of a catalog row as a reader names them: ``Livy, 59 BCE-16; Spillan, D. (Daniel),
    -1854 [Translator]`` is ``Livy``; ``Austen, Jane, 1775-1817`` is ``Jane Austen``. Translators,
    editors and other contributors are left out; at most three authors."""
    names: list[str] = []
    for raw in authors.split(";"):
        raw = raw.strip()
        role = _ROLE_RE.search(raw)
        if role is not None:
            if role[1].strip().casefold() != "author":
                continue
            raw = raw[: role.start()].strip()
        parts = [p.strip() for p in raw.split(",")]
        parts = [p for p in parts if p and not _DATES_RE.fullmatch(p)]
        parts = [re.sub(r"\s*\([^)]*\)", "", p).strip() for p in parts]  # "D. (Daniel)" -> "D."
        parts = [p for p in parts if p]
        if not parts:
            continue
        name = parts[0] if len(parts) == 1 else f"{parts[1]} {parts[0]}"
        if name not in names:
            names.append(name)
        if len(names) == _BYLINE_AUTHORS:
            break
    return ", ".join(names)


def load_catalog(path: Path) -> dict[int, Book]:
    books: dict[int, Book] = {}
    with gzip.open(path, "rt", encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            number = (row.get("Text#") or "").strip()
            if not number.isdigit():
                continue
            books[int(number)] = Book(
                number=int(number),
                title=" ".join((row.get("Title") or "").split()) or f"Project Gutenberg eBook #{number}",
                kind=(row.get("Type") or "Text").strip(),
                issued=(row.get("Issued") or "").strip(),
                languages=tuple(x.strip() for x in (row.get("Language") or "").split(";") if x.strip()),
                authors=(row.get("Authors") or "").strip(),
                subjects=(row.get("Subjects") or "").strip(),
                locc=(row.get("LoCC") or "").strip(),
                bookshelves=(row.get("Bookshelves") or "").strip(),
            )
    return books


def parse_rdf(data: bytes) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    """``(summary, alternative titles, author web pages)`` of one ``pg<id>.rdf``."""
    root = ET.fromstring(data)  # noqa: S314 - Gutenberg's own catalog feed
    ebook = root.find("pgterms:ebook", _NS)
    if ebook is None:
        return "", (), ()
    summary = " ".join((ebook.findtext("pgterms:marc520", "", _NS)).split())
    summary = summary.removesuffix("(This is an automatically generated summary.)").strip()
    alternatives = tuple(" ".join((a.text or "").split()) for a in ebook.iterfind("dcterms:alternative", _NS))
    pages = tuple(
        url
        for page in ebook.iterfind("dcterms:creator/pgterms:agent/pgterms:webpage", _NS)
        if (url := page.get(f"{{{_NS['rdf']}}}resource", "")).startswith(("http://", "https://"))
    )
    return summary, tuple(a for a in alternatives if a), pages


def add_rdf(books: Mapping[int, Book], path: Path) -> None:
    """Stream ``rdf-files.tar.bz2`` and attach summaries, alternative titles and author pages."""
    with open_bz2(path) as raw, tarfile.open(fileobj=raw, mode="r|") as tar:
        for member in tar:
            match = _RDF_RE.search(member.name)
            if not member.isfile() or match is None or int(match.group(1)) not in books:
                continue
            fh = tar.extractfile(member)
            if fh is None:
                continue
            try:
                summary, alternatives, pages = parse_rdf(fh.read())
            except ET.ParseError:
                log.warning("unparsable RDF %s", member.name)
                continue
            book = books[int(match.group(1))]
            book.summary, book.alternatives, book.author_pages = summary, alternatives, pages


def iter_texts(path: Path) -> Iterator[tuple[int, str]]:
    """``(ebook number, raw text)`` streamed from ``txt-files.tar.zip`` (a ZIP holding one tar)."""
    with zipfile.ZipFile(path) as archive:
        for info in archive.infolist():
            if not info.filename.endswith(".tar"):
                continue
            with archive.open(info) as raw, tarfile.open(fileobj=raw, mode="r|") as tar:
                for member in tar:
                    match = _TXT_RE.search(member.name)
                    if not member.isfile() or match is None:
                        continue
                    fh = tar.extractfile(member)
                    if fh is not None:
                        yield int(match.group(1)), fh.read().decode("utf-8", errors="replace")


# --- Kiwix reading copy ------------------------------------------------------------------------------


def reading_copy_zim(generation_dir: Path, source_id: str) -> Path | None:
    """The ZIM of the current generation of ``source_id`` stored next to this source, if any."""
    source_dir = generation_dir.resolve().parent.parent / source_id
    try:
        current = (source_dir / "CURRENT.txt").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    up = upstream_dir(source_dir / current)
    return next(iter(sorted(up.glob("*.zim"))), None) if up.is_dir() else None


def zim_book_paths(zim: Path, cache: Path) -> dict[int, str]:
    """``ebook number -> ZIM path`` of each book's main page (``<title>.<id>``), cached as JSON.

    A book is a number with a cover page (``<title>_cover.<id>``): ``Katherine Mary Barrow.2701`` is
    an author's page and ``Moby Dick; Or, The Whale.2701`` the book, and taking the first path by
    number linked a third of the books to an author (2026-10-16 field exam: "the illustrated edition"
    of Moby Dick and Pride and Prejudice opened author pages). A book with no HTML edition (an audio
    book) has its cover page only; that is its page then.
    """
    key = f"{zim.name}:{zim.stat().st_size}:{_ZIM_PATHS_RULE}"
    if cache.is_file():
        try:
            data = json.loads(cache.read_text(encoding="utf-8"))
            if data.get("zim") == key:
                return {int(k): str(v) for k, v in dict(data["paths"]).items()}
        except (ValueError, KeyError):
            pass
    from libzim.reader import Archive  # imported on use: only the linking pass needs it

    archive = Archive(zim)
    covers: dict[int, str] = {}
    pages: set[str] = set()
    for i in range(archive.all_entry_count):
        path = archive._get_entry_by_id(i).path  # pyright: ignore[reportPrivateUsage]
        match = _ZIM_BOOK_RE.search(path)
        if match is None:
            continue
        if path.endswith(f"_cover.{match.group(1)}"):
            covers.setdefault(int(match.group(1)), path)
        else:
            pages.add(path)
    paths: dict[int, str] = {}
    for number, cover in covers.items():
        page = f"{cover.removesuffix(f'_cover.{number}')}.{number}"
        paths[number] = page if page in pages else cover
    cache.parent.mkdir(parents=True, exist_ok=True)
    tmp = cache.with_name(cache.name + ".partial")
    tmp.write_text(json.dumps({"zim": key, "paths": paths}), encoding="utf-8")
    tmp.replace(cache)
    return paths


# --- adapter -----------------------------------------------------------------------------------------


class GutenbergAdapter:
    """``SourceAdapter`` for the Project Gutenberg feeds."""

    id = "gutenberg"

    def discover(self, ctx: Ctx) -> Iterable[ReleaseRef]:
        spec = ctx.source
        base = str(access(spec).get("catalog") or FEEDS_URL)
        base = base if base.endswith("/") else base + "/"
        files, meta = head_release(ctx.fetcher, [(base + n, n) for n in self.file_names(spec)])
        stamps = [datetime.fromisoformat(v) for k, v in meta.items() if k.startswith(LAST_MODIFIED_META)]
        # Identity from HEAD (name, size, ETag, Last-Modified): the same upstream bytes are the same
        # version and are not fetched again (audit H3). Today's date only when HEAD says nothing.
        version = head_version(files, meta) or datetime.now(UTC).strftime("%Y-%m-%d")
        return [
            ReleaseRef(
                source_id=spec.id,
                version=version,
                published=max(stamps) if stamps else None,
                files=tuple(files),
                meta=meta,
                format="gutenberg-feeds",
            )
        ]

    @staticmethod
    def file_names(spec: SourceSpec) -> tuple[str, ...]:
        return str_list(access(spec).get("files")) or FEED_FILES

    def plan(self, ctx: Ctx, have: Generation | None, want: ReleaseRef) -> FetchPlan:
        """Skip feed files the previous generation holds unchanged (no checksums upstream)."""
        return plan_by_head(have, want)

    def depends_on(self, ctx: Ctx, generation_dir: Path) -> frozenset[str]:
        """Earlier generations holding feed files this one reads (``GenerationDependencies``)."""
        files = files_across_generations(generation_dir, self.file_names(ctx.source))
        return frozenset(holder_of(p) for p in files.values()) - {generation_dir.name}

    def fetch(self, ctx: Ctx, plan: FetchPlan) -> Iterable[FetchResult]:
        return fetch_all(ctx, plan)

    def verify(self, ctx: Ctx, remote: RemoteFile, local: Path) -> Verdict:
        return verify_file(remote, local)

    def documents(self, ctx: Ctx, generation_dir: Path) -> Iterator[DocRecord]:
        spec = ctx.source
        feeds = files_across_generations(generation_dir, self.file_names(spec))
        books = load_catalog(feeds[CATALOG]) if CATALOG in feeds else {}
        if RDF in feeds:
            add_rdf(books, feeds[RDF])
        zim_source = str(access(spec).get("reading_copy", "gutenberg-zim"))
        zim_paths: dict[int, str] = {}
        zim = reading_copy_zim(generation_dir, zim_source) if zim_source else None
        if zim is not None:
            zim_paths = zim_book_paths(zim, ctx.workdir / f"{zim_source}-paths.json")
        render = _Renderer(spec.id, spec.license, zim_source, zim_paths)
        done: set[int] = set()
        if TEXTS in feeds:
            for number, raw in iter_texts(feeds[TEXTS]):
                if number in done:
                    continue
                done.add(number)
                yield render(books.get(number) or Book(number, f"Project Gutenberg eBook #{number}"), raw)
        for number in sorted(books):
            if number not in done:
                yield render(books[number], None)


def _lang(languages: Iterable[str]) -> str:
    return next(iter(languages), "") or "und"


@dataclass(frozen=True, slots=True)
class _Renderer:
    source_id: str
    license: str
    zim_source: str
    zim_paths: Mapping[int, str]

    def __call__(self, book: Book, raw: str | None) -> DocRecord:
        header = [book.title]
        if book.authors:
            header.append(f"By {book.authors}")
        facts = [
            f"Project Gutenberg eBook #{book.number}",
            f"released {book.issued}" if book.issued else "",
            f"type {book.kind}" if book.kind and book.kind != "Text" else "",
        ]
        header.append(", ".join(f for f in facts if f) + ".")
        if book.subjects:
            header.append(f"Subjects: {book.subjects}")
        if book.bookshelves:
            header.append(f"Bookshelves: {book.bookshelves}")
        if book.summary:
            header.append(f"Summary: {book.summary}")
        body = strip_boilerplate(raw) if raw is not None else ""
        if raw is None:
            header.append("Catalog record only: no plain-text edition in the feed.")
        text = "\n".join([*header, body]) if body else "\n".join(header)
        links: list[str] = []
        if (path := self.zim_paths.get(book.number)) is not None:
            with contextlib.suppress(InvalidDocIdError):
                links.append(make_doc_id(self.zim_source, path))
        links.extend(book.author_pages)
        native = str(book.number)
        issued = None
        try:
            issued = datetime.fromisoformat(book.issued).replace(tzinfo=UTC) if book.issued else None
        except ValueError:
            issued = None
        return DocRecord(
            doc_id=make_doc_id(self.source_id, native),
            source_id=self.source_id,
            native_id=native,
            title=book.title,
            lang=_lang(book.languages),
            license=self.license,
            text=text,
            timestamp=issued,
            url_native=f"https://www.gutenberg.org/ebooks/{book.number}",
            links=tuple(dict.fromkeys(links)),
            content_hash=content_hash(text),
            aliases=tuple(a for a in book.alternatives if a != book.title),
            # Searchable with the title (wsp_index.lexical): "Livy History of Rome" found encyclopedia
            # pages and never the book, whose author was only in its 1.4 M characters of text.
            meta={BYLINE_META: by} if (by := byline(book.authors)) else {},
        )
