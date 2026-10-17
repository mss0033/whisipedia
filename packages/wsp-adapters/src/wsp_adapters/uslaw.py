"""``us-law`` adapter: the United States Code and the eCFR, one document per section.

Both are federal works in the public domain, published in bulk as XML without checksums, so a
release is identified by HTTP HEAD (:func:`~wsp_adapters._common.head_version`) and only changed
files are fetched again (:func:`~wsp_adapters._common.plan_by_head`).

**United States Code** (``access.kind: uscode``). The Office of the Law Revision Counsel's download
page links the current release point as one zip of USLM XML (``xml_uscAll@119-121.zip``, ~110 MB,
one ``uscNN.xml`` per title). Every ``<section>`` of a title is a document: ``native_id``
``t26/s501``, title ``26 U.S.C. § 501: Exemption from tax on corporations…``, text the heading,
the body (subsections, paragraphs, …) and the source credit. Editorial notes (amendment histories)
are left out: they are longer than most sections and would drown the law in search. Sections quoted
inside notes (an amending act's text) are not sections of the Code and are skipped. Cross references
to other sections become links.

**eCFR** (``access.kind: ecfr``). GovInfo's bulk repository holds the current eCFR as one XML file
per title (``ECFR-title21.xml``, ~0.8 GB in all, updated daily). Every ``DIV8 TYPE="SECTION"`` and
``DIV9 TYPE="APPENDIX"`` is a document: ``native_id`` ``t21/s101.1``, title ``21 CFR 101.1:
Scope``, text the section with its part's heading for context and its source citation.
"""

from __future__ import annotations

import io
import logging
import re
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urljoin

from wsp_adapters._common import (
    access,
    fetch_all,
    files_across_generations,
    head_release,
    head_version,
    holder_of,
    plan_by_head,
    upstream_dir,
    verify_file,
)
from wsp_core.ids import content_hash, make_doc_id
from wsp_core.model import DocRecord, FetchPlan, FetchResult, Generation, ReleaseRef, RemoteFile, Verdict
from wsp_core.ports import Ctx

log = logging.getLogger(__name__)

USCODE_PAGE = "https://uscode.house.gov/download.html"
ECFR_BASE = "https://www.govinfo.gov/bulkdata/ECFR/"
ECFR_TITLES = tuple(t for t in range(1, 51) if t != 35)  # title 35 (Panama Canal) is reserved
USLM = "{http://xml.house.gov/schemas/uslm/1.0}"
_ALL_TITLES = re.compile(r'href="([^"]*xml_uscAll@([0-9]+-[0-9A-Za-z]+)\.zip)"')
_SECTION_ID = re.compile(r"^/us/usc/t([0-9]+a?)/s([^/]+)$")
_TITLE_FILE = re.compile(r"usc([0-9]+a?)\.xml$")
_WS = re.compile(r"[ \t\r\f\v]+")
_BLANKS = re.compile(r"\n\s*\n+")
_LONE_NUM = re.compile(r"^(\([0-9A-Za-z]+\))\n+", re.MULTILINE)  # "(1)" on its own line, then its text

# USLM elements that start a new line in a section's text.
_USLM_BLOCKS = frozenset(
    {"p", "subsection", "paragraph", "subparagraph", "clause", "subclause", "item", "subitem",
     "subsubitem", "chapeau", "continuation", "content", "table", "tr", "def", "quotedContent",
     "sourceCredit", "proviso"}
)  # fmt: skip
_USLM_SKIP = frozenset({"notes", "note", "toc", "layout"})
_ECFR_BLOCKS = frozenset(
    {"P", "FP", "HD", "HD1", "HD2", "HD3", "EXTRACT", "NOTE", "CITA", "FTNT", "ROW", "GPOTABLE"}
)
_ECFR_SKIP = frozenset({"SECAUTH", "PRTPAGE"})


def _tag(el: ET.Element) -> str:
    return el.tag.rsplit("}", 1)[-1]


def _text(el: ET.Element, blocks: frozenset[str], skip: frozenset[str]) -> str:
    """Text of ``el`` with a line break around each block element; ``skip`` subtrees left out."""
    out: list[str] = []

    def walk(node: ET.Element) -> None:
        tag = _tag(node)
        if tag in skip:
            return
        block = tag in blocks
        if block:
            out.append("\n")
        if node.text:
            out.append(node.text)
        for child in node:
            walk(child)
            if child.tail:
                out.append(child.tail)
        if block:
            out.append("\n")

    walk(el)
    lines = (_WS.sub(" ", line).strip() for line in "".join(out).split("\n"))
    return _LONE_NUM.sub(r"\1 ", _BLANKS.sub("\n\n", "\n".join(lines))).strip()


def _record(
    source_id: str,
    license_: str,
    native: str,
    title: str,
    text: str,
    url: str,
    *,
    links: tuple[str, ...] = (),
    aliases: tuple[str, ...] = (),
    meta: dict[str, str] | None = None,
) -> DocRecord:
    return DocRecord(
        doc_id=make_doc_id(source_id, native),
        source_id=source_id,
        native_id=native,
        title=title,
        lang="en",
        license=license_,
        text=text,
        url_native=url,
        content_hash=content_hash(text),
        links=links,
        aliases=aliases,
        meta=meta or {},
    )


# --- United States Code ------------------------------------------------------------------------


def uscode_sections(xml: bytes | io.BufferedIOBase, source_id: str, license_: str) -> Iterator[DocRecord]:
    """Documents of one title's USLM XML: one per section of the Code (not quoted ones)."""
    depth = 0  # sections open around the current element (a quoted act's sections sit inside notes)
    stream = io.BytesIO(xml) if isinstance(xml, bytes) else xml
    for event, el in ET.iterparse(stream, events=("start", "end")):  # noqa: S314 - federal bulk XML
        if _tag(el) != "section":
            continue
        if event == "start":
            depth += 1
            continue
        depth -= 1
        if depth > 0:
            continue
        doc = _uscode_section(el, source_id, license_)
        el.clear()
        if doc is not None:
            yield doc


def _uscode_section(el: ET.Element, source_id: str, license_: str) -> DocRecord | None:
    m = _SECTION_ID.match(el.get("identifier", ""))
    if m is None:
        return None
    title, number = m.group(1), m.group(2)
    found = el.find(f"{USLM}heading")
    heading = "" if found is None else _WS.sub(" ", "".join(found.itertext())).strip()
    citation = f"{title} U.S.C. § {number}"
    body = [
        _text(child, _USLM_BLOCKS, _USLM_SKIP)
        for child in el
        if _tag(child) not in ("num", "heading", "notes")
    ]
    status = el.get("status", "")
    head = f"{citation}. {heading}" if heading else citation
    text = "\n\n".join([head, *(b for b in body if b)])
    if status and status not in ("operational",) and status.lower() not in text.lower():
        text += f"\n\n[{status.capitalize()}]"
    links: list[str] = []
    for ref in el.iter(f"{USLM}ref"):
        target = _SECTION_ID.match(ref.get("href", ""))
        if target is not None and not _inside_notes(el, ref):
            links.append(make_doc_id(source_id, f"t{target.group(1)}/s{target.group(2)}"))
    return _record(
        source_id,
        license_,
        f"t{title}/s{number}",
        f"{citation}: {heading}" if heading else citation,
        text,
        f"https://uscode.house.gov/view.xhtml?req=granuleid:USC-prelim-title{title}-section{number}&num=0&edition=prelim",
        links=tuple(dict.fromkeys(links)),
        aliases=(f"{title} USC {number}", f"{title} U.S.C. {number}", f"Title {title} section {number}"),
        meta={"citation": citation},
    )


def _inside_notes(section: ET.Element, ref: ET.Element) -> bool:
    notes = section.find(f"{USLM}notes")
    return notes is not None and any(r is ref for r in notes.iter(f"{USLM}ref"))


def uscode_documents(zip_path: Path, source_id: str, license_: str) -> Iterator[DocRecord]:
    with zipfile.ZipFile(zip_path) as archive:
        for name in sorted(archive.namelist(), key=_title_order):
            if _TITLE_FILE.search(name) is None:
                continue
            with archive.open(name) as member:
                yield from uscode_sections(member, source_id, license_)  # type: ignore[arg-type]


def _title_order(name: str) -> tuple[int, str]:
    m = _TITLE_FILE.search(name)
    if m is None:
        return (10_000, name)
    digits = m.group(1).rstrip("a")
    return (int(digits), m.group(1))


# --- eCFR --------------------------------------------------------------------------------------


def ecfr_sections(
    xml: bytes | io.BufferedIOBase, title: int, source_id: str, license_: str
) -> Iterator[DocRecord]:
    """Documents of one eCFR title: each section and appendix, with its part's heading."""
    part_head = ""
    in_part = False  # the next HEAD closed is the part's own heading
    seen: dict[str, int] = {}
    stream = io.BytesIO(xml) if isinstance(xml, bytes) else xml
    for event, el in ET.iterparse(stream, events=("start", "end")):  # noqa: S314 - federal bulk XML
        kind = el.get("TYPE", "")
        if event == "start":
            if el.tag == "DIV5" and kind == "PART":
                part_head, in_part = "", True
            continue
        if el.tag == "HEAD" and in_part:
            part_head, in_part = _WS.sub(" ", "".join(el.itertext())).strip(), False
        elif (el.tag == "DIV8" and kind == "SECTION") or (el.tag == "DIV9" and kind == "APPENDIX"):
            doc = _ecfr_section(el, kind, title, part_head, seen, source_id, license_)
            el.clear()
            if doc is not None:
                yield doc
        elif el.tag in ("DIV5", "DIV6", "DIV7"):
            el.clear()


def _ecfr_section(
    el: ET.Element, kind: str, title: int, part: str, seen: dict[str, int], source_id: str, license_: str
) -> DocRecord | None:
    head_el = el.find("HEAD")
    head = _WS.sub(" ", "".join(head_el.itertext())).strip() if head_el is not None else ""
    n = (el.get("N") or "").replace("§", "").strip()
    if not n:
        return None
    if kind == "SECTION":
        subject = re.sub(r"^§+\s*[^\s]+\s*", "", head).strip().rstrip(".")
        number = n.replace(" ", "")
        native = f"t{title}/s{number}"
        citation = f"{title} CFR {number}"
        url = f"https://www.ecfr.gov/current/title-{title}/section-{number}"
    else:
        subject = head
        number = re.sub(r"[^0-9A-Za-z.\-]+", "-", n).strip("-")
        native = f"t{title}/appendix-{number}"
        citation = f"{title} CFR {head.split('—')[0].strip() or number}"
        url = f"https://www.ecfr.gov/current/title-{title}"
    count = seen.get(native, 0)
    seen[native] = count + 1
    if count:
        native = f"{native}~{count + 1}"  # a number used twice in a title (reserved, then reused)
    body = _text(el, _ECFR_BLOCKS, _ECFR_SKIP | {"HEAD"})
    if not body and "[Reserved]" not in head:
        return None
    text = "\n\n".join(x for x in (head, body) if x)
    return _record(
        source_id,
        license_,
        native,
        f"{citation}: {subject}" if subject else citation,
        text,
        url,
        aliases=(f"{title} C.F.R. {number}", f"{title} CFR § {number}") if kind == "SECTION" else (),
        meta={"citation": citation, **({"part": part} if part else {})},
    )


def ecfr_documents(path: Path, title: int, source_id: str, license_: str) -> Iterator[DocRecord]:
    with path.open("rb") as fh:
        yield from ecfr_sections(fh, title, source_id, license_)  # type: ignore[arg-type]


def ecfr_name(title: int) -> str:
    return f"ECFR-title{title}.xml"


# --- adapter -----------------------------------------------------------------------------------


class UsLawAdapter:
    """``SourceAdapter`` for the United States Code (``kind: uscode``) and the eCFR (``kind: ecfr``)."""

    id = "us-law"

    @staticmethod
    def kind(ctx: Ctx) -> str:
        kind = str(access(ctx.source).get("kind", ""))
        if kind not in ("uscode", "ecfr"):
            raise ValueError(f"{ctx.source.id}: us-law needs access.kind uscode or ecfr")
        return kind

    def discover(self, ctx: Ctx) -> Iterable[ReleaseRef]:
        spec = ctx.source
        if self.kind(ctx) == "uscode":
            page = str(access(spec).get("page") or USCODE_PAGE)
            html = ctx.fetcher.get_bytes(page, max_bytes=2 * 1024 * 1024).decode("utf-8", "replace")
            m = _ALL_TITLES.search(html)
            if m is None:
                log.warning("%s: no all-titles XML zip on %s", spec.id, page)
                return []
            urls = [(urljoin(page, m.group(1)), f"xml_uscAll@{m.group(2)}.zip")]
            extra = {"release_point": m.group(2)}
            fmt = "uslm-xml"
        else:
            base = str(access(spec).get("base") or ECFR_BASE)
            urls = [(f"{base}title-{t}/{ecfr_name(t)}", ecfr_name(t)) for t in ECFR_TITLES]
            extra = {}
            fmt = "ecfr-xml"
        files, meta = head_release(ctx.fetcher, urls)
        version = head_version(files, meta) or datetime.now(UTC).strftime("%Y-%m-%d")
        return [
            ReleaseRef(
                spec.id, version, None, tuple(files), meta={**meta, **extra}, format=fmt, license=spec.license
            )
        ]

    def plan(self, ctx: Ctx, have: Generation | None, want: ReleaseRef) -> FetchPlan:
        return plan_by_head(have, want)

    def _files(self, ctx: Ctx, generation_dir: Path) -> dict[str, Path]:
        if self.kind(ctx) == "ecfr":
            return files_across_generations(generation_dir, [ecfr_name(t) for t in ECFR_TITLES])
        found = sorted(upstream_dir(generation_dir).glob("xml_uscAll@*.zip"))
        return {p.name: p for p in found[-1:]}

    def depends_on(self, ctx: Ctx, generation_dir: Path) -> frozenset[str]:
        return frozenset(holder_of(p) for p in self._files(ctx, generation_dir).values()) - {
            generation_dir.name
        }

    def fetch(self, ctx: Ctx, plan: FetchPlan) -> Iterable[FetchResult]:
        return fetch_all(ctx, plan)

    def verify(self, ctx: Ctx, remote: RemoteFile, local: Path) -> Verdict:
        return verify_file(remote, local)

    def documents(self, ctx: Ctx, generation_dir: Path) -> Iterator[DocRecord]:
        spec = ctx.source
        files = self._files(ctx, generation_dir)
        if self.kind(ctx) == "uscode":
            for path in files.values():
                yield from uscode_documents(path, spec.id, spec.license)
            return
        for t in ECFR_TITLES:
            path = files.get(ecfr_name(t))
            if path is not None:
                yield from ecfr_documents(path, t, spec.id, spec.license)
