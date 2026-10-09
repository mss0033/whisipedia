"""Stable identifiers (PLAN §9.2).

``doc_id`` format: ``wsp:<source_id>/<native_id>``. ``native_id`` may contain ``/``.
"""

from __future__ import annotations

import re
import unicodedata
from typing import NewType

import blake3

DocId = NewType("DocId", str)

_SOURCE_RE = re.compile(r"^[a-z0-9][a-z0-9.-]*$")
_PREFIX = "wsp:"


class InvalidDocIdError(ValueError):
    pass


def make_doc_id(source_id: str, native_id: str) -> DocId:
    if not _SOURCE_RE.match(source_id):
        raise InvalidDocIdError(f"invalid source id: {source_id!r}")
    if not native_id or native_id != native_id.strip():
        raise InvalidDocIdError(f"invalid native id: {native_id!r}")
    return DocId(f"{_PREFIX}{source_id}/{native_id}")


def parse_doc_id(doc_id: str) -> tuple[str, str]:
    """Return ``(source_id, native_id)``."""
    if not doc_id.startswith(_PREFIX):
        raise InvalidDocIdError(f"missing {_PREFIX!r} prefix: {doc_id!r}")
    source_id, sep, native_id = doc_id[len(_PREFIX) :].partition("/")
    if not sep:
        raise InvalidDocIdError(f"missing native id: {doc_id!r}")
    make_doc_id(source_id, native_id)  # validates both parts
    return source_id, native_id


def normalize_text(text: str) -> str:
    """Canonical form used for content hashing: NFC, LF newlines, no trailing whitespace per line."""
    text = unicodedata.normalize("NFC", text).replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(line.rstrip() for line in text.split("\n")).strip("\n")


def content_hash(text: str) -> str:
    """Content-version id: blake3 hex digest of the normalized text."""
    return blake3.blake3(normalize_text(text).encode("utf-8")).hexdigest()
