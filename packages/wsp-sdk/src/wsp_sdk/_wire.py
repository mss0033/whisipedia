"""Transport-independent pieces shared by the sync and async clients: requests and SSE parsing."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import cast
from urllib.parse import parse_qsl, quote

DEFAULT_BASE_URL = "http://127.0.0.1:8737"
DEFAULT_RETRY_MS = 3000  # SSE reconnection delay until the server sends its own `retry:`

Params = tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class Req:
    method: str
    path: str
    params: Params = ()
    json: dict[str, object] | None = None


def _p(**kwargs: object) -> Params:
    out: list[tuple[str, str]] = []
    for key, value in kwargs.items():
        if value is None:
            continue
        if isinstance(value, bool):
            out.append((key, "true" if value else "false"))
        elif isinstance(value, list | tuple):
            out.extend((key, str(v)) for v in cast(Sequence[object], value))
        else:
            out.append((key, str(value)))
    return tuple(out)


def doc_path(doc_id: str) -> str:
    # The doc id is ONE path segment: '/' becomes %2F (same bytes as JavaScript's encodeURIComponent).
    return "/v1/documents/" + quote(doc_id, safe="!'()*")


def health() -> Req:
    return Req("GET", "/v1/health")


def sources(cursor: str | None, limit: int) -> Req:
    return Req("GET", "/v1/sources", _p(cursor=cursor, limit=limit))


def source(source_id: str) -> Req:
    return Req("GET", f"/v1/sources/{quote(source_id, safe='')}")


def generations(source_id: str, cursor: str | None, limit: int) -> Req:
    return Req("GET", f"/v1/sources/{quote(source_id, safe='')}/generations", _p(cursor=cursor, limit=limit))


def document(doc_id: str, at: str | None, fields: Sequence[str] | None = None) -> Req:
    return Req("GET", doc_path(doc_id), _p(at=at, fields=list(fields) if fields else None))


def history(doc_id: str, cursor: str | None, limit: int) -> Req:
    return Req("GET", doc_path(doc_id) + "/history", _p(cursor=cursor, limit=limit))


def search(q: str, sources: Sequence[str] | None, limit: int, offset: int, kind: str | None = None) -> Req:
    return Req(
        "GET",
        "/v1/search",
        _p(q=q, sources=list(sources) if sources else None, kind=kind, limit=limit, offset=offset),
    )


def suggest(q: str, limit: int) -> Req:
    return Req("GET", "/v1/suggest", _p(q=q, limit=limit))


def changes(since: str | None, limit: int) -> Req:
    return Req("GET", "/v1/changes", _p(since=since, limit=limit))


def recent_changes(limit: int, source: str | None) -> Req:
    return Req("GET", "/v1/changes/recent", _p(limit=limit, source=source))


def portal() -> Req:
    return Req("GET", "/v1/portal")


def directory(path: str) -> Req:
    return Req("GET", "/v1/directory", _p(path=path))


def random(source: str | None, seed: int | None) -> Req:
    return Req("GET", "/v1/random", _p(source=source, seed=seed))


def report_gap(href: str, from_doc_id: str | None) -> Req:
    return Req("POST", "/v1/gaps", json={"href": href, "from_doc_id": from_doc_id})


def changes_stream(since: str | None, max_seconds: float | None) -> Req:
    return Req("GET", "/v1/changes/stream", _p(since=since, max_seconds=max_seconds))


def system(ledger_tail: int | None) -> Req:
    return Req("GET", "/v1/system", _p(ledger_tail=ledger_tail))


def proposals(status: str | None) -> Req:
    return Req("GET", "/v1/proposals", _p(status=status))


def resolve(address: str) -> Req:
    path, _, qs = address.strip().partition("?")
    return Req("GET", "/go/" + quote(path, safe="/:"), tuple(parse_qsl(qs)))


# --- Server-sent events --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SSEEvent:
    event: str
    data: str
    id: str | None


@dataclass(slots=True)
class SSEParser:
    """Incremental parser for ``text/event-stream`` (WHATWG HTML §9.2), fed one line at a time.

    ``SSEEvent.id`` is the ``id`` field sent *with that event* (the server sets it on the last change of
    a batch). ``last_event_id`` is the spec's last-event-ID buffer: it persists across events and is
    what a client sends back as ``Last-Event-ID`` when it reconnects. ``retry_ms`` is the server's
    reconnection delay, if it sent one.
    """

    _event: str = ""
    _data: list[str] = field(default_factory=list[str])
    _id: str | None = None
    last_event_id: str | None = None
    retry_ms: int | None = None

    def feed(self, line: str) -> SSEEvent | None:
        line = line.rstrip("\r\n")
        if not line:
            if not self._data:
                self._event, self._id = "", None
                return None
            ev = SSEEvent(self._event or "message", "\n".join(self._data), self._id)
            self._event, self._data, self._id = "", [], None
            return ev
        if line.startswith(":"):
            return None
        name, _, value = line.partition(":")
        value = value.removeprefix(" ")
        if name == "data":
            self._data.append(value)
        elif name == "event":
            self._event = value
        elif name == "id" and "\0" not in value:
            self._id = value
            self.last_event_id = value or None
        elif name == "retry" and value.isdigit():
            self.retry_ms = int(value)
        return None
