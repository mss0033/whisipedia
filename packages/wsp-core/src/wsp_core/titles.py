"""Titles made from file names: what a reader sees instead of ``IS0505_IG.pdf``.

Shared by the document-collection adapter (titles at refine time) and the API (documents refined
before the adapter cleaned them still show a file name; field exam 2026-10-14).
"""

from __future__ import annotations

import re
from urllib.parse import unquote

# Documents, and the data files of data sources (filter data_files): a name the pattern misses gets
# ".pdf" and fails the PDF check (GHCN-Daily's .tar.gz, 2026-10-20; NARA's .jsonl, 2026-10-24).
_EXT_RE = re.compile(
    r"\.(pdf|epub|html?|txt|docx?|xml|json|jsonl|ndjson|zip|tiff?|jpe?g|png|bundle"
    r"|gz|tgz|bz2|xz|zst|tar|7z|pbf|npz|nc|grib2|csv|tsv|sdf|obo|owl|dat|fasta|cif|sql|sqlite)$",
    re.IGNORECASE,
)
_PCT_RE = re.compile(r"%[0-9A-Fa-f]{2}")


def decode_url_text(text: str) -> str:
    """``%XX`` escapes decoded, repeatedly (a link encoded twice, ``%2520``, decodes to a space)."""
    for _ in range(3):
        if not _PCT_RE.search(text):
            break
        text = unquote(text)
    return text


def is_filename_title(text: str) -> bool:
    """A title that is a file name: it ends in a file extension, or is one token joined by ``_``/``-``,
    or still carries URL escapes."""
    t = text.strip()
    return bool(_EXT_RE.search(t) or _PCT_RE.search(t) or (" " not in t and re.search(r"[_\-]", t)))


def clean_title(text: str) -> str:
    """A title without file-name debris: escapes decoded, no extension, and, for a file name, ``_`` and
    ``-`` as spaces ("IS0505 IG.pdf" -> "IS0505 IG")."""
    t = decode_url_text(text.strip())
    filename = is_filename_title(t)
    t = _EXT_RE.sub("", t).strip()
    if filename:
        # A space escaped once and slugged ("Programming-20Guide"): the debris of a key.
        t = re.sub(r"(?<=[A-Za-z])-20(?=[A-Za-z])", " ", t)
        # One token ("IS0505_IG", "Field-Guide"): every separator is a space. A name with spaces keeps
        # its hyphens ("Pool 2023-2027").
        t = re.sub(r"[_\-]+" if " " not in t else r"_+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


_TOPO_RE = re.compile(r"^([A-Z]{2})[ _]+(.+?)[ _]+(\d{4,7})[ _]+(\d{4})[ _]+(\d{4,7})(?:[ _]+geo)?$")


def display_title(text: str) -> str:
    """:func:`clean_title`, and a human title for file-name patterns the library knows: a USGS sheet
    "RI East Greenwich 353279 1942 24000 geo" reads "East Greenwich, RI (1942) — 1:24,000 topographic
    map" (the original stays an alias, so the cell id is searchable)."""
    t = clean_title(text)
    if m := _TOPO_RE.match(t.replace("-", " ")):
        state, place, _cell, year, scale = m.groups()
        return f"{place.replace('_', ' ').strip()}, {state} ({year}) — 1:{int(scale):,} topographic map"
    return t


def shown_title(title: str) -> str:
    """``title`` as a reader should see it: a file-name title made readable, any other unchanged.

    Narrower than :func:`is_filename_title`, since it is applied to every source's titles: only a
    file extension or URL escapes mark a file name here. "Wi-Fi" is one hyphenated token and
    "pthread_create(3)" one joined by an underscore, and both are titles."""
    t = title.strip()
    if not t or not (_EXT_RE.search(t) or _PCT_RE.search(t)):
        return title
    return display_title(t) or title
