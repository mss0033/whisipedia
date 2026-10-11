"""'Did you mean': classic noisy-channel spelling correction over the library's own vocabulary.

Language model: word counts from the indexed text itself (titles count extra), built offline by
``wsp index spell`` (also the last step of ``wsp index build``). Error model: each edit (insertion,
deletion, substitution, adjacent transposition) costs a factor ``epsilon``, so the correction of a
word ``w`` is ``argmax_c count(c) * epsilon ** dist(w, c)`` over known words within two edits,
the word itself included (Norvig, "How to Write a Spelling Corrector", 2007). A word is only
replaced when that wins by the error model's margin, so real words are never "corrected" into
more frequent neighbours unless they are far rarer.

Candidates come from a symmetric-delete index (SymSpell, Garbe 2012): every vocabulary word
stores the hashes of its variants with up to two characters deleted from its first ``prefix``
characters; a query word's own delete variants are looked up by binary search, and candidates are
verified with the exact optimal-string-alignment distance. Lookups touch a few dozen hashes, so a
correction takes well under a millisecond, and nothing is held in Python objects: the file is
memory-mapped.

File format (``<index>/lexical/spelling.bin``, little endian, replaced atomically)::

    b"WSPSPEL1" | u64 words | u64 blob bytes | u64 deletes
    | u64 word_hash[words] (sorted) | u32 count[words] | u64 offset[words + 1] | blob (UTF-8)
    | u64 delete_hash[deletes] (sorted) | u32 delete_word[deletes]

Hashes are ``crc32 << 32 | adler32``; a collision can only add a candidate, which the exact
distance check then rejects.
"""

from __future__ import annotations

import os
import re
import time
import zlib
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from itertools import combinations
from pathlib import Path
from typing import Any, cast

import numpy as np
import numpy.typing as npt
import pyarrow as pa
import pyarrow.compute as _pc

from wsp_index.refinery import RefineryReader

SPELL_FILE = "spelling.bin"
MAGIC = b"WSPSPEL1"
_HEADER = len(MAGIC) + 24
_WORD = re.compile(r"[^\W\d_]+(?:['" "\u2019" r"][^\W\d_]+)*", re.UNICODE)

pc: Any = _pc  # the compute stubs are too partial for strict checking; results are cast at use

U64 = npt.NDArray[np.uint64]
U32 = npt.NDArray[np.uint32]


def word_hash(word: str) -> int:
    b = word.encode("utf-8")
    return (zlib.crc32(b) << 32) | zlib.adler32(b)


def deletes(word: str, max_distance: int, prefix: int) -> set[str]:
    """Variants of ``word[:prefix]`` with up to ``max_distance`` characters deleted (itself included)."""
    head = word[:prefix]
    out = {head}
    for k in range(1, min(max_distance, len(head)) + 1):
        for drop in combinations(range(len(head)), k):
            out.add("".join(ch for i, ch in enumerate(head) if i not in drop))
    return out


def osa_distance(a: str, b: str, limit: int) -> int:
    """Optimal string alignment distance (Damerau-Levenshtein without repeated edits), capped."""
    if abs(len(a) - len(b)) > limit:
        return limit + 1
    prev2: list[int] = []
    prev = list(range(len(b) + 1))
    for i in range(1, len(a) + 1):
        cur = [i] + [0] * len(b)
        best = cur[0]
        for j in range(1, len(b) + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            v = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                v = min(v, prev2[j - 2] + 1)
            cur[j] = v
            best = min(best, v)
        if best > limit:
            return limit + 1
        prev2, prev = prev, cur
    return prev[len(b)]


@dataclass(frozen=True, slots=True)
class SpellConfig:
    max_distance: int = 2
    prefix: int = 7
    """Characters of each word that get delete variants (SymSpell's prefix trick)."""
    min_count: int = 3
    """Words seen fewer times are not in the vocabulary (typos in the corpus itself)."""
    min_count_docs: int = 100_000
    """``min_count`` applies from this many documents on; a smaller library keeps every word."""
    max_words: int = 500_000
    min_length: int = 3
    max_length: int = 24
    title_weight: int = 5
    """A word in a title counts this many times (titles name the things people look for)."""
    max_chars: int = 3_000
    """Text characters read per document (the lead carries most of the vocabulary)."""
    max_docs: int = 2_000_000
    """Documents read per source."""
    batch_size: int = 4096


@dataclass(frozen=True, slots=True)
class VocabStats:
    documents: int
    distinct_tokens: int
    words: int
    deletes: int
    seconds: float

    def line(self) -> str:
        return (
            f"{self.words:,} words from {self.documents:,} documents ({self.distinct_tokens:,} distinct "
            f"tokens), {self.deletes:,} delete keys in {self.seconds:.1f}s"
        )


# --- building --------------------------------------------------------------------------------------


def _tokens(texts: pa.Array[Any], cfg: SpellConfig) -> pa.Table:
    """``(w, c)`` counts of letter-only tokens of the right length in ``texts``."""
    low = pc.utf8_lower(texts)
    parts = pc.split_pattern_regex(low, pattern=r"[^\pL\pM]+")
    flat = cast("pa.Array[Any]", pc.list_flatten(parts))
    lengths = pc.utf8_length(flat)
    ok = pc.and_(
        pc.greater_equal(lengths, cfg.min_length),
        pc.less_equal(lengths, cfg.max_length),
    )
    flat = flat.filter(ok)
    if not len(flat):
        return pa.Table.from_arrays([pa.array([], pa.string()), pa.array([], pa.int64())], names=["w", "c"])
    vc = pc.value_counts(flat)
    counts = pc.cast(vc.field("counts"), pa.int64())
    return pa.Table.from_arrays([vc.field("values"), counts], names=["w", "c"])


class _Counter:
    """Arrow-side word counter with periodic compaction (bounded memory)."""

    def __init__(self, compact_rows: int = 4_000_000) -> None:
        self.acc: pa.Table | None = None
        self.pending: list[pa.Table] = []
        self.rows = 0
        self.compact_rows = compact_rows

    def add(self, t: pa.Table, weight: int = 1) -> None:
        if t.num_rows == 0:
            return
        if weight != 1:
            c = pc.multiply(t.column("c"), weight)
            t = pa.Table.from_arrays([t.column("w"), c], names=["w", "c"])
        self.pending.append(t)
        self.rows += t.num_rows
        if self.rows > self.compact_rows:
            self.compact()

    def compact(self) -> None:
        parts = [*([self.acc] if self.acc is not None else []), *self.pending]
        if parts:
            merged = pa.concat_tables(parts)
            agg = merged.group_by("w").aggregate([("c", "sum")])
            self.acc = agg.rename_columns(["w", "c"])
        self.pending, self.rows = [], 0


def count_words(
    readers: Sequence[tuple[str, RefineryReader]],
    cfg: SpellConfig = SpellConfig(),  # noqa: B008 - immutable
    progress: Callable[[str], None] | None = None,
) -> tuple[dict[str, int], int, int]:
    """``({word: count}, documents read, distinct tokens)`` over titles and text leads."""
    counter = _Counter()
    docs = 0
    for source, reader in readers:
        seen = 0
        for batch in reader.scan(["title", "text"], batch_size=cfg.batch_size):
            titles = cast("pa.Array[Any]", batch.column(0))
            texts = cast("pa.Array[Any]", batch.column(1))
            lead = pc.utf8_slice_codeunits(texts, 0, cfg.max_chars)
            counter.add(_tokens(titles, cfg), cfg.title_weight)
            counter.add(_tokens(cast("pa.Array[Any]", lead), cfg))
            seen += batch.num_rows
            if seen >= cfg.max_docs:
                break
        docs += seen
        if progress:
            progress(f"spell: {source}: {seen:,} documents read")
    counter.compact()
    acc = counter.acc
    if acc is None or acc.num_rows == 0:
        return {}, docs, 0
    distinct = acc.num_rows
    floor = cfg.min_count if docs >= cfg.min_count_docs else 1
    acc = acc.filter(pc.greater_equal(acc.column("c"), floor))
    counts = cast("npt.NDArray[np.int64]", acc.column("c").to_numpy())
    if len(counts) > cfg.max_words:
        keep = np.argpartition(-counts, cfg.max_words - 1)[: cfg.max_words]
        acc = acc.take(keep.tolist())
    words = cast(list[str], acc.column("w").to_pylist())
    values = cast(list[int], acc.column("c").to_pylist())
    return dict(zip(words, values, strict=True)), docs, distinct


def encode_vocabulary(counts: Mapping[str, int], cfg: SpellConfig = SpellConfig()) -> tuple[bytes, int]:  # noqa: B008
    """The vocabulary file's bytes and its number of delete keys."""
    words = sorted(counts, key=word_hash)
    hashes = np.fromiter((word_hash(w) for w in words), dtype=np.uint64, count=len(words))
    freq = np.fromiter((min(counts[w], 2**32 - 1) for w in words), dtype=np.uint32, count=len(words))
    blobs = [w.encode("utf-8") for w in words]
    offsets = np.zeros(len(words) + 1, dtype=np.uint64)
    if blobs:
        offsets[1:] = np.cumsum([len(b) for b in blobs], dtype=np.uint64)
    blob = b"".join(blobs)
    del_h: list[int] = []
    del_w: list[int] = []
    for i, w in enumerate(words):
        for v in deletes(w, cfg.max_distance, cfg.prefix):
            del_h.append(word_hash(v))
            del_w.append(i)
    dh = np.array(del_h, dtype=np.uint64)
    dw = np.array(del_w, dtype=np.uint32)
    order = np.argsort(dh, kind="stable")
    parts = [
        MAGIC,
        np.array([len(words), len(blob), len(dh)], dtype=np.uint64).tobytes(),
        hashes.tobytes(),
        freq.tobytes(),
        offsets.tobytes(),
        blob,
        dh[order].tobytes(),
        dw[order].tobytes(),
    ]
    return b"".join(parts), len(dh)


def write_vocabulary(path: Path, counts: Mapping[str, int], cfg: SpellConfig = SpellConfig()) -> int:  # noqa: B008
    """Write the vocabulary and its delete index atomically; returns the number of delete keys."""
    data, n_del = encode_vocabulary(counts, cfg)
    tmp = path.with_name(path.name + ".partial")
    path.parent.mkdir(parents=True, exist_ok=True)
    with tmp.open("wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    return n_del


def build_vocabulary(
    readers: Sequence[tuple[str, RefineryReader]],
    path: Path,
    cfg: SpellConfig = SpellConfig(),  # noqa: B008 - immutable
    progress: Callable[[str], None] | None = None,
) -> VocabStats:
    t0 = time.monotonic()
    counts, docs, distinct = count_words(readers, cfg, progress)
    n_del = write_vocabulary(path, counts, cfg)
    return VocabStats(docs, distinct, len(counts), n_del, time.monotonic() - t0)


# --- correcting ------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Correction:
    word: str
    candidate: str
    distance: int
    count: int


class Speller:
    """Noisy-channel corrector over a vocabulary file (or in-memory counts, for tests and samples)."""

    def __init__(
        self,
        buf: npt.NDArray[np.uint8],
        *,
        max_distance: int = 2,
        prefix: int = 7,
        epsilon: float = 0.01,
    ) -> None:
        if bytes(buf[: len(MAGIC)]) != MAGIC:
            raise ValueError("not a spelling vocabulary")
        n, blob_len, m = (int(x) for x in np.frombuffer(buf[len(MAGIC) : _HEADER], dtype=np.uint64))
        expected = _HEADER + n * 8 + n * 4 + (n + 1) * 8 + blob_len + m * 8 + m * 4
        if len(buf) != expected:
            raise ValueError(f"truncated spelling vocabulary ({len(buf)} bytes, want {expected})")
        off = _HEADER
        self._hash: U64 = buf[off : off + 8 * n].view(np.uint64)
        off += 8 * n
        self._count: U32 = buf[off : off + 4 * n].view(np.uint32)
        off += 4 * n
        self._offsets: U64 = buf[off : off + 8 * (n + 1)].view(np.uint64)
        off += 8 * (n + 1)
        self._blob = buf[off : off + blob_len]
        off += blob_len
        self._del_hash: U64 = buf[off : off + 8 * m].view(np.uint64)
        off += 8 * m
        self._del_word: U32 = buf[off : off + 4 * m].view(np.uint32)
        self.size = n
        self.max_distance = max_distance
        self.prefix = prefix
        self.log_eps = float(np.log(epsilon))

    @classmethod
    def open(cls, path: Path, **kw: Any) -> Speller | None:
        try:
            if not path.is_file():
                return None
            return cls(np.memmap(path, dtype=np.uint8, mode="r"), **kw)
        except (OSError, ValueError):
            return None

    @classmethod
    def from_counts(cls, counts: Mapping[str, int], **kw: Any) -> Speller:
        """An in-memory speller (tests, the sample library)."""
        cfg = SpellConfig(max_distance=kw.get("max_distance", 2), prefix=kw.get("prefix", 7), min_count=1)
        data, _ = encode_vocabulary({w.casefold(): c for w, c in counts.items()}, cfg)
        return cls(np.frombuffer(data, dtype=np.uint8), **kw)

    @classmethod
    def from_texts(cls, texts: Iterable[str], **kw: Any) -> Speller:
        counts: dict[str, int] = {}
        for t in texts:
            for w in iter_words(t):
                counts[w] = counts.get(w, 0) + 1
        return cls.from_counts(counts, **kw)

    def __len__(self) -> int:
        return self.size

    def _word(self, i: int) -> str:
        a, b = int(self._offsets[i]), int(self._offsets[i + 1])
        return bytes(self._blob[a:b]).decode("utf-8")

    def count(self, word: str) -> int:
        """How often ``word`` (lower case) occurs in the library; 0 if it is not in the vocabulary."""
        if not self.size:
            return 0
        h = np.uint64(word_hash(word))
        i = int(np.searchsorted(self._hash, h))
        while i < self.size and self._hash[i] == h:
            if self._word(i) == word:
                return int(self._count[i])
            i += 1
        return 0

    def _candidate_ids(self, word: str, limit: int) -> npt.NDArray[np.int64]:
        """Vocabulary rows sharing a delete variant with ``word``, by count (highest first)."""
        if not self.size or not len(self._del_hash):
            return np.zeros(0, dtype=np.int64)
        keys = np.fromiter((word_hash(v) for v in deletes(word, limit, self.prefix)), dtype=np.uint64)
        lo = np.searchsorted(self._del_hash, keys, side="left")
        hi = np.searchsorted(self._del_hash, keys, side="right")
        spans = [self._del_word[a:b] for a, b in zip(lo.tolist(), hi.tolist(), strict=True) if b > a]
        if not spans:
            return np.zeros(0, dtype=np.int64)
        ids = np.unique(np.concatenate(spans)).astype(np.int64)
        nbytes = (self._offsets[ids + 1] - self._offsets[ids]).astype(np.int64)
        wlen = len(word.encode("utf-8"))
        ids = ids[np.abs(nbytes - wlen) <= 2 * limit]  # bytes: ≤ 2 per character in Latin scripts
        return ids[np.argsort(-self._count[ids].astype(np.int64), kind="stable")]

    def _limit(self, word: str) -> int:
        return self.max_distance if len(word) > 4 else min(1, self.max_distance)

    def _cost(self, word: str, cand: str, dist: int) -> float:
        """Error-model log probability: ``epsilon`` per edit, one more when the first letter differs
        (people rarely mistype it: "lisbn" is Lisbon, not ISBN)."""
        extra = 1 if word[:1] != cand[:1] else 0
        return (dist + extra) * self.log_eps

    def candidates(self, word: str) -> list[tuple[str, int, int]]:
        """``(candidate, distance, count)`` of vocabulary words within the edit limit, by count."""
        limit = self._limit(word)
        out: list[tuple[str, int, int]] = []
        for i in self._candidate_ids(word, limit).tolist():
            cand = self._word(i)
            dist = osa_distance(word, cand, limit)
            if dist <= limit:
                out.append((cand, dist, int(self._count[i])))
        return out

    def best(self, word: str) -> Correction | None:
        """The noisy-channel choice for one lower-case word, or None to keep it."""
        own = self.count(word)
        best_score = float(np.log(own)) if own else -np.inf
        choice: Correction | None = None
        limit = self._limit(word)
        for i in self._candidate_ids(word, limit).tolist():
            count = int(self._count[i])
            if float(np.log(count)) + self.log_eps < best_score:
                break  # sorted by count: no remaining candidate can win
            cand = self._word(i)
            if cand == word:
                continue
            dist = osa_distance(word, cand, limit)
            if dist > limit:
                continue
            score = float(np.log(count)) + self._cost(word, cand, dist)
            if score > best_score:
                best_score = score
                choice = Correction(word, cand, dist, count)
        return choice

    def correct(self, query: str, *, only: Iterable[str] | None = None) -> str | None:
        """The query with misspelled words replaced, or None when nothing changes.

        ``only`` restricts corrections to these (lower-case) words. Digits, short words and
        anything that is not a letter run are left alone; capitalization is kept.
        """
        allowed = None if only is None else {w.casefold() for w in only}
        changed = False

        def fix(m: re.Match[str]) -> str:
            nonlocal changed
            raw = m.group(0)
            low = raw.casefold()
            if len(low) < 3 or (allowed is not None and low not in allowed):
                return raw
            c = self.best(low)
            if c is None:
                return raw
            changed = True
            if raw.isupper() and len(raw) > 1:
                return c.candidate.upper()
            if raw[0].isupper():
                return c.candidate[:1].upper() + c.candidate[1:]
            return c.candidate

        out = _WORD.sub(fix, query)
        return out if changed and out != query else None

    def unknown(self, query: str) -> list[str]:
        """Lower-case words of the query (3+ letters) that are not in the vocabulary."""
        return [w for w in iter_words(query) if len(w) >= 3 and not self.count(w)]


def iter_words(text: str) -> Iterator[str]:
    for m in _WORD.finditer(text):
        yield m.group(0).casefold()


class ReloadingSpeller:
    """A :class:`Speller` over a file that ``wsp index spell`` may replace while the API runs."""

    def __init__(self, path: Path, *, every: float = 5.0) -> None:
        self.path = path
        self.every = every
        self._stamp: tuple[int, int, int] = (-1, -1, -1)
        self._checked = -1e9
        self._speller: Speller | None = None

    def current(self) -> Speller | None:
        now = time.monotonic()
        if now - self._checked >= self.every:
            self._checked = now
            try:
                st = self.path.stat()
                stamp = (st.st_ino, st.st_mtime_ns, st.st_size)
            except OSError:
                stamp = (0, 0, 0)
            if stamp != self._stamp:
                self._stamp = stamp
                self._speller = Speller.open(self.path)
        return self._speller

    def correct(self, query: str, *, only: Iterable[str] | None = None) -> str | None:
        s = self.current()
        return None if s is None else s.correct(query, only=only)

    def unknown(self, query: str) -> list[str]:
        s = self.current()
        return [] if s is None else s.unknown(query)
