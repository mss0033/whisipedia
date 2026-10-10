"""Section-aware passages of about 200 words (PLAN §9.4) with stable ids ``<doc_id>#<n>``.

Refinery text keeps one paragraph per line and headings as short lines of their own (see
``wsp_adapters.html_text``). The chunker:

* detects headings (short lines without closing punctuation that are followed by text) and never
  lets a chunk span two sections; each chunk remembers its section heading;
* packs whole paragraphs up to ``target_words``; a paragraph longer than ``max_words`` is split at
  sentence boundaries, and a run-on sentence at word boundaries;
* merges a short tail (< ``min_words``) into the previous chunk of the same section;
* drops reference-like trailing sections ("References", "External links", ...);
* prefixes the title (and section) to the text that is embedded, so a passage about "his early
  life" still knows whose life it is.

Chunk numbering depends only on the text, so the same content always yields the same ids.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass

from wsp_core.model import DocRecord

CHUNK_SEP = "#"

_SENTENCE_END = re.compile(r"(?<=[.!?…。\uff01\uff1f])[\"'”\u2019)\]]*\s+")
_CLOSING = tuple(".!?:;,…\"'”\u2019)]。\uff01\uff1f")
_SKIP_SECTIONS = frozenset(
    s.casefold()
    for s in (
        "References",
        "External links",
        "See also",
        "Further reading",
        "Notes",
        "Bibliography",
        "Sources",
        "Citations",
        "Footnotes",
        "Notes and references",
        "Works cited",
        "Explanatory notes",
        "Einzelnachweise",
        "Weblinks",
        "Literatur",
        "Références",
        "Liens externes",
        "Referencias",
        "Enlaces externos",
        "Note",
        "Collegamenti esterni",
    )
)


@dataclass(frozen=True, slots=True)
class Chunk:
    chunk_id: str
    doc_id: str
    n: int
    title: str
    section: str
    text: str

    def embed_text(self) -> str:
        """What the embedder sees: title (and section) prefix, then the passage."""
        head = f"{self.title} — {self.section}" if self.section else self.title
        return f"{head}\n{self.text}"


def make_chunk_id(doc_id: str, n: int) -> str:
    return f"{doc_id}{CHUNK_SEP}{n}"


def split_chunk_id(chunk_id: str) -> tuple[str, int]:
    """``(doc_id, n)``. Doc ids may contain ``#``, so the number after the *last* one is the index."""
    doc_id, sep, n = chunk_id.rpartition(CHUNK_SEP)
    if not sep or not n.isdigit():
        raise ValueError(f"not a chunk id: {chunk_id!r}")
    return doc_id, int(n)


def doc_of(chunk_or_doc_id: str) -> str:
    """The doc id of a chunk id; anything else is returned unchanged."""
    doc_id, sep, n = chunk_or_doc_id.rpartition(CHUNK_SEP)
    return doc_id if sep and n.isdigit() else chunk_or_doc_id


def _is_heading(line: str, nxt: str | None) -> bool:
    if nxt is None or len(line) > 90:
        return False
    words = line.split()
    if not words or len(words) > 10 or line.endswith(_CLOSING):
        return False
    if line[0] in "-*•·\u2013—|":  # list items and table rows
        return False
    return len(nxt.split()) > len(words)


def _sections(text: str, title: str) -> Iterator[tuple[str, list[str]]]:
    lines = [ln.strip() for ln in text.split("\n")]
    lines = [ln for ln in lines if ln]
    if lines and lines[0].casefold() == title.casefold():
        lines = lines[1:]  # the article title repeated as the first line
    section, paras = "", list[str]()
    for i, line in enumerate(lines):
        nxt = lines[i + 1] if i + 1 < len(lines) else None
        if _is_heading(line, nxt):
            if paras:
                yield section, paras
            section, paras = line, []
        else:
            paras.append(line)
    if paras:
        yield section, paras


def _pieces(paragraph: str, max_words: int, keep_whole: int) -> Iterator[list[str]]:
    """A paragraph as word lists of at most ``max_words``, split at sentences where possible.

    Paragraphs up to ``keep_whole`` words stay in one piece.
    """
    words = paragraph.split()
    if len(words) <= keep_whole:
        yield words
        return
    cur: list[str] = []
    for sentence in _SENTENCE_END.split(paragraph):
        sw = sentence.split()
        while len(sw) > max_words:  # a run-on "sentence" (lists, tables)
            if cur:
                yield cur
                cur = []
            yield sw[:max_words]
            sw = sw[max_words:]
        if cur and len(cur) + len(sw) > max_words:
            yield cur
            cur = []
        cur += sw
    if cur:
        yield cur


def chunk_text(
    title: str,
    text: str,
    *,
    target_words: int = 200,
    max_words: int | None = None,
    min_words: int | None = None,
    max_chunks: int = 256,
) -> list[tuple[str, str]]:
    """``(section, passage)`` pairs in document order."""
    hard = max_words or int(target_words * 1.5)
    small = min_words if min_words is not None else max(1, target_words // 5)
    out: list[tuple[str, str]] = []
    for section, paras in _sections(text, title):
        if section.casefold() in _SKIP_SECTIONS:
            continue
        cur: list[str] = []
        sec_start = len(out)
        for para in paras:
            for piece in _pieces(para, target_words, hard):
                if cur and len(cur) + len(piece) > target_words:
                    out.append((section, " ".join(cur)))
                    cur = []
                cur += piece
        if cur:
            if len(cur) < small and len(out) > sec_start:
                prev_section, prev = out[-1]
                if len(prev.split()) + len(cur) <= hard:
                    out[-1] = (prev_section, f"{prev} {' '.join(cur)}")
                    cur = []
            if cur:
                out.append((section, " ".join(cur)))
        if len(out) >= max_chunks:
            return out[:max_chunks]
    return out


def chunk_document(
    doc: DocRecord, *, target_words: int = 200, max_chunks: int = 256, min_words: int | None = None
) -> list[Chunk]:
    """Chunks of ``doc``. A document with no usable text yields one chunk holding its title."""
    pairs = chunk_text(
        doc.title, doc.text, target_words=target_words, max_chunks=max_chunks, min_words=min_words
    )
    if not pairs:
        pairs = [("", doc.title)]
    return [
        Chunk(make_chunk_id(doc.doc_id, n), doc.doc_id, n, doc.title, section, text)
        for n, (section, text) in enumerate(pairs)
    ]
