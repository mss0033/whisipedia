"""FastAPI app factory: ``create_app(services, settings)`` (PLAN §11.1, §12.2).

Routes depend only on :class:`~wsp_api.services.Services` (ports, plus the seams in
:mod:`wsp_api.seams`). Navigator-only resources (portal, directory, random, assets, gaps) are in
:mod:`wsp_api.browse`. Where each route runs (audit 2026-10-11 #5):

* ``async`` and memory-only: health, sources, generations, system and proposals read the library
  snapshot (:mod:`wsp_api.snapshot`), which the server refreshes in the background;
* the bounded ``nas`` executor (:mod:`wsp_api.offload`): routes that may read a ZIM (documents, assets,
  ``/go``); the ``search`` executor: search; over capacity or past their deadline they answer 503;
* plain ``def`` (FastAPI's thread pool): routes on local disk only (suggest, changes, history, portal).

With ``ApiSettings.background`` (``wsp serve``) the snapshot refresher and the warm-up start with the
application (lifespan) and stop with it; an app built for tests or the in-process CLI starts no thread.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import re
import threading
import time
from collections.abc import AsyncGenerator
from dataclasses import replace
from typing import Annotated, Any, Literal
from urllib.parse import unquote

from fastapi import APIRouter, FastAPI, Header, Path, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response, StreamingResponse
from fastapi.telemetry import TelemetryConfig
from starlette.middleware.gzip import DEFAULT_EXCLUDED_CONTENT_TYPES, GZipMiddleware

from wsp_api import convert, memory, sites
from wsp_api.addresses import resolve
from wsp_api.browse import browse_router
from wsp_api.errors import ApiError, error_response, errors, install_error_handlers, streamed
from wsp_api.files import files_router
from wsp_api.maps import maps_router
from wsp_api.offload import RETRY_AFTER
from wsp_api.pages import rendered_text
from wsp_api.routing import RawSegmentRouting
from wsp_api.schemas import (
    API_VERSION,
    Change,
    ChangePage,
    DocKind,
    Document,
    DocumentVersion,
    ErrorResponse,
    GenerationPage,
    Health,
    HistoryPage,
    ProposalPage,
    RecentChanges,
    Resolution,
    SearchResults,
    SourceFreshness,
    SourceInfo,
    SourcePage,
    Suggestions,
    SystemStatus,
    TranslatedSegment,
    TranslateRequest,
    TranslateResponse,
    TranslationStatus,
)
from wsp_api.searching import WINDOW, Searcher
from wsp_api.security import SecurityMiddleware
from wsp_api.services import ApiSettings, Services
from wsp_api.serving import NavigatorFiles, SlowRequestLog
from wsp_api.snapshot import MAX_LEDGER_TAIL, proposals_with
from wsp_api.sse import change_events
from wsp_api.views import Library, parse_doc
from wsp_api.weather import weather_router
from wsp_api.zimread import ZimUnavailable
from wsp_core.activity import ActivityMiddleware
from wsp_core.errors import TranslationUnavailableError
from wsp_core.ids import DocId
from wsp_core.listing import is_replay_stub

RECENT_SCAN_MAX = 50_000  # changes read back at most to fill one What's New page

_DESCRIPTION = """\
Read API of a private offline library. Everything is served from local storage; the server makes no
outbound connections. Errors always use the envelope `{"error": {"code", "message"}}`; `503` (with
`Retry-After`) means the library storage is busy or still loading.

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

GZIP_MIN_BYTES = 1024
DOCUMENT_CACHE = "private, no-cache"  # revalidate with the ETag: `has_history` and link targets change
_RFC_SHORT = re.compile(
    r"wsp:rfc/0*(\d{1,5})"
)  # `wsp:rfc/9110`, the address readers type, for `rfc:RFC-9110`

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
    memory.start_tracing_from_env()  # WSP_DIAG_TRACEMALLOC: where Python memory goes (/v1/system/memory)
    lib = Library(services, settings)

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncGenerator[None]:
        if settings.background:
            lib.state.start()
            threading.Thread(target=_warm_search, args=(services,), name="search-warm", daemon=True).start()
        try:
            yield
        finally:
            lib.close()

    app = WspApp(
        title="Whisipedia API",
        version=API_VERSION,
        description=_DESCRIPTION,
        openapi_url="/v1/openapi.json",
        docs_url=None,  # Swagger/ReDoc load assets from CDNs: incompatible with the offline CSP
        redoc_url=None,
        telemetry=_TELEMETRY_OFF,
        responses={500: {"model": ErrorResponse, "description": "internal_error"}},
        lifespan=lifespan,
    )
    app.state.library = lib
    install_error_handlers(app)

    async def zim_unavailable(_: Request, exc: Exception) -> Response:
        return error_response(503, "zim_unavailable", str(exc), retry_after=RETRY_AFTER)

    app.add_exception_handler(ZimUnavailable, zim_unavailable)
    # 0.84-2.2 MB pages -> ~5x smaller. Level 6, not Starlette's 9: half the CPU (13.7 -> 7.4 ms on a
    # 448 KB page), 1% more bytes, and the browser is on loopback (2026-10-14 research).
    # Already-compressed and large binary originals are sent as they are: a 400 MB GeoTIFF or PDF
    # went through zlib in the API process and its download showed no size (2026-10-26 review #117).
    app.add_middleware(
        GZipMiddleware,
        minimum_size=GZIP_MIN_BYTES,
        compresslevel=6,
        exclude_content_types=(
            *DEFAULT_EXCLUDED_CONTENT_TYPES,
            "application/pdf",
            "image/tiff",
            "application/epub+zip",
            "application/octet-stream",
            "application/x-bzip2",
            "application/x-xz",
            "application/zstd",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        ),
    )
    app.add_middleware(RawSegmentRouting)
    app.add_middleware(SecurityMiddleware, settings=settings)
    app.add_middleware(ActivityMiddleware, path=settings.activity_marker)  # Warden yields (ADR-0009)
    app.add_middleware(SlowRequestLog, slow_seconds=settings.slow_request_seconds)
    app.include_router(_v1(lib, settings))
    app.include_router(files_router(lib))
    app.include_router(maps_router(lib))
    app.include_router(weather_router(lib))
    app.include_router(browse_router(lib))
    app.include_router(_go(lib))
    _mount_navigator(app, settings)
    return app


def _warm_search(services: Services) -> None:
    """The first query otherwise pays for loading index segments and models (~1 s). Server only."""
    with contextlib.suppress(Exception):
        services.index.search("the library", limit=10)
        services.index.suggest("a", limit=10)


def _etag(body: bytes) -> str:
    return 'W/"' + hashlib.blake2b(body, digest_size=16).hexdigest() + '"'


def _not_modified(request: Request, etag: str) -> bool:
    wanted = request.headers.get("if-none-match")
    if not wanted:
        return False
    tags = {t.strip() for t in wanted.split(",")}
    return "*" in tags or etag in tags or etag.removeprefix("W/") in tags


MAX_SEGMENT = 4000  # characters of one translated segment (a long paragraph; split longer ones)


def _v1(lib: Library, settings: ApiSettings) -> APIRouter:
    r = APIRouter(prefix="/v1")
    s = lib.s
    state = lib.state
    searcher = Searcher(lib)
    started = time.monotonic()

    @r.head("/health", include_in_schema=False)  # monitors probe with HEAD
    @r.get("/health", tags=["meta"])
    async def health() -> Health:
        return Health(sources=len(s.sources))

    # --- Sources --------------------------------------------------------------------------------

    @r.get("/sources", tags=["sources"], responses=errors(400, 503))
    async def list_sources(cursor: Cursor = None, limit: Limit = 50) -> SourcePage:
        def build() -> SourcePage:
            snap = state.snapshot()
            specs = sorted(s.sources, key=lambda x: x.id)
            chunk, nxt = convert.page(specs, cursor, limit)
            return SourcePage(items=[lib.source_info(x, snap) for x in chunk], next_cursor=nxt)

        return await state.run(build)

    @r.get("/sources/{source_id}", tags=["sources"], responses=errors(404, 503))
    async def get_source(source_id: str) -> SourceInfo:
        spec = lib.source(source_id)
        return await state.run(lambda: lib.source_info(spec))

    @r.get("/sources/{source_id}/generations", tags=["sources"], responses=errors(400, 404, 503))
    async def list_generations(source_id: str, cursor: Cursor = None, limit: Limit = 50) -> GenerationPage:
        lib.source(source_id)

        def build() -> GenerationPage:
            st = state.snapshot().source(source_id)
            # A partitioned source serves a set of generations at once (ADR-0010): each one is current.
            current = {g.generation_id for g in (st.members or ([st.current] if st.current else []))}
            gens = sorted(st.generations, key=lambda g: g.generation_id, reverse=True)
            chunk, nxt = convert.page(gens, cursor, limit)
            items = [
                convert.generation_info(
                    g, current=g.generation_id in current, size=st.sizes.get(g.generation_id)
                )
                for g in chunk
            ]
            return GenerationPage(items=items, next_cursor=nxt)

        return await state.run(build)

    # --- Documents ------------------------------------------------------------------------------
    # The doc id is one raw path segment (wsp_api.routing), so the two routes cannot collide.

    @r.get("/documents/{doc_id}/history", tags=["documents"], responses=errors(400, 404, 503))
    async def document_history(
        doc_id: DocIdSegment, cursor: Cursor = None, limit: Limit = 100
    ) -> HistoryPage:
        # Every older generation is a Refinery read, on the NAS since ADR-0011: the NAS pool's deadline
        # and shedding apply, not the default thread pool's.
        return await lib.nas.run(lambda: _history(doc_id, cursor, limit))

    def _history(doc_id: str, cursor: str | None, limit: int) -> HistoryPage:
        did = parse_doc(unquote(doc_id))
        versions = lib.history(did)
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

    @r.get(
        "/documents/{doc_id}",
        tags=["documents"],
        response_model=Document,
        responses=errors(400, 404, 503) | {304: {"description": "Not modified (`If-None-Match`)"}},
    )
    async def get_document(
        request: Request,
        doc_id: DocIdSegment,
        at: Annotated[
            str | None, Query(description="Generation id: the document as of that snapshot.")
        ] = None,
        fields: Annotated[
            list[Literal["text"]] | None,
            Query(description="Extra fields: `text` also sends the plain text of an HTML page."),
        ] = None,
    ) -> Response:
        """A document. An HTML page's plain `text` is sent only with `fields=text` (it doubles the size).
        Responses carry an `ETag`; send it back as `If-None-Match` to get `304`."""
        did = parse_doc(unquote(doc_id))
        include_text = "text" in (fields or [])

        def build() -> bytes:
            try:
                view = lib.document(did, at)
            except ApiError as exc:
                # A typed or relative address loses a native id's trailing slash (`xkcd.com/` for
                # the front page, `banana-bread/`): answer with that document rather than a 404 the
                # client must retry (field exam 2026-10-14). The response carries the real doc_id.
                # An RFC is keyed `rfc:RFC-9110`, but readers, the Navigator's links in an RFC's
                # text and the location bar say `wsp:rfc/9110` (every RFC-to-RFC link was dead,
                # 2026-10-17 trawl).
                if exc.status != 404 or exc.code != "document_not_found":
                    raise
                short = _RFC_SHORT.fullmatch(did)
                other = f"wsp:rfc/rfc:RFC-{int(short.group(1))}" if short else did + "/"
                if other.endswith("//"):
                    raise
                try:
                    view = lib.document(DocId(other), at)
                except ApiError:
                    raise exc from None
            doc = view.doc
            if include_text and view.rendition is not None and view.html:
                # The page shown is another source's rendering (the Wikipedia ZIM for the dump's
                # article): its text has the figures and formulas the dump text lacks.
                doc = replace(doc, text=rendered_text(view.html))
            model = convert.document(
                doc,
                spec=s.source(view.doc.source_id),
                links=lib.links(view.doc),
                generation_id=view.generation_id,
                html=view.html,
                vault=view.vault,
                rendition=view.rendition,
                has_history=view.has_history,
                include_text=include_text,
                copies=lib.copies(view.doc, s.source(view.doc.source_id)),
                section=view.section,
                parts=lib.parts(view.doc),
            )
            return model.model_dump_json().encode("utf-8")

        body = await lib.nas.run(build)
        etag = _etag(body)
        headers = {"ETag": etag, "Cache-Control": DOCUMENT_CACHE}
        if _not_modified(request, etag):
            return Response(status_code=304, headers=headers)
        return Response(body, media_type="application/json", headers=headers)

    @r.get("/documents/{rest:path}", include_in_schema=False)
    def unencoded_document_path(rest: str) -> None:
        raise ApiError(
            404,
            "doc_id_not_encoded",
            "put the doc id in ONE path segment, percent-encoded (`/` as %2F): "
            "/v1/documents/wsp%3Arfc%2F9110",
        )

    # --- Search ---------------------------------------------------------------------------------

    @r.get("/search", tags=["search"], responses=errors(422, 503))
    async def run_search(
        q: Annotated[str, Query(min_length=1, max_length=1000, description="Plain-language query.")],
        sources: Annotated[
            list[str] | None, Query(description="Restrict to these source ids (repeat or comma-separate).")
        ] = None,
        kind: Annotated[DocKind | None, Query(description="Restrict to sources of this kind.")] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 10,
        offset: Annotated[
            int, Query(ge=0, le=WINDOW, description=f"Results are ranked {WINDOW} deep; pages end there.")
        ] = 0,
    ) -> SearchResults:
        wanted = [x for raw in sources or [] for x in raw.split(",") if x] or None
        return await lib.searches.run(lambda: searcher.run(q, wanted, kind, limit, offset))

    @r.get("/suggest", tags=["search"], responses=errors(422))
    def suggest(
        q: Annotated[
            str,
            Query(max_length=200, description="Text typed so far: titles with a word that starts with it."),
        ],
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
        # Placeholders of uncaptured players are not news (field exam 2026-10-14) and are dropped
        # before the page is cut. One web-archive release adds thousands of them at once, so the tail
        # is read further back until the page is full (a cheap index range; capped).
        wanted = [source] if source else None
        ask = limit * 4
        while True:
            records, cursor = lib.catalog.recent(limit=ask, sources=wanted)
            shown = [c for c in records if not is_replay_stub(c.doc_id.partition("/")[2])][:limit]
            if len(shown) >= limit or len(records) < ask or ask >= RECENT_SCAN_MAX:
                break
            ask = min(ask * 8, RECENT_SCAN_MAX)
        return RecentChanges(items=[lib.change(c) for c in shown], cursor=cursor)

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
        summarize: Annotated[
            bool,
            Query(
                description="Skip a backlog: when a whole batch is waiting, send one `skipped` event "
                "(its `id` is the feed head) and continue from the head. For readers of recent changes."
            ),
        ] = False,
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
            summarize=summarize,
        )
        return StreamingResponse(
            events,
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    # --- System Monitor -------------------------------------------------------------------------

    @r.get("/proposals", tags=["system"], responses=errors(503))
    async def proposals(
        status: Annotated[
            Literal["open", "applied", "accepted", "rejected"] | None, Query(description="Filter by status.")
        ] = None,
    ) -> ProposalPage:
        """Curator proposals (newest first). Deciding them is an owner action: `wsp proposals`."""
        return await state.run(
            lambda: ProposalPage(items=proposals_with(state.proposals.get() or (), status))
        )

    # --- Machine translation (ADR-0013) ---------------------------------------------------------

    @r.get("/translate", tags=["translation"])
    def translation_status() -> TranslationStatus:
        """Whether a reader may ask for an English machine translation, of which languages, and with
        which model. ``state`` is the on-demand server's (started by the first request)."""
        if s.translator is None:
            return TranslationStatus(available=False, reason="translation is not set up on this library")
        st = s.translator.status()
        return TranslationStatus.model_validate(
            {k: v for k, v in st.items() if k in TranslationStatus.model_fields}
        )

    @r.post("/translate", tags=["translation"], responses=errors(400, 501, 503))
    def translate(body: TranslateRequest) -> TranslateResponse:
        """Translate up to 32 segments of one document into English. Each is checked (numbers,
        placeholders, length, sentences): one that fails comes back with ``text: null`` and the
        reason, for the reader to see in its own language. 503 with ``Retry-After`` while the server
        starts (about half a minute) or while the GPU is busy."""
        if s.translator is None:
            raise ApiError(501, "not_implemented", "translation is not set up on this library")
        if any(len(seg) > MAX_SEGMENT for seg in body.segments):
            raise ApiError(400, "bad_request", f"a segment is longer than {MAX_SEGMENT} characters")
        try:
            done = s.translator.translate(body.lang, body.segments)
        except TranslationUnavailableError as exc:
            retry = None if exc.retry_after is None else str(int(exc.retry_after))
            raise ApiError(503, "translation_unavailable", str(exc), retry_after=retry) from exc
        except ValueError as exc:
            raise ApiError(400, "bad_request", str(exc)) from exc
        st = s.translator.status()
        return TranslateResponse(
            model=str(st.get("model") or ""),
            prompt_version=str(st.get("prompt_version") or ""),
            segments=[
                TranslatedSegment(text=d.text, ok=d.ok, reason=d.reason, cached=d.cached) for d in done
            ],
        )

    @r.get("/system/memory", tags=["system"])
    async def system_memory() -> dict[str, Any]:
        """Where the API's memory is (wsp_api.memory): process, glibc heap, Arrow, open readers,
        Python objects (allocation sites when started with WSP_DIAG_TRACEMALLOC)."""
        return await state.run(lambda: memory.report(s))

    @r.post("/system/memory/trim", tags=["system"])
    async def system_memory_trim() -> dict[str, Any]:
        """Return free heap memory to the system; the size before and after (a large drop: fragmentation)."""
        return await state.run(memory.trim)

    @r.get("/system", tags=["system"], responses=errors(503))
    async def system(
        ledger_tail: Annotated[int, Query(ge=0, le=MAX_LEDGER_TAIL)] = settings.ledger_tail,
    ) -> SystemStatus:
        """Freshness, degraded generations, ledger and operations, from the library snapshot. The
        server refreshes it in the background, so the parts can be up to a refresh period old."""

        def build() -> SystemStatus:
            now = s.clock()
            snap = state.snapshot()
            fresh: list[SourceFreshness] = []
            for spec in sorted(s.sources, key=lambda x: x.id):
                cur = snap.source(spec.id).current
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
            events = state.ledger_tail.get() or ()
            tail = list(events[-ledger_tail:]) if ledger_tail else []
            return SystemStatus(
                generated_at=now,
                freshness=fresh,
                degraded=[
                    convert.generation_info(
                        g, current=c, size=snap.source(g.source_id).sizes.get(g.generation_id)
                    )
                    for g, c in snap.degraded()
                ],
                ledger_tail=[convert.ledger_entry(e) for e in tail],
                ledger_ok=state.ledger_ok.get(),
                uptime_seconds=round(time.monotonic() - started, 3),
                ops=state.ops.get(),
            )

        return await state.run(build)

    return r


def _go(lib: Library) -> APIRouter:
    r = APIRouter()

    @r.get(
        "/go/{address:path}",
        tags=["addresses"],
        response_model=Resolution,
        responses={307: {"description": "Redirect to `href` (with `?redirect=true`)"}}
        | errors(400, 404, 503),
    )
    async def go(
        request: Request,
        address: Annotated[str, Path(description="`wsp://…` address, with or without the scheme.")],
        redirect: Annotated[bool, Query(description="Answer 307 to the resource instead of JSON.")] = False,
    ) -> Resolution | RedirectResponse:
        query = {k: v for k, v in request.query_params.items() if k != "redirect"}
        res = await lib.nas.run(
            lambda: resolve(lib.s, address, query, exists=lib.exists, stand_in=lib.stand_in)
        )
        if redirect and res.href is not None:
            return RedirectResponse(res.href, status_code=307)
        return res

    return r


def _mount_navigator(app: FastAPI, settings: ApiSettings) -> None:
    static = settings.static_dir
    if static is not None and static.is_dir():
        # Mounted last so /v1 and /go win. html=True serves index.html for directories.
        app.mount("/", NavigatorFiles(directory=static, html=True), name="navigator")
        return

    @app.get("/", include_in_schema=False)
    def placeholder() -> HTMLResponse:
        return HTMLResponse(_PLACEHOLDER)


def library_of(app: FastAPI) -> Library:
    """The :class:`Library` behind an app (tests and tools)."""
    found: object = app.state.library
    assert isinstance(found, Library)
    return found
