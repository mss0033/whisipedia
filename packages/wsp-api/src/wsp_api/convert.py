"""Domain objects (``wsp_core.model``) to wire models (``wsp_api.schemas``), plus cursor pagination."""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Mapping, Sequence
from typing import cast

from pydantic import JsonValue

from wsp_api.addresses import address_of
from wsp_api.errors import ApiError
from wsp_api.schemas import Change, Document, GenerationInfo, Hit, LedgerEntry, SourceInfo
from wsp_core.model import ChangeRecord, DocRecord, Generation, LedgerEvent, SearchHit, SourceSpec


def source_info(spec: SourceSpec, current: Generation | None) -> SourceInfo:
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


def document(doc: DocRecord) -> Document:
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
        links=list(doc.links),
        address=address_of(doc.source_id, doc.native_id),
    )


def hit(h: SearchHit) -> Hit:
    source_id, _, native_id = h.doc_id.removeprefix("wsp:").partition("/")
    return Hit(
        doc_id=h.doc_id,
        score=h.score,
        title=h.title,
        snippet=h.snippet,
        source_id=h.source_id or source_id,
        address=address_of(source_id, native_id),
    )


def change(c: ChangeRecord) -> Change:
    return Change(
        doc_id=c.doc_id,
        kind=c.kind.value,
        generation_id=c.generation_id,
        old_hash=c.old_hash,
        new_hash=c.new_hash,
        renamed_from=c.renamed_from,
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
