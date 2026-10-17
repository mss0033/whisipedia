"""Tiny real ZIM files for tests (written with libzim)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

from libzim.writer import (  # pyright: ignore[reportMissingModuleSource]
    Creator,
    Hint,
    Item,
    StringProvider,
)


class _Item(Item):
    def __init__(self, path: str, mimetype: str, content: str) -> None:
        super().__init__()
        self._path, self._mimetype, self._content = path, mimetype, content

    def get_path(self) -> str:
        return self._path

    def get_title(self) -> str:
        return self._path

    def get_mimetype(self) -> str:
        return self._mimetype

    def get_contentprovider(self) -> StringProvider:
        return StringProvider(self._content)

    def get_hints(self) -> dict[Hint, int]:
        return {Hint.FRONT_ARTICLE: self._mimetype == "text/html"}


def write_zim(
    path: Path,
    name: str,
    items: Sequence[tuple[str, str, str]],
    redirects: Sequence[tuple[str, str]] = (),
    metadata: Mapping[str, str] | None = None,
) -> Path:
    """A ZIM at ``path`` with ``Name`` metadata ``name``; ``items`` are ``(path, mimetype, content)``
    (the first is the main page) and ``redirects`` are ``(path, target)``; ``metadata`` adds more
    (``Scraper``, ``Source``)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with Creator(path).config_indexing(False, "eng").config_verbose(False) as creator:
        creator.set_mainpath(items[0][0])
        for p, mime, body in items:
            creator.add_item(_Item(p, mime, body))
        for p, target in redirects:
            creator.add_redirection(p, p, target, {Hint.FRONT_ARTICLE: True})
        creator.add_metadata("Name", name)
        creator.add_metadata("Language", "eng")
        creator.add_metadata("Date", "2026-09-01")
        for key, value in (metadata or {}).items():
            creator.add_metadata(key, value)
    return path


def tiny_pdf(texts: list[str]) -> bytes:
    """A PDF with one page per text (Helvetica, one line each), written by hand with a correct xref."""
    objs = ["<< /Type /Catalog /Pages 2 0 R >>", ""]
    kids: list[str] = []
    for i, text in enumerate(texts):
        page, content = 4 + 2 * i, 5 + 2 * i
        kids.append(f"{page} 0 R")
        stream = f"BT /F1 24 Tf 72 700 Td ({text}) Tj ET"
        objs.append(
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 3 0 R >> >> /Contents {content} 0 R >>"
        )
        objs.append(f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream")
    objs[1] = f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(texts)} >>"
    objs.insert(2, "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    out, offsets = b"%PDF-1.4\n", list[int]()
    for k, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{k} 0 obj\n{body}\nendobj\n".encode()
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return out
