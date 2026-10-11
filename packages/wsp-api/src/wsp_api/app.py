"""FastAPI app factory: ``create_app(services, settings)`` (PLAN §11.1, §12.2).

Routes depend only on :class:`~wsp_api.services.Services` (ports, plus the seams in
:mod:`wsp_api.seams`). Sync port calls run in FastAPI's worker threads (plain ``def`` routes);
streaming routes are ``async``. Navigator-only resources (portal, directory, random, assets, gaps) are
in :mod:`wsp_api.browse`.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections import Counter
from collections.abc import AsyncIterator, Sequence
from dataclasses import replace
from typing import Annotated, Any, Literal, cast
from urllib.parse import unquote

from fastapi import APIRouter, FastAPI, Header, Path, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.telemetry import TelemetryConfig

from wsp_api import convert, search, sites
from wsp_api.addresses import address_of, resolve
from wsp_api.browse import browse_router
from wsp_api.errors import ApiError, errors, install_error_handlers, streamed
from wsp_api.htmlrewrite import rewrite_html
from wsp_api.renditions import borrowed, zim_document
from wsp_api.routing import RawSegmentRouting
from wsp_api.schemas import (
    API_VERSION,
    Change,
    ChangePage,
    DocKind,
    Document,
    DocumentVersion,
    ErrorResponse,
    GenerationInfo,
    GenerationPage,
    Health,
    HistoryPage,
    Hit,
    KindFacet,
    ProposalPage,
    RecentChanges,
    Resolution,
    SearchResults,
    SourceFreshness,
    SourceInfo,
    SourcePage,
    Suggestions,
    SummarizeRequest,
    SummaryCitation,
    SummaryDone,
    SummaryError,
    SummaryEvent,
    SummaryNotice,
    SystemStatus,
)
from wsp_api.security import SecurityMiddleware
from wsp_api.services import ApiSettings, Services
from wsp_api.sse import change_events, format_event
from wsp_api.summaries import CitationSplitter
from wsp_api.views import Library, parse_doc
from wsp_api.zim import ZimAssetStore
from wsp_core.ids import parse_doc_id
from wsp_core.model import GenerationStatus, SearchHit

_DESCRIPTION = """\
Read API of a private offline library. Everything is served from local storage; the server makes no
outbound connections. Errors always use the envelope `{"error": {"code", "message"}}`.

List endpoints use opaque cursors: pass `next_cursor` back as `cursor`. The change feed uses its own
cursor (`since`). Document ids are `wsp:<source_id>/<native_id>`; in URL paths a doc id is ONE
percent-encoded segment (`/v1/documents/wsp%3Arfc%2F9110`), as produced by `encodeURIComponent`.
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

# Search: facets and totals come from the top of the ranking; beyond it the index is paged directly.
_WINDOW = 200

Limit = Annotated[int, Query(ge=1, le=200, description="Page size.")]
Cursor = Annotated[str | None, Query(description="Opaque cursor from a previous page's `next_cursor`.")]
DocIdSegment = Annotated[
    str, Path(description="`wsp:<source_id>/<native_id>`, percent-encoded as one segment (`/` → `%2F`).")
]


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
    lib = Library(services)
    install_error_handlers(app)
    app.add_middleware(RawSegmentRouting)
    app.add_middleware(SecurityMiddleware, settings=settings)
    app.include_router(_v1(lib, settings))
    app.include_router(browse_router(lib))
    app.include_router(_go(services))
    _mount_navigator(app, settings)
    if isinstance(lib.assets, ZimAssetStore):
        # Open big single-book ZIMs in the background so the first page view does not wait on SMB.
        zims = [x.id for x in services.sources if x.adapter == "kiwix-opds"]
        threading.Thread(target=lib.assets.warm, args=(zims,), name="zim-warm", daemon=True).start()
    return app


def _v1(lib: Library, settings: ApiSettings) -> APIRouter:
    r = APIRouter(prefix="/v1")
    s = lib.s
    started = time.monotonic()

    @r.get("/health", tags=["meta"])
    def health() -> Health:
        return Health(sources=len(s.sources))

    # --- Sources --------------------------------------------------------------------------------

    @r.get("/sources", tags=["sources"], responses=errors(400))
    def list_sources(cursor: Cursor = None, limit: Limit = 50) -> SourcePage:
        specs = sorted(s.sources, key=lambda x: x.id)
        chunk, nxt = convert.page(specs, cursor, limit)
        return SourcePage(items=[lib.source_info(x) for x in chunk], next_cursor=nxt)

    @r.get("/sources/{source_id}", tags=["sources"], responses=errors(404))
    def get_source(source_id: str) -> SourceInfo:
        return lib.source_info(lib.source(source_id))

    @r.get("/sources/{source_id}/generations", tags=["sources"], responses=errors(400, 404))
    def list_generations(source_id: str, cursor: Cursor = None, limit: Limit = 50) -> GenerationPage:
        lib.source(source_id)
        cur = s.vault.current(source_id)
        gens = sorted(s.vault.generations(source_id), key=lambda g: g.generation_id, reverse=True)
        chunk, nxt = convert.page(gens, cursor, limit)
        items = [
            convert.generation_info(g, current=cur is not None and g.generation_id == cur.generation_id)
            for g in chunk
        ]
        return GenerationPage(items=items, next_cursor=nxt)

    # --- Documents ------------------------------------------------------------------------------
    # The doc id is one raw path segment (wsp_api.routing), so the two routes cannot collide.

    @r.get("/documents/{doc_id}/history", tags=["documents"], responses=errors(400, 404))
    def document_history(doc_id: DocIdSegment, cursor: Cursor = None, limit: Limit = 100) -> HistoryPage:
        did = parse_doc(unquote(doc_id))
        versions = s.documents.history(did)
        if not versions:
            raise ApiError(404, "document_not_found", f"no such document: {did}")
        chunk, nxt = convert.page(versions, cursor, limit)
        first = versions[0][0]
        items: list[DocumentVersion] = []
        for gen, h in chunk:
            doc = s.documents.get(did, at_generation=gen)
            items.append(
                DocumentVersion(
                    generation_id=gen,
                    content_hash=h,
                    revision=None if doc is None else doc.revision,
                    timestamp=None if doc is None else doc.timestamp,
                    kind="added" if gen == first else "modified",
                )
            )
        return HistoryPage(doc_id=did, items=items, next_cursor=nxt)

    @r.get("/documents/{doc_id}", tags=["documents"], responses=errors(400, 404))
    def get_document(
        doc_id: DocIdSegment,
        at: Annotated[
            str | None, Query(description="Generation id: the document as of that snapshot.")
        ] = None,
    ) -> Document:
        did = parse_doc(unquote(doc_id))
        doc = s.documents.get(did, at_generation=at)
        if doc is None and at is None:
            # Not refined (yet): Kiwix sources are browsable straight from their ZIM.
            source_id, native_id = parse_doc_id(did)
            doc = zim_document(s.source(source_id), native_id, s.vault, lib.assets)
        if doc is None:
            where = f" at generation {at}" if at else ""
            raise ApiError(404, "document_not_found", f"no such document{where}: {did}")
        introduced = lib.generation_of(did, doc.content_hash, at)
        gen = lib.serving_generation(doc.source_id, introduced, at) or (
            s.vault.current(doc.source_id) if doc.html_ref else None
        )
        html: str | None = None
        if doc.html_ref and gen is not None:
            original = lib.assets.html(doc.source_id, gen.generation_id, doc.html_ref)
            if original is not None:
                html = rewrite_html(
                    original,
                    source_id=doc.source_id,
                    generation_id=gen.generation_id,
                    native_id=doc.native_id,
                )
        if html is None and at is None and (alt := borrowed(s.source(doc.source_id), doc, s.vault)):
            # e.g. the Wikipedia dump (indexed text) rendered with the Kiwix ZIM's page for its title.
            original = lib.assets.html(alt.source_id, alt.generation.generation_id, alt.html_ref)
            if original is not None:
                html = rewrite_html(
                    original,
                    source_id=alt.source_id,
                    generation_id=alt.generation.generation_id,
                    native_id=alt.html_ref.removeprefix("zim:"),
                )
        return convert.document(
            doc,
            spec=s.source(doc.source_id),
            links=lib.links(doc),
            generation_id=gen.generation_id if gen is not None else introduced,
            html=html,
            vault=None if gen is None else lib.vault_location(gen, doc),
            has_history=len(s.documents.history(did)) > 1,
        )

    @r.get("/documents/{rest:path}", include_in_schema=False)
    def unencoded_document_path(rest: str) -> None:
        raise ApiError(
            404,
            "doc_id_not_encoded",
            "put the doc id in ONE path segment, percent-encoded (`/` as %2F): "
            "/v1/documents/wsp%3Arfc%2F9110",
        )

    # --- Search ---------------------------------------------------------------------------------

    def _kind_sources(kind: DocKind | None, wanted: list[str] | None) -> list[str] | None:
        if kind is None:
            return wanted
        ids = [spec.id for spec in s.sources if sites.kind_of(spec) is kind]
        return [x for x in ids if wanted is None or x in wanted]

    def _query(q: str, limit: int, offset: int, scope: list[str] | None) -> list[SearchHit]:
        if scope == []:  # a filter that matches no source: nothing, not everything
            return []
        return list(s.index.search(q, limit=limit, offset=offset, sources=scope))

    def _window(q: str, scope: list[str] | None) -> list[SearchHit]:
        """The wide window for totals and facets: snippet-free when the index supports it."""
        if scope == []:
            return []
        hits_only = getattr(s.index, "search_hits", None)
        if callable(hits_only):
            return list(cast(Sequence[SearchHit], hits_only(q, limit=_WINDOW, offset=0, sources=scope)))
        return _query(q, _WINDOW, 0, scope)

    @r.get("/search", tags=["search"], responses=errors(422))
    def run_search(
        q: Annotated[str, Query(min_length=1, max_length=1000, description="Plain-language query.")],
        sources: Annotated[
            list[str] | None, Query(description="Restrict to these source ids (repeat or comma-separate).")
        ] = None,
        kind: Annotated[DocKind | None, Query(description="Restrict to sources of this kind.")] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 10,
        offset: Annotated[int, Query(ge=0, le=10_000)] = 0,
    ) -> SearchResults:
        t0 = time.perf_counter()
        wanted = [x for raw in sources or [] for x in raw.split(",") if x] or None
        scope = _kind_sources(kind, wanted)
        window = _window(q, scope)
        raw = _query(q, limit + 1, offset, scope)  # only the visible page gets snippets
        page, more = raw[:limit], len(raw) > limit
        estimate = len(window) >= _WINDOW
        total = max(len(window), offset + len(page) + (1 if more else 0))
        facet_hits = window if kind is None else _window(q, wanted)
        kinds: dict[str, DocKind] = {spec.id: sites.kind_of(spec) for spec in s.sources}
        counts = Counter(
            kinds.get(h.source_id or h.doc_id[4:].partition("/")[0], DocKind.OTHER) for h in facet_hits
        )
        terms = search.terms(q)
        hits: list[Hit] = []
        for h in page:
            plain = search.plain_snippet(h.snippet)  # index snippets are HTML (<b>…</b>); the API is plain
            hits.append(
                convert.hit(
                    replace(h, snippet=plain),
                    spec=s.source(h.source_id or h.doc_id[4:].partition("/")[0]),
                    doc=s.documents.get(h.doc_id),
                    highlights=search.highlights(plain, terms),
                )
            )
        nav = search.navigate_to(s, q) if offset == 0 else None
        if s.queries is not None and offset == 0:
            s.queries.record(s.clock(), q, total, not window)
        return SearchResults(
            query=q,
            offset=offset,
            limit=limit,
            hits=hits,
            next_offset=offset + limit if more else None,
            kind=kind,
            total=total,
            total_is_estimate=estimate,
            took_ms=round((time.perf_counter() - t0) * 1000, 2),
            intent=search.intent(q, navigational=nav is not None),
            facets=[
                KindFacet(kind=k, count=n) for k, n in sorted(counts.items(), key=lambda x: (-x[1], x[0]))
            ],
            navigate_to=nav,
            low_confidence=not window,
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
        return ChangePage(items=[lib.change(c) for c in records], cursor=nxt)

    @r.get("/changes", tags=["changes"], responses=errors(400))
    def changes(
        since: Annotated[str | None, Query(description="Cursor from a previous page; omit to start.")] = None,
        limit: Annotated[int, Query(ge=1, le=5000)] = 1000,
    ) -> ChangePage:
        return _feed_since(since, limit)

    @r.get("/changes/recent", tags=["changes"], responses=errors(404))
    def recent_changes(
        limit: Annotated[int, Query(ge=1, le=500)] = 50,
        source: Annotated[str | None, Query(description="Only changes of this source.")] = None,
    ) -> RecentChanges:
        if source is not None:
            lib.source(source)
        records, cursor = lib.catalog.recent(limit=limit, sources=[source] if source else None)
        return RecentChanges(items=[lib.change(c) for c in records], cursor=cursor)

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
            describe=lib.change,
        )
        return StreamingResponse(
            events,
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    # --- System Monitor -------------------------------------------------------------------------

    verified: list[tuple[float, bool]] = []  # (monotonic time, result) of the last chain replay

    def ledger_ok() -> bool:
        now = time.monotonic()
        if not verified or now - verified[0][0] > settings.ledger_verify_seconds:
            verified[:] = [(now, s.ledger.verify())]
        return verified[0][1]

    @r.get("/proposals", tags=["system"])
    def proposals(
        status: Annotated[
            Literal["open", "applied", "accepted", "rejected"] | None, Query(description="Filter by status.")
        ] = None,
    ) -> ProposalPage:
        """Curator proposals (newest first). Deciding them is an owner action: `wsp proposals`."""
        items = [] if s.ops is None else list(s.ops.proposals(status))
        return ProposalPage(items=sorted(items, key=lambda p: p.id, reverse=True))

    @r.get("/system", tags=["system"])
    def system(ledger_tail: Annotated[int, Query(ge=0, le=500)] = settings.ledger_tail) -> SystemStatus:
        now = s.clock()
        fresh: list[SourceFreshness] = []
        degraded: list[GenerationInfo] = []
        for spec in sorted(s.sources, key=lambda x: x.id):
            cur = s.vault.current(spec.id)
            f = sites.freshness(spec, cur, now)
            fresh.append(
                SourceFreshness(
                    source_id=spec.id,
                    name=spec.name,
                    status=spec.status.value,
                    cadence=spec.cadence,
                    current_generation=None if cur is None else cur.generation_id,
                    sealed_at=None if cur is None else cur.sealed_at,
                    age_seconds=f.age_seconds,
                    stale=f.stale,
                    health=f.health,
                )
            )
            degraded += [
                convert.generation_info(g, current=cur is not None and g.generation_id == cur.generation_id)
                for g in s.vault.generations(spec.id)
                if g.status is GenerationStatus.DEGRADED
            ]
        tail = list(s.ledger.tail(ledger_tail)) if ledger_tail else []
        return SystemStatus(
            generated_at=now,
            freshness=fresh,
            degraded=degraded,
            ledger_tail=[convert.ledger_entry(e) for e in tail],
            ledger_ok=ledger_ok(),
            uptime_seconds=round(time.monotonic() - started, 3),
            ops=None if s.ops is None else s.ops.status(now),
        )

    # --- Summaries ------------------------------------------------------------------------------

    @r.post(
        "/summarize",
        tags=["search"],
        response_class=StreamingResponse,
        responses=streamed(
            SummaryEvent,
            "text/event-stream",
            "SSE stream: `event: <type>` and `data: <SummaryEvent JSON>` per event. "
            "Without a local language model the endpoint answers 501.",
        )
        | errors(400, 404, 422, 501),
    )
    async def summarize(body: SummarizeRequest) -> StreamingResponse:
        generator = s.generator
        if generator is None:
            raise ApiError(501, "not_configured", "no local language model is configured")
        citations: dict[int, SummaryCitation] = {}
        passages: list[str] = []
        missing: list[str] = []
        for n, raw in enumerate(body.doc_ids, start=1):
            did = parse_doc(raw)
            doc = await asyncio.to_thread(s.documents.get, did)
            if doc is None:
                missing.append(raw)
                continue
            citations[n] = SummaryCitation(
                index=n, doc_id=doc.doc_id, title=doc.title, address=address_of(doc.source_id, doc.native_id)
            )
            passages.append(f"[{n}] {doc.title}\n{doc.text[:2000]}")
        if not passages:
            raise ApiError(404, "document_not_found", "none of the documents are in the library")
        prompt = (
            "Summarize the passages below to answer the question. Cite every sentence with [n]. "
            "If the passages do not answer it, say so.\n\n"
            f"Question: {body.query}\n\n" + "\n\n".join(passages)
        )

        def sse(ev: SummaryEvent) -> str:
            return format_event(ev.model_dump_json(), event=ev.root.type)

        async def events() -> AsyncIterator[str]:
            if missing:
                text = f"{len(missing)} result(s) are no longer in the library and were left out."
                yield sse(SummaryEvent(SummaryNotice(text=text)))
            splitter = CitationSplitter(citations)
            try:
                async for chunk in generator.stream(prompt, max_tokens=body.max_tokens):
                    for ev in splitter.feed(chunk):
                        yield sse(SummaryEvent(ev))
            except Exception as exc:  # the model died mid-stream: tell the client, never leak internals
                yield sse(SummaryEvent(SummaryError(message=f"summarizer failed ({type(exc).__name__})")))
                return
            for ev in splitter.flush():
                yield sse(SummaryEvent(ev))
            yield sse(SummaryEvent(SummaryDone()))

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
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
