"""Sync and async clients. Both expose the same methods; the async ones are awaitable/async-iterable.

    with WspClient() as wsp:                                   # http://127.0.0.1:8737 by default
        for hit in wsp.iter_search("rfc about http caching"):
            print(hit.title, hit.address)

    async with AsyncWspClient() as wsp:
        async for item in wsp.stream_changes(since=cursor):
            ...

Pass ``http=`` to inject a preconfigured ``httpx`` client (custom transport, timeouts, in-process app).
"""

from __future__ import annotations

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
    Document,
    DocumentVersion,
    GenerationInfo,
    GenerationPage,
    Health,
    HistoryPage,
    Hit,
    Resolution,
    SearchResults,
    SourceInfo,
    SourcePage,
    StreamedChange,
    Suggestions,
    SummaryChunk,
    SystemStatus,
)

DEFAULT_BASE_URL = w.DEFAULT_BASE_URL


def _parse[M: BaseModel](model: type[M], response: httpx.Response) -> M:
    raise_for_error(response)
    return model.model_validate_json(response.content)


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

    def document(self, doc_id: str, *, at: str | None = None) -> Document:
        return _parse(Document, self._send(w.document(doc_id, at)))

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
        self, q: str, *, sources: Sequence[str] | None = None, limit: int = 10, offset: int = 0
    ) -> SearchResults:
        return _parse(SearchResults, self._send(w.search(q, sources, limit, offset)))

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
        self, *, since: str | None = None, max_seconds: float | None = None
    ) -> Iterator[StreamedChange]:
        """Follow the SSE stream. ``cursor`` is set on the last change of each server batch."""
        req = w.changes_stream(since, max_seconds)
        with self._http.stream(req.method, req.path, params=req.params, timeout=None) as r:
            if r.status_code >= 400:
                raise_for_error(r, r.read())
            parser = w.SSEParser()
            for line in r.iter_lines():
                ev = parser.feed(line)
                if ev is not None and (item := _change_from(ev)) is not None:
                    yield item

    # --- system, summaries, addresses ---

    def system(self, *, ledger_tail: int | None = None) -> SystemStatus:
        return _parse(SystemStatus, self._send(w.system(ledger_tail)))

    def summarize(
        self, query: str, doc_ids: Sequence[str], *, max_tokens: int = 512
    ) -> Iterator[SummaryChunk]:
        """Stream a cited summary. Raises ``NotConfiguredError`` if the server has no local LLM."""
        req = w.summarize(query, doc_ids, max_tokens)
        with self._http.stream(req.method, req.path, json=req.json) as r:
            if r.status_code >= 400:
                raise_for_error(r, r.read())
            for line in r.iter_lines():
                if line.strip():
                    yield SummaryChunk.model_validate_json(line)

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

    async def document(self, doc_id: str, *, at: str | None = None) -> Document:
        return _parse(Document, await self._send(w.document(doc_id, at)))

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
        self, q: str, *, sources: Sequence[str] | None = None, limit: int = 10, offset: int = 0
    ) -> SearchResults:
        return _parse(SearchResults, await self._send(w.search(q, sources, limit, offset)))

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
        self, *, since: str | None = None, max_seconds: float | None = None
    ) -> AsyncIterator[StreamedChange]:
        req = w.changes_stream(since, max_seconds)
        async with self._http.stream(req.method, req.path, params=req.params, timeout=None) as r:
            if r.status_code >= 400:
                raise_for_error(r, await r.aread())
            parser = w.SSEParser()
            async for line in r.aiter_lines():
                ev = parser.feed(line)
                if ev is not None and (item := _change_from(ev)) is not None:
                    yield item

    async def system(self, *, ledger_tail: int | None = None) -> SystemStatus:
        return _parse(SystemStatus, await self._send(w.system(ledger_tail)))

    async def summarize(
        self, query: str, doc_ids: Sequence[str], *, max_tokens: int = 512
    ) -> AsyncIterator[SummaryChunk]:
        req = w.summarize(query, doc_ids, max_tokens)
        async with self._http.stream(req.method, req.path, json=req.json) as r:
            if r.status_code >= 400:
                raise_for_error(r, await r.aread())
            async for line in r.aiter_lines():
                if line.strip():
                    yield SummaryChunk.model_validate_json(line)

    async def resolve(self, address: str) -> Resolution:
        return _parse(Resolution, await self._send(w.resolve(address)))
