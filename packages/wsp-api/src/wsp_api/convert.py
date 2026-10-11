"""Domain objects (``wsp_core.model``) to wire models (``wsp_api.schemas``), plus cursor pagination."""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import cast

from pydantic import JsonValue

from wsp_api import sites
from wsp_api.addresses import address_of
from wsp_api.errors import ApiError
from wsp_api.schemas import (
    Change,
    DocKind,
    Document,
    DocumentLink,
    GenerationInfo,
    Hit,
    LedgerEntry,
    SourceHealth,
    SourceInfo,
    VaultLocation,
)
from wsp_core.banners import banner_for, currency_of
from wsp_core.model import ChangeRecord, DocRecord, Generation, LedgerEvent, SearchHit, SourceSpec


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


def generation_info(gen: Generation, *, current: bool) -> GenerationInfo:
    return GenerationInfo(
        source_id=gen.source_id,
        generation_id=gen.generation_id,
        status=gen.status.value,
        sealed_at=gen.sealed_at,
        artifact_count=len(gen.artifacts),
        total_bytes=sum(a.size for a in gen.artifacts),
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
    has_history: bool = False,
) -> Document:
    return Document(
        doc_id=doc.doc_id,
        source_id=doc.source_id,
        native_id=doc.native_id,
        title=doc.title,
        lang=doc.lang,
        license=doc.license,
        text=doc.text,
        content_hash=doc.content_hash,
        revision=doc.revision,
        timestamp=doc.timestamp,
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
        has_history=has_history,
        currency=currency_of(doc.meta),
        banner=banner_for(doc.meta),
    )


def hit(
    h: SearchHit,
    *,
    spec: SourceSpec | None = None,
    doc: DocRecord | None = None,
    highlights: Sequence[tuple[int, int]] = (),
) -> Hit:
    source_id, _, native_id = h.doc_id.removeprefix("wsp:").partition("/")
    return Hit(
        doc_id=h.doc_id,
        score=h.score,
        title=h.title,
        snippet=h.snippet,
        source_id=h.source_id or source_id,
        address=address_of(source_id, native_id),
        source_name=(h.source_id or source_id) if spec is None else spec.name,
        kind=DocKind.OTHER if spec is None else sites.kind_of(spec),
        license=None if doc is None else doc.license,
        as_of=None if doc is None else doc.timestamp,
        lang=None if doc is None else doc.lang,
        url_native=None if doc is None else doc.url_native,
        highlights=list(highlights),
    )


def change(c: ChangeRecord, *, title: str | None = None) -> Change:
    source_id, _, native_id = c.doc_id.removeprefix("wsp:").partition("/")
    return Change(
        doc_id=c.doc_id,
        kind=c.kind.value,
        generation_id=c.generation_id,
        old_hash=c.old_hash,
        new_hash=c.new_hash,
        renamed_from=c.renamed_from,
        source_id=source_id,
        title=title,
        address=address_of(source_id, native_id),
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
