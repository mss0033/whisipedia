"""TEI texts of collections published as git repositories (a ``reader`` in the registry ``access``).

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


READERS: dict[str, tuple[Callable[[str], str | None], Callable[[bytes, str], Iterator[TeiDoc]]]] = {
    "tei-frus": (_frus_member, frus_volume),
    "tei-cts": (_cts_member, cts_work),
}


def read_snapshot(path: Path, reader: str) -> Iterator[TeiDoc]:
    """Every document a ``reader`` finds in a repository snapshot (a zip of its files)."""
    member, parse = READERS[reader]
    with zipfile.ZipFile(path) as archive:
        for info in archive.infolist():
            stem = member(info.filename)
            if stem is None or info.is_dir():
                continue
            try:
                yield from parse(archive.read(info), stem)
            except ET.ParseError:
                continue  # one malformed file does not stop the collection
