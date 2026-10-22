"""'Did you mean': noisy-channel spelling correction over the library's own vocabularies, per language.

Language model: word counts from the indexed text itself (titles count extra), one vocabulary per
language, built offline by ``wsp index spell`` (also the last step of ``wsp index build``) from a
*stratified* sample: row groups spread evenly over every part that holds the language, so a source
stored in language blocks (Cebuano first) no longer fills the vocabulary with its first language.

Error model: each edit (insertion, deletion, substitution, adjacent transposition) costs a factor
``epsilon``, so the correction of a word ``w`` is ``argmax_c count(c) * epsilon ** dist(w, c)`` over
known words within two edits, the word itself included (Norvig 2007). A word that is not in the
vocabulary counts as if it had the vocabulary's minimum count (``floor``): it is replaced only by a
word at least ``1 / epsilon`` times more frequent per edit (100x for one edit, 10,000x for two with
the default epsilon), so rare junk ("zzq") is left alone instead of becoming whatever is nearest.

The query picks its vocabulary: the words' script narrows the candidates (Latin, Cyrillic, Greek,
Devanagari, ...), and among those the language under which the query is most probable wins (known
words by frequency, unknown words by their best correction).

Build and query share one tokenizer, :func:`spell_tokens` (Arrow's Unicode case mapping, final sigma
folded, letters and combining marks, inner apostrophes kept), so "Straße", "λόγος", "नमस्ते" and
"don't" are the same words on both sides.

Candidates come from a symmetric-delete index (SymSpell, Garbe 2012); lookups touch a few dozen
hashes, so a correction takes well under a millisecond, and the file is memory-mapped
(``MADV_RANDOM``).

File format (``<index>/lexical/spelling.bin``, little endian, replaced atomically)::

    b"WSPSPEL2" | u64 sections | sections x (8-byte lang, 8-byte script, u64 offset, u64 length)
    section (8-aligned): u64 words, u64 deletes, u64 blob bytes, u64 floor, u64 total
        | u64 word_hash[words] (sorted) | u64 offset[words + 1] | u64 delete_hash[deletes] (sorted)
        | u32 count[words] | u32 delete_word[deletes] | blob (UTF-8)

Every array is naturally aligned (``WSPSPEL1`` put the blob first, so 7 of 8 files gave numpy
misaligned views and every ``searchsorted`` copied 93 MB: 17.5 ms against 0.15 ms). ``WSPSPEL1``
files stay readable, as one vocabulary for every script.

Hashes are ``crc32 << 32 | adler32``; a collision can only add a candidate, which the exact distance
check then rejects.

A rebuild reuses what has not changed (:class:`SpellCache`, ``<index>/lexical/spell-cache/``): each
generation's language map (the ``lang`` column of every part, read once per generation, not once per
night) and each vocabulary's counts, kept under a key of everything its sample is drawn from (the
generations' files, their row groups, the settings). The sample is deterministic, so a kept
vocabulary is the one a recount would make; only the languages whose sources changed are read again.
2026-10-22: the nightly rebuild read every language of every source again, 8 hours on the NAS.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import math
import mmap
import os
import time
import unicodedata
import zlib
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from functools import cache
from itertools import combinations
from pathlib import Path
from typing import Any, cast

import numpy as np
import numpy.typing as npt
import pyarrow as pa
import pyarrow.compute as _pc
import pyarrow.parquet as pq

from wsp_index.refinery import RefineryReader
from wsp_index.text import language_row_groups, primary_lang

SPELL_FILE = "spelling.bin"
MAGIC = b"WSPSPEL2"
MAGIC_V1 = b"WSPSPEL1"
ANY = "any"

pc: Any = _pc  # the compute stubs are too partial for strict checking; results are cast at use

U64 = npt.NDArray[np.uint64]
U32 = npt.NDArray[np.uint32]

# --- the shared tokenizer ----------------------------------------------------------------------------

_SPLIT = r"[^\pL\pM'\x{2019}]+"
_APOSTROPHES = "'\u2019"
_NO_SPACE = r"^[\p{Han}\p{Hiragana}\p{Katakana}\p{Thai}\p{Lao}\p{Khmer}\p{Myanmar}\p{Tibetan}]"


def normalize_words(words: pa.Array[Any]) -> pa.Array[Any]:
    """Arrow's per-character lower case, final sigma folded to sigma, the right single quote as ``'``.

    Not ``str.casefold`` (which turns "ß" into "ss") and not ``str.lower`` (context-dependent final
    sigma): the query runs through this same function, so both sides always agree.
    """
    low = pc.utf8_lower(words)
    low = pc.replace_substring(low, pattern="\u03c2", replacement="\u03c3")
    return cast("pa.Array[Any]", pc.replace_substring(low, pattern="\u2019", replacement="'"))


def spell_tokens(texts: pa.Array[Any]) -> pa.Array[Any]:
    """Raw word tokens of ``texts`` (case kept): runs of letters, combining marks and apostrophes,
    apostrophes trimmed from both ends ("'hello'" -> "hello", "don't" stays one word)."""
    parts = pc.split_pattern_regex(texts, pattern=_SPLIT)
    flat = pc.list_flatten(parts)
    flat = pc.utf8_trim(flat, characters=_APOSTROPHES)
    return cast("pa.Array[Any]", flat.filter(pc.greater(pc.utf8_length(flat), 0)))


def query_words(query: str) -> list[tuple[str, str]]:
    """``(raw token, normalized word)`` of a query, in order (the query side of :func:`spell_tokens`)."""
    raw = spell_tokens(pa.array([query], type=pa.string()))
    if not len(raw):
        return []
    norm = normalize_words(raw)
    return list(zip(cast(list[str], raw.to_pylist()), cast(list[str], norm.to_pylist()), strict=True))


def iter_words(text: str) -> Iterator[str]:
    """Normalized words of ``text`` (any length)."""
    for _, w in query_words(text):
        yield w


@cache
def script_of(ch: str) -> str:
    """Coarse Unicode script of a letter: the first word of its name (LATIN, CYRILLIC, CJK, ...)."""
    name = unicodedata.name(ch, "")
    head = name.split(" ", 1)[0] if name else ""
    return {"HIRAGANA": "KANA", "KATAKANA": "KANA"}.get(head, head)[:8] or "OTHER"


def word_script(word: str) -> str:
    for ch in word:
        if ch.isalpha():
            return script_of(ch)
    return "OTHER"


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


# --- configuration -----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SpellConfig:
    max_distance: int = 2
    prefix: int = 7
    """Characters of each word that get delete variants (SymSpell's prefix trick)."""
    min_count: int = 3
    """Words seen fewer times are not in the vocabulary (typos in the corpus itself)."""
    min_count_docs: int = 100_000
    """``min_count`` applies from this many documents of a language on; fewer keep every word."""
    max_words: int = 300_000
    """Words kept per language (the most frequent)."""
    min_length: int = 3
    max_length: int = 24
    title_weight: int = 5
    """A word in a title counts this many times (titles name the things people look for)."""
    max_chars: int = 3_000
    """Text characters read per document (the lead carries most of the vocabulary)."""
    max_docs: int = 400_000
    """Documents sampled per language (row groups spread evenly over the language's parts)."""
    min_language_docs: int = 50
    """Languages with fewer documents share one ``any`` vocabulary instead of getting their own."""
    languages: tuple[str, ...] = ()
    """Only these languages (primary subtags); empty: every language with enough documents."""
    batch_size: int = 4096


@dataclass(frozen=True, slots=True)
class VocabStats:
    documents: int
    distinct_tokens: int
    words: int
    deletes: int
    seconds: float
    languages: Mapping[str, int] = field(default_factory=dict[str, int])
    """Words per language vocabulary."""

    def line(self) -> str:
        langs = ", ".join(f"{k} {v:,}" for k, v in sorted(self.languages.items(), key=lambda kv: -kv[1])[:12])
        return (
            f"{self.words:,} words from {self.documents:,} documents ({self.distinct_tokens:,} distinct "
            f"tokens), {self.deletes:,} delete keys in {self.seconds:.1f}s" + (f" [{langs}]" if langs else "")
        )


# --- counting -----------------------------------------------------------------------------------------


def _counts(texts: pa.Array[Any], cfg: SpellConfig) -> pa.Table:
    """``(w, c)`` counts of the normalized words of the right length in ``texts``."""
    flat = spell_tokens(texts)
    lengths = pc.utf8_length(flat)
    ok = pc.and_(pc.greater_equal(lengths, cfg.min_length), pc.less_equal(lengths, cfg.max_length))
    flat = flat.filter(ok)
    flat = flat.filter(pc.invert(pc.match_substring_regex(flat, pattern=_NO_SPACE)))
    if not len(flat):
        return pa.Table.from_arrays([pa.array([], pa.string()), pa.array([], pa.int64())], names=["w", "c"])
    vc = pc.value_counts(normalize_words(flat))
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


def _even(n: int, k: int) -> list[int]:
    """``k`` indexes spread evenly over ``range(n)`` (all of them when ``k >= n``)."""
    if k >= n:
        return list(range(n))
    return sorted({int(i * n / k) for i in range(k)})


def sample_language(
    reader: RefineryReader,
    groups: Sequence[tuple[int, int, int]],
    lang: str,
    max_docs: int,
    columns: Sequence[str],
) -> Iterator[pa.Table]:
    """Rows whose ``lang`` column is ``lang``, about ``max_docs`` of them, from the row groups that hold
    the language (``(part, row group, rows)``, :func:`wsp_index.text.language_row_groups`), spread
    evenly over them. Never the first N rows in storage order."""
    total = sum(n for _, _, n in groups)
    if total == 0:
        return
    share = min(1.0, max_docs / total)
    chosen = [groups[i] for i in _even(len(groups), max(1, math.ceil(len(groups) * share)))]
    files: dict[int, pq.ParquetFile] = {}
    for part, rg, _ in chosen:
        pf = files.get(part)
        if pf is None:
            files.clear()  # chosen is in part order: one file open at a time
            pf = files[part] = pq.ParquetFile(reader.dir / f"part-{part:05d}.parquet")
        table = pf.read_row_group(rg, columns=[*columns, "lang"])  # pyright: ignore[reportUnknownMemberType]
        yield table.filter(pc.equal(table.column("lang"), lang)).select(list(columns))


CACHE_FORMAT = 1
"""Bumped when the counting changes in a way the settings do not show: every kept vocabulary is stale."""


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".partial")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def generation_fingerprint(reader: RefineryReader) -> str:
    """The generation's directory and each part's size and modification time: a refine (again)
    writes new parts, so a changed generation gets a new fingerprint."""
    h = hashlib.sha256(str(reader.dir.resolve()).encode())
    for path in reader.parts:
        st = path.stat()
        h.update(f"{path.name}:{st.st_size}:{st.st_mtime_ns};".encode())
    return h.hexdigest()[:32]


class SpellCache:
    """What a vocabulary rebuild can reuse (see the module notes): ``groups/<fingerprint>.json``
    (a generation's language map) and ``vocab/<key>.parquet`` (one vocabulary's counts)."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.used: set[Path] = set()
        self.kept = 0

    def row_groups(self, reader: RefineryReader) -> tuple[str, dict[str, list[tuple[int, int, int]]]]:
        fp = generation_fingerprint(reader)
        path = self.root / "groups" / f"{fp}.json"
        self.used.add(path)
        try:
            data = cast(dict[str, list[list[int]]], json.loads(path.read_text(encoding="utf-8")))
            return fp, {lang: [(a, b, c) for a, b, c in rgs] for lang, rgs in data.items()}
        except (OSError, ValueError, TypeError):
            pass
        groups = language_row_groups(reader)
        _write_atomic(path, json.dumps(groups, separators=(",", ":")).encode())
        return fp, groups

    def counts(self, key: str) -> tuple[dict[str, int], int, int] | None:
        """``(words, documents read, distinct tokens)`` kept under ``key``, or ``None``."""
        path = self.root / "vocab" / f"{key}.parquet"
        self.used.add(path)
        try:
            table = pq.read_table(path)  # pyright: ignore[reportUnknownMemberType]
            meta = json.loads((table.schema.metadata or {})[b"wsp"])
            words = cast(list[str], table.column("w").to_pylist())
            values = cast(list[int], table.column("c").to_pylist())
            return dict(zip(words, values, strict=True)), int(meta["docs"]), int(meta["distinct"])
        except (OSError, ValueError, KeyError, TypeError, pa.ArrowException):
            return None

    def save(self, key: str, words: list[str], values: list[int], docs: int, distinct: int) -> None:
        table = pa.Table.from_arrays(
            [pa.array(words, pa.string()), pa.array(values, pa.int64())],
            names=["w", "c"],
            metadata={"wsp": json.dumps({"docs": docs, "distinct": distinct})},
        )
        path = self.root / "vocab" / f"{key}.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".partial")
        pq.write_table(table, tmp)  # pyright: ignore[reportUnknownMemberType]
        os.replace(tmp, path)

    def prune(self) -> None:
        """Remove what this rebuild did not use (old generations, languages whose sources changed)."""
        for folder in ("groups", "vocab"):
            for path in (self.root / folder).glob("*"):
                if path not in self.used:
                    with contextlib.suppress(OSError):
                        path.unlink()


def _vocab_key(
    cfg: SpellConfig, langs: Sequence[str], sources: Mapping[str, Sequence[tuple[str, str, Sequence[Any]]]]
) -> str:
    settings = (
        cfg.min_count,
        cfg.min_count_docs,
        cfg.max_words,
        cfg.min_length,
        cfg.max_length,
        cfg.title_weight,
        cfg.max_chars,
        cfg.max_docs,
    )
    spec = [CACHE_FORMAT, settings, [(lang, [list(x) for x in sources[lang]]) for lang in langs]]
    return hashlib.sha256(json.dumps(spec, separators=(",", ":")).encode()).hexdigest()[:32]


def count_words(
    readers: Sequence[tuple[str, RefineryReader]],
    cfg: SpellConfig = SpellConfig(),  # noqa: B008 - immutable
    progress: Callable[[str], None] | None = None,
    cache: SpellCache | None = None,
) -> tuple[dict[str, dict[str, int]], dict[str, int], int]:
    """``({lang: {word: count}}, {lang: documents read}, distinct tokens)`` over titles and leads.

    One vocabulary per language with at least ``min_language_docs`` documents; the rest share the
    ``any`` vocabulary. Each language is sampled from row groups spread over its own parts. With a
    ``cache``, a vocabulary whose sample would be drawn from the same files is reused, not recounted.
    """
    plan: dict[str, list[tuple[RefineryReader, str, list[tuple[int, int, int]]]]] = {}
    fingerprints: dict[str, list[tuple[str, str, list[tuple[int, int, int]]]]] = {}
    for _source, reader in readers:
        fp, by_lang = cache.row_groups(reader) if cache is not None else ("", language_row_groups(reader))
        for raw, rgs in by_lang.items():
            plan.setdefault(primary_lang(raw), []).append((reader, raw, rgs))
            fingerprints.setdefault(primary_lang(raw), []).append((fp, raw, rgs))
    wanted = {primary_lang(x) for x in cfg.languages}
    sizes = {lang: sum(n for _, _, rgs in srcs for _, _, n in rgs) for lang, srcs in plan.items()}
    groups: dict[str, list[str]] = {}
    for lang in sorted(plan):
        if wanted and lang not in wanted:
            continue
        groups.setdefault(lang if sizes[lang] >= cfg.min_language_docs and lang != ANY else ANY, []).append(
            lang
        )
    out: dict[str, dict[str, int]] = {}
    docs_by_lang: dict[str, int] = {}
    distinct = 0
    for vocab, langs in sorted(groups.items()):
        available = sum(sizes[lang] for lang in langs)
        key = _vocab_key(cfg, langs, fingerprints) if cache is not None else ""
        kept = cache.counts(key) if cache is not None else None
        if kept is not None:
            out[vocab], docs_by_lang[vocab], seen = kept
            distinct += seen
            cache.kept += 1  # pyright: ignore[reportOptionalMemberAccess]  # kept implies a cache
            if progress:
                progress(f"spell: {vocab}: unchanged, {len(out[vocab]):,} words kept")
            continue
        counter = _Counter()
        docs = 0
        for lang in langs:
            for reader, raw, rgs in plan[lang]:
                share = sum(n for _, _, n in rgs) / available
                budget = max(1, round(cfg.max_docs * share))
                for table in sample_language(reader, rgs, raw, budget, ("title", "text")):
                    titles = table.column(0).combine_chunks()
                    texts = table.column(1).combine_chunks()
                    lead = pc.utf8_slice_codeunits(texts, 0, cfg.max_chars)
                    counter.add(_counts(titles, cfg), cfg.title_weight)
                    counter.add(_counts(cast("pa.Array[Any]", lead), cfg))
                    docs += table.num_rows
        counter.compact()
        acc = counter.acc
        if acc is None or acc.num_rows == 0:
            continue
        seen = acc.num_rows
        distinct += seen
        floor = cfg.min_count if docs >= cfg.min_count_docs else 1
        acc = acc.filter(pc.greater_equal(acc.column("c"), floor))
        counts = cast("npt.NDArray[np.int64]", acc.column("c").to_numpy())
        if len(counts) > cfg.max_words:
            keep = np.argpartition(-counts, cfg.max_words - 1)[: cfg.max_words]
            acc = acc.take(pa.array(np.sort(keep).astype(np.int64), type=pa.int64()))  # pyright: ignore[reportArgumentType]
        words = cast(list[str], acc.column("w").to_pylist())
        values = cast(list[int], acc.column("c").to_pylist())
        out[vocab] = dict(zip(words, values, strict=True))
        docs_by_lang[vocab] = docs
        if cache is not None:
            cache.save(key, words, values, docs, seen)
        if progress:
            progress(f"spell: {vocab}: {docs:,} of {available:,} documents sampled, {len(words):,} words")
    return out, docs_by_lang, distinct


# --- the file -----------------------------------------------------------------------------------------


def _section(counts: Mapping[str, int], cfg: SpellConfig, floor: int) -> tuple[bytes, int]:
    words = sorted(counts, key=word_hash)
    n = len(words)
    hashes = np.fromiter((word_hash(w) for w in words), dtype=np.uint64, count=n)
    freq = np.fromiter((min(counts[w], 2**32 - 1) for w in words), dtype=np.uint32, count=n)
    blobs = [w.encode("utf-8") for w in words]
    offsets = np.zeros(n + 1, dtype=np.uint64)
    if blobs:
        offsets[1:] = np.cumsum(np.fromiter((len(b) for b in blobs), dtype=np.uint64, count=n))
    blob = b"".join(blobs)
    del blobs
    # the delete index, straight into numpy (no Python lists of tens of millions of ints)
    per_word = [deletes(w, cfg.max_distance, cfg.prefix) for w in words]
    sizes = np.fromiter((len(v) for v in per_word), dtype=np.int64, count=n)
    m = int(sizes.sum())
    dh = np.fromiter((word_hash(v) for vs in per_word for v in vs), dtype=np.uint64, count=m)
    del per_word
    dw = np.repeat(np.arange(n, dtype=np.uint32), sizes)
    order = np.argsort(dh, kind="stable")
    dh, dw = dh[order], dw[order]
    total = int(freq.sum(dtype=np.uint64))
    head = np.array([n, m, len(blob), floor, total], dtype=np.uint64)
    body = b"".join(
        [
            head.tobytes(),
            hashes.tobytes(),
            offsets.tobytes(),
            dh.tobytes(),
            freq.tobytes(),
            dw.tobytes(),
            blob,
        ]
    )
    return body, m


def _pad(n: int) -> int:
    return (-n) % 8


def _vocab_script(counts: Mapping[str, int]) -> str:
    """The script most of a vocabulary's frequent words are written in."""
    top = sorted(counts.items(), key=lambda kv: -kv[1])[:500]
    tally: dict[str, int] = {}
    for w, _ in top:
        s = word_script(w)
        tally[s] = tally.get(s, 0) + 1
    return max(tally.items(), key=lambda kv: kv[1])[0] if tally else "OTHER"


def encode_vocabularies(
    vocabs: Mapping[str, Mapping[str, int]],
    cfg: SpellConfig = SpellConfig(),  # noqa: B008 - immutable
    floors: Mapping[str, int] | None = None,
) -> tuple[bytes, int]:
    """The ``WSPSPEL2`` file's bytes and its number of delete keys."""
    langs = sorted(vocabs)
    sections: list[tuple[str, str, bytes]] = []
    n_del = 0
    for lang in langs:
        floor = (floors or {}).get(lang, cfg.min_count)
        body, m = _section(vocabs[lang], cfg, max(1, floor))
        sections.append((lang, _vocab_script(vocabs[lang]), body))
        n_del += m
    table_size = 8 + 8 + 32 * len(sections)
    offset = table_size + _pad(table_size)
    entries: list[bytes] = []
    for lang, script, body in sections:
        entries.append(
            lang.encode("ascii", "replace")[:8].ljust(8, b"\0")
            + script.encode("ascii", "replace")[:8].ljust(8, b"\0")
            + np.array([offset, len(body)], dtype=np.uint64).tobytes()
        )
        offset += len(body) + _pad(len(body))
    out = [MAGIC, np.uint64(len(sections)).tobytes(), *entries, b"\0" * _pad(table_size)]
    for _, _, body in sections:
        out += [body, b"\0" * _pad(len(body))]
    return b"".join(out), n_del


def encode_vocabulary(counts: Mapping[str, int], cfg: SpellConfig = SpellConfig()) -> tuple[bytes, int]:  # noqa: B008
    """A single-vocabulary file (``any`` language, every script), as for tests and samples."""
    return encode_vocabularies({ANY: counts}, cfg, {ANY: cfg.min_count})


def write_vocabularies(
    path: Path,
    vocabs: Mapping[str, Mapping[str, int]],
    cfg: SpellConfig = SpellConfig(),  # noqa: B008 - immutable
    floors: Mapping[str, int] | None = None,
) -> int:
    """Write the vocabularies and their delete indexes atomically; returns the number of delete keys."""
    data, n_del = encode_vocabularies(vocabs, cfg, floors)
    tmp = path.with_name(path.name + ".partial")
    path.parent.mkdir(parents=True, exist_ok=True)
    with tmp.open("wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    return n_del


def write_vocabulary(path: Path, counts: Mapping[str, int], cfg: SpellConfig = SpellConfig()) -> int:  # noqa: B008
    return write_vocabularies(path, {ANY: counts}, cfg, {ANY: cfg.min_count})


def build_vocabulary(
    readers: Sequence[tuple[str, RefineryReader]],
    path: Path,
    cfg: SpellConfig = SpellConfig(),  # noqa: B008 - immutable
    progress: Callable[[str], None] | None = None,
    cache: SpellCache | None = None,
) -> VocabStats:
    t0 = time.monotonic()
    vocabs, docs, distinct = count_words(readers, cfg, progress, cache)
    if cache is not None and not cfg.languages:  # a partial rebuild must not drop the other languages
        cache.prune()
    floors = {lang: (cfg.min_count if n >= cfg.min_count_docs else 1) for lang, n in docs.items()}
    n_del = write_vocabularies(path, vocabs, cfg, floors)
    return VocabStats(
        documents=sum(docs.values()),
        distinct_tokens=distinct,
        words=sum(len(v) for v in vocabs.values()),
        deletes=n_del,
        seconds=time.monotonic() - t0,
        languages={lang: len(v) for lang, v in vocabs.items()},
    )


# --- correcting ---------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Correction:
    word: str
    candidate: str
    distance: int
    count: int


class Vocabulary:
    """One language's vocabulary over aligned views of a buffer."""

    def __init__(
        self,
        lang: str,
        script: str,
        buf: npt.NDArray[np.uint8],
        *,
        version: int = 2,
        max_distance: int = 2,
        prefix: int = 7,
        epsilon: float = 0.01,
        min_ratio: float = 10.0,
    ) -> None:
        self.lang, self.script = lang, script
        if version == 1:
            n, blob_len, m = (int(x) for x in np.frombuffer(buf[8:32].tobytes(), dtype=np.uint64))
            off = 32
            hash_off = off
            count_off = off + 8 * n
            offs_off = count_off + 4 * n
            blob_off = offs_off + 8 * (n + 1)
            dh_off = blob_off + blob_len
            dw_off = dh_off + 8 * m
            expected = dw_off + 4 * m
            floor, total = 1, 0
            if len(buf) != expected:
                raise ValueError(f"truncated spelling vocabulary ({len(buf)} bytes, want {expected})")
            # version 1 may be misaligned: copy the arrays once (the blob stays a view)
            self._hash = np.frombuffer(buf[hash_off:count_off].tobytes(), dtype=np.uint64)
            self._count = np.frombuffer(buf[count_off:offs_off].tobytes(), dtype=np.uint32)
            self._offsets = np.frombuffer(buf[offs_off:blob_off].tobytes(), dtype=np.uint64)
            self._del_hash = np.frombuffer(buf[dh_off:dw_off].tobytes(), dtype=np.uint64)
            self._del_word = np.frombuffer(buf[dw_off:expected].tobytes(), dtype=np.uint32)
            self._blob = buf[blob_off:dh_off]
        else:
            if len(buf) < 40:
                raise ValueError("truncated spelling vocabulary section")
            n, m, blob_len, floor, total = (int(x) for x in buf[:40].view(np.uint64))
            off = 40
            self._hash = buf[off : off + 8 * n].view(np.uint64)
            off += 8 * n
            self._offsets = buf[off : off + 8 * (n + 1)].view(np.uint64)
            off += 8 * (n + 1)
            self._del_hash = buf[off : off + 8 * m].view(np.uint64)
            off += 8 * m
            self._count = buf[off : off + 4 * n].view(np.uint32)
            off += 4 * n
            self._del_word = buf[off : off + 4 * m].view(np.uint32)
            off += 4 * m
            self._blob = buf[off : off + blob_len]
            if off + blob_len != len(buf):
                raise ValueError(f"spelling vocabulary section of {len(buf)} bytes, want {off + blob_len}")
        self.size = n
        self.floor = max(1, floor)
        self.total = total or int(self._count.sum(dtype=np.uint64)) or 1
        self.max_distance = max_distance
        self.prefix = prefix
        self.log_eps = float(np.log(epsilon))
        self.log_total = math.log(self.total)
        # A vocabulary that kept every word (floor 1: a small library) has no evidence that a rare
        # neighbour is junk: any word seen at all may replace an unknown one at one edit.
        ratio = min_ratio if self.floor > 1 else min(min_ratio, 0.5)
        self.unknown_score = math.log(self.floor * max(1e-6, ratio)) + self.log_eps

    def __len__(self) -> int:
        return self.size

    def hot_arrays(self) -> tuple[npt.NDArray[np.uint64], ...]:
        """The arrays every lookup binary-searches (the word hashes, the deletion hashes): what a
        warm-up reads ahead. The word blob is not among them."""
        return (self._hash, self._del_hash)

    def _word(self, i: int) -> str:
        a, b = int(self._offsets[i]), int(self._offsets[i + 1])
        return bytes(self._blob[a:b]).decode("utf-8")

    def count(self, word: str) -> int:
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
        width = max(len(ch.encode("utf-8")) for ch in word)  # 1 Latin, 2 Cyrillic, 3 Devanagari ...
        ids = ids[np.abs(nbytes - wlen) <= max(2, width) * limit]
        return ids[np.argsort(-self._count[ids].astype(np.int64), kind="stable")]

    def limit(self, word: str) -> int:
        return self.max_distance if len(word) > 4 else min(1, self.max_distance)

    def cost(self, word: str, cand: str, dist: int) -> float:
        """Error-model log probability: ``epsilon`` per edit, one more when the first letter differs
        (people rarely mistype it: "lisbn" is Lisbon, not ISBN)."""
        extra = 1 if word[:1] != cand[:1] else 0
        return (dist + extra) * self.log_eps

    def candidates(self, word: str) -> list[tuple[str, int, int]]:
        limit = self.limit(word)
        out: list[tuple[str, int, int]] = []
        for i in self._candidate_ids(word, limit).tolist():
            cand = self._word(i)
            dist = osa_distance(word, cand, limit)
            if dist <= limit:
                out.append((cand, dist, int(self._count[i])))
        return out

    def best(self, word: str) -> tuple[Correction | None, float]:
        """The noisy-channel choice for one normalized word (None: keep it) and the log probability
        of the word under this vocabulary (its own count, or its best correction's, minus the edits).

        A known word is replaced only by a word ``1 / epsilon`` times more frequent per edit. An
        unknown word is replaced only by a word at least ``min_ratio * floor`` times frequent at one
        edit (30 occurrences in the sample with the defaults; 3,000 at two edits): "zzq" is not
        "zzz" (7 occurrences in 222k English articles), "insulen" is "insulin" (114); words under
        five letters need ten times more. An unknown word without a correction scores as a word
        three edits from a ``floor``-count word.
        """
        own = self.count(word)
        known = own > 0
        best_score = float(np.log(own)) if known else self.unknown_score
        if not known and len(word) < 5:
            best_score += math.log(10.0)  # short words have many neighbours: "zzq" is not "zzr"
        choice: Correction | None = None
        limit = self.limit(word)
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
            score = float(np.log(count)) + self.cost(word, cand, dist)
            if score > best_score:
                best_score = score
                choice = Correction(word, cand, dist, count)
        if choice is None and not known:
            best_score = float(np.log(self.floor)) + 3 * self.log_eps
        return choice, best_score - self.log_total


def _display(candidate: str, raw: str) -> str:
    """The candidate in the raw word's capitalization; Greek final sigma restored."""
    if candidate.endswith("\u03c3") and script_of(candidate[0]) == "GREEK":
        candidate = candidate[:-1] + "ς"
    if raw.isupper() and len(raw) > 1:
        return candidate.upper()
    if raw[:1].isupper():
        return candidate[:1].upper() + candidate[1:]
    return candidate


class Speller:
    """Noisy-channel corrector over a vocabulary file (or in-memory counts, for tests and samples)."""

    def __init__(
        self,
        buf: npt.NDArray[np.uint8],
        *,
        max_distance: int = 2,
        prefix: int = 7,
        epsilon: float = 0.01,
        min_ratio: float = 10.0,
        prefer: Sequence[str] = ("en",),
        prefer_weight: float = 100.0,
        fuzzy_min_words: int = 20_000,
        fuzzy_languages: int = 12,
    ) -> None:
        """``prefer``: languages assumed ``prefer_weight`` times likelier a priori (the interface
        language); it only matters when the words themselves do not tell (all unknown everywhere).
        Then only the preferred languages are searched for corrections, or, with none of the script,
        at most ``fuzzy_languages`` vocabularies of at least ``fuzzy_min_words`` words (:meth:`choose`)."""
        kw: dict[str, Any] = {
            "max_distance": max_distance,
            "prefix": prefix,
            "epsilon": epsilon,
            "min_ratio": min_ratio,
        }
        self.prefer = frozenset(prefer)
        self.log_prefer = math.log(max(1.0, prefer_weight))
        self.fuzzy_min_words = fuzzy_min_words
        self.fuzzy_languages = fuzzy_languages
        head = bytes(buf[:8])
        self.vocabs: list[Vocabulary] = []
        if head == MAGIC_V1:
            self.vocabs.append(Vocabulary(ANY, ANY.upper(), buf, version=1, **kw))
        elif head == MAGIC:
            if len(buf) < 16:
                raise ValueError("truncated spelling vocabulary")
            k = int(np.frombuffer(buf[8:16].tobytes(), dtype=np.uint64)[0])
            table_end = 16 + 32 * k
            if len(buf) < table_end:
                raise ValueError("truncated spelling vocabulary table")
            end = table_end + _pad(table_end)
            for i in range(k):
                e = buf[16 + 32 * i : 16 + 32 * (i + 1)].tobytes()
                lang = e[:8].rstrip(b"\0").decode("ascii")
                script = e[8:16].rstrip(b"\0").decode("ascii")
                off, length = (int(x) for x in np.frombuffer(e[16:32], dtype=np.uint64))
                if off % 8 or off + length > len(buf):
                    raise ValueError(f"spelling vocabulary section {lang!r} out of bounds")
                self.vocabs.append(Vocabulary(lang, script, buf[off : off + length], **kw))
                end = max(end, off + length + _pad(length))
            if end != len(buf):
                raise ValueError(f"spelling vocabulary of {len(buf)} bytes, want {end}")
        else:
            raise ValueError("not a spelling vocabulary")
        self.size = sum(len(v) for v in self.vocabs)
        self.max_distance = max_distance
        self.prefix = prefix

    @classmethod
    def open(cls, path: Path, **kw: Any) -> Speller | None:
        try:
            if not path.is_file():
                return None
            with path.open("rb") as fh:
                mm = mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ)
            if hasattr(mmap, "MADV_RANDOM"):
                mm.madvise(mmap.MADV_RANDOM)
            buf = np.frombuffer(mm, dtype=np.uint8)
            speller = cls(buf, **kw)
            speller._mm = mm
            speller._base = int(buf.ctypes.data)
            speller.warm()
            return speller
        except (OSError, ValueError):
            return None

    _mm: mmap.mmap | None = None
    _base: int = 0

    def warm(self) -> int:
        """Ask the kernel to read the hash arrays ahead (``MADV_WILLNEED``): a binary search over a cold
        array is dozens of page faults, and "did you mean" cost 0.1-0.3 s a query on a fresh process
        (0.01-0.05 s warm; 2026-10-14). The word blob stays cold: only a correction reads it.
        Returns the bytes advised (0 for an in-memory speller)."""
        mm = self._mm
        if mm is None or not hasattr(mmap, "MADV_WILLNEED"):
            return 0
        page = mmap.PAGESIZE
        total = 0
        for v in self.vocabs:
            for arr in v.hot_arrays():
                raw = arr.view(np.uint8)
                off = int(raw.ctypes.data) - self._base
                if raw.nbytes == 0 or off < 0 or off + raw.nbytes > len(mm):
                    continue  # a version-1 file's arrays are copies, not views of the mapping
                start = off - off % page
                with contextlib.suppress(OSError, ValueError):
                    mm.madvise(mmap.MADV_WILLNEED, start, raw.nbytes + (off - start))
                    total += raw.nbytes
        return total

    @classmethod
    def from_counts(cls, counts: Mapping[str, int], **kw: Any) -> Speller:
        """An in-memory single-vocabulary speller (tests, the sample library)."""
        return cls.from_vocabularies({ANY: counts}, **kw)

    @classmethod
    def from_vocabularies(cls, vocabs: Mapping[str, Mapping[str, int]], **kw: Any) -> Speller:
        """An in-memory speller with one vocabulary per language (``{lang: {word: count}}``).

        Small in-memory vocabularies (tests, the sample library) default to ``min_ratio=0.5``: their
        counts are too small for the library default of 10: here a word seen once beats an
        unknown word at one edit."""
        kw.setdefault("min_ratio", 0.5)
        cfg = SpellConfig(max_distance=kw.get("max_distance", 2), prefix=kw.get("prefix", 7), min_count=1)
        floors = kw.pop("floors", None)
        norm: dict[str, dict[str, int]] = {}
        for lang, counts in vocabs.items():
            words = list(counts)
            folded = (
                cast(list[str], normalize_words(pa.array(words, type=pa.string())).to_pylist())
                if words
                else []
            )
            merged: dict[str, int] = {}
            for w, f in zip(folded, words, strict=True):
                merged[w] = merged.get(w, 0) + counts[f]
            norm[lang] = merged
        data, _ = encode_vocabularies(norm, cfg, floors or dict.fromkeys(norm, 1))
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

    @property
    def languages(self) -> list[str]:
        return [v.lang for v in self.vocabs]

    # --- vocabulary choice ---

    def _for_script(self, script: str) -> list[Vocabulary]:
        exact = [v for v in self.vocabs if v.script == script]
        return exact or [v for v in self.vocabs if v.script in (ANY.upper(), "OTHER")]

    def choose(self, words: Sequence[str]) -> Vocabulary | None:
        """The vocabulary under which these normalized words are most probable.

        The script of most words picks the candidates (Latin, Cyrillic, ...). Among them, the ones that
        know the most of the words, by the sum of those words' log probabilities plus the prior of
        ``prefer``. Only when no candidate knows any word does each word's best correction score it
        (:meth:`Vocabulary.best`): that fuzzy search costs random reads all over a multi-GB file, and
        scoring every language with it held an ordinary query for 1-10 s (2026-10-13)."""
        usable = [w for w in words if len(w) >= 3]
        if not self.vocabs:
            return None
        if not usable:
            return self.vocabs[0] if len(self.vocabs) == 1 else None
        scripts: dict[str, int] = {}
        for w in usable:
            s = word_script(w)
            scripts[s] = scripts.get(s, 0) + 1
        script = max(scripts.items(), key=lambda kv: kv[1])[0]
        pool = self._for_script(script)
        if len(pool) <= 1:
            return pool[0] if pool else None
        counts = [[v.count(w) for w in usable] for v in pool]
        known = [sum(1 for c in cs if c) for cs in counts]
        most = max(known)
        if not most:
            # Unknown everywhere: each language would be scored by how close its best corrections
            # come, a fuzzy search per word and language. Only the preferred languages are searched
            # (else the largest vocabularies): a tiny one (Lezgian's 338 words) scores an unknown word
            # as likely, so it won and nothing was corrected, searching all 322 held "hypertenshun
            # medicashun" for 4-11 s, and Polish offered "zza" for "zzq" (2026-10-17). A language
            # other than the preferred ones corrects a query only when it knows one of its words.
            keep = [i for i, v in enumerate(pool) if v.lang in self.prefer]
            if not keep:
                big = sorted(range(len(pool)), key=lambda i: -len(pool[i]))
                keep = [i for i in big if len(pool[i]) >= self.fuzzy_min_words][: self.fuzzy_languages]
            if not keep:
                return None
            pool, counts, known = [pool[i] for i in keep], [counts[i] for i in keep], [known[i] for i in keep]
        best: tuple[float, int, Vocabulary] | None = None
        for v, cs, k in zip(pool, counts, known, strict=True):
            if k < most:
                continue
            score = self.log_prefer if v.lang in self.prefer else 0.0
            for w, c in zip(usable, cs, strict=True):
                if c:
                    score += math.log(c) - v.log_total
                elif not most:  # unknown everywhere: how close each language comes decides
                    score += v.best(w)[1]
            key = (score, len(v), v)
            if best is None or key[:2] > best[:2]:
                best = key
        if best is not None and most and best[2].lang not in self.prefer:
            # A language that merely knows more of the words (Latin has "diabetis", and some Latin
            # book "insulen") is weighed against the preferred one read with its corrections
            # ("diabetes insulin": far more probable): "diabetis insulen" offered nothing (2026-10-17).
            # Fuzzy searches for the preferred languages only, and only then.
            for v, cs in zip(pool, counts, strict=True):
                if v.lang not in self.prefer:
                    continue
                score = self.log_prefer + sum(
                    math.log(c) - v.log_total if c else v.best(w)[1] for w, c in zip(usable, cs, strict=True)
                )
                if score > best[0]:
                    best = (score, len(v), v)
        return best[2] if best else None

    # --- the port ---

    def count(self, word: str, lang: str | None = None) -> int:
        """How often ``word`` (normalized) occurs; the largest count over vocabularies of its script
        unless ``lang`` names one; 0 if no vocabulary knows it."""
        if lang is not None:
            return next((v.count(word) for v in self.vocabs if v.lang == lang), 0)
        return max((v.count(word) for v in self._for_script(word_script(word))), default=0)

    def best(self, word: str) -> Correction | None:
        """The noisy-channel choice for one normalized word in its vocabulary, or None to keep it."""
        v = self.choose([word])
        return None if v is None else v.best(word)[0]

    def candidates(self, word: str) -> list[tuple[str, int, int]]:
        v = self.choose([word])
        return [] if v is None else v.candidates(word)

    def correct(self, query: str, *, only: Iterable[str] | None = None) -> str | None:
        """The query with misspelled words replaced, or None when nothing changes.

        ``only`` restricts corrections to these words (normalized like the query). Digits, short
        words and anything that is not a letter run are left alone; capitalization is kept.
        """
        pairs = query_words(query)
        if not pairs:
            return None
        vocab = self.choose([w for _, w in pairs])
        if vocab is None:
            return None
        allowed = None if only is None else {w for _, w in query_words(" ".join(only))}
        out: list[str] = []
        pos = 0
        changed = False
        for raw, word in pairs:
            at = query.find(raw, pos)
            if at < 0:  # cannot happen: tokens are substrings of the query; keep it safe anyway
                return None
            out.append(query[pos:at])
            pos = at + len(raw)
            fix = None
            if len(word) >= 3 and (allowed is None or word in allowed):
                fix = vocab.best(word)[0]
            if fix is None:
                out.append(raw)
            else:
                out.append(_display(fix.candidate, raw))
                changed = True
        out.append(query[pos:])
        fixed = "".join(out)
        return fixed if changed and fixed != query else None

    def unknown(self, query: str) -> list[str]:
        """Normalized words of the query (3+ letters) that its vocabulary does not know."""
        words = [w for _, w in query_words(query)]
        vocab = self.choose(words)
        if vocab is None:
            return []
        return [w for w in words if len(w) >= 3 and not vocab.count(w)]


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
