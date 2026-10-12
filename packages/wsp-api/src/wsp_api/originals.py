"""A document's stored original: the PDF, scanned map or archive a collection document was refined from.

Document collections refine a fetched file into searchable text (``html_ref = "file:<generation>/<path>"``);
the reader also wants the file itself: a map is a picture, a handbook has figures and tables. Routes:

* ``GET /v1/files/{doc_id}``: the bytes, from the document's own generation in the vault (never a path
  outside it). PDFs and web images open inline; anything else downloads.
* ``GET /v1/files/{doc_id}/preview``: a JPEG of a scanned map or image (browsers cannot show a GeoTIFF)
  or of a PDF's first page (a USGS map sheet), at most :data:`PREVIEW_SIZE` pixels on its long side,
  cached on local disk.

The routes are in :mod:`wsp_api.files`. The security middleware gives their responses their own policy
(:func:`files_csp`): a PDF may be framed by the Navigator; nothing else from upstream runs at our origin.
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from urllib.parse import quote

from wsp_api.schemas import OriginalFile
from wsp_core.model import DocRecord

FILES_PREFIX = "/v1/files/"
FILE_REF = "file:"
PREVIEW_SIZE = 2000
MEDIA_TYPES: dict[str, str] = {
    ".pdf": "application/pdf",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".zip": "application/zip",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".epub": "application/epub+zip",
    ".html": "text/html",
    ".htm": "text/html",
    ".txt": "text/plain",
    ".xml": "application/xml",
    ".json": "application/json",
}
INLINE = frozenset({".pdf", ".jpg", ".jpeg", ".png"})  # what a browser shows safely itself
PREVIEWABLE = frozenset({".pdf", ".tif", ".tiff", ".jpg", ".jpeg", ".png"})  # a PDF: its first page


def stored_file(doc: DocRecord) -> tuple[str, str] | None:
    """``(generation id, path inside it)`` of a document's stored original, or ``None``."""
    ref = doc.html_ref or ""
    if not ref.startswith(FILE_REF):
        return None
    generation, _, rel = ref[len(FILE_REF) :].partition("/")
    parts = PurePosixPath(rel).parts
    if not generation or not rel or rel.startswith("/") or ".." in parts or "" in parts:
        return None
    return generation, rel


_STORED_NAME = re.compile(r"^[a-z0-9-]+__(?P<stem>.+?)(?:-[0-9a-f]{8})?(?P<ext>\.[A-Za-z0-9]+)$")


def display_name(stored: str) -> str:
    """The file name a reader sees: the fetcher's ``<prefix>__<name>-<hash8>.<ext>`` becomes
    ``<name>.<ext>`` (a doubled extension, ``x.pdf.pdf``, is kept once)."""
    m = _STORED_NAME.match(stored)
    if m is None:
        return stored
    stem, ext = m["stem"], m["ext"]
    if stem.lower().endswith(ext.lower()):
        stem = stem[: -len(ext)]
    return f"{stem}{ext}" if stem else stored


def original_of(doc: DocRecord) -> OriginalFile | None:
    """How a client reaches the stored original (the document JSON's ``original``)."""
    where = stored_file(doc)
    if where is None:
        return None
    suffix = PurePosixPath(where[1]).suffix.lower()
    url = f"{FILES_PREFIX}{quote(doc.doc_id, safe='')}"
    return OriginalFile(
        url=url,
        name=display_name(PurePosixPath(where[1]).name),
        media_type=MEDIA_TYPES.get(suffix, "application/octet-stream"),
        inline=suffix in INLINE,
        preview_url=f"{url}/preview" if suffix in PREVIEWABLE else None,
    )


def files_csp(path: str, content_type: bytes) -> bytes | None:
    """The policy for a ``/v1/files/`` response (``None`` for other paths): no subresources ever; a PDF
    may be framed by our own pages; anything else is sandboxed (upstream HTML or SVG runs nothing here)."""
    if not path.startswith(FILES_PREFIX):
        return None
    if content_type.split(b";")[0].strip() == b"application/pdf":
        return b"default-src 'none'; frame-ancestors 'self'"
    return b"default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'self'; sandbox"
