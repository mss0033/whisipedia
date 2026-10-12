import asyncio
import json

import httpx
from fastapi import FastAPI

from wsp_api import Services
from wsp_api.sse import format_event
from wsp_api.testing import local_async_client
from wsp_core.ids import make_doc_id
from wsp_core.model import ChangeKind, ChangeRecord


def _events(raw: str) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for block in raw.split("\n\n"):
        fields: dict[str, str] = {}
        for line in block.splitlines():
            if line.startswith(":") or not line:
                continue
            name, _, value = line.partition(": ")
            fields[name] = value
        if "data" in fields:
            out.append(fields)
    return out


def test_stream_replays_backlog_with_resumable_ids(client: httpx.Client) -> None:
    r = client.get("/v1/changes/stream", params={"max_seconds": 0.05})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    assert r.text.startswith("retry: 3000\n\n")
    events = _events(r.text)
    assert [json.loads(e["data"])["kind"] for e in events] == ["added"] * 4 + ["modified"]
    # The backlog is one feed batch; only its last event carries the cursor.
    assert [e.get("id") for e in events] == [None, None, None, None, "5"]


def test_stream_resumes_from_last_event_id(client: httpx.Client) -> None:
    r = client.get("/v1/changes/stream", params={"max_seconds": 0.05}, headers={"Last-Event-ID": "4"})
    events = _events(r.text)
    assert [json.loads(e["data"])["kind"] for e in events] == ["modified"]


def test_stream_sends_keepalives_when_idle(client: httpx.Client) -> None:
    r = client.get("/v1/changes/stream", params={"since": "5", "max_seconds": 0.1})
    assert ": keepalive" in r.text
    assert _events(r.text) == []


def test_stream_rejects_bad_cursor_before_streaming(client: httpx.Client) -> None:
    r = client.get("/v1/changes/stream", params={"since": "nope"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_cursor"


def test_stream_delivers_changes_published_while_connected(app: FastAPI, services: Services) -> None:
    new = ChangeRecord(make_doc_id("rfc", "9111"), ChangeKind.ADDED, "2026-11-01", None, "abc")

    async def run() -> str:
        async with local_async_client(app) as c:
            pending = asyncio.create_task(
                c.get("/v1/changes/stream", params={"since": "5", "max_seconds": 0.3})
            )
            await asyncio.sleep(0.05)
            services.changes.publish([new])
            return (await pending).text

    events = _events(asyncio.run(run()))
    assert [json.loads(e["data"])["doc_id"] for e in events] == ["wsp:rfc/9111"]
    assert events[0]["id"] == "6"


def test_format_event_splits_multiline_data() -> None:
    assert format_event("a\nb", event="x", event_id="7") == "id: 7\nevent: x\ndata: a\ndata: b\n\n"


def test_summarize_skips_a_backlog_to_the_head() -> None:
    from collections.abc import Sequence

    from wsp_api.sse import change_events

    records = [
        ChangeRecord(make_doc_id("s", f"d{i}"), ChangeKind.ADDED, "g1", None, f"h{i}") for i in range(10)
    ]

    class Feed:
        def since(self, cursor: str | None, *, limit: int = 1000) -> tuple[Sequence[ChangeRecord], str]:
            start = int(cursor or 0)
            chunk = records[start : start + limit]
            return chunk, str(start + len(chunk))

        def head(self) -> str:
            return str(len(records))

    async def collect(summarize: bool) -> list[dict[str, str]]:
        async def gone() -> bool:
            return True

        out = ""
        async for part in change_events(
            Feed(),  # type: ignore[arg-type]
            None,
            poll_seconds=0,
            heartbeat_seconds=60,
            max_seconds=None,
            is_disconnected=gone,
            batch=4,
            summarize=summarize,
        ):
            out += part
        return _events(out)

    skipped = asyncio.run(collect(True))
    assert [(e["event"], e.get("id")) for e in skipped] == [("skipped", "10")]  # one event, not ten
    assert json.loads(skipped[0]["data"]) == {"cursor": "10"}
    assert len(asyncio.run(collect(False))) == 10  # without it, every record is delivered
