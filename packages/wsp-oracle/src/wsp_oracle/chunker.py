"""Section-aware passages of about 200 words (PLAN §9.4) with stable ids ``<doc_id>#<n>``.

Refinery text keeps one paragraph per line and headings as short lines of their own (see
``wsp_adapters.html_text``). The chunker:

* detects headings (short capitalized lines without closing punctuation that are followed by
  longer text) and starts a new chunk at each one, except that a chunk shorter than half the target
  continues into the next section (formula fragments, one-line sections); each chunk remembers the
  section it starts in;
* packs whole paragraphs up to ``target_words``; a paragraph longer than ``max_words`` is split at
  sentence boundaries, and a run-on sentence at word boundaries;
* merges a short tail (< ``min_words``) into the previous chunk of the same section;
* stops at the first reference-like section ("See also", "References", "External links", ...),
  which is where Wikipedia's navigation boxes and category lines follow;
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


_HATNOTES = ("main article:", "see also:", "further information:", "main articles:", "for ", "this article ")
_END_LINES = ("categories:", "hidden categories:")


def _is_heading(line: str, nxt: str | None) -> bool:
    if nxt is None or len(line) > 90:
        return False
    words = line.split()
    if not words or len(words) > 10 or line.endswith(_CLOSING):
        return False
    if not (line[0].isupper() or line[0].isdigit()) or sum(ch.isalpha() for ch in line) < 3:
        return False  # list items, table cells, formula fragments ("λ", "2", "¯")
    if line.casefold().startswith(_HATNOTES):
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
    cur: list[str] = []
    cur_section = ""

    def flush() -> None:
        nonlocal cur
        if cur:
            out.append((cur_section, " ".join(cur)))
            cur = []

    for section, paras in _sections(text, title):
        if section.casefold() in _SKIP_SECTIONS:
            break  # "See also", "References", ... and the navigation that follows them
        if len(cur) >= target_words // 2:
            flush()  # sections are boundaries, but short sections merge into their neighbours
        if not cur:
            cur_section = section
        for para in paras:
            if para.casefold().startswith(_END_LINES):
                break
            for piece in _pieces(para, target_words, hard):
                if cur and len(cur) + len(piece) > target_words:
                    flush()
                    cur_section = section
                cur += piece
        if len(out) >= max_chunks:
            return out[:max_chunks]
    if cur and len(cur) < small and out and len(out[-1][1].split()) + len(cur) <= hard:
        prev_section, prev = out[-1]
        out[-1] = (prev_section, f"{prev} {' '.join(cur)}")
        cur = []
    flush()
    return out[:max_chunks]


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
