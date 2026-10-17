"""ZIMs made by openZIM's nautilus scraper: a catalogue of documents, not web pages.

The zimgit libraries (Food for Preppers, Water Treatment, Post Disaster, Medicine) and the USDA
Complete Guide to Home Canning are nautilus archives: a JavaScript app whose catalogue is
``database.js`` (``var DATABASE = [{'_id': '00004', 'ti': 'Part 4', 'dsc': 'Vegetables and Vegetable
Products', 'aut': '', 'fp': ['GUIDE04_HomeCan_rev0715.pdf']}, ...]``) and whose documents are the
files it names under ``files/``. The app shell (``home``) is a template the Navigator never runs, so
the 135 PDFs of these archives, the USDA guide's Part 4 among them (green beans, a field-exam
question), were neither listed nor searchable (2026-10-17).

This module reads the catalogue and writes plain pages for it: a front page that lists every
document (:func:`home_html`, served for ``home``) and a page per document (:func:`item_html`,
``nautilus/<id>``) with its description, its files and their text. The refiner (``wsp_adapters.kiwix``)
indexes each document under ``nautilus/<id>`` with its files' text; the API (``wsp_api.zim``) serves
the pages. Reading the archive and extracting text is the caller's job.
"""

from __future__ import annotations

import ast
import html
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast
from urllib.parse import quote

DATABASE = "database.js"
HOME = "home"
ITEM_PREFIX = "nautilus/"
FILES = "files/"
SCRAPER = "nautilus"  # the ``Scraper`` metadata of such archives starts with it ("nautiluszim 1.1.1")
_ARRAY = re.compile(r"\bvar\s+DATABASE\s*=\s*(\[.*\])\s*;?\s*$", re.DOTALL)


@dataclass(frozen=True, slots=True)
class Item:
    """One catalogue entry: a document and the files it is made of (``files/<name>``)."""

    id: str
    title: str
    description: str
    author: str
    files: tuple[str, ...]

    @property
    def path(self) -> str:
        return f"{ITEM_PREFIX}{self.id}"

    @property
    def heading(self) -> str:
        """The title, with the description when the title alone says little ("Part 4: Vegetables and
        Vegetable Products")."""
        if self.description and (len(self.title) <= 24 or not self.title):
            return f"{self.title}: {self.description}" if self.title else self.description
        return self.title or self.id


def items(database: bytes | str) -> list[Item]:
    """The catalogue of a ``database.js`` (empty if it is not one)."""
    text = database.decode("utf-8", "replace") if isinstance(database, bytes) else database
    m = _ARRAY.search(text.strip())
    if m is None:
        return []
    try:
        rows: object = ast.literal_eval(m.group(1))  # a literal list of dicts: nothing is executed
    except (ValueError, SyntaxError, MemoryError, RecursionError):
        return []
    out: list[Item] = []
    for raw in cast(list[object], rows) if isinstance(rows, list) else []:
        if not isinstance(raw, Mapping):
            continue
        row = cast(Mapping[str, Any], raw)
        files = tuple(str(f) for f in cast(list[object], row.get("fp") or []) if isinstance(f, str) and f)
        ident = str(row.get("_id", "")).strip()
        if not ident or not files or "/" in ident:
            continue
        out.append(
            Item(
                ident,
                _clean(row.get("ti")),
                _clean(row.get("dsc")),
                _clean(row.get("aut")),
                files,
            )
        )
    return out


def _clean(value: object) -> str:
    return " ".join(str(value or "").split())


def home_html(title: str, description: str, catalogue: Sequence[Item]) -> str:
    """The archive's front page: every document with its description (links relative to ``home``)."""
    rows = "".join(
        f'<li><a href="{ITEM_PREFIX}{quote(i.id)}">{html.escape(i.heading)}</a>'
        + (f" ({html.escape(i.author)})" if i.author else "")
        + "</li>"
        for i in catalogue
    )
    intro = f"<p>{html.escape(description)}</p>" if description else ""
    count = f"<p>{len(catalogue)} document{'s' if len(catalogue) != 1 else ''}.</p>"
    name = html.escape(title)
    head = f"<html><head><title>{name}</title></head>"
    return f"{head}<body><h1>{name}</h1>{intro}{count}<ul>{rows}</ul></body></html>"


def item_html(item: Item, texts: Mapping[str, str]) -> str:
    """A document's page: its description and author, its files (links relative to ``nautilus/<id>``,
    which the API offers as downloads) and their text (``texts``: by file name)."""
    name = html.escape(item.heading)
    parts = [f"<h1>{name}</h1>"]
    if item.description and item.description not in item.heading:
        parts.append(f"<p>{html.escape(item.description)}</p>")
    if item.author:
        parts.append(f"<p>By {html.escape(item.author)}</p>")
    links = "".join(f'<li><a href="../{FILES}{quote(f)}">{html.escape(f)}</a></li>' for f in item.files)
    parts.append(f"<ul>{links}</ul>")
    for f in item.files:
        text = texts.get(f, "")
        if not text.strip():
            continue
        if len(item.files) > 1:
            parts.append(f"<h2>{html.escape(f)}</h2>")
        for block in re.split(r"\n\s*\n|\f", text):
            block = block.strip()
            if block:
                parts.append("<p>" + html.escape(block).replace("\n", "<br>") + "</p>")
    return f"<html><head><title>{name}</title></head><body>{''.join(parts)}</body></html>"
