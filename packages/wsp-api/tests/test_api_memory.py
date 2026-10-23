"""``/v1/system/memory``: where the API's memory is (2026-10-23: 4.3 GB after 26 hours, every cache
bounded, and nothing said which part grew)."""

from __future__ import annotations

import tracemalloc

import httpx

from wsp_api import memory


def test_the_memory_report_names_each_part(client: httpx.Client) -> None:
    r = client.get("/v1/system/memory")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"process_mb", "glibc", "arrow", "readers", "python"}
    assert body["process_mb"]["VmRSS"] > 0
    assert body["glibc"]["in_use_mb"] >= 0
    assert body["arrow"]["allocated_mb"] >= 0
    assert body["python"]["objects"] > 0
    assert body["python"]["types"]["dict"] > 0  # live objects by type: what grows between reports
    assert "WSP_DIAG_TRACEMALLOC" in body["python"]["tracing"]  # off unless asked for


def test_tracing_is_turned_on_only_when_asked_and_then_names_lines() -> None:
    assert not memory.start_tracing_from_env({})
    assert not memory.start_tracing_from_env({"WSP_DIAG_TRACEMALLOC": "no"})
    try:
        assert memory.start_tracing_from_env({"WSP_DIAG_TRACEMALLOC": "3"})
        hold = [bytes(1024) for _ in range(2000)]
        top = memory.report(object())["python"]["top"]
        assert top
        assert any("test_api_memory.py" in row["where"] for row in top)
        del hold
    finally:
        tracemalloc.stop()


def test_trim_says_what_the_process_was_before_and_after(client: httpx.Client) -> None:
    r = client.post("/v1/system/memory/trim")
    assert r.status_code == 200
    body = r.json()
    assert body["before_mb"]["VmRSS"] > 0
    assert body["after_mb"]["VmRSS"] > 0
