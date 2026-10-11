import json
from collections.abc import Iterator

import httpx
import pytest

from wsp_sdk import WspClient
from wsp_sdk._wire import SSEEvent, SSEParser, doc_path, resolve, search
from wsp_sdk.errors import WspApiError, raise_for_error


def _parse(text: str) -> list[SSEEvent]:
    parser = SSEParser()
    return [ev for line in text.split("\n") if (ev := parser.feed(line)) is not None]


def test_parses_ids_events_multiline_data_and_comments() -> None:
    raw = (
        "retry: 3000\n\n: keepalive\n\n"
        "event: change\ndata: a\ndata: b\n\n"
        "id: 7\nevent: change\ndata:c\n\n"
        "data: x\n\n"
    )
    assert _parse(raw) == [
        SSEEvent("change", "a\nb", None),
        SSEEvent("change", "c", "7"),
        SSEEvent("message", "x", None),
    ]


def test_crlf_lines() -> None:
    assert _parse("data: x\r\n\r\n") == [SSEEvent("message", "x", None)]


def test_paths_and_params() -> None:
    assert doc_path("wsp:rfc/a b?c#d%") == "/v1/documents/wsp%3Arfc%2Fa%20b%3Fc%23d%25"
    assert doc_path("wsp:x/(it's)!*~") == "/v1/documents/wsp%3Ax%2F(it's)!*~"  # = encodeURIComponent
    assert search("q", ["a", "b"], 5, 0).params == (
        ("q", "q"),
        ("sources", "a"),
        ("sources", "b"),
        ("limit", "5"),
        ("offset", "0"),
    )
    r = resolve(" wsp://search?q=a+b ")
    assert (r.path, r.params) == ("/go/wsp://search", (("q", "a b"),))


def test_non_envelope_error_body_still_raises() -> None:
    response = httpx.Response(502, content=b"<html>bad gateway</html>")
    with pytest.raises(WspApiError) as exc:
        raise_for_error(response)
    assert exc.value.code == "http_error"
    assert exc.value.status == 502


def test_last_event_id_persists_across_events_and_retry_is_read() -> None:
    parser = SSEParser()
    text = "retry: 25\n\nid: c.1\nevent: change\ndata: a\n\nevent: change\ndata: b\n\n"
    events = [ev for line in text.split("\n") if (ev := parser.feed(line)) is not None]
    assert [e.id for e in events] == ["c.1", None]  # the event's own id ...
    assert parser.last_event_id == "c.1"  # ... and the buffer a reconnect sends back (WHATWG)
    assert parser.retry_ms == 25


class _Body(httpx.SyncByteStream):
    def __init__(self, text: str, *, drop: bool) -> None:
        self.text, self.drop = text, drop

    def __iter__(self) -> Iterator[bytes]:
        yield self.text.encode()
        if self.drop:
            raise httpx.ReadError("connection reset")


def _event(doc: str, event_id: str | None = None) -> str:
    data = json.dumps(
        {"doc_id": doc, "kind": "added", "generation_id": "g", "source_id": "s", "address": "x"}
    )
    return ("" if event_id is None else f"id: {event_id}\n") + f"event: change\ndata: {data}\n\n"


def _sse(body: str, *, drop: bool) -> httpx.Response:
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=_Body(body, drop=drop))


def test_stream_reconnects_with_last_event_id_after_a_drop() -> None:
    seen: list[dict[str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        n = len(seen)
        seen.append({"last": request.headers.get("last-event-id"), "since": request.url.params.get("since")})
        if n == 0:  # one batch, then the connection drops in the middle of the next
            return _sse("retry: 1\n\n" + _event("wsp:s/1", "e.1") + _event("wsp:s/2"), drop=True)
        if n == 1:  # resumed after e.1: the interrupted batch again (at-least-once), then a clean close
            return _sse("retry: 1\n\n" + _event("wsp:s/2", "e.2"), drop=False)
        raise httpx.ConnectError("server gone")

    http = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://127.0.0.1:8737")
    got: list[str] = []

    def follow() -> None:
        with WspClient(http=http) as wsp:
            got.extend(item.change.doc_id for item in wsp.stream_changes(since="e.0", max_retries=1))

    with pytest.raises(httpx.ConnectError):
        follow()
    assert got == ["wsp:s/1", "wsp:s/2", "wsp:s/2"]
    assert seen[0] == {"last": None, "since": "e.0"}
    assert seen[1]["last"] == "e.1"
    assert seen[2]["last"] == "e.2"  # after a clean close it reconnects too (no max_seconds)
    assert len(seen) == 4  # then two failed connects: max_retries=1 allows one


def test_stream_without_reconnect_raises_and_reads_with_a_timeout() -> None:
    timeouts: list[object] = []

    def handler(request: httpx.Request) -> httpx.Response:
        timeouts.append(request.extensions.get("timeout"))
        raise httpx.ReadTimeout("silent server")

    http = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://127.0.0.1:8737")
    with WspClient(http=http) as wsp, pytest.raises(httpx.ReadTimeout):
        list(wsp.stream_changes(reconnect=False, read_timeout=7))
    assert timeouts == [{"connect": 10.0, "read": 7, "write": 10.0, "pool": 10.0}]
