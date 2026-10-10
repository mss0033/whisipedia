"""Citation guard, summarizer events, the Generator-port adapter and the llama.cpp client."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Sequence

import httpx
import pytest
from oracle_helpers import ScriptedLLM

from wsp_oracle.generator import (
    NOTHING_CITED,
    REFUSAL,
    CitationGuard,
    CitedGenerator,
    Event,
    GeneratorError,
    LlamaServerGenerator,
    Passage,
    Summarizer,
    parse_prompt,
)

PASSAGES = [
    Passage(1, "Vaccine", "A vaccine trains the immune system.", "wsp:e/Vaccine", "wsp://e/Vaccine"),
    Passage(2, "Immune system", "Memory cells persist after infection.", "wsp:e/Immune", "wsp://e/Immune"),
]


def guard_all(pieces: Sequence[str], valid: Sequence[int] = (1, 2)) -> tuple[list[str], CitationGuard]:
    g = CitationGuard(valid)
    out: list[str] = []
    for p in pieces:
        out += g.feed(p)
    out += g.flush()
    return out, g


def test_guard_keeps_cited_and_drops_uncited() -> None:
    out, g = guard_all(["Vaccines train ", "immunity [1]. This is made up. Memory ", "cells persist [2]."])
    assert [s.strip() for s in out] == ["Vaccines train immunity [1].", "Memory cells persist [2]."]
    assert g.dropped == ["This is made up."]
    assert g.kept == 2


def test_guard_markers_after_punctuation_and_split_markers() -> None:
    out, _ = guard_all(["It works. [", "1][2] Next one [2", "]. Last"])
    assert [s.strip() for s in out] == ["It works. [1][2]", "Next one [2]."]


def test_guard_removes_unknown_markers_and_drops_if_none_left() -> None:
    out, g = guard_all(["Fact [1][7]. Other [9]."])
    assert [s.strip() for s in out] == ["Fact [1]."]
    assert g.removed_markers == 1
    assert g.dropped == ["Other [9]."]


def test_guard_abbreviations_and_lowercase_do_not_split() -> None:
    out, _ = guard_all(["Dr. Smith and J. Doe, e.g. in the U.S. army, studied 3.5 cases [1]. Fine [2]."])
    assert len(out) == 2
    assert out[0].startswith("Dr. Smith")


def test_guard_newlines_split_list_items() -> None:
    out, g = guard_all(["- first item [1]\n- second item\n- third [2]\n"])
    assert [s.strip() for s in out] == ["- first item [1]", "- third [2]"]
    assert g.dropped == ["- second item"]


def collect(gen: AsyncIterator[Event]) -> list[Event]:
    async def run() -> list[Event]:
        return [e async for e in gen]

    return asyncio.run(run())


def test_summarizer_events_shape_and_order() -> None:
    llm = ScriptedLLM(["Vaccines train the immune ", "system [1]. Unsupported claim. ", "Memory lasts [2]."])
    events = collect(Summarizer(llm).events("how do vaccines work", PASSAGES))
    kinds = [e["type"] for e in events]
    assert kinds == ["token", "citation", "token", "token", "citation", "token", "notice", "done"]
    cit = events[1]
    assert cit == {
        "type": "citation",
        "index": 1,
        "doc_id": "wsp:e/Vaccine",
        "title": "Vaccine",
        "address": "wsp://e/Vaccine",
    }
    assert "1 sentence" in str(events[-2]["text"])
    system = llm.messages[0]["content"]
    assert "[1]" in system
    assert "[1] Vaccine" in llm.messages[1]["content"]


def test_low_confidence_refuses_without_calling_the_model() -> None:
    llm = ScriptedLLM(["should not run [1]."])
    s = Summarizer(llm, scorer=lambda q, ps: [0.01 for _ in ps], min_confidence=0.2)
    events = collect(s.events("unrelated", PASSAGES))
    assert events == [{"type": "notice", "text": REFUSAL}, {"type": "done"}]
    assert llm.messages == []


def test_no_answer_sentinel_refuses() -> None:
    events = collect(Summarizer(ScriptedLLM(["NO_", "ANSWER"])).events("q", PASSAGES))
    assert events[0] == {"type": "notice", "text": REFUSAL}


def test_nothing_cited_is_announced() -> None:
    events = collect(Summarizer(ScriptedLLM(["No citations here. None at all."])).events("q", PASSAGES))
    texts = [e.get("text") for e in events if e["type"] == "notice"]
    assert NOTHING_CITED in texts


class Exploding:
    async def stream(self, prompt: str, *, max_tokens: int = 512) -> AsyncIterator[str]:
        yield "Partial [1]. "
        raise RuntimeError("boom")


def test_model_failure_becomes_error_event() -> None:
    events = collect(Summarizer(Exploding()).events("q", PASSAGES))
    assert events[-1] == {"type": "error", "message": "summarizer failed (RuntimeError)"}


API_PROMPT = (
    "Summarize the passages below to answer the question. Cite every sentence with [n]. "
    "If the passages do not answer it, say so.\n\n"
    "Question: how do vaccines work\n\n"
    "[1] Vaccine\nA vaccine trains the immune system.\nSecond line.\n\n"
    "[2] Immune system\nMemory cells persist."
)


def test_parse_api_prompt() -> None:
    parsed = parse_prompt(API_PROMPT)
    assert parsed is not None
    q, ps = parsed
    assert q == "how do vaccines work"
    assert [(p.index, p.title) for p in ps] == [(1, "Vaccine"), (2, "Immune system")]
    assert ps[0].text == "A vaccine trains the immune system.\nSecond line."
    assert parse_prompt("hello") is None


def test_cited_generator_on_the_api_prompt() -> None:
    gen = CitedGenerator(
        Summarizer(ScriptedLLM(["Vaccines train immunity [1]. Invented. Memory persists [2]."]))
    )

    async def run(prompt: str) -> str:
        return "".join([p async for p in gen.stream(prompt)])

    assert asyncio.run(run(API_PROMPT)) == "Vaccines train immunity [1]. Memory persists [2]."
    assert asyncio.run(run("not a summary prompt")) == REFUSAL


def sse_transport(lines: list[str], status: int = 200) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["stream"] is True
        assert body["chat_template_kwargs"] == {"enable_thinking": False}
        return httpx.Response(
            status, content="\n".join(lines).encode(), headers={"content-type": "text/event-stream"}
        )

    return httpx.MockTransport(handler)


def test_llama_server_client_streams_content_only() -> None:
    lines = [
        "data: " + json.dumps({"choices": [{"delta": {"reasoning_content": "hmm"}}]}),
        "",
        "data: " + json.dumps({"choices": [{"delta": {"content": "Hello"}}]}),
        ": keep-alive",
        "data: " + json.dumps({"choices": [{"delta": {"content": " world"}}]}),
        "data: [DONE]",
    ]
    gen = LlamaServerGenerator("http://127.0.0.1:8738", transport=sse_transport(lines), system="sys")

    async def run() -> str:
        return "".join([p async for p in gen.stream("hi", max_tokens=8)])

    assert asyncio.run(run()) == "Hello world"


def test_llama_server_errors_and_loopback_only() -> None:
    gen = LlamaServerGenerator("http://localhost:1", transport=sse_transport(["oops"], status=503))

    async def run() -> str:
        return "".join([p async for p in gen.stream("hi")])

    with pytest.raises(GeneratorError, match="503"):
        asyncio.run(run())
    with pytest.raises(GeneratorError, match="loopback"):
        LlamaServerGenerator("http://llm.example.org:8738")
    LlamaServerGenerator("http://[::1]:8738")
    assert asyncio.run(LlamaServerGenerator("http://127.0.0.1:9").health()) is False
