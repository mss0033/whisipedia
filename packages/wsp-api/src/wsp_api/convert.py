"""Domain objects (``wsp_core.model``) to wire models (``wsp_api.schemas``), plus cursor pagination."""

from __future__ import annotations

import base64
import binascii
import json
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import cast

from pydantic import JsonValue

from wsp_api import sites
from wsp_api.addresses import address_of
from wsp_api.errors import ApiError
from wsp_api.originals import original_of
from wsp_api.schemas import (
    Change,
    DocKind,
    Document,
    DocumentLink,
    DocumentParts,
    GenerationInfo,
    Hit,
    LedgerEntry,
    OtherCopy,
    Rendition,
    SourceHealth,
    SourceInfo,
    VaultLocation,
)
from wsp_core.banners import HISTORICAL_BANNER, banner_for, currency_of
from wsp_core.model import ChangeRecord, DocRecord, Generation, LedgerEvent, SearchHit, SourceSpec
from wsp_core.titles import shown_title

_EDITION_RE = re.compile(r"^(\d{4})(?:-(\d{2})(?:-(\d{2}))?)?$")


def edition_time(revision: str | None) -> datetime | None:
    """A document's date from its edition (``revision`` 1928, 1975-06 or 1992-04-01) when it has no
    timestamp of its own: maps and collection documents showed "date unknown" while stating
    "Edition: 1928" (2026-10-14 field exam)."""
    m = _EDITION_RE.match((revision or "").strip())
    if m is None:
        return None
    try:
        return datetime(int(m.group(1)), int(m.group(2) or 1), int(m.group(3) or 1), tzinfo=UTC)
    except ValueError:
        return None


def dated_banner(banner: str | None, when: datetime | None) -> str | None:
    """The historical notice with the document's year ("Historical document (1975) — ..."): the same
    sentence on every old page told a reader nothing about this one (2026-10-14 field exam)."""
    if banner is None or when is None or not banner.startswith(HISTORICAL_BANNER):
        return banner
    head, _, tail = HISTORICAL_BANNER.partition(" — ")
    return f"{head} ({when.year}) — {tail}{banner[len(HISTORICAL_BANNER) :]}"


def readable_text(text: str | None) -> str | None:
    """Stored text as a reader should see it. PDF text extraction writes U+FFFD for glyphs the PDF
    does not map to a character (the USDA canning tables' spacing: "30\ufffdmin" for "30 min", 310 times
    in one guide; 2026-10-14 field exam): a space reads right, the replacement character never does.
    Soft hyphens (U+00AD) go: invisible in a browser, they split a word for a text reader or a search
    of the page ("inter\u00adnational")."""
    if not text:
        return text
    if "\ufffd" in text:
        text = text.replace("\ufffd", " ")
    return text.replace("\u00ad", "") if "\u00ad" in text else text


def source_info(
    spec: SourceSpec,
    current: Generation | None,
    *,
    specs: Sequence[SourceSpec] = (),
    now: datetime | None = None,
    doc_count: int | None = None,
    home_doc_id: str | None = None,
    members: Sequence[Generation] = (),
) -> SourceInfo:
    """``members``: a partitioned source's current set (ADR-0010). The site is all of them: its size is
    their sum and it is as fresh as the newest; ``current`` is then the newest member."""
    if members:
        current = max(members, key=lambda g: (g.sealed_at is not None, g.sealed_at, g.generation_id))
    held = members or ([current] if current is not None else [])
    fresh = None if now is None else sites.freshness(spec, current, now)
    return SourceInfo(
        id=spec.id,
        name=spec.name,
        tier=spec.tier.value,
        status=spec.status.value,
        license=spec.license,
        legal_basis=spec.legal_basis,
        domain=list(spec.domain),
        adapter=spec.adapter,
        cadence=spec.cadence,
        strategy=None if spec.strategy is None else spec.strategy.value,
        irreplaceable=spec.irreplaceable,
        address=address_of(spec.id),
        current_generation=None if current is None else current.generation_id,
        publisher=sites.raw_str(spec, "publisher"),
        description=sites.raw_str(spec, "description"),
        kind=sites.kind_of(spec),
        health=SourceHealth.OFFLINE if fresh is None else fresh.health,
        doc_count=doc_count,
        bytes=sum(a.size for g in held for a in g.artifacts) if held else None,
        as_of=None if current is None else current.sealed_at,
        home_doc_id=sites.raw_str(spec, "home_doc_id") or home_doc_id,
        related=sites.related(spec, specs),
    )


def generation_info(gen: Generation, *, current: bool, size: tuple[int, int] | None = None) -> GenerationInfo:
    """``size``: (files, bytes) when ``gen`` comes without its artifacts (the snapshot's lists)."""
    files, total = size if size is not None else (len(gen.artifacts), sum(a.size for a in gen.artifacts))
    return GenerationInfo(
        source_id=gen.source_id,
        generation_id=gen.generation_id,
        status=gen.status.value,
        sealed_at=gen.sealed_at,
        artifact_count=files,
        total_bytes=total,
        current=current,
    )


def document(
    doc: DocRecord,
    *,
    spec: SourceSpec | None,
    links: Sequence[DocumentLink] = (),
    generation_id: str | None = None,
    html: str | None = None,
    vault: VaultLocation | None = None,
    rendition: Rendition | None = None,
    has_history: bool = False,
    include_text: bool = True,
    copies: Sequence[OtherCopy] = (),
    section: str | None = None,
    parts: DocumentParts | None = None,
) -> Document:
    """``include_text``: send ``text`` even when there is HTML (always sent when there is none)."""
    return Document(
        doc_id=doc.doc_id,
        copies=list(copies),
        source_id=doc.source_id,
        native_id=doc.native_id,
        title=shown_title(doc.title),
        lang=doc.lang,
        license=doc.license,
        text=readable_text(doc.text) if include_text or html is None else None,
        content_hash=doc.content_hash,
        revision=doc.revision,
        timestamp=doc.timestamp or edition_time(doc.revision),
        url_native=doc.url_native,
        html_ref=doc.html_ref,
        entities=list(doc.entities),
        links=list(links),
        address=address_of(doc.source_id, doc.native_id),
        source_name=doc.source_id if spec is None else spec.name,
        kind=DocKind.OTHER if spec is None else sites.kind_of(spec),
        generation_id=generation_id,
        format="text" if html is None else "html",
        html=html,
        vault=vault,
        rendition=rendition,
        has_history=has_history,
        currency=currency_of(doc.meta),
        banner=dated_banner(banner_for(doc.meta), doc.timestamp or edition_time(doc.revision)),
        original=original_of(doc),
        section=section,
        parts=parts,
    )


def hit(
    h: SearchHit,
    *,
    spec: SourceSpec | None = None,
    doc: DocRecord | Mapping[str, object] | None = None,
    highlights: Sequence[tuple[int, int]] = (),
) -> Hit:
    """``doc``: the document, or just its ``license``/``timestamp``/``lang``/``url_native`` fields."""
    source_id, _, native_id = h.doc_id.removeprefix("wsp:").partition("/")
    meta: Mapping[str, object] = (
        {} if doc is None else doc if isinstance(doc, Mapping) else {f: getattr(doc, f) for f in _HIT_FIELDS}
    )
    return Hit(
        doc_id=h.doc_id,
        score=h.score,
        title=shown_title(h.title),
        snippet=h.snippet,
        source_id=h.source_id or source_id,
        address=address_of(source_id, native_id),
        source_name=(h.source_id or source_id) if spec is None else spec.name,
        kind=DocKind.OTHER if spec is None else sites.kind_of(spec),
        license=_opt_str(meta.get("license")),
        as_of=ts
        if isinstance(ts := meta.get("timestamp"), datetime)
        else edition_time(_opt_str(meta.get("revision"))),
        lang=_opt_str(meta.get("lang")),
        url_native=_opt_str(meta.get("url_native")),
        highlights=list(highlights),
    )


_HIT_FIELDS = ("license", "timestamp", "lang", "url_native", "revision")


def _opt_str(value: object) -> str | None:
    return None if value is None else str(value)


def change(c: ChangeRecord, *, title: str | None = None, added_at: datetime | None = None) -> Change:
    source_id, _, native_id = c.doc_id.removeprefix("wsp:").partition("/")
    return Change(
        doc_id=c.doc_id,
        kind=c.kind.value,
        generation_id=c.generation_id,
        old_hash=c.old_hash,
        new_hash=c.new_hash,
        renamed_from=c.renamed_from,
        source_id=source_id,
        title=shown_title(title) if title else title,
        address=address_of(source_id, native_id),
        added_at=added_at,
    )


def _jsonable(data: Mapping[str, object]) -> dict[str, JsonValue]:
    return cast(dict[str, JsonValue], json.loads(json.dumps(dict(data), default=str)))


def ledger_entry(e: LedgerEvent) -> LedgerEntry:
    return LedgerEntry(
        seq=e.seq, at=e.at, kind=e.kind, subject=e.subject, data=_jsonable(e.data), hash=e.hash
    )


# --- Cursor pagination ---------------------------------------------------------------------------
# Cursors are opaque to clients. Today they encode an offset into a stable ordering; the encoding may
# change to keyset form without a breaking change because clients only echo them back.


def encode_cursor(offset: int) -> str:
    return base64.urlsafe_b64encode(json.dumps({"o": offset}).encode()).decode().rstrip("=")


def decode_cursor(cursor: str | None) -> int:
    if not cursor:
        return 0
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        offset = json.loads(raw)["o"]
    except (binascii.Error, ValueError, KeyError, TypeError) as exc:
        raise ApiError(400, "invalid_cursor", "cursor was not issued by this server") from exc
    if not isinstance(offset, int) or offset < 0:
        raise ApiError(400, "invalid_cursor", "cursor was not issued by this server")
    return offset


def page[T](items: Sequence[T], cursor: str | None, limit: int) -> tuple[Sequence[T], str | None]:
    start = decode_cursor(cursor)
    chunk = items[start : start + limit]
    nxt = encode_cursor(start + limit) if start + limit < len(items) else None
    return chunk, nxt
