import httpx
import pytest

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
    assert doc_path("wsp:rfc/a b?c#d%") == "/v1/documents/wsp:rfc/a%20b%3Fc%23d%25"
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
