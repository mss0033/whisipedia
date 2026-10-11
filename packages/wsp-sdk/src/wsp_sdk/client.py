"""Sync and async clients. Both expose the same methods; the async ones are awaitable/async-iterable.

    with WspClient() as wsp:                                   # http://127.0.0.1:8737 by default
        for hit in wsp.iter_search("rfc about http caching"):
            print(hit.title, hit.address)

    async with AsyncWspClient() as wsp:
        async for item in wsp.stream_changes(since=cursor):
            ...

Pass ``http=`` to inject a preconfigured ``httpx`` client (custom transport, timeouts, in-process app).

``stream_changes`` follows the SSE change feed: it reads with a timeout (the server sends a keepalive
at least every 15 s, so a silent connection is a dead one) and, when the connection drops, reconnects
with ``Last-Event-ID`` after the server's retry delay. Delivery is at-least-once: a batch cut off in
the middle is sent again.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Iterator, Sequence
from types import TracebackType
from typing import Self

import httpx
from pydantic import BaseModel

from wsp_sdk import _wire as w
from wsp_sdk.errors import raise_for_error
from wsp_sdk.models import (
    Change,
    ChangePage,
    DirectoryPage,
    DocKind,
    DocRef,
    Document,
    DocumentVersion,
    GenerationInfo,
    GenerationPage,
    Health,
    HistoryPage,
    Hit,
    Portal,
    ProposalPage,
    RecentChanges,
    Resolution,
    SearchResults,
    SourceInfo,
    SourcePage,
    StreamedChange,
    Suggestions,
    SystemStatus,
)

DEFAULT_BASE_URL = w.DEFAULT_BASE_URL


def _parse[M: BaseModel](model: type[M], response: httpx.Response) -> M:
    raise_for_error(response)
    return model.model_validate_json(response.content)


STREAM_READ_TIMEOUT = 60.0  # seconds without a byte before a stream counts as dead (keepalive: 15 s)
MAX_RETRY_SECONDS = 30.0


def _timeout(read: float) -> httpx.Timeout:
    return httpx.Timeout(10.0, read=read)


class _Follow:
    """Reconnect bookkeeping for ``stream_changes`` (shared by the sync and async clients)."""

    def __init__(
        self, since: str | None, max_seconds: float | None, reconnect: bool, max_retries: int | None
    ) -> None:
        self.since = since
        self.last_event_id: str | None = None
        self.retry = w.DEFAULT_RETRY_MS / 1000
        self.failures = 0
        self.reconnect = reconnect
        self.max_retries = max_retries
        self.deadline = None if max_seconds is None else time.monotonic() + max_seconds

    def remaining(self) -> float | None:
        return None if self.deadline is None else max(0.001, self.deadline - time.monotonic())

    def headers(self) -> dict[str, str]:
        """``Last-Event-ID`` after the first connection: the server resumes after it (and it wins over
        ``since``)."""
        return {} if self.last_event_id is None else {"Last-Event-ID": self.last_event_id}

    def saw(self, parser: w.SSEParser) -> None:
        if parser.last_event_id is not None:
            self.last_event_id = parser.last_event_id
        if parser.retry_ms is not None:
            self.retry = parser.retry_ms / 1000
        self.failures = 0

    def failed(self, exc: Exception) -> float:
        """How long to wait before reconnecting after ``exc``; re-raises it when that is not wanted."""
        self.failures += 1
        if not self.reconnect or (self.max_retries is not None and self.failures > self.max_retries):
            raise exc
        if self.deadline is not None and time.monotonic() >= self.deadline:
            raise exc
        return min(self.retry * self.failures, MAX_RETRY_SECONDS)

    def done(self) -> bool:
        """After the server closed the stream cleanly: finished, or reconnect after :meth:`delay`."""
        return not self.reconnect or (self.deadline is not None and time.monotonic() >= self.deadline - 0.05)

    def delay(self) -> float:
        return self.retry


def _change_from(ev: w.SSEEvent) -> StreamedChange | None:
    if ev.event != "change":
        return None
    return StreamedChange(change=Change.model_validate_json(ev.data), cursor=ev.id)


class WspClient:
    """Synchronous client for API v1."""

    def __init__(
        self, base_url: str = DEFAULT_BASE_URL, *, timeout: float = 30.0, http: httpx.Client | None = None
    ) -> None:
        self._owned = http is None
        self._http = http or httpx.Client(base_url=base_url, timeout=timeout)

    def close(self) -> None:
        if self._owned:
            self._http.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        self.close()

    def _send(self, req: w.Req) -> httpx.Response:
        return self._http.request(req.method, req.path, params=req.params, json=req.json)

    # --- meta & sources ---

    def health(self) -> Health:
        return _parse(Health, self._send(w.health()))

    def sources(self, *, cursor: str | None = None, limit: int = 50) -> SourcePage:
        return _parse(SourcePage, self._send(w.sources(cursor, limit)))

    def iter_sources(self, *, page_size: int = 50) -> Iterator[SourceInfo]:
        cursor: str | None = None
        while True:
            page = self.sources(cursor=cursor, limit=page_size)
            yield from page.items
            if (cursor := page.next_cursor) is None:
                return

    def source(self, source_id: str) -> SourceInfo:
        return _parse(SourceInfo, self._send(w.source(source_id)))

    def generations(self, source_id: str, *, cursor: str | None = None, limit: int = 50) -> GenerationPage:
        return _parse(GenerationPage, self._send(w.generations(source_id, cursor, limit)))

    def iter_generations(self, source_id: str, *, page_size: int = 50) -> Iterator[GenerationInfo]:
        cursor: str | None = None
        while True:
            page = self.generations(source_id, cursor=cursor, limit=page_size)
            yield from page.items
            if (cursor := page.next_cursor) is None:
                return

    # --- documents ---

    def document(
        self, doc_id: str, *, at: str | None = None, fields: Sequence[str] | None = None
    ) -> Document:
        """``fields=["text"]``: also the plain text of an HTML page (``text`` is null otherwise)."""
        return _parse(Document, self._send(w.document(doc_id, at, fields)))

    def history(self, doc_id: str, *, cursor: str | None = None, limit: int = 100) -> HistoryPage:
        return _parse(HistoryPage, self._send(w.history(doc_id, cursor, limit)))

    def iter_history(self, doc_id: str, *, page_size: int = 100) -> Iterator[DocumentVersion]:
        cursor: str | None = None
        while True:
            page = self.history(doc_id, cursor=cursor, limit=page_size)
            yield from page.items
            if (cursor := page.next_cursor) is None:
                return

    # --- search ---

    def search(
        self,
        q: str,
        *,
        sources: Sequence[str] | None = None,
        kind: DocKind | None = None,
        limit: int = 10,
        offset: int = 0,
    ) -> SearchResults:
        return _parse(SearchResults, self._send(w.search(q, sources, limit, offset, kind)))

    def iter_search(
        self, q: str, *, sources: Sequence[str] | None = None, page_size: int = 10, max_results: int = 100
    ) -> Iterator[Hit]:
        offset, seen = 0, 0
        while seen < max_results:
            page = self.search(q, sources=sources, limit=min(page_size, max_results - seen), offset=offset)
            yield from page.hits
            seen += len(page.hits)
            if page.next_offset is None:
                return
            offset = page.next_offset

    def suggest(self, q: str, *, limit: int = 10) -> Suggestions:
        return _parse(Suggestions, self._send(w.suggest(q, limit)))

    # --- change feed ---

    def changes(self, *, since: str | None = None, limit: int = 1000) -> ChangePage:
        return _parse(ChangePage, self._send(w.changes(since, limit)))

    def iter_changes(self, *, since: str | None = None, page_size: int = 1000) -> Iterator[ChangePage]:
        """Drain the feed: yields non-empty pages until caught up. Persist ``page.cursor`` to resume."""
        while True:
            page = self.changes(since=since, limit=page_size)
            if not page.items:
                return
            yield page
            since = page.cursor

    def stream_changes(
        self,
        *,
        since: str | None = None,
        max_seconds: float | None = None,
        reconnect: bool = True,
        max_retries: int | None = None,
        read_timeout: float = STREAM_READ_TIMEOUT,
    ) -> Iterator[StreamedChange]:
        """Follow the SSE stream. ``cursor`` is set on the last change of each server batch.

        Without ``max_seconds`` the stream runs until the caller stops: a dropped or silent connection
        (nothing, not even a keepalive, for ``read_timeout`` seconds) or a server restart is followed by
        a reconnect from the last cursor (``reconnect=False`` raises instead; ``max_retries`` bounds
        consecutive failures). With ``max_seconds`` the stream ends when the server closes it.
        """
        follow = _Follow(since, max_seconds, reconnect, max_retries)
        while True:
            req = w.changes_stream(follow.since, follow.remaining())
            try:
                with self._http.stream(
                    req.method,
                    req.path,
                    params=req.params,
                    headers=follow.headers(),
                    timeout=_timeout(read_timeout),
                ) as r:
                    if r.status_code >= 400:
                        raise_for_error(r, r.read())
                    parser = w.SSEParser()
                    for line in r.iter_lines():
                        ev = parser.feed(line)
                        follow.saw(parser)
                        if ev is not None and (item := _change_from(ev)) is not None:
                            yield item
            except httpx.TransportError as exc:
                time.sleep(follow.failed(exc))
                continue
            if follow.done():
                return
            time.sleep(follow.delay())

    # --- system, addresses ---

    def system(self, *, ledger_tail: int | None = None) -> SystemStatus:
        return _parse(SystemStatus, self._send(w.system(ledger_tail)))

    def proposals(self, *, status: str | None = None) -> ProposalPage:
        return _parse(ProposalPage, self._send(w.proposals(status)))

    # --- Navigator resources ---

    def recent_changes(self, *, limit: int = 50, source: str | None = None) -> RecentChanges:
        return _parse(RecentChanges, self._send(w.recent_changes(limit, source)))

    def portal(self) -> Portal:
        return _parse(Portal, self._send(w.portal()))

    def directory(self, path: str = "") -> DirectoryPage:
        return _parse(DirectoryPage, self._send(w.directory(path)))

    def random(self, *, source: str | None = None, seed: int | None = None) -> DocRef:
        return _parse(DocRef, self._send(w.random(source, seed)))

    def report_gap(self, href: str, *, from_doc_id: str | None = None) -> None:
        """Tell the gap miner a link is not in the library (loopback clients only)."""
        raise_for_error(self._send(w.report_gap(href, from_doc_id)))

    def resolve(self, address: str) -> Resolution:
        return _parse(Resolution, self._send(w.resolve(address)))


class AsyncWspClient:
    """Asynchronous client for API v1; mirrors :class:`WspClient`."""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        *,
        timeout: float = 30.0,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self._owned = http is None
        self._http = http or httpx.AsyncClient(base_url=base_url, timeout=timeout)

    async def aclose(self) -> None:
        if self._owned:
            await self._http.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        await self.aclose()

    async def _send(self, req: w.Req) -> httpx.Response:
        return await self._http.request(req.method, req.path, params=req.params, json=req.json)

    async def health(self) -> Health:
        return _parse(Health, await self._send(w.health()))

    async def sources(self, *, cursor: str | None = None, limit: int = 50) -> SourcePage:
        return _parse(SourcePage, await self._send(w.sources(cursor, limit)))

    async def iter_sources(self, *, page_size: int = 50) -> AsyncIterator[SourceInfo]:
        cursor: str | None = None
        while True:
            page = await self.sources(cursor=cursor, limit=page_size)
            for item in page.items:
                yield item
            if (cursor := page.next_cursor) is None:
                return

    async def source(self, source_id: str) -> SourceInfo:
        return _parse(SourceInfo, await self._send(w.source(source_id)))

    async def generations(
        self, source_id: str, *, cursor: str | None = None, limit: int = 50
    ) -> GenerationPage:
        return _parse(GenerationPage, await self._send(w.generations(source_id, cursor, limit)))

    async def iter_generations(self, source_id: str, *, page_size: int = 50) -> AsyncIterator[GenerationInfo]:
        cursor: str | None = None
        while True:
            page = await self.generations(source_id, cursor=cursor, limit=page_size)
            for item in page.items:
                yield item
            if (cursor := page.next_cursor) is None:
                return

    async def document(
        self, doc_id: str, *, at: str | None = None, fields: Sequence[str] | None = None
    ) -> Document:
        return _parse(Document, await self._send(w.document(doc_id, at, fields)))

    async def history(self, doc_id: str, *, cursor: str | None = None, limit: int = 100) -> HistoryPage:
        return _parse(HistoryPage, await self._send(w.history(doc_id, cursor, limit)))

    async def iter_history(self, doc_id: str, *, page_size: int = 100) -> AsyncIterator[DocumentVersion]:
        cursor: str | None = None
        while True:
            page = await self.history(doc_id, cursor=cursor, limit=page_size)
            for item in page.items:
                yield item
            if (cursor := page.next_cursor) is None:
                return

    async def search(
        self,
        q: str,
        *,
        sources: Sequence[str] | None = None,
        kind: DocKind | None = None,
        limit: int = 10,
        offset: int = 0,
    ) -> SearchResults:
        return _parse(SearchResults, await self._send(w.search(q, sources, limit, offset, kind)))

    async def iter_search(
        self, q: str, *, sources: Sequence[str] | None = None, page_size: int = 10, max_results: int = 100
    ) -> AsyncIterator[Hit]:
        offset, seen = 0, 0
        while seen < max_results:
            page = await self.search(
                q, sources=sources, limit=min(page_size, max_results - seen), offset=offset
            )
            for item in page.hits:
                yield item
            seen += len(page.hits)
            if page.next_offset is None:
                return
            offset = page.next_offset

    async def suggest(self, q: str, *, limit: int = 10) -> Suggestions:
        return _parse(Suggestions, await self._send(w.suggest(q, limit)))

    async def changes(self, *, since: str | None = None, limit: int = 1000) -> ChangePage:
        return _parse(ChangePage, await self._send(w.changes(since, limit)))

    async def iter_changes(
        self, *, since: str | None = None, page_size: int = 1000
    ) -> AsyncIterator[ChangePage]:
        while True:
            page = await self.changes(since=since, limit=page_size)
            if not page.items:
                return
            yield page
            since = page.cursor

    async def stream_changes(
        self,
        *,
        since: str | None = None,
        max_seconds: float | None = None,
        reconnect: bool = True,
        max_retries: int | None = None,
        read_timeout: float = STREAM_READ_TIMEOUT,
    ) -> AsyncIterator[StreamedChange]:
        """See :meth:`WspClient.stream_changes`."""
        follow = _Follow(since, max_seconds, reconnect, max_retries)
        while True:
            req = w.changes_stream(follow.since, follow.remaining())
            try:
                async with self._http.stream(
                    req.method,
                    req.path,
                    params=req.params,
                    headers=follow.headers(),
                    timeout=_timeout(read_timeout),
                ) as r:
                    if r.status_code >= 400:
                        raise_for_error(r, await r.aread())
                    parser = w.SSEParser()
                    async for line in r.aiter_lines():
                        ev = parser.feed(line)
                        follow.saw(parser)
                        if ev is not None and (item := _change_from(ev)) is not None:
                            yield item
            except httpx.TransportError as exc:
                await asyncio.sleep(follow.failed(exc))
                continue
            if follow.done():
                return
            await asyncio.sleep(follow.delay())

    async def system(self, *, ledger_tail: int | None = None) -> SystemStatus:
        return _parse(SystemStatus, await self._send(w.system(ledger_tail)))

    async def proposals(self, *, status: str | None = None) -> ProposalPage:
        return _parse(ProposalPage, await self._send(w.proposals(status)))

    async def recent_changes(self, *, limit: int = 50, source: str | None = None) -> RecentChanges:
        return _parse(RecentChanges, await self._send(w.recent_changes(limit, source)))

    async def portal(self) -> Portal:
        return _parse(Portal, await self._send(w.portal()))

    async def directory(self, path: str = "") -> DirectoryPage:
        return _parse(DirectoryPage, await self._send(w.directory(path)))

    async def random(self, *, source: str | None = None, seed: int | None = None) -> DocRef:
        return _parse(DocRef, await self._send(w.random(source, seed)))

    async def report_gap(self, href: str, *, from_doc_id: str | None = None) -> None:
        raise_for_error(await self._send(w.report_gap(href, from_doc_id)))

    async def resolve(self, address: str) -> Resolution:
        return _parse(Resolution, await self._send(w.resolve(address)))
