"""Texts of collections published as git repositories (a ``reader`` in the registry ``access``).

A repository is fetched as a snapshot of its files (``git archive`` of the default branch, one zip:
``discovery: [{kind: git, snapshot: true}]``), and the refiner reads its TEI files with a reader:

* ``tei-frus`` (the Office of the Historian's Foreign Relations of the United States,
  ``volumes/<volume id>.xml``): every ``<div type="document">`` is a document, native id
  ``<volume id>/<xml:id>`` (``frus1969-76v01/d1``), titled "Editorial Note (FRUS 1969-1976, Volume
  I, Document 1)",
  dated by its ``frus:doc-dateTime-min``, linked to history.state.gov's page of it.
* ``tei-cts`` (Perseus canonical-greekLit and canonical-latinLit, CTS EpiDoc): the English
  translations (``*-eng*.xml``); each book (the first level of ``textpart``) is a document, native id
  ``<file stem>/<book>``, titled "<author>, <title>, Book <n> (tr. <translator>)"; a work without
  books is one document. Chapter numbers stay at the start of their paragraphs ("9. In the year...")
  so a citation such as "Appian, Civil Wars 1.9" can be followed in the text.

* ``tei-tcp`` (the Text Creation Partnership's EEBO, ECCO and Evans texts, one TEI file per book,
  ``A00001.xml``): the book is one document, titled "<title> (<author>, <year>)" from its header,
  linked to the text's repository; front matter, body and back matter in order, notes left out.

* ``tei-ecpa`` (the Eighteenth-Century Poetry Archive, Oxford: one TEI file per work,
  ``<id>/<id>.xml``, every word and stop marked): the work is one document, titled "<title>
  (<author>)", linked to its page on eighteenthcenturypoetry.org. Its spaces are its ``<c>``
  elements; the indentation between word elements is not text.

* ``tei-dta`` (the Deutsches Textarchiv, BBAW: German 1600-1900, one TEI file per book in its
  download zip, ``<id>.TEI-P5.xml``): the book is one document, titled "<main title> (<author>,
  <year>)", linked to its page on deutschestextarchiv.de. The transcription is diplomatic; the text
  is made searchable: long s is s, a superscript e over a, o, u is the umlaut (``uͤ`` is ``ü``), a
  word hyphenated at a line end is joined (``an-``/``dern`` is ``andern``); running heads and
  catchwords (``<fw>``) are left out.

* ``rst-kernel`` (the Linux kernel's ``Documentation/``, reStructuredText): each page a document,
  native id its path below ``Documentation/`` without ``.rst``, linked to docs.kernel.org;
  translations (``Documentation/translations/``) left out.

The 2026-10-17 field exam found Appian only as a noisy OCR scan and asked for FRUS, which the
registry held as a git mirror no refiner read (2026-10-12 audit, CA-12).
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

TEI = "{http://www.tei-c.org/ns/1.0}"
FRUS_NS = "{http://history.state.gov/frus/ns/1.0}"
_WS = re.compile(r"\s+")
_BLOCKS = frozenset({"p", "head", "item", "l", "ab", "closer", "dateline", "salute", "signed", "opener"})
_SKIP = frozenset({"note", "teiHeader", "fw", "pb", "lb"})  # footnotes and page furniture


@dataclass(frozen=True, slots=True)
class TeiDoc:
    key: str  # appended to the collection document's key
    title: str
    text: str
    date: str = ""  # YYYY-MM-DD
    url: str = ""
    lang: str = "en"


def _local(tag: object) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _flat(el: ET.Element | None) -> str:
    return "" if el is None else _WS.sub(" ", "".join(el.itertext())).strip()


def blocks(el: ET.Element, *, numbered: bool = False) -> str:
    """Paragraph text of ``el``: a blank line between blocks, footnotes and page breaks left out. With
    ``numbered``, a CTS chapter's number opens its first paragraph."""
    out: list[str] = []
    line: list[str] = []
    pending = ""

    def flush() -> None:
        nonlocal pending
        text = _WS.sub(" ", "".join(line)).strip()
        if text:
            out.append(pending + text)
            pending = ""
        line.clear()

    def walk(node: ET.Element) -> None:
        nonlocal pending
        tag = _local(node.tag)
        if tag in _SKIP:
            if node.tail:
                line.append(node.tail)
            return
        if numbered and tag == "div" and node.get("subtype") == "chapter" and (node.get("n") or "").isdigit():
            flush()
            pending = f"{node.get('n')}. "
        block = tag in _BLOCKS
        if block:
            flush()
        if node.text:
            line.append(node.text)
        for child in node:
            walk(child)
        if block:
            flush()
        if node.tail:
            line.append(node.tail)

    walk(el)
    flush()
    return "\n\n".join(out)


# --- FRUS ---------------------------------------------------------------------------------------


def frus_volume(xml: bytes, volume: str) -> Iterator[TeiDoc]:
    """The documents of one FRUS volume (``volumes/<volume>.xml``)."""
    root = ET.fromstring(xml)  # noqa: S314 - the Office of the Historian's own TEI
    title_stmt = root.find(f"{TEI}teiHeader/{TEI}fileDesc/{TEI}titleStmt")
    vol_title = ""
    if title_stmt is not None:
        complete = title_stmt.find(f"{TEI}title[@type='complete']")
        parts = [_flat(t) for t in title_stmt.findall(f"{TEI}title") if t.get("type") != "complete"]
        vol_title = _flat(complete) or ", ".join(p for p in parts if p)
    vol_title = vol_title or volume
    # "FRUS 1969-1976, Volume I": the series and volume, without the volume's subject
    short = re.sub(r"^Foreign Relations of the United States", "FRUS", vol_title)
    short = re.sub(r"^(FRUS, [^,]+, Volume [^,]+),.*$", r"\1", short).replace("FRUS, ", "FRUS ", 1)
    for div in root.iter(f"{TEI}div"):
        if div.get("type") != "document" or not div.get("{http://www.w3.org/XML/1998/namespace}id"):
            continue
        doc_id = div.get("{http://www.w3.org/XML/1998/namespace}id") or ""
        found = div.find(f"{TEI}head")
        head = "" if found is None else _WS.sub(" ", blocks(found)).strip()  # its source note left out
        number = div.get("n") or doc_id.lstrip("d")
        subject = re.sub(rf"^{re.escape(number)}\.\s*", "", head) if head else ""
        where = f"{short}, Document {number}"
        title = f"{subject} ({where})" if subject else where
        when = (div.get(f"{FRUS_NS}doc-dateTime-min") or "")[:10]
        text = blocks(div)
        if not text:
            continue
        yield TeiDoc(
            key=f"{volume}/{doc_id}",
            title=title,
            text=f"{vol_title}\n\n{text}",
            date=when if re.fullmatch(r"\d{4}-\d{2}-\d{2}", when) else "",
            url=f"https://history.state.gov/historicaldocuments/{volume}/{doc_id}",
        )


def _frus_member(name: str) -> str | None:
    m = re.fullmatch(r"(?:[^/]+/)?volumes/(frus[\w.-]+)\.xml", name)
    return m.group(1) if m else None


# --- CTS (Perseus) ------------------------------------------------------------------------------


def cts_work(xml: bytes, stem: str) -> Iterator[TeiDoc]:
    """The books of one CTS translation (``tlg0551.tlg017.perseus-eng2``)."""
    root = ET.fromstring(xml)  # noqa: S314 - Perseus's own EpiDoc
    stmt = root.find(f"{TEI}teiHeader/{TEI}fileDesc/{TEI}titleStmt")
    work = _flat(stmt.find(f"{TEI}title")) if stmt is not None else ""
    author = _flat(stmt.find(f"{TEI}author")) if stmt is not None else ""
    translator = ""
    if stmt is not None:
        for ed in stmt.findall(f"{TEI}editor"):
            if ed.get("role") == "translator":
                translator = _flat(ed)
                break
    body = root.find(f"{TEI}text/{TEI}body")
    if body is None:
        return
    name = ", ".join(x for x in (author, work or stem) if x)
    tr = f" (tr. {translator})" if translator else ""
    top = next((d for d in body if _local(d.tag) == "div"), None)
    parts = [d for d in (top if top is not None else body) if _local(d.tag) == "div"]
    books = [d for d in parts if d.get("type") == "textpart" and d.get("subtype") in ("book", "part")]
    if not books:
        text = blocks(top if top is not None else body, numbered=True)
        if text:
            yield TeiDoc(key=stem, title=f"{name}{tr}", text=f"{name}{tr}\n\n{text}")
        return
    for book in books:
        n = book.get("n") or str(books.index(book) + 1)
        label = f"{name}, {(book.get('subtype') or 'book').capitalize()} {n}{tr}"
        text = blocks(book, numbered=True)
        if text:
            yield TeiDoc(key=f"{stem}/{n}", title=label, text=f"{label}\n\n{text}")


def _cts_member(name: str) -> str | None:
    m = re.fullmatch(r"(?:.+/)?data/[^/]+/[^/]+/([\w.]+\.[\w-]*-eng\d*)\.xml", name)
    return m.group(1) if m else None


# --- reStructuredText (the Linux kernel's Documentation/) ------------------------------------------

_RST_RULE = re.compile(r"^([=\-~^*#+`':.\"_])\1{2,}\s*$")
_RST_COMMENT = re.compile(r"^\.\. (?:SPDX-License-Identifier|_[\w-]+:|\|[^|]+\| )")


def rst_page(data: bytes, stem: str) -> Iterator[TeiDoc]:
    """One kernel documentation page (``Documentation/admin-guide/cgroup-v2.rst``): titled by its
    first section title, its text as written, heading rules and label lines left out. The field
    exam asked about cgroup v2's memory.max and found only a Wikibooks outline (2026-10-17)."""
    lines = data.decode("utf-8", errors="replace").splitlines()
    title = ""
    out: list[str] = []
    for i, line in enumerate(lines):
        if _RST_RULE.match(line) or _RST_COMMENT.match(line):
            continue
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        if not title and line.strip() and _RST_RULE.match(nxt) and len(nxt.strip()) >= len(line.strip()):
            title = line.strip()
        out.append(line.rstrip())
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()
    if not text:
        return
    title = title or stem.rsplit("/", 1)[-1].replace("-", " ").replace("_", " ")
    yield TeiDoc(key=stem, title=title, text=text, url=f"https://docs.kernel.org/{stem}.html")


# --- Text Creation Partnership ------------------------------------------------------------------

_TCP_ID = re.compile(r"(?:^|[/_])([ABKN]\d{5})(?:-[0-9a-f]{8})?\.xml$")


def tcp_text(xml: bytes, stem: str) -> Iterator[TeiDoc]:
    """One TCP book (``<stem>.xml``)."""
    root = ET.fromstring(xml)  # noqa: S314 - the Text Creation Partnership's own TEI
    stmt = root.find(f"{TEI}teiHeader/{TEI}fileDesc/{TEI}titleStmt")
    title = _flat(stmt.find(f"{TEI}title")) if stmt is not None else ""
    author = _flat(stmt.find(f"{TEI}author")) if stmt is not None else ""
    pub = root.find(f"{TEI}teiHeader/{TEI}fileDesc/{TEI}sourceDesc/{TEI}biblFull/{TEI}publicationStmt")
    year = re.search(r"1[4-8]\d\d", _flat(pub.find(f"{TEI}date")) if pub is not None else "")
    text_el = root.find(f"{TEI}text")
    text = blocks(text_el) if text_el is not None else ""
    if not text:
        return
    title = title if len(title) <= 200 else title[:200].rsplit(" ", 1)[0] + "..."
    who = ", ".join(x for x in (author.rstrip("."), year.group(0) if year else "") if x)
    yield TeiDoc(
        key=stem,
        title=f"{title or stem} ({who})" if who else title or stem,
        text=text,
        url=f"https://github.com/textcreationpartnership/{stem}",
    )


_DTA_ID = re.compile(r"([^/]+)\.TEI-P5\.xml$")
_LB = "\x01"  # a line break of the print, kept through blocks() so a hyphenated word can be joined
_UMLAUT = {"a": "ä", "o": "ö", "u": "ü", "A": "Ä", "O": "Ö", "U": "Ü"}


def dta_text(text: str) -> str:
    """The diplomatic transcription made searchable (see the module notes)."""
    text = re.sub(f"[-¬]\\s*{_LB}\\s*", "", text)  # "an-" + line break + "dern"
    text = re.sub(f"\\s*{_LB}\\s*", " ", text)
    text = re.sub("([aouAOU])\u0364", lambda m: _UMLAUT[m.group(1)], text.replace("\u017f", "s"))
    return "\n\n".join(b.strip() for b in text.split("\n\n") if b.strip())


def dta_book(xml: bytes, stem: str) -> Iterator[TeiDoc]:
    """One DTA book (``<stem>.TEI-P5.xml``)."""
    root = ET.fromstring(xml)  # noqa: S314 - the archive's own TEI; expat rejects external entities
    text_el = root.find(f"{TEI}text")
    if text_el is None:
        return
    for lb in text_el.iter(f"{TEI}lb"):
        lb.tail = _LB + (lb.tail or "")
    text = dta_text(blocks(text_el))
    if not text:
        return
    stmt = root.find(f"{TEI}teiHeader/{TEI}fileDesc/{TEI}titleStmt")
    main = stmt.find(f"{TEI}title[@type='main']") if stmt is not None else None
    title = _flat(main if main is not None else (stmt.find(f"{TEI}title") if stmt is not None else None))
    person = stmt.find(f"{TEI}author/{TEI}persName") if stmt is not None else None
    author = (
        " ".join(_flat(person.find(f"{TEI}{part}")) for part in ("forename", "surname")).strip()
        if person is not None
        else ""
    )
    year = re.search(r"_(1[5-9]\d\d)$", stem)
    title = dta_text(title if len(title) <= 200 else title[:200].rsplit(" ", 1)[0] + "...")
    who = ", ".join(x for x in (author, year.group(1) if year else "") if x)
    yield TeiDoc(
        key=stem,
        title=f"{title or stem} ({who})" if who else title or stem,
        text=text,
        url=f"https://www.deutschestextarchiv.de/book/show/{stem}",
        lang="de",
    )


def _dta_member(name: str) -> str | None:
    m = _DTA_ID.search(name)
    return m.group(1) if m else None


_ECPA_ID = re.compile(r"([a-z0-9]+-w\d{4})\.xml$")


def ecpa_work(xml: bytes, stem: str) -> Iterator[TeiDoc]:
    """One ECPA work (``<id>.xml``)."""
    root = ET.fromstring(xml)  # noqa: S314 - the archive's own TEI; expat rejects external entities
    text_el = root.find(f"{TEI}text")
    if text_el is None:
        return
    for el in text_el.iter():
        # Word-level markup: "<w>noblest</w>\n   <w>counsels</w>\n   <pc>,</pc>" is "noblest counsels,"
        # with the spaces in <c> elements; the layout whitespace between elements is not text.
        if el.tail is not None and not el.tail.strip():
            el.tail = ""
    text = blocks(text_el)
    if not text:
        return
    stmt = root.find(f"{TEI}teiHeader/{TEI}fileDesc/{TEI}titleStmt")
    title = _flat(stmt.find(f"{TEI}title")) if stmt is not None else ""
    author = _flat(stmt.find(f"{TEI}author")) if stmt is not None else ""
    title = title if len(title) <= 200 else title[:200].rsplit(" ", 1)[0] + "..."
    yield TeiDoc(
        key=stem,
        title=f"{title or stem} ({author})" if author else title or stem,
        text=text,
        url=f"https://www.eighteenthcenturypoetry.org/works/{stem}.shtml",
    )


def _ecpa_member(name: str) -> str | None:
    m = _ECPA_ID.search(name)
    return m.group(1) if m else None


def _tcp_member(name: str) -> str | None:
    m = _TCP_ID.search(name)
    return m.group(1) if m else None


def _kernel_member(name: str) -> str | None:
    m = re.fullmatch(r"(?:[^/]+/)?Documentation/([\w./+-]+)\.rst", name)
    return m.group(1) if m and "/translations/" not in f"/{m.group(1)}" else None


READERS: dict[str, tuple[Callable[[str], str | None], Callable[[bytes, str], Iterator[TeiDoc]]]] = {
    "tei-frus": (_frus_member, frus_volume),
    "tei-cts": (_cts_member, cts_work),
    "tei-tcp": (_tcp_member, tcp_text),
    "tei-ecpa": (_ecpa_member, ecpa_work),
    "tei-dta": (_dta_member, dta_book),
    "rst-kernel": (_kernel_member, rst_page),
}


def read_snapshot(path: Path, reader: str, name: str | None = None) -> Iterator[TeiDoc]:
    """Every document a ``reader`` finds in a repository snapshot (a zip of its files), or in a single
    file the reader reads (a catalogue published as one XML or JSON file: CAPEC, MedlinePlus).

    ``name``: the single file's name as published (its URL's last part). The vault stores it under its
    document key (``medlineplus__MPLUS_TOPICS-2353a4ac.xml``), which no reader recognises: MedlinePlus
    was indexed with no documents (2026-10-19)."""
    from wsp_adapters.doccollection import structured  # its readers build TeiDocs too

    whole = structured.ARCHIVE_READERS.get(reader)
    if whole is not None:  # a reader of the whole snapshot (a book's chapters in its own order)
        try:
            with zipfile.ZipFile(path) as archive:
                yield from whole(archive)
        except (zipfile.BadZipFile, ET.ParseError, KeyError):
            return
        return
    member, parse = {**READERS, **structured.READERS}[reader]
    if not zipfile.is_zipfile(path):
        stem = member(name) if name else None
        stem = stem if stem is not None else member(path.name)
        if stem is not None:
            try:
                yield from parse(path.read_bytes(), stem)
            except (ET.ParseError, ValueError):
                return
        return
    with zipfile.ZipFile(path) as archive:
        for info in archive.infolist():
            stem = member(info.filename)
            if stem is None or info.is_dir():
                continue
            try:
                yield from parse(archive.read(info), stem)
            except (ET.ParseError, ValueError):
                continue  # one malformed file does not stop the collection
