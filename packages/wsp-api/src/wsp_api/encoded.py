"""Archived files still in their transfer encoding.

A crawler that saved a response body without undoing its ``Content-Encoding`` leaves a text file
(an SVG, a stylesheet) as brotli or gzip bytes under a text mimetype: the browser shows a broken
image (anonymousplanet.org's Creative Commons icons, 2026-10-21 audit). Such a file is decoded once
here, when it does not start as text and decodes to text; anything else is served as it is.
"""

from __future__ import annotations

import zlib
from collections.abc import Callable
from typing import Any, cast

import brotli

_br = cast(Any, brotli)  # the package has no type information
compress_brotli = cast(Callable[[bytes], bytes], _br.compress)

MAX_INPUT = 4 << 20  # files larger than this are not guessed at
MAX_OUTPUT = 32 << 20  # a decoded file stops here (a bad archive is not a reason to fill memory)
_TEXTUAL = frozenset(
    {
        "image/svg+xml",
        "application/javascript",
        "application/json",
        "application/xml",
        "application/xhtml+xml",
    }
)


def _textual(mimetype: str) -> bool:
    kind = mimetype.split(";", 1)[0].strip().lower()
    return kind.startswith("text/") or kind in _TEXTUAL


def _looks_like_text(data: bytes) -> bool:
    head = data[:512]
    try:
        head.decode("utf-8")
    except UnicodeDecodeError as exc:
        if exc.start < len(head) - 3:  # a character cut at the end of the sample is still text
            return False
    return not any(b < 9 or 13 < b < 32 for b in head)


def _brotli(data: bytes) -> bytes | None:
    d = _br.Decompressor()
    try:
        out = cast(bytes, d.process(data, output_buffer_limit=MAX_OUTPUT))
    except _br.error:
        return None
    # the limit only stops the buffer growing: a short input can still come out whole
    return out if d.is_finished() and len(out) <= MAX_OUTPUT else None


def _gzip(data: bytes) -> bytes | None:
    d = zlib.decompressobj(wbits=31)
    try:
        out = d.decompress(data, MAX_OUTPUT)
    except zlib.error:
        return None
    return out if d.eof and not d.unconsumed_tail and len(out) <= MAX_OUTPUT else None


# How a picture of each type starts: a gzip-encoded one is decoded only when it decodes to one.
_SIGNATURES: dict[str, tuple[bytes, ...]] = {
    "image/jpeg": (b"\xff\xd8\xff",),
    "image/png": (b"\x89PNG\r\n\x1a\n",),
    "image/gif": (b"GIF87a", b"GIF89a"),
    "image/webp": (b"RIFF",),
    "image/bmp": (b"BM",),
    "image/x-icon": (b"\x00\x00\x01\x00",),
    "image/vnd.microsoft.icon": (b"\x00\x00\x01\x00",),
}


def decoded(content: bytes, mimetype: str) -> bytes:
    """``content``, or its decoding when a text file was stored brotli- or gzip-encoded, or a picture
    gzip-encoded (footballdatabase.com's club badges, JPEGs in gzip, 2026-10-21 audit)."""
    if not content or len(content) > MAX_INPUT:
        return content
    kind = mimetype.split(";", 1)[0].strip().lower()
    if kind in _SIGNATURES:
        if content[:2] != b"\x1f\x8b":
            return content
        out = _gzip(content)
        return out if out is not None and out.startswith(_SIGNATURES[kind]) else content
    if not _textual(mimetype) or _looks_like_text(content):
        return content
    out = _gzip(content) if content[:2] == b"\x1f\x8b" else _brotli(content)
    return out if out is not None and _looks_like_text(out) else content


def html_as_image(content: bytes, mimetype: str) -> bool:
    """A web page saved under a picture's name: a raster image type whose bytes are HTML (LibreTexts
    kept OpenStax's error page as fig-ch04_18_01.png, 2026-10-19 and 2026-10-21 audits). Served as a
    picture it is a broken image; it is no picture at all."""
    kind = mimetype.split(";", 1)[0].strip().lower()
    if not kind.startswith("image/") or kind == "image/svg+xml":
        return False
    head = content[:64].lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    return head.startswith((b"<!doctype html", b"<html", b"<head", b"<body"))
