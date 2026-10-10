"""Server-sent events for the change feed (PLAN §11.1).

Event format::

    id: <cursor after this batch>     (only on the last event of a batch)
    event: change
    data: {"doc_id": ..., "kind": ..., ...}

Clients resume with ``Last-Event-ID`` (browsers do this automatically) or ``?since=``. Delivery is
at-least-once: after a disconnect mid-batch, the batch is replayed. Comment lines (``: keepalive``)
are sent while idle.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence

from wsp_api.convert import change
from wsp_api.schemas import Change
from wsp_core.model import ChangeRecord
from wsp_core.ports import ChangeFeed

RETRY_MS = 3000


def format_event(data: str, *, event: str | None = None, event_id: str | None = None) -> str:
    lines: list[str] = []
    if event_id is not None:
        lines.append(f"id: {event_id}")
    if event is not None:
        lines.append(f"event: {event}")
    lines.extend(f"data: {line}" for line in data.split("\n"))
    return "\n".join(lines) + "\n\n"


async def change_events(
    feed: ChangeFeed,
    cursor: str | None,
    *,
    poll_seconds: float,
    heartbeat_seconds: float,
    max_seconds: float | None,
    is_disconnected: Callable[[], Awaitable[bool]],
    batch: int = 500,
    describe: Callable[[ChangeRecord], Change] = change,
) -> AsyncIterator[str]:
    yield f"retry: {RETRY_MS}\n\n"
    started = last_sent = time.monotonic()
    while True:
        records, new_cursor = await asyncio.to_thread(feed.since, cursor, limit=batch)
        described = await asyncio.to_thread(_describe_all, describe, records)
        for i, item in enumerate(described):
            last = i == len(described) - 1
            yield format_event(item.model_dump_json(), event="change", event_id=new_cursor if last else None)
        if records:
            cursor, last_sent = new_cursor, time.monotonic()
            continue  # drain backlog before sleeping
        now = time.monotonic()
        if max_seconds is not None and now - started >= max_seconds:
            return
        if now - last_sent >= heartbeat_seconds:
            yield ": keepalive\n\n"
            last_sent = now
        if await is_disconnected():
            return
        await asyncio.sleep(poll_seconds)


def _describe_all(
    describe: Callable[[ChangeRecord], Change], records: Sequence[ChangeRecord]
) -> list[Change]:
    return [describe(r) for r in records]
