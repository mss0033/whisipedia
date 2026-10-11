"""Cited summaries (PLAN §9.3 summary rules, §10.3) over a local llama.cpp server.

* :class:`LlamaServerGenerator` streams tokens from ``llama-server``'s OpenAI-compatible endpoint
  on a **loopback** address only (the Oracle has no network capability, PLAN §10.5).
* :class:`CitationGuard` validates the stream sentence by sentence: every sentence must carry at
  least one ``[n]`` marker naming a passage that exists. Markers naming unknown passages are
  removed; sentences left without a valid marker are dropped and counted.
* :class:`Summarizer` adds the retrieval-confidence gate (no summary when the best passage's
  relevance is below ``min_confidence``) and turns the guarded stream into the API's SSE event
  types: ``notice``, ``token``, ``citation``, ``done``, ``error``.
* :class:`CitedGenerator` implements the ``Generator`` port for the current ``/v1/summarize``
  route, which builds its own prompt: it parses the numbered passages back out of that prompt,
  so the route gets the guard and the gate without changing. The port can only carry text, so
  refusals arrive as a plain sentence and dropped sentences are not announced; routes that use
  :meth:`Summarizer.events` get proper ``notice`` events.
"""

from __future__ import annotations

import ipaddress
import json
import re
from collections.abc import AsyncIterator, Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, cast, runtime_checkable
from urllib.parse import urlparse

import httpx

from wsp_core.ports import Generator

REFUSAL = "The search results do not clearly answer this question, so no summary was written."
NOTHING_CITED = "No sentence of the draft summary could be tied to a result, so none is shown."
NO_ANSWER = "NO_ANSWER"

SYSTEM_PROMPT = (
    "You write short factual summaries for the search engine of an offline library.\n"
    "Rules:\n"
    "- Use only the numbered passages. Do not add outside knowledge.\n"
    "- End every sentence with the number of each passage that supports it in square brackets, "
    "for example [1] or [2][3]. Only cite numbers that are listed.\n"
    f"- If the passages do not answer the question, reply with exactly {NO_ANSWER}.\n"
    "- Write two to five sentences in the language of the question. No preamble, no headings."
)

Scorer = Callable[[str, Sequence[str]], Sequence[float]]
Event = dict[str, object]

_MARK = re.compile(r"\[(\d{1,3})\]")
_BOUNDARY = re.compile(r"[.!?。\uff01\uff1f][\"'”\u2019)]*((?:\s*\[\d{1,3}\])*)(?=(\s+)(\S))|\n+")
_ABBREV = frozenset(
    [
        "e.g",
        "i.e",
        "etc",
        "vs",
        "dr",
        "mr",
        "mrs",
        "ms",
        "st",
        "no",
        "ca",
        "c",
        "approx",
        "fig",
        "jr",
        "sr",
        "prof",
        "gen",
        "col",
        "lt",
        "mt",
        "ft",
        "inc",
        "ltd",
        "co",
    ]
)


# --- the llama.cpp client -------------------------------------------------------------------------


class GeneratorError(RuntimeError):
    pass


def require_loopback(url: str) -> str:
    host = urlparse(url).hostname or ""
    if host == "localhost":
        return url
    try:
        if ipaddress.ip_address(host).is_loopback:
            return url
    except ValueError:
        pass
    raise GeneratorError(f"generator URL must be a loopback address (PLAN §10.5), got {url!r}")


@runtime_checkable
class ChatGenerator(Protocol):
    def stream_chat(
        self, messages: Sequence[Mapping[str, str]], *, max_tokens: int = 512
    ) -> AsyncIterator[str]: ...


class LlamaServerGenerator:
    """``Generator`` (and :class:`ChatGenerator`) backed by ``llama-server`` on 127.0.0.1."""

    def __init__(
        self,
        url: str,
        *,
        timeout_s: float = 60.0,
        temperature: float = 0.2,
        system: str = "",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.url = require_loopback(url).rstrip("/")
        self.timeout_s = timeout_s
        self.temperature = temperature
        self.system = system
        self._transport = transport

    def _client(self) -> httpx.AsyncClient:
        # trust_env=False: never pick up an HTTP(S)_PROXY from the environment for a loopback call.
        return httpx.AsyncClient(timeout=self.timeout_s, transport=self._transport, trust_env=False)

    async def stream_chat(
        self, messages: Sequence[Mapping[str, str]], *, max_tokens: int = 512
    ) -> AsyncIterator[str]:
        body: dict[str, Any] = {
            "messages": [dict(m) for m in messages],
            "stream": True,
            "max_tokens": max_tokens,
            "temperature": self.temperature,
            "cache_prompt": True,
            "chat_template_kwargs": {"enable_thinking": False},
        }
        async with (
            self._client() as client,
            client.stream("POST", f"{self.url}/v1/chat/completions", json=body) as r,
        ):
            if r.status_code != 200:
                detail = (await r.aread()).decode("utf-8", "replace")[:200]
                raise GeneratorError(f"llama-server answered {r.status_code}: {detail}")
            async for line in r.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    return
                event = cast(dict[str, Any], json.loads(data))
                for choice in cast(list[dict[str, Any]], event.get("choices") or []):
                    delta = cast(dict[str, Any], choice.get("delta") or {})
                    piece = delta.get("content")
                    if piece:  # reasoning_content (thinking) is never shown
                        yield str(piece)

    async def stream(self, prompt: str, *, max_tokens: int = 512) -> AsyncIterator[str]:
        messages = [{"role": "system", "content": self.system}] if self.system else []
        messages.append({"role": "user", "content": prompt})
        async for piece in self.stream_chat(messages, max_tokens=max_tokens):
            yield piece

    async def health(self) -> bool:
        try:
            async with self._client() as client:
                r = await client.get(f"{self.url}/health")
        except httpx.HTTPError:
            return False
        return r.status_code == 200


# --- citation enforcement -------------------------------------------------------------------------


class CitationGuard:
    """Sentence-level citation check over a token stream.

    ``feed`` returns the sentences that became complete and passed; ``flush`` the last one. A
    sentence passes when it carries at least one marker in ``valid``; unknown markers are removed.
    Markers right after the closing punctuation (``... end. [2] Next``) belong to that sentence.
    """

    def __init__(self, valid: Collection[int]) -> None:
        self.valid = frozenset(valid)
        self._buf = ""
        self.kept = 0
        self.dropped: list[str] = []
        self.removed_markers = 0

    def _abbreviation(self, text: str, dot: int) -> bool:
        if text[dot] != ".":
            return False
        start = dot
        while start > 0 and not text[start - 1].isspace():
            start -= 1
        word = text[start:dot].casefold().strip("(\"'")
        return word in _ABBREV or (len(word) == 1 and word.isalpha())

    def _check(self, sentence: str) -> str | None:
        body = sentence.strip()
        if not body:
            return None
        markers = [int(m) for m in _MARK.findall(body)]
        if not any(m in self.valid for m in markers):
            self.dropped.append(body)
            return None
        bad = sum(1 for m in markers if m not in self.valid)
        if bad:
            self.removed_markers += bad
            body = _MARK.sub(lambda m: m.group(0) if int(m.group(1)) in self.valid else "", body)
            body = re.sub(r"\s+([.,;:!?])", r"\1", re.sub(r"[ \t]{2,}", " ", body))
        self.kept += 1
        return body

    def feed(self, text: str) -> list[str]:
        self._buf += text
        out: list[str] = []
        pos = 0
        for m in _BOUNDARY.finditer(self._buf):
            if m.group(0).startswith("\n"):
                end, sep = m.start(), "\n"
            else:
                nxt = m.group(3)
                if nxt == "[":  # a marker is still arriving
                    break
                if nxt.islower() or nxt.isdigit() or self._abbreviation(self._buf, m.start()):
                    continue
                end, sep = m.end(), " "
            if end <= pos:
                pos = m.end()
                continue
            checked = self._check(self._buf[pos:end])
            if checked is not None:
                out.append(checked + sep)
            pos = m.end() if m.group(0).startswith("\n") else m.end() + len(m.group(2))
        self._buf = self._buf[pos:]
        return out

    def flush(self) -> list[str]:
        rest, self._buf = self._buf, ""
        checked = self._check(rest)
        return [] if checked is None else [checked]


# --- summaries ------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Passage:
    index: int
    title: str
    text: str
    doc_id: str = ""
    address: str = ""


_QUESTION = re.compile(r"^Question:\s*(.+)$", re.M)
_PASSAGE = re.compile(r"^\[(\d{1,3})\] ([^\n]*)\n(.*?)(?=\n\n\[\d{1,3}\] |\Z)", re.M | re.S)


def parse_prompt(prompt: str) -> tuple[str, list[Passage]] | None:
    """The question and numbered passages of a ``/v1/summarize`` prompt (``None``: not that format)."""
    q = _QUESTION.search(prompt)
    if q is None:
        return None
    body = prompt[q.end() :]
    passages = [
        Passage(int(m.group(1)), m.group(2).strip(), m.group(3).strip()) for m in _PASSAGE.finditer(body)
    ]
    return (q.group(1).strip(), passages) if passages else None


def user_prompt(question: str, passages: Sequence[Passage], *, passage_chars: int = 1500) -> str:
    parts = [f"[{p.index}] {p.title}\n{p.text[:passage_chars]}" for p in passages]
    return f"Question: {question}\n\nPassages:\n\n" + "\n\n".join(parts)


class Summarizer:
    """Confidence gate + prompt + citation guard over a local LLM."""

    def __init__(
        self,
        llm: Generator,
        *,
        scorer: Scorer | None = None,
        min_confidence: float = 0.2,
        passage_chars: int = 1500,
        max_passages: int = 8,
    ) -> None:
        self.llm = llm
        self.scorer = scorer
        self.min_confidence = min_confidence
        self.passage_chars = passage_chars
        self.max_passages = max_passages

    def confidence(self, question: str, passages: Sequence[Passage]) -> float | None:
        if self.scorer is None or not passages:
            return None
        scores = self.scorer(question, [f"{p.title}\n{p.text[: self.passage_chars]}" for p in passages])
        return max(scores, default=0.0)

    async def _llm(self, question: str, passages: Sequence[Passage], max_tokens: int) -> AsyncIterator[str]:
        user = user_prompt(question, passages, passage_chars=self.passage_chars)
        if isinstance(self.llm, ChatGenerator):
            messages = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user}]
            async for piece in self.llm.stream_chat(messages, max_tokens=max_tokens):
                yield piece
        else:
            async for piece in self.llm.stream(f"{SYSTEM_PROMPT}\n\n{user}", max_tokens=max_tokens):
                yield piece

    async def sentences(
        self,
        question: str,
        passages: Sequence[Passage],
        *,
        max_tokens: int = 512,
        guard: CitationGuard | None = None,
    ) -> AsyncIterator[str | None]:
        """Validated sentences; a single ``None`` means "refused" (low confidence or NO_ANSWER)."""
        passages = list(passages)[: self.max_passages]
        conf = self.confidence(question, passages)
        if not passages or (conf is not None and conf < self.min_confidence):
            yield None
            return
        guard = guard or CitationGuard([p.index for p in passages])
        held, released = "", False
        async for piece in self._llm(question, passages, max_tokens):
            if not released:  # hold the opening until it cannot be the NO_ANSWER sentinel
                held += piece
                start = held.lstrip()
                if start.startswith(NO_ANSWER):
                    yield None
                    return
                if len(start) < len(NO_ANSWER) and NO_ANSWER.startswith(start):
                    continue
                piece, released = held, True
            for s in guard.feed(piece):
                yield s
        if not released and held.strip():
            yield None  # the stream ended inside the sentinel
            return
        for s in guard.flush():
            yield s

    async def events(
        self, question: str, passages: Sequence[Passage], *, max_tokens: int = 512
    ) -> AsyncIterator[Event]:
        """``notice`` / ``token`` / ``citation`` / ``done`` / ``error`` events as the API emits them."""
        by_index = {p.index: p for p in passages}
        guard = CitationGuard(list(by_index))
        try:
            async for s in self.sentences(question, passages, max_tokens=max_tokens, guard=guard):
                if s is None:
                    yield {"type": "notice", "text": REFUSAL}
                    yield {"type": "done"}
                    return
                pos = 0
                for m in _MARK.finditer(s):
                    if m.start() > pos:
                        yield {"type": "token", "text": s[pos : m.start()]}
                    p = by_index[int(m.group(1))]
                    yield {
                        "type": "citation",
                        "index": p.index,
                        "doc_id": p.doc_id,
                        "title": p.title,
                        "address": p.address,
                    }
                    pos = m.end()
                if pos < len(s):
                    yield {"type": "token", "text": s[pos:]}
        except Exception as exc:  # the model died mid-stream: report, never leak internals
            yield {"type": "error", "message": f"summarizer failed ({type(exc).__name__})"}
            return
        if guard.dropped:
            n = len(guard.dropped)
            yield {"type": "notice", "text": f"{n} sentence(s) without a valid citation were left out."}
        if guard.kept == 0:
            yield {"type": "notice", "text": NOTHING_CITED}
        yield {"type": "done"}


class CitedGenerator:
    """``Generator`` port for the existing ``/v1/summarize`` route: guard + gate on its prompt."""

    def __init__(self, summarizer: Summarizer) -> None:
        self.summarizer = summarizer

    async def stream(self, prompt: str, *, max_tokens: int = 512) -> AsyncIterator[str]:
        parsed = parse_prompt(prompt)
        if parsed is None:  # not a summary prompt: nothing to cite against, refuse rather than guess
            yield REFUSAL
            return
        question, passages = parsed
        guard = CitationGuard([p.index for p in passages])
        async for s in self.summarizer.sentences(question, passages, max_tokens=max_tokens, guard=guard):
            if s is None:
                yield REFUSAL
                return
            yield s
        if guard.kept == 0:
            yield NOTHING_CITED
