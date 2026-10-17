"""Plain text of a PDF held in memory (a nautilus archive's documents, ``wsp_core.nautilus``).

The refiner reads PDFs in reading order (``wsp_adapters.pdftext``); a page served straight from an
archive only needs the words, so this is pdfium's own text, page by page. pdfium keeps every parsed
page until the document closes (a CAD-drawing PDF grew past 7 GB in the nightly refine, 2026-10-17),
so the document is reopened every :data:`PAGES_PER_OPEN` pages, and reading stops after
:data:`SECONDS` with a note of the pages left.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable

# pdfium writes U+FFFE for a hyphen that breaks a word at a line's end ("fol\ufffe\r\nlowing"): joined.
_LINE_HYPHEN = re.compile("\ufffe[ \t]*(?:\r?\n)?")
PAGES_PER_OPEN = 10
MAX_PAGES = 400
SECONDS = 60.0


def pdf_text(
    data: bytes,
    *,
    max_pages: int = MAX_PAGES,
    seconds: float = SECONDS,
    clock: Callable[[], float] = time.monotonic,
) -> str:
    """The text of ``data``'s first ``max_pages`` pages, a form feed between pages ("" if unreadable)."""
    import pypdfium2 as pdfium

    try:
        pdf = pdfium.PdfDocument(data)
    except pdfium.PdfiumError:
        return ""
    out: list[str] = []
    try:
        pages = min(len(pdf), max_pages)
        started = clock()
        for i in range(pages):
            if clock() - started > seconds:
                out.append(f"[Pages {i + 1}-{pages} were not read here: download the file to read them.]")
                break
            if i and i % PAGES_PER_OPEN == 0:
                pdf.close()
                pdf = pdfium.PdfDocument(data)
            page = pdf[i]
            try:
                textpage = page.get_textpage()
                out.append(textpage.get_text_range())
                textpage.close()
            finally:
                page.close()
    finally:
        pdf.close()
    return clean("\f".join(out))


def clean(text: str) -> str:
    """pdfium's text as the refiner reads it (``wsp_adapters.doccollection``): U+FFFD, a glyph the PDF
    maps to no character (the USDA canning guide's spaces: "Recommended\ufffdprocess\ufffdtime"), is a
    space; a line-end hyphen joins its word again; soft hyphens go."""
    text = text.replace("\r\n", "\n").replace("\ufffd", " ").replace("\u00ad", "")
    return _LINE_HYPHEN.sub("", text)
