"""Section-aware passages of about 200 words (PLAN §9.4) with stable ids ``<doc_id>#<n>``.

Refinery text keeps one paragraph per line and headings as short lines of their own (see
``wsp_adapters.html_text``). The chunker:

* detects headings (short capitalized lines without closing punctuation that are followed by
  longer text) and starts a new chunk at each one, except that a chunk shorter than half the target
  continues into the next section (formula fragments, one-line sections); each chunk remembers the
  section it starts in;
* packs whole paragraphs up to ``target_words``; a paragraph longer than ``max_words`` is split at
  sentence boundaries (also after the CJK full stops, which no space follows), and a run-on sentence at word
  boundaries;
* measures length in *units* that track the embedder's tokens: a word of a spaced script is one
  unit, each Han/Kana/Hangul/Thai character is one (a Chinese paragraph is one "word" to
  ``str.split`` and was cut at the model's 512 tokens, losing 85% of it);
* merges a short tail (< ``min_words``) into the previous chunk of the same section;
* with ``stop_at_references`` (wiki-like sources), stops at a reference-like section ("See also",
  "References", "Notes", ...) only in the last third of the text, where Wikipedia's navigation
  follows; a book's "Notes" chapter in the middle no longer ends the book;
* has no cap on passages unless ``max_chunks`` is given (a 256 cap kept the first ~51k words of a
  book);
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

_SENTENCE_END = re.compile(
    r"(?<=[.!?\u2026])[\"'\u201d\u2019)\]]*\s+"
    r"|(?<=[\u3002\uff01\uff1f])[\u300d\u300f\u201d\u2019)\]]*\s*"
)
_CLOSING = tuple(".!?:;,\u2026\"'\u201d\u2019)]\u3002\uff01\uff1f")
_DENSE = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uac00-\ud7af\u0e00-\u0e7f]")
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


def is_wiki_like(source_id: str) -> bool:
    """Sources whose trailing reference sections are navigation, not content (Wikipedia & co)."""
    return any(k in source_id for k in ("wiki", "mdwiki"))


_HATNOTES = ("main article:", "see also:", "further information:", "main articles:", "for ", "this article ")
_END_LINES = ("categories:", "hidden categories:")


def units(word: str) -> int:
    """Length of a whitespace word in units: 1, or its count of dense-script characters (CJK, ...)."""
    dense = len(_DENSE.findall(word))
    return dense if dense else 1


def _split_dense(word: str, size: int) -> list[str]:
    """A dense-script run longer than ``size`` units as slices of ``size`` characters."""
    if units(word) <= size:
        return [word]
    return [word[i : i + size] for i in range(0, len(word), size)]


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


def _sections(text: str, title: str) -> Iterator[tuple[str, list[str], float]]:
    """``(heading, paragraphs, position of the heading in [0, 1))``."""
    lines = [ln.strip() for ln in text.split("\n")]
    lines = [ln for ln in lines if ln]
    if lines and lines[0].casefold() == title.casefold():
        lines = lines[1:]  # the article title repeated as the first line
    total = max(1, sum(len(ln) for ln in lines))
    seen = 0
    section, paras, at = "", list[str](), 0.0
    for i, line in enumerate(lines):
        nxt = lines[i + 1] if i + 1 < len(lines) else None
        if _is_heading(line, nxt):
            if paras:
                yield section, paras, at
            section, paras, at = line, [], seen / total
        else:
            paras.append(line)
        seen += len(line)
    if paras:
        yield section, paras, at


def _pieces(paragraph: str, max_units: int, keep_whole: int) -> Iterator[list[str]]:
    """A paragraph as word lists of at most ``max_units`` units, split at sentences where possible.

    Paragraphs up to ``keep_whole`` units stay in one piece."""
    words = [p for w in paragraph.split() for p in _split_dense(w, max_units)]
    if sum(units(w) for w in words) <= keep_whole:
        yield words
        return
    cur: list[str] = []
    cur_units = 0
    for sentence in _SENTENCE_END.split(paragraph):
        sw = [p for w in sentence.split() for p in _split_dense(w, max_units)]
        for w in sw:
            u = units(w)
            if cur and cur_units + u > max_units:
                yield cur
                cur, cur_units = [], 0
            cur.append(w)
            cur_units += u
        if cur_units >= max_units // 2:  # a sentence boundary is a good place to cut
            yield cur
            cur, cur_units = [], 0
    if cur:
        yield cur


def chunk_text(
    title: str,
    text: str,
    *,
    target_words: int = 200,
    max_words: int | None = None,
    min_words: int | None = None,
    max_chunks: int = 0,
    stop_at_references: bool = True,
) -> list[tuple[str, str]]:
    """``(section, passage)`` pairs in document order (``max_chunks`` 0: no cap)."""
    hard = max_words or int(target_words * 1.5)
    small = min_words if min_words is not None else max(1, target_words // 5)
    out: list[tuple[str, str]] = []
    cur: list[str] = []
    cur_units = 0
    cur_section = ""

    def flush() -> None:
        nonlocal cur, cur_units
        if cur:
            out.append((cur_section, " ".join(cur)))
            cur, cur_units = [], 0

    for section, paras, at in _sections(text, title):
        if stop_at_references and at >= 2 / 3 and section.casefold() in _SKIP_SECTIONS:
            break  # "See also", "References", ... near the end, and the navigation after them
        if cur_units >= target_words // 2:
            flush()  # sections are boundaries, but short sections merge into their neighbours
        if not cur:
            cur_section = section
        for para in paras:
            if para.casefold().startswith(_END_LINES):
                break
            for piece in _pieces(para, target_words, hard):
                n = sum(units(w) for w in piece)
                if cur and cur_units + n > target_words:
                    flush()
                    cur_section = section
                cur += piece
                cur_units += n
        if max_chunks and len(out) >= max_chunks:
            return out[:max_chunks]
    if cur and cur_units < small and out and sum(units(w) for w in out[-1][1].split()) + cur_units <= hard:
        prev_section, prev = out[-1]
        out[-1] = (prev_section, f"{prev} {' '.join(cur)}")
        cur = []
    flush()
    return out[:max_chunks] if max_chunks else out


def chunk_document(
    doc: DocRecord, *, target_words: int = 200, max_chunks: int = 0, min_words: int | None = None
) -> list[Chunk]:
    """Chunks of ``doc``. A document with no usable text yields one chunk holding its title."""
    pairs = chunk_text(
        doc.title,
        doc.text,
        target_words=target_words,
        max_chunks=max_chunks,
        min_words=min_words,
        stop_at_references=is_wiki_like(doc.source_id),
    )
    if not pairs:
        pairs = [("", doc.title)]
    return [
        Chunk(make_chunk_id(doc.doc_id, n), doc.doc_id, n, doc.title, section, text)
        for n, (section, text) in enumerate(pairs)
    ]
