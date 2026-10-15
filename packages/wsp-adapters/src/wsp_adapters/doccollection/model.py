"""Candidate documents and the per-document metadata record (research doc §5.2)."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, replace
from typing import Any, cast
from urllib.parse import unquote, urlsplit

from wsp_adapters.doccollection.policy import DocFacts, normalize_pub_number
from wsp_core.titles import (  # re-exported: the adapter's modules import them from here
    _EXT_RE as _EXT_RE,  # pyright: ignore[reportPrivateUsage]
)
from wsp_core.titles import (
    _PCT_RE as _PCT_RE,  # pyright: ignore[reportPrivateUsage]
)
from wsp_core.titles import (
    clean_title as clean_title,
)
from wsp_core.titles import (
    decode_url_text as decode_url_text,
)
from wsp_core.titles import (
    display_title as display_title,
)
from wsp_core.titles import (
    is_filename_title as is_filename_title,
)

_KEY_BAD = re.compile(r"[^A-Za-z0-9._-]+")
_NAME_BAD = re.compile(r"[^A-Za-z0-9._-]+")
_GENERIC_TITLES = frozenset(
    {"pdf", "download", "download pdf", "read more", "read report", "view", "here", "link", "full text"}
)


def key_part(text: str) -> str:
    return _KEY_BAD.sub("-", text.strip()).strip("-") or "x"


def make_key(prefix: str, ident: str, edition: str = "") -> str:
    """``army:FM-5-0:2022-11-04`` (issuer prefix, normalised publication number or id, edition)."""
    parts = [key_part(prefix), key_part(ident)]
    if edition:
        parts.append(key_part(edition))
    return ":".join(parts)


def key_stem_from_url(url: str, *, decode: bool = False) -> str:
    """The URL part a document key is made from when the issuer gives no number.

    ``decode=False`` is the historical spelling (one ``unquote``): document keys and doc ids derived
    from it must never change, so a twice-encoded ``%2520`` stays ``%20`` and its key holds ``-20``.
    ``decode=True`` (registry ``access.decode_url_keys``, for sources with no stored documents yet)
    decodes every escape first, so new keys read ``Programming-Guide``.
    """
    if decode:
        return title_from_url(url)
    stem = unquote(urlsplit(url).path.rsplit("/", 1)[-1])
    stem = _EXT_RE.sub("", stem)
    return re.sub(r"[_\-\s]+", " ", stem).strip() or url


def title_from_url(url: str) -> str:
    """A display title from a URL's file name: escapes decoded, no extension, separators as spaces."""
    stem = decode_url_text(urlsplit(url).path.rsplit("/", 1)[-1])
    stem = _EXT_RE.sub("", stem)
    return re.sub(r"[_\-\s]+", " ", stem).strip() or url


def is_generic_title(text: str) -> bool:
    return text.strip().casefold() in _GENERIC_TITLES or len(text.strip()) < 3


def is_fragment(text: str) -> bool:
    """A title that is a piece of a sentence ("being sold on the commercial market"): it starts with a
    lower-case letter, has no capital letter at all, or is too short to name anything."""
    t = text.strip().strip("\"'“”")
    letters = [c for c in t if c.isalpha()]
    if len(letters) < 3:
        return True
    return letters[0].islower() or not any(c.isupper() for c in letters)


def extension_of(url: str, default: str = ".pdf") -> str:
    m = _EXT_RE.search(urlsplit(url).path)
    return "." + m.group(1).lower() if m else default


@dataclass(frozen=True, slots=True)
class Candidate:
    """One document an issuer lists, before anything about its bytes is known."""

    key: str
    url: str  # where the document's bytes are (HTTPS); may be a landing page until resolved
    title: str
    issuer: str
    pub_number: str = ""
    edition_date: str = ""  # ISO date (or year) when the issuer states one
    size: int | None = None
    md5: str | None = None
    sha256: str | None = None
    mirrors: tuple[str, ...] = ()
    subjects: tuple[str, ...] = ()
    abstract: str = ""
    rights: str = ""
    language: str = "en"
    landing_url: str = ""
    status: str = ""  # issuer's status label (ACTIVE, RESCINDED, ...)
    discovered_by: str = ""
    resolve: str = ""  # resolver name when ``url`` is a landing page (detail hop)
    partition: str = ""  # sub-generation for very large collections (state, collection, batch)
    extra: Mapping[str, str] = field(default_factory=dict[str, str])

    @property
    def facts(self) -> DocFacts:
        return DocFacts(
            key=self.key,
            pub_number=self.pub_number,
            title=self.screening_title,
            subjects=self.subjects,
            abstract=self.abstract[:2000],
            rights=self.rights,
        )

    @property
    def file_name(self) -> str:
        """Unique, flat file name under ``upstream/`` derived from the key; the extension is the
        URL's, or ``extra['suffix']`` where a discoverer knows better (a GOV.UK HTML page)."""
        base = _NAME_BAD.sub("_", self.key.replace(":", "__")).strip("_")[:150]
        digest = hashlib.sha256(self.key.encode()).hexdigest()[:8]
        return f"{base}-{digest}{self.extra.get('suffix') or extension_of(self.url)}"

    @property
    def screening_title(self) -> str:
        """The title as the issuer printed it (``extra['raw_title']``), for the screens; ``title`` may be
        recased for display, which would hide a case-sensitive marking such as ``(CUI)``."""
        return self.extra.get("raw_title") or self.title

    def with_url(self, url: str, **changes: Any) -> Candidate:
        return replace(self, url=url, resolve="", **changes)

    def to_json(self) -> dict[str, Any]:
        out = asdict(self)
        out["mirrors"] = list(self.mirrors)
        out["subjects"] = list(self.subjects)
        out["extra"] = dict(self.extra)
        return {k: v for k, v in out.items() if v not in ("", None, [], {})}

    @staticmethod
    def from_json(data: Mapping[str, Any]) -> Candidate:
        d = dict(data)
        d["mirrors"] = tuple(cast(list[str], d.get("mirrors") or []))
        d["subjects"] = tuple(cast(list[str], d.get("subjects") or []))
        d["extra"] = dict(cast(Mapping[str, str], d.get("extra") or {}))
        known = set(Candidate.__dataclass_fields__)
        return Candidate(**{k: v for k, v in d.items() if k in known})


def pub_key(prefix: str, pub_number: str, edition: str, fallback: str) -> str:
    if pub_number:
        return make_key(prefix, normalize_pub_number(pub_number), edition)
    return make_key(prefix, fallback, edition)


@dataclass(frozen=True, slots=True)
class DocumentMeta:
    """One line of ``meta/documents.jsonl``: everything known about one stored document version."""

    doc_key: str
    file: str  # path relative to the generation, e.g. upstream/army__FM-5-0__2022-11-04-ab12cd34.pdf
    issuer: str
    title: str
    generation: str = ""  # generation holding the bytes when it is an earlier one ("" = this one)
    pub_number: str = ""
    edition_date: str = ""
    distribution: str = ""  # A | public-release | license:<SPDX> | PD | declassified
    distribution_text: str = ""
    currency: str = "current"
    historical: bool = False
    banner: str = ""
    supersedes: str = ""
    superseded_by: str = ""
    filter_decision: str = ""
    origin_url: str = ""
    landing_url: str = ""
    mirrors: tuple[str, ...] = ()
    fetched_at: str = ""
    http: Mapping[str, str] = field(default_factory=dict[str, str])
    sha256: str = ""
    bytes: int = 0
    mirror_sha256: str = ""
    language: str = "en"
    subjects: tuple[str, ...] = ()
    license: str = ""
    md5: str = ""  # MD5 of the stored bytes (the issuer's listing MD5 matches it when unchanged)
    # The policy rule that now keeps the stored document out of the index ("" = indexed); see
    # wsp_adapters.doccollection.catalog.
    excluded: str = ""

    def to_line(self) -> str:
        data = asdict(self)
        data["mirrors"] = list(self.mirrors)
        data["subjects"] = list(self.subjects)
        data["http"] = dict(self.http)
        return json.dumps(
            {k: v for k, v in data.items() if v not in ("", None, [], {}, False)}, sort_keys=True
        )

    @staticmethod
    def from_line(line: str) -> DocumentMeta:
        d = cast(dict[str, Any], json.loads(line))
        d["mirrors"] = tuple(cast(list[str], d.get("mirrors") or []))
        d["subjects"] = tuple(cast(list[str], d.get("subjects") or []))
        d["http"] = dict(cast(Mapping[str, str], d.get("http") or {}))
        known = set(DocumentMeta.__dataclass_fields__)
        return DocumentMeta(**{k: v for k, v in d.items() if k in known})

    def record_meta(self) -> dict[str, str]:
        """String metadata carried in ``DocRecord.meta`` (and on to the API and Navigator banner)."""
        out = {
            "doc_key": self.doc_key,
            "issuer": self.issuer,
            "pub_number": self.pub_number,
            "edition_date": self.edition_date,
            "distribution": self.distribution,
            "currency": self.currency,
            "historical": "true" if self.historical else "",
            "banner": self.banner,
            "supersedes": self.supersedes,
            "superseded_by": self.superseded_by,
            "filter_decision": self.filter_decision,
            "origin_url": self.origin_url,
        }
        return {k: v for k, v in out.items() if v}
