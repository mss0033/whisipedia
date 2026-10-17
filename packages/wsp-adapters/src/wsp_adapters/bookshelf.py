"""``ncbi-bookshelf`` adapter: books of the NCBI Bookshelf open-access subset, one document per chapter.

The NLM LitArch open-access subset (``https://ftp.ncbi.nlm.nih.gov/pub/litarch/``) is the sanctioned
bulk channel for Bookshelf books: ``file_list.csv`` lists each book's archive (``<dir>/<book>_<NBK
id>.tar.gz``), title, publisher and last update; an archive holds one BITS XML file (``.nxml``) per
chapter, front-matter part or article, with the book's licence in each. Licences vary by book
(StatPearls CC BY-NC-ND 4.0, WHO guidelines CC BY-NC-SA 3.0 IGO, US agencies public domain); each
document carries its own.

**Selection.** ``access.books`` (NBK ids) and ``access.publishers`` (exact names in the list) choose
the books; ``access.standalone`` names books whose parts are articles in their own right (StatPearls,
GeneReviews): their titles are not prefixed with the book's.

**Documents.** ``native_id`` ``<NBK id>/<part id>`` (``NBK430685/article-23948``); the text is the
part's body with its headings, lists and table rows (references and the table of contents left out).
"""

from __future__ import annotations

import csv
import io
import json
import logging
import re
import tarfile
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from wsp_adapters._common import (
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
from wsp_core.ids import content_hash, make_doc_id
from wsp_core.model import DocRecord, FetchPlan, FetchResult, Generation, ReleaseRef, RemoteFile, Verdict
from wsp_core.ports import Ctx

log = logging.getLogger(__name__)

BASE_URL = "https://ftp.ncbi.nlm.nih.gov/pub/litarch/"
LIST_FILE = "file_list.csv"
BOOKS_FILE = "meta/books.json"
_BLOCKS = frozenset({"p", "title", "list-item", "tr", "caption", "label", "disp-quote", "def-item", "term"})
_SKIP = frozenset({"ref-list", "fn-group", "xref", "object-id", "alt-text", "graphic", "inline-graphic"})
_CELLS = frozenset({"td", "th"})
_WS = re.compile(r"\s+")
_ARCHIVE = re.compile(r"^(?P<dir>[^/_]+(?:_[^/_]+)*?)_(?P<nbk>NBK\d+)\.tar\.gz$")


@dataclass(frozen=True, slots=True)
class Book:
    path: str  # "<2 hex>/<2 hex>/<dir>_<NBK>.tar.gz"
    title: str
    publisher: str
    nbk: str
    updated: str

    @property
    def name(self) -> str:
        return self.path.rsplit("/", 1)[-1]


def read_list(data: bytes) -> list[Book]:
    """The rows of ``file_list.csv`` (UTF-8, or Latin-1 where NLM wrote a ``®`` that way)."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = data.decode("latin-1")
    out: list[Book] = []
    for row in csv.DictReader(io.StringIO(text)):
        path, nbk = (row.get("File") or "").strip(), (row.get("Accession ID") or "").strip()
        if path and nbk:
            updated = next((v for k, v in row.items() if k and k.startswith("Last Updated")), "") or ""
            out.append(
                Book(
                    path, (row.get("Title") or "").strip(), (row.get("Publisher") or "").strip(), nbk, updated
                )
            )
    return out


def chosen(books: Iterable[Book], ids: Iterable[str], publishers: Iterable[str]) -> list[Book]:
    want_ids, want_pubs = set(ids), set(publishers)
    return sorted(
        (b for b in books if b.nbk in want_ids or b.publisher in want_pubs),
        key=lambda b: (b.publisher, b.nbk),
    )


def _text(el: ET.Element) -> str:
    out: list[str] = []

    def walk(node: ET.Element) -> None:
        tag = node.tag
        if tag in _SKIP:
            if node.tail:
                out.append(node.tail)
            return
        block = tag in _BLOCKS
        if block:
            out.append("\x00")
        if tag in _CELLS:
            out.append(" | ")
        if node.text:
            out.append(node.text)
        for child in node:
            walk(child)
            if child.tail and child.tag not in _SKIP:
                out.append(child.tail)
        if block:
            out.append("\x00")

    walk(el)
    lines = (_WS.sub(" ", part).strip(" |") for part in "".join(out).split("\x00"))
    return "\n".join(line.strip() for line in lines if line.strip())


def part_record(
    xml: bytes, nbk: str, book_dir: str, source_id: str, fallback_license: str, *, standalone: bool
) -> DocRecord | None:
    """One Bookshelf part (a chapter, an article, a front-matter section) as a document, or None."""
    root = ET.fromstring(xml)  # noqa: S314 - NLM's open-access archive (no external entities are read)
    if root.get("content-type") == "toc":
        return None
    part = root.find("book-part")
    body = part.find("body") if part is not None else None
    if part is None or body is None:
        return None
    text = _text(body)
    if len(text) < 40:
        return None
    part_id = root.get("id") or ""
    if not part_id:
        found = part.find("book-part-meta/book-part-id")
        part_id = (found.text or "").strip() if found is not None else ""
    if not part_id:
        return None
    title_el = part.find("book-part-meta/title-group/title")
    title = _WS.sub(" ", "".join(title_el.itertext())).strip() if title_el is not None else ""
    label = _WS.sub(" ", part.findtext("book-part-meta/title-group/label") or "").strip()
    book_title_el = root.find("book-meta/book-title-group/book-title")
    book_title = _WS.sub(" ", "".join(book_title_el.itertext())).strip() if book_title_el is not None else ""
    shown = (
        f"{label}. {title}"
        if label and title and not title.startswith(label)
        else (title or label or part_id)
    )
    if not standalone and book_title and book_title.casefold() != shown.casefold():
        shown = f"{shown} — {book_title}"
    lic = root.find("book-meta/permissions/license")
    license_ = fallback_license
    if lic is not None:
        href = next((v for k, v in lic.attrib.items() if k.endswith("href")), "")
        license_ = href or fallback_license
    head = book_title if standalone or not book_title else f"{book_title}: {title or part_id}"
    return DocRecord(
        doc_id=make_doc_id(source_id, f"{nbk}/{part_id}"),
        source_id=source_id,
        native_id=f"{nbk}/{part_id}",
        title=shown,
        lang="en",
        license=license_,
        text=f"{head}\n\n{text}" if head else text,
        url_native=f"https://www.ncbi.nlm.nih.gov/books/n/{book_dir}/{part_id}/",
        content_hash=content_hash(f"{head}\n\n{text}" if head else text),
        meta={k: v for k, v in (("book", book_title), ("book_id", nbk)) if v},
    )


def book_parts(archive: Path) -> Iterator[tuple[str, bytes]]:
    """``(member name, XML)`` of each ``.nxml`` part of a book archive, in archive order."""
    with tarfile.open(archive, "r:gz") as tar:
        for member in tar:
            if member.isfile() and member.name.endswith(".nxml"):
                fh = tar.extractfile(member)
                if fh is not None:
                    yield member.name, fh.read()


class BookshelfAdapter:
    """``SourceAdapter`` for the NCBI Bookshelf open-access subset (a chosen set of books)."""

    id = "ncbi-bookshelf"

    @staticmethod
    def _choice(ctx: Ctx) -> tuple[Sequence[str], Sequence[str], frozenset[str]]:
        acc = access(ctx.source)
        return (
            str_list(acc.get("books")),
            str_list(acc.get("publishers")),
            frozenset(str_list(acc.get("standalone"))),
        )

    def discover(self, ctx: Ctx) -> Iterable[ReleaseRef]:
        base = str(access(ctx.source).get("base") or BASE_URL)
        books = read_list(ctx.fetcher.get_bytes(base + LIST_FILE, max_bytes=64 * 1024 * 1024))
        ids, pubs, _ = self._choice(ctx)
        picked = chosen(books, ids, pubs)
        if not picked:
            log.warning("%s: no book in %s matches the selection", ctx.source.id, LIST_FILE)
            return []
        files, meta = head_release(ctx.fetcher, [(base + b.path, b.name) for b in picked])
        version = head_version(files, meta) or datetime.now(UTC).strftime("%Y-%m-%d")
        return [
            ReleaseRef(ctx.source.id, version, None, tuple(files), meta=meta, format="bits-xml", license=None)
        ]

    def plan(self, ctx: Ctx, have: Generation | None, want: ReleaseRef) -> FetchPlan:
        return plan_by_head(have, want)

    def finalize(
        self, ctx: Ctx, plan: FetchPlan, previous_dir: Path | None, kept: Mapping[str, Path]
    ) -> Sequence[tuple[str, Path]]:
        """The release's book list (``meta/books.json``): a release fetches only changed books and reads
        the rest from earlier generations, so the list says which ones it is made of."""
        ctx.workdir.mkdir(parents=True, exist_ok=True)
        out = ctx.workdir / BOOKS_FILE.rsplit("/", 1)[-1]
        out.write_text(
            json.dumps(sorted(f.name for f in plan.release.files), indent=1) + "\n", encoding="utf-8"
        )
        return [(BOOKS_FILE, out)]

    @staticmethod
    def _files(generation_dir: Path) -> dict[str, Path]:
        """Every book archive of the release (``meta/books.json``), here or in an earlier generation."""
        listed = generation_dir / BOOKS_FILE
        if listed.is_file():
            names = [str(n) for n in json.loads(listed.read_text(encoding="utf-8"))]
        else:
            names = sorted(p.name for p in upstream_dir(generation_dir).glob("*.tar.gz"))
        return files_across_generations(generation_dir, names) if names else {}

    def depends_on(self, ctx: Ctx, generation_dir: Path) -> frozenset[str]:
        """Earlier generations holding books this one reads (``GenerationDependencies``)."""
        return frozenset(holder_of(p) for p in self._files(generation_dir).values()) - {generation_dir.name}

    def fetch(self, ctx: Ctx, plan: FetchPlan) -> Iterable[FetchResult]:
        return fetch_all(ctx, plan)

    def verify(self, ctx: Ctx, remote: RemoteFile, local: Path) -> Verdict:
        return verify_file(remote, local)

    def documents(self, ctx: Ctx, generation_dir: Path) -> Iterator[DocRecord]:
        spec = ctx.source
        _, _, standalone = self._choice(ctx)
        for name, path in sorted(self._files(generation_dir).items()):
            m = _ARCHIVE.match(name)
            if m is None:
                continue
            nbk, book_dir = m.group("nbk"), m.group("dir")
            seen: set[str] = set()
            for member, xml in book_parts(path):
                try:
                    doc = part_record(xml, nbk, book_dir, spec.id, spec.license, standalone=nbk in standalone)
                except ET.ParseError as exc:
                    log.warning("%s: %s in %s does not parse: %s", spec.id, member, name, exc)
                    continue
                if doc is not None and doc.native_id not in seen:
                    seen.add(doc.native_id)
                    yield doc
