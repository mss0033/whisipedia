"""The API harness rides out a busy library (503 + Retry-After) instead of dying mid-run."""

from __future__ import annotations

import io
import json
import urllib.error
from email.message import Message
from typing import Any

import pytest

from wsp_oracle import evaluation


class _Resp(io.BytesIO):
    def __enter__(self) -> _Resp:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _busy(retry_after: str | None) -> urllib.error.HTTPError:
    hdrs = Message()
    if retry_after is not None:
        hdrs["Retry-After"] = retry_after
    return urllib.error.HTTPError("http://x/v1/search", 503, "busy", hdrs, io.BytesIO(b""))


def _install(monkeypatch: pytest.MonkeyPatch, outcomes: list[Any]) -> list[str]:
    calls: list[str] = []

    def fake(req: Any, timeout: float = 0) -> _Resp:
        calls.append(req.full_url)
        item = outcomes.pop(0)
        if isinstance(item, BaseException):
            raise item
        return _Resp(json.dumps(item).encode())

    monkeypatch.setattr("urllib.request.urlopen", fake)
    return calls


def test_503_is_retried_after_retry_after(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _install(monkeypatch, [_busy("7"), _busy(None), {"hits": [{"doc_id": "a"}, {"doc_id": "b"}]}])
    waits: list[float] = []
    search = evaluation.api_searcher("http://127.0.0.1:1/", sleep=waits.append)
    assert search("why is the sky blue") == ["a", "b"]
    assert len(calls) == 3
    assert waits == [7.0, 10.0]  # Retry-After honoured, then back-off 5*2**1


def test_other_errors_and_exhaustion_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    gone = urllib.error.HTTPError("http://x", 404, "nope", Message(), io.BytesIO(b""))
    _install(monkeypatch, [gone])
    with pytest.raises(urllib.error.HTTPError):
        evaluation.api_searcher("http://h", sleep=lambda s: None)("q")
    _install(monkeypatch, [_busy("1")] * 3)
    with pytest.raises(urllib.error.HTTPError):
        evaluation.api_searcher("http://h", attempts=3, sleep=lambda s: None)("q")


def test_retry_after_is_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, [_busy("9999"), {"hits": []}])
    waits: list[float] = []
    assert evaluation.api_searcher("http://h", sleep=waits.append)("q") == []
    assert waits == [120.0]
