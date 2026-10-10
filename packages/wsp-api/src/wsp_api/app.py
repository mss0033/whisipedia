"""FastAPI app factory: ``create_app(services, settings)`` (PLAN §11.1, §12.2).

Routes depend only on :class:`~wsp_api.services.Services` (ports). Sync port calls run in FastAPI's
worker threads (plain ``def`` routes); streaming routes are ``async``.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Annotated, Any

from fastapi import APIRouter, FastAPI, Header, Path, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.telemetry import TelemetryConfig

from wsp_api import convert
from wsp_api.addresses import address_of, resolve
from wsp_api.errors import ApiError, errors, install_error_handlers, streamed
from wsp_api.schemas import (
    API_VERSION,
    Change,
    ChangePage,
    Citation,
    Document,
    DocumentVersion,
    ErrorResponse,
    GenerationInfo,
    GenerationPage,
    Health,
    HistoryPage,
    Resolution,
    SearchResults,
    SourceFreshness,
    SourceInfo,
    SourcePage,
    Suggestions,
    SummarizeRequest,
    SummaryChunk,
    SystemStatus,
)
from wsp_api.security import SecurityMiddleware
from wsp_api.services import ApiSettings, Services
from wsp_api.sse import change_events
from wsp_core.ids import DocId, InvalidDocIdError, parse_doc_id
from wsp_core.model import GenerationStatus, SourceSpec, SourceStatus

_DESCRIPTION = """\
Read API of a private offline library. Everything is served from local storage; the server makes no
outbound connections. Errors always use the envelope `{"error": {"code", "message"}}`.

List endpoints use opaque cursors: pass `next_cursor` back as `cursor`. The change feed uses its own
cursor (`since`). Document ids are `wsp:<source_id>/<native_id>` and may contain `/`.
"""

_PLACEHOLDER = """<!doctype html><html><head><title>Whisipedia</title></head><body>
<h1>Whisipedia</h1><p>The Navigator bundle is not built. API: <a href="/v1/health">/v1/health</a>.</p>
</body></html>"""

# FastAPI's native OpenTelemetry is on by default and can auto-configure exporters from the
# environment. The API must never make outbound connections, so it is switched off entirely.
_TELEMETRY_OFF: TelemetryConfig = {
    "tracing": False,
    "metrics": False,
    "logs": False,
    "operation_spans": False,
    "auto_configure": False,
}

Limit = Annotated[int, Query(ge=1, le=200, description="Page size.")]
Cursor = Annotated[str | None, Query(description="Opaque cursor from a previous page's `next_cursor`.")]


def _doc_id(raw: str) -> DocId:
    try:
        parse_doc_id(raw)
    except InvalidDocIdError as exc:
        raise ApiError(400, "invalid_doc_id", str(exc)) from exc
    return DocId(raw)


def _cadence(iso: str | None) -> timedelta | None:
    """Coarse ISO 8601 duration (PnY, PnM, PnW, PnD) for staleness checks."""
    if not iso or not iso.startswith("P") or len(iso) < 3 or not iso[1:-1].isdigit():
        return None
    days = {"Y": 365, "M": 31, "W": 7, "D": 1}.get(iso[-1])
    return None if days is None else timedelta(days=int(iso[1:-1]) * days)


class WspApp(FastAPI):
    """FastAPI with the spec post-processed to match what the server actually sends."""

    def openapi(self) -> dict[str, Any]:
        if self.openapi_schema:
            return self.openapi_schema
        spec = super().openapi()
        envelope = {"application/json": {"schema": {"$ref": "#/components/schemas/ErrorResponse"}}}
        for operations in spec["paths"].values():
            for op in operations.values():
                responses = op["responses"]
                if "422" in responses:  # validation errors use the envelope, not FastAPI's default shape
                    responses["422"] = {"description": "invalid_request", "content": envelope}
                content = responses.get("200", {}).get("content", {})
                for media in ("application/x-ndjson", "text/event-stream"):
                    if media in content and "application/json" in content:
                        content[media] = content.pop("application/json")
        schemas = spec.get("components", {}).get("schemas", {})
        for unused in ("HTTPValidationError", "ValidationError"):
            schemas.pop(unused, None)
        self.openapi_schema = spec
        return spec


def create_app(services: Services, settings: ApiSettings | None = None) -> FastAPI:
    settings = settings or ApiSettings()
    app = WspApp(
        title="Whisipedia API",
        version=API_VERSION,
        description=_DESCRIPTION,
        openapi_url="/v1/openapi.json",
        docs_url=None,  # Swagger/ReDoc load assets from CDNs: incompatible with the offline CSP
        redoc_url=None,
        telemetry=_TELEMETRY_OFF,
        responses={500: {"model": ErrorResponse, "description": "internal_error"}},
    )
    install_error_handlers(app)
    app.add_middleware(SecurityMiddleware, settings=settings)
    app.include_router(_v1(services, settings))
    app.include_router(_go(services))
    _mount_navigator(app, settings)
    return app


def _v1(s: Services, settings: ApiSettings) -> APIRouter:
    r = APIRouter(prefix="/v1")

    @r.get("/health", tags=["meta"])
    def health() -> Health:
        return Health(sources=len(s.sources))

    # --- Sources --------------------------------------------------------------------------------

    @r.get("/sources", tags=["sources"], responses=errors(400))
    def list_sources(cursor: Cursor = None, limit: Limit = 50) -> SourcePage:
        specs = sorted(s.sources, key=lambda x: x.id)
        chunk, nxt = convert.page(specs, cursor, limit)
        return SourcePage(
            items=[convert.source_info(x, s.vault.current(x.id)) for x in chunk], next_cursor=nxt
        )

    def _source(source_id: str) -> SourceSpec:
        spec = s.source(source_id)
        if spec is None:
            raise ApiError(404, "source_not_found", f"unknown source: {source_id}")
        return spec

    @r.get("/sources/{source_id}", tags=["sources"], responses=errors(404))
    def get_source(source_id: str) -> SourceInfo:
        return convert.source_info(_source(source_id), s.vault.current(source_id))

    @r.get("/sources/{source_id}/generations", tags=["sources"], responses=errors(400, 404))
    def list_generations(source_id: str, cursor: Cursor = None, limit: Limit = 50) -> GenerationPage:
        _source(source_id)
        cur = s.vault.current(source_id)
        gens = sorted(s.vault.generations(source_id), key=lambda g: g.generation_id, reverse=True)
        chunk, nxt = convert.page(gens, cursor, limit)
        items = [
            convert.generation_info(g, current=cur is not None and g.generation_id == cur.generation_id)
            for g in chunk
        ]
        return GenerationPage(items=items, next_cursor=nxt)

    # --- Documents ------------------------------------------------------------------------------
    # The history route is declared first: ``{doc_id:path}`` is greedy.

    @r.get("/documents/{doc_id:path}/history", tags=["documents"], responses=errors(400, 404))
    def document_history(
        doc_id: Annotated[str, Path(description="`wsp:<source_id>/<native_id>`")],
        cursor: Cursor = None,
        limit: Limit = 100,
    ) -> HistoryPage:
        did = _doc_id(doc_id)
        versions = s.documents.history(did)
        if not versions:
            raise ApiError(404, "document_not_found", f"no such document: {doc_id}")
        chunk, nxt = convert.page(versions, cursor, limit)
        return HistoryPage(
            doc_id=did,
            items=[DocumentVersion(generation_id=g, content_hash=h) for g, h in chunk],
            next_cursor=nxt,
        )

    @r.get("/documents/{doc_id:path}", tags=["documents"], responses=errors(400, 404))
    def get_document(
        doc_id: Annotated[str, Path(description="`wsp:<source_id>/<native_id>`")],
        at: Annotated[
            str | None, Query(description="Generation id: the document as of that snapshot.")
        ] = None,
    ) -> Document:
        doc = s.documents.get(_doc_id(doc_id), at_generation=at)
        if doc is None:
            where = f" at generation {at}" if at else ""
            raise ApiError(404, "document_not_found", f"no such document{where}: {doc_id}")
        return convert.document(doc)

    # --- Search ---------------------------------------------------------------------------------

    @r.get("/search", tags=["search"], responses=errors(422))
    def search(
        q: Annotated[str, Query(min_length=1, max_length=1000, description="Plain-language query.")],
        sources: Annotated[
            list[str] | None, Query(description="Restrict to these source ids (repeat or comma-separate).")
        ] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 10,
        offset: Annotated[int, Query(ge=0, le=10_000)] = 0,
    ) -> SearchResults:
        wanted = [x for raw in sources or [] for x in raw.split(",") if x] or None
        hits = s.index.search(q, limit=limit + 1, offset=offset, sources=wanted)
        return SearchResults(
            query=q,
            offset=offset,
            limit=limit,
            hits=[convert.hit(h) for h in hits[:limit]],
            next_offset=offset + limit if len(hits) > limit else None,
        )

    @r.get("/suggest", tags=["search"], responses=errors(422))
    def suggest(
        q: Annotated[str, Query(max_length=200, description="Title prefix.")],
        limit: Annotated[int, Query(ge=1, le=50)] = 10,
    ) -> Suggestions:
        return Suggestions(query=q, suggestions=list(s.index.suggest(q, limit=limit)))

    # --- Change feed ----------------------------------------------------------------------------

    def _feed_since(cursor: str | None, limit: int) -> ChangePage:
        try:
            records, nxt = s.changes.since(cursor, limit=limit)
        except ValueError as exc:  # TODO(port): a typed InvalidCursorError in wsp_core.ports
            raise ApiError(400, "invalid_cursor", str(exc)) from exc
        return ChangePage(items=[convert.change(c) for c in records], cursor=nxt)

    @r.get("/changes", tags=["changes"], responses=errors(400))
    def changes(
        since: Annotated[str | None, Query(description="Cursor from a previous page; omit to start.")] = None,
        limit: Annotated[int, Query(ge=1, le=5000)] = 1000,
    ) -> ChangePage:
        return _feed_since(since, limit)

    @r.get(
        "/changes/stream",
        tags=["changes"],
        response_class=StreamingResponse,
        responses=streamed(
            Change, "text/event-stream", "SSE stream; each `change` event's data is one `Change`"
        )
        | errors(400),
    )
    async def changes_stream(
        request: Request,
        since: Annotated[str | None, Query(description="Start after this cursor.")] = None,
        last_event_id: Annotated[str | None, Header(description="Set by EventSource on reconnect.")] = None,
        max_seconds: Annotated[
            float | None,
            Query(gt=0, le=3600, description="Close the stream after this long (then reconnect)."),
        ] = None,
    ) -> StreamingResponse:
        cursor = last_event_id or since
        await asyncio.to_thread(_feed_since, cursor, 1)  # validate the cursor before committing to 200
        events = change_events(
            s.changes,
            cursor,
            poll_seconds=settings.sse_poll_seconds,
            heartbeat_seconds=settings.sse_heartbeat_seconds,
            max_seconds=max_seconds,
            is_disconnected=request.is_disconnected,
        )
        return StreamingResponse(
            events,
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    # --- System Monitor -------------------------------------------------------------------------

    @r.get("/system", tags=["system"])
    def system(ledger_tail: Annotated[int, Query(ge=0, le=500)] = settings.ledger_tail) -> SystemStatus:
        now = s.clock()
        fresh: list[SourceFreshness] = []
        degraded: list[GenerationInfo] = []
        for spec in sorted(s.sources, key=lambda x: x.id):
            cur = s.vault.current(spec.id)
            age = None if cur is None or cur.sealed_at is None else (now - cur.sealed_at).total_seconds()
            limit = _cadence(spec.cadence)
            stale = (cur is None and spec.status is SourceStatus.ACTIVE) or (
                age is not None and limit is not None and age > limit.total_seconds()
            )
            fresh.append(
                SourceFreshness(
                    source_id=spec.id,
                    name=spec.name,
                    status=spec.status.value,
                    cadence=spec.cadence,
                    current_generation=None if cur is None else cur.generation_id,
                    sealed_at=None if cur is None else cur.sealed_at,
                    age_seconds=age,
                    stale=stale,
                )
            )
            degraded += [
                convert.generation_info(g, current=cur is not None and g.generation_id == cur.generation_id)
                for g in s.vault.generations(spec.id)
                if g.status is GenerationStatus.DEGRADED
            ]
        # TODO(port): Ledger.tail(n) would avoid replaying the whole log here.
        tail = list(deque(s.ledger.events(), maxlen=ledger_tail)) if ledger_tail else []
        return SystemStatus(
            generated_at=now,
            freshness=fresh,
            degraded=degraded,
            ledger_tail=[convert.ledger_entry(e) for e in tail],
            ledger_ok=s.ledger.verify(),
        )

    # --- Summaries ------------------------------------------------------------------------------

    @r.post(
        "/summarize",
        tags=["search"],
        response_class=StreamingResponse,
        responses=streamed(
            SummaryChunk,
            "application/x-ndjson",
            "NDJSON stream, one `SummaryChunk` per line: citations, text…, done",
        )
        | errors(404, 422, 501),
    )
    async def summarize(body: SummarizeRequest) -> StreamingResponse:
        generator = s.generator
        if generator is None:
            raise ApiError(501, "not_configured", "no local language model is configured")
        citations: list[Citation] = []
        passages: list[str] = []
        for n, raw in enumerate(body.doc_ids, start=1):
            doc = await asyncio.to_thread(s.documents.get, _doc_id(raw))
            if doc is None:
                raise ApiError(404, "document_not_found", f"no such document: {raw}")
            citations.append(
                Citation(
                    n=n, doc_id=doc.doc_id, title=doc.title, address=address_of(doc.source_id, doc.native_id)
                )
            )
            passages.append(f"[{n}] {doc.title}\n{doc.text[:2000]}")
        prompt = (
            "Summarize the passages below to answer the question. Cite every sentence with [n]. "
            "If the passages do not answer it, say so.\n\n"
            f"Question: {body.query}\n\n" + "\n\n".join(passages)
        )

        async def chunks() -> AsyncIterator[str]:
            yield (
                SummaryChunk(type="citations", citations=citations).model_dump_json(exclude_none=True) + "\n"
            )
            async for text in generator.stream(prompt, max_tokens=body.max_tokens):
                yield SummaryChunk(type="text", text=text).model_dump_json(exclude_none=True) + "\n"
            yield SummaryChunk(type="done").model_dump_json(exclude_none=True) + "\n"

        return StreamingResponse(
            chunks(), media_type="application/x-ndjson", headers={"Cache-Control": "no-store"}
        )

    return r


def _go(s: Services) -> APIRouter:
    r = APIRouter()

    @r.get(
        "/go/{address:path}",
        tags=["addresses"],
        response_model=Resolution,
        responses={307: {"description": "Redirect to `href` (with `?redirect=true`)"}} | errors(400, 404),
    )
    def go(
        request: Request,
        address: Annotated[str, Path(description="`wsp://…` address, with or without the scheme.")],
        redirect: Annotated[bool, Query(description="Answer 307 to the resource instead of JSON.")] = False,
    ) -> Resolution | RedirectResponse:
        query = {k: v for k, v in request.query_params.items() if k != "redirect"}
        res = resolve(s, address, query)
        if redirect and res.href is not None:
            return RedirectResponse(res.href, status_code=307)
        return res

    return r


def _mount_navigator(app: FastAPI, settings: ApiSettings) -> None:
    static = settings.static_dir
    if static is not None and static.is_dir():
        # Mounted last so /v1 and /go win. html=True serves index.html for directories.
        app.mount("/", StaticFiles(directory=static, html=True), name="navigator")
        return

    @app.get("/", include_in_schema=False)
    def placeholder() -> HTMLResponse:
        return HTMLResponse(_PLACEHOLDER)
