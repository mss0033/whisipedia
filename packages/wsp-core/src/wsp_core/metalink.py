"""Metalink v4 (RFC 5854) parsing: one parser for the fetcher and the adapters.

The Kiwix adapter had its own, weaker copy (no root-tag check, piece hashes not lowercased, piece
coverage not checked, a raw XML error in place of :class:`MetalinkError`); a checksum fix landed in one
copy and not the other (2026-10-29 spot check). Generation stays in ``wsp_fetch.metalink``.

Kiwix (MirrorBrain) serves ``<file>.meta4`` with the full-file digests, a mirror list ordered by
priority and per-piece SHA-1 hashes; those let us verify every piece as it lands and re-fetch only a
bad piece, from a different mirror (PLAN §6.5).
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

from wsp_core.checksums import normalize_algorithm
from wsp_core.errors import FetchError
from wsp_core.model import Checksum

NS = "urn:ietf:params:xml:ns:metalink"
_Q = f"{{{NS}}}"


class MetalinkError(FetchError):
    """The document is not a usable Metalink v4 file."""


@dataclass(frozen=True, slots=True)
class MetalinkUrl:
    url: str
    priority: int = 999_999  # RFC 5854 §4.2.16: lower is preferred, 1..999999
    location: str | None = None


@dataclass(frozen=True, slots=True)
class Pieces:
    """Per-piece digests (RFC 5854 §4.2.11): piece ``i`` covers ``[i*length, (i+1)*length)``."""

    length: int
    algorithm: str
    hashes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class MetalinkFile:
    name: str
    size: int | None = None
    checksums: tuple[Checksum, ...] = ()
    pieces: Pieces | None = None
    urls: tuple[MetalinkUrl, ...] = field(default_factory=tuple[MetalinkUrl, ...])

    def mirror_urls(self) -> tuple[str, ...]:
        """URLs sorted by priority (stable for equal priorities)."""
        return tuple(u.url for u in sorted(self.urls, key=lambda u: u.priority))


def parse_metalink(data: bytes) -> tuple[MetalinkFile, ...]:
    """Parse a Metalink v4 document. Input is size-capped by the caller (``get_bytes``)."""
    try:
        # expat >= 2.4.1 (bundled with CPython 3.12) refuses entity-expansion bombs; no DTDs are loaded.
        root = ET.fromstring(data)  # noqa: S314
    except ET.ParseError as exc:
        raise MetalinkError(f"invalid XML: {exc}") from exc
    if root.tag != f"{_Q}metalink":
        raise MetalinkError(f"not a Metalink v4 document (root {root.tag!r})")
    files: list[MetalinkFile] = []
    for f in root.iter(f"{_Q}file"):
        name = f.get("name")
        if not name:
            raise MetalinkError("<file> without name")
        size_el = f.find(f"{_Q}size")
        size = int((size_el.text or "").strip()) if size_el is not None else None
        sums = tuple(
            Checksum(normalize_algorithm(h.get("type", "")), (h.text or "").strip().lower())
            for h in f.findall(f"{_Q}hash")
        )
        pieces: Pieces | None = None
        p = f.find(f"{_Q}pieces")
        if p is not None:
            hashes = tuple((h.text or "").strip().lower() for h in p.findall(f"{_Q}hash"))
            length = int(p.get("length", "0"))
            if length <= 0 or not hashes:
                raise MetalinkError("<pieces> needs a positive length and at least one hash")
            if size is not None and len(hashes) != -(-size // length):
                raise MetalinkError(f"{len(hashes)} piece hashes do not cover {size} bytes")
            pieces = Pieces(length, normalize_algorithm(p.get("type", "")), hashes)
        urls = tuple(
            MetalinkUrl((u.text or "").strip(), int(u.get("priority", "999999")), u.get("location"))
            for u in f.findall(f"{_Q}url")
            if (u.text or "").strip()
        )
        files.append(MetalinkFile(name, size, sums, pieces, urls))
    if not files:
        raise MetalinkError("Metalink contains no <file>")
    return tuple(files)
