"""Turn a model's token stream into summary events, lifting ``[n]`` markers out as citation events."""

from __future__ import annotations

import re
from collections.abc import Mapping

from wsp_api.schemas import SummaryCitation, SummaryToken

_MARKER = re.compile(r"\[(\d{1,3})\]")
_PARTIAL = re.compile(r"\[\d{0,3}$")  # a marker that may be completed by the next chunk


class CitationSplitter:
    def __init__(self, citations: Mapping[int, SummaryCitation]) -> None:
        self._citations = citations
        self._buf = ""

    def feed(self, chunk: str) -> list[SummaryToken | SummaryCitation]:
        self._buf += chunk
        hold = _PARTIAL.search(self._buf)
        ready, self._buf = (self._buf[: hold.start()], self._buf[hold.start() :]) if hold else (self._buf, "")
        return self._split(ready)

    def flush(self) -> list[SummaryToken | SummaryCitation]:
        rest, self._buf = self._buf, ""
        return self._split(rest)

    def _split(self, text: str) -> list[SummaryToken | SummaryCitation]:
        out: list[SummaryToken | SummaryCitation] = []
        pos = 0
        for m in _MARKER.finditer(text):
            citation = self._citations.get(int(m.group(1)))
            if citation is None:
                continue  # not one of ours: keep it as text
            if m.start() > pos:
                out.append(SummaryToken(text=text[pos : m.start()]))
            out.append(citation)
            pos = m.end()
        if pos < len(text):
            out.append(SummaryToken(text=text[pos:]))
        return out
