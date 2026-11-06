"""Title autocomplete (PLAN §9.3: < 10 ms).

Structure (chosen by ``scripts/bench_index.py``; see ``docs/components/index.md``):

* A **MARISA trie** (``marisa-trie``, mmapped) holds one key per title or alias,
  ``"<normalized>\\x1f<title>\\x1f<alias>\\x1f<lang>\\x1f<doc_id>"`` (``<alias>`` empty on the title's
  own key) with a float32 score record. Prefix enumeration is a trie walk; the trie is several times
  smaller than a Python list of the same keys. Builds before 2026-11-05 wrote
  ``"<normalized>\\x1f<title>\\x1f<doc_id>"`` and no ``FORMAT`` file; they are still read.
* Short prefixes ("a", "the ") match millions of keys, so enumerating and sorting them would blow
  the budget. At build time every prefix whose completion set exceeds ``heavy_threshold`` gets its
  top-``top_k`` keys precomputed (a second trie). Each depth partitions the keys, so there are at most
  ``depth * n / heavy_threshold`` such prefixes: small.
* Other prefixes have at most ``heavy_threshold`` completions, enumerated and sorted on the fly.
* A small in-memory **overlay** absorbs incremental upserts/deletes between rebuilds.

Ranking: ``score = prior + 1 / (1 + len(title))``. ``prior`` is a popularity signal in ``[0, 1]`` (the
static rank's feature); the length term prefers "Paris" over "Paris Commune" when priors tie. An alias
key scores :data:`ALIAS_PENALTY` lower, so a title that starts with what was typed comes before a
document that only an alias (a redirect, another language's label) matched; an alias typed in full
ranks with the titles again. Results in the reader's language (``lang``) come before others, and an
alias match shows the alias, not the title: typing "pho" in the English Navigator listed 磷, 摄影 and
音位 (their English labels matched), and picking one searched for "磷" (2026-11-05 deep dive).

On disk each build is a version directory (``v000007/`` with both tries) named by the ``CURRENT``
pointer, which a build replaces atomically once the version is complete: a reader never pairs a new
titles trie with an old heavy-prefix trie (the two files used to be replaced one after the other).
The previous version is kept for readers that read the pointer just before it moved.
"""

from __future__ import annotations

import bisect
import heapq
import itertools
import os
import re
import shutil
import sys
import threading
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple, Protocol, cast

import marisa_trie  # pyright: ignore[reportMissingTypeStubs]  # untyped C extension; wrapped below
import numpy as np

from wsp_core.ids import DocId
from wsp_core.model import Suggestion
from wsp_index.text import normalize_key, primary_lang

SEP = "\x1f"
_MAX_CHAR = chr(sys.maxunicode)  # U+10FFFF, a noncharacter some titles do contain
_LIST_SEP = "\x1e"
TRIE_FILE = "titles.marisa"
HEAVY_FILE = "heavy.marisa"
POINTER_FILE = "CURRENT"
FORMAT_FILE = "FORMAT"  # the key layout of a build; absent in builds before 2026-11-05 (layout 1)
KEY_FORMAT = 2
_VERSION = re.compile(r"^v(\d{6,})$")
DEFAULT_HEAVY_THRESHOLD = 512
_MAX_SCAN = 4_096  # completions examined past a heavy prefix's precomputed top list
DEFAULT_TOP_K = 32
# Above any title key's score (a prior in [0, 1] plus a length term of at most 0.5): every alias key
# ranks below every title key in the precomputed top lists and in the scan.
ALIAS_PENALTY = 2.0
POOL_FACTOR = 4  # candidates read per result wanted, then ranked by language and match
# ... at most a heavy prefix's precomputed list (unless ``limit`` is more): one more read scans
# the prefix's first _MAX_SCAN keys, on every keystroke.
_POOL_MAX = DEFAULT_TOP_K
_EXACT_MAX = 64  # keys equal to what was typed, looked up besides the prefix's best
_NEUTRAL_LANGS = frozenset({"", "und", "mul", "zxx"})  # no language: never "foreign"


class _RecordTrie(Protocol):
    def items(self, prefix: str = "") -> list[tuple[str, tuple[float]]]: ...

    def __getitem__(self, key: str) -> list[tuple[float]]: ...

    def __len__(self) -> int: ...

    def save(self, path: str) -> None: ...

    def iterkeys(self, prefix: str = ...) -> Iterator[str]: ...


class _BytesTrie(Protocol):
    def get(self, key: str) -> list[bytes] | None: ...

    def save(self, path: str) -> None: ...


def _record_trie(items: Iterable[tuple[str, tuple[float]]]) -> _RecordTrie:
    return cast(_RecordTrie, marisa_trie.RecordTrie("<f", items))  # pyright: ignore[reportUnknownMemberType]  # untyped


def _bytes_trie(items: Iterable[tuple[str, bytes]]) -> _BytesTrie:
    return cast(_BytesTrie, marisa_trie.BytesTrie(items))  # pyright: ignore[reportUnknownMemberType]  # untyped


def _mmap_record_trie(path: Path) -> _RecordTrie:
    return cast(_RecordTrie, marisa_trie.RecordTrie("<f").mmap(str(path)))  # pyright: ignore[reportUnknownMemberType]  # untyped


def _mmap_bytes_trie(path: Path) -> _BytesTrie:
    return cast(_BytesTrie, marisa_trie.BytesTrie().mmap(str(path)))  # pyright: ignore[reportUnknownMemberType]  # untyped


@dataclass(frozen=True, slots=True)
class SuggestEntry:
    """One suggestible document: its display title plus optional aliases, a popularity prior and its
    language (BCP-47 primary subtag; empty when unknown)."""

    doc_id: str
    title: str
    aliases: tuple[str, ...] = ()
    prior: float = 0.0
    lang: str = ""

    @property
    def score(self) -> float:
        return entry_score(self.prior, self.title)

    def keyed(self) -> list[tuple[str, str]]:
        """``(normalized key, alias)`` per match key, the title's first with alias ``""``; deduplicated
        (an alias that normalizes like the title or an earlier alias is dropped), empty keys dropped."""
        seen: dict[str, str] = {}
        for i, raw in enumerate((self.title, *self.aliases)):
            key = normalize_key(raw)
            if key:
                seen.setdefault(key, raw if i else "")
        return list(seen.items())

    def match_keys(self) -> list[str]:
        """Normalized match keys (title first), deduplicated, empty ones dropped."""
        return [k for k, _ in self.keyed()]


def entry_score(prior: float, title: str) -> float:
    return prior + 1.0 / (1.0 + len(title))


def key_score(prior: float, title: str, alias: str = "") -> float:
    """The stored score of one key: the title's, or the alias's own (by its length) less
    :data:`ALIAS_PENALTY`."""
    return entry_score(prior, alias) - ALIAS_PENALTY if alias else entry_score(prior, title)


def _field(text: str) -> str:
    return text.replace(SEP, " ")


def _trie_key(norm: str, title: str, alias: str, lang: str, doc_id: str) -> str:
    return SEP.join((norm, _field(title), _field(alias), _field(lang), doc_id))


class Candidate(NamedTuple):
    score: float  # as stored: an alias key's is ALIAS_PENALTY lower
    norm: str  # the normalized key that matched
    title: str
    alias: str  # the alias that matched; "" when the title did
    lang: str
    doc_id: str

    @property
    def text(self) -> str:
        return self.alias or self.title


def _split_key(key: str, score: float, fmt: int) -> Candidate:
    if fmt >= 2:
        norm, title, alias, lang, doc_id = key.split(SEP, 4)
        return Candidate(score, norm, title, alias, lang, doc_id)
    norm, title, doc_id = key.split(SEP, 2)  # layout 1: no alias or language stored
    return Candidate(score, norm, title, "", "", doc_id)


class TitleTrie:
    """Immutable prefix index over titles and aliases, stored as two mmapped MARISA tries."""

    def __init__(self, trie: _RecordTrie, heavy: _BytesTrie, fmt: int = KEY_FORMAT) -> None:
        self._trie = trie
        self._heavy = heavy
        self._fmt = fmt

    def __len__(self) -> int:
        return len(self._trie)

    @classmethod
    def build(
        cls,
        entries: Iterable[SuggestEntry],
        directory: Path,
        *,
        heavy_threshold: int = DEFAULT_HEAVY_THRESHOLD,
        top_k: int = DEFAULT_TOP_K,
    ) -> TitleTrie:
        keys: list[str] = []
        scores_list: list[float] = []
        for e in entries:
            for norm, alias in e.keyed():
                keys.append(_trie_key(norm, e.title, alias, e.lang, e.doc_id))
                scores_list.append(key_score(e.prior, e.title, alias))
        order = sorted(range(len(keys)), key=keys.__getitem__)
        keys = [keys[i] for i in order]
        scores = np.asarray(scores_list, dtype=np.float32)[np.asarray(order, dtype=np.int64)]
        heavy = _heavy_prefixes(keys, scores, heavy_threshold, top_k)

        directory.mkdir(parents=True, exist_ok=True)
        trie = _record_trie(zip(keys, ((float(s),) for s in scores.tolist()), strict=True))
        heavy_trie = _bytes_trie(
            (prefix, _LIST_SEP.join(top).encode("utf-8")) for prefix, top in heavy.items()
        )
        versions = _versions(directory)
        name = f"v{(versions[-1][0] + 1) if versions else 1:06d}"
        target = directory / name
        shutil.rmtree(target, ignore_errors=True)  # a build that died before its pointer moved
        target.mkdir()
        for obj, file in ((trie, TRIE_FILE), (heavy_trie, HEAVY_FILE)):
            obj.save(str(target / file))
            _fsync(target / file)
        (target / FORMAT_FILE).write_text(f"{KEY_FORMAT}\n", encoding="utf-8")
        _fsync(target / FORMAT_FILE)
        _fsync(target)
        partial = directory / f"{POINTER_FILE}.{os.getpid()}.partial"
        partial.write_text(name + "\n", encoding="utf-8")
        _fsync(partial)
        os.replace(partial, directory / POINTER_FILE)
        _fsync(directory)
        previous = [v for _, v in versions if v != name][-1:]
        for _, old in versions:
            if old not in (name, *previous):
                shutil.rmtree(directory / old, ignore_errors=True)
        for legacy in (TRIE_FILE, HEAVY_FILE):  # the flat layout of older builds
            (directory / legacy).unlink(missing_ok=True)
        return cls.open(directory)

    @classmethod
    def open(cls, directory: Path) -> TitleTrie:
        """The current version (a pointer read just before a build moved it and deleted the version it
        named is read again once)."""
        try:
            return cls._open_at(_current(directory))
        except FileNotFoundError:
            return cls._open_at(_current(directory))

    @classmethod
    def _open_at(cls, where: Path) -> TitleTrie:
        trie, heavy = _mmap_record_trie(where / TRIE_FILE), _mmap_bytes_trie(where / HEAVY_FILE)
        try:
            fmt = int((where / FORMAT_FILE).read_text(encoding="utf-8").strip())
        except FileNotFoundError:
            fmt = 1  # written before 2026-11-05
        return cls(trie, heavy, fmt)

    @classmethod
    def exists(cls, directory: Path) -> bool:
        where = _current(directory)
        return (where / TRIE_FILE).is_file() and (where / HEAVY_FILE).is_file()

    @classmethod
    def stamp(cls, directory: Path) -> tuple[int, int, int]:
        """Identity of the current build on disk (each build replaces the pointer); zeros if none."""
        path = directory / POINTER_FILE
        if not path.is_file():
            path = directory / TRIE_FILE  # the flat layout of older builds
        try:
            st = path.stat()
        except OSError:
            return (0, 0, 0)
        return (st.st_ino, st.st_mtime_ns, st.st_size)

    def _score(self, key: str) -> float:
        return self._trie[key][0][0]

    def candidates(self, prefix: str) -> Iterator[Candidate]:
        """Completions of a *normalized* prefix in descending score order (lazy)."""
        packed = self._heavy.get(prefix)
        if packed:
            top = packed[0].decode("utf-8").split(_LIST_SEP)
            for key in top:
                yield _split_key(key, self._score(key), self._fmt)
            # Rarely reached: consumer filtered out more than top_k (deletions, duplicates).
            seen = set(top)
        else:
            seen: set[str] = set()
        # Bounded: a heavy prefix ("a") has millions of completions; enumerating them all on a keystroke
        # took 2 s and 500 MB on 9.6M keys. Light prefixes have at most ``heavy_threshold`` anyway.
        window = (
            (self._score(k), k)
            for k in itertools.islice(self._trie.iterkeys(prefix), _MAX_SCAN)
            if k not in seen
        )
        for score, key in heapq.nsmallest(_MAX_SCAN, window, key=lambda sk: (-sk[0], sk[1])):
            yield _split_key(key, score, self._fmt)

    def exact(self, norm: str) -> list[Candidate]:
        """Keys equal to a *normalized* string (at most :data:`_EXACT_MAX`): an alias typed in full ranks
        with the titles, though a heavy prefix's precomputed list holds titles only."""
        keys = itertools.islice(self._trie.iterkeys(norm + SEP), _EXACT_MAX)
        return [_split_key(k, self._score(k), self._fmt) for k in keys]

    def exact(self, norm: str, limit: int) -> list[Candidate]:
        """Up to ``limit`` entries whose title or alias *is* ``norm`` (normalized), best first: the keys
        ``norm`` + separator, a trie walk as short as the matches."""
        if not norm:
            return []
        keys = itertools.islice(self._trie.iterkeys(norm + SEP), _MAX_SCAN)
        found = [(self._score(k), *_split_key(k)) for k in keys]
        return heapq.nsmallest(limit, found, key=lambda c: (-c[0], c[1], c[2]))


def _current(directory: Path) -> Path:
    """The directory of the current build: the one ``CURRENT`` names, else the flat older layout."""
    try:
        name = (directory / POINTER_FILE).read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return directory
    if not _VERSION.match(name):
        raise ValueError(f"corrupt suggest pointer in {directory}: {name!r}")
    return directory / name


def _versions(directory: Path) -> list[tuple[int, str]]:
    """Build directories, oldest first."""
    found = ((_VERSION.match(p.name), p.name) for p in directory.iterdir() if p.is_dir())
    return sorted((int(m.group(1)), name) for m, name in found if m)


def _fsync(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _heavy_prefixes(
    keys: Sequence[str], scores: np.ndarray[tuple[int], np.dtype[np.float32]], threshold: int, top_k: int
) -> dict[str, list[str]]:
    """Top-``top_k`` keys of every prefix with more than ``threshold`` completions (keys sorted)."""
    out: dict[str, list[str]] = {}
    stack: list[tuple[str, int, int]] = [("", 0, len(keys))]
    while stack:
        prefix, lo, hi = stack.pop()
        if hi - lo <= threshold:
            continue
        if prefix:
            window = -scores[lo:hi]
            k = min(top_k, hi - lo)
            idx = np.argpartition(window, k - 1)[:k]
            # Ties broken by key for deterministic output.
            best: list[int] = sorted((int(i) for i in idx), key=lambda i: (float(window[i]), keys[lo + i]))
            out[prefix] = [keys[lo + i] for i in best]
        depth = len(prefix)
        pos = lo
        while pos < hi:
            key = keys[pos]
            if len(key) <= depth:
                pos += 1
                continue
            ch = key[depth]
            # Keys after ``prefix + ch``; nothing sorts after the largest code point, so every key
            # left in the window shares that branch.
            end = hi if ch == _MAX_CHAR else bisect.bisect_left(keys, prefix + chr(ord(ch) + 1), pos, hi)
            if ch != SEP:
                stack.append((prefix + ch, pos, end))
            pos = end
    return out


class Suggester:
    """``TitleTrie`` plus an in-memory overlay for changes since the trie was built. Thread-safe."""

    def __init__(self, base: TitleTrie | None = None) -> None:
        self._base = base
        self._lock = threading.Lock()
        self._added: dict[str, tuple[SuggestEntry, list[tuple[str, str]]]] = {}
        self._hidden: set[str] = set()  # doc ids whose base entries are stale

    @property
    def pending(self) -> int:
        """Number of overlay changes; rebuild the base trie when this grows large."""
        with self._lock:
            return len(self._added) + len(self._hidden)

    def replace_base(self, base: TitleTrie | None) -> None:
        with self._lock:
            self._base = base
            self._added.clear()
            self._hidden.clear()

    def upsert(self, entries: Iterable[SuggestEntry]) -> None:
        with self._lock:
            for e in entries:
                self._hidden.add(e.doc_id)
                self._added[e.doc_id] = (e, e.keyed())

    def remove(self, doc_ids: Iterable[str]) -> None:
        with self._lock:
            for d in doc_ids:
                self._hidden.add(d)
                self._added.pop(d, None)

    def suggest(self, prefix: str, *, limit: int = 10, lang: str | None = None) -> list[str]:
        """The text of each of :meth:`suggest_docs`."""
        return [s.text for s in self.suggest_docs(prefix, limit=limit, lang=lang)]

    def suggest_docs(self, prefix: str, *, limit: int = 10, lang: str | None = None) -> list[Suggestion]:
        """Up to ``limit`` documents whose title or alias starts with ``prefix``: the ``limit *``
        :data:`POOL_FACTOR` best by score (:func:`_pool_size`) and the keys equal to it, ranked by
        :func:`_rank`."""
        norm = normalize_key(prefix)
        if not norm or limit <= 0:
            return []
        with self._lock:
            base = self._base
            hidden = frozenset(self._hidden) if self._hidden else frozenset[str]()
            extra: list[Candidate] = [
                Candidate(key_score(e.prior, e.title, alias), k, e.title, alias, e.lang, e.doc_id)
                for e, keys in self._added.values()
                for k, alias in keys
                if k.startswith(norm)
            ]
        extra.sort(key=lambda c: (-c.score, c.title))
        streams: list[Iterator[Candidate]] = [iter(extra)]
        exact = [c for c in extra if c.norm == norm]
        if base is not None:
            streams.append(c for c in base.candidates(norm) if c.doc_id not in hidden)
            exact += [c for c in base.exact(norm) if c.doc_id not in hidden]
        pool = itertools.islice(heapq.merge(*streams, key=lambda c: -c.score), _pool_size(limit))
        return _rank([*pool, *exact], norm, lang, limit)

    def named(self, text: str, *, limit: int = 16) -> list[tuple[str, str]]:
        """``(doc_id, title)`` of the documents titled or aliased exactly ``text`` (case, accents and
        spacing aside), best first: a redirect's target is found by the redirect's name ("heart attack"
        -> Myocardial infarction). Only what the trie holds: the titles and the redirects it kept."""
        norm = normalize_key(text)
        if not norm or limit <= 0:
            return []
        with self._lock:
            base = self._base
            hidden = frozenset(self._hidden) if self._hidden else frozenset[str]()
            extra: list[Candidate] = [
                (e.score, e.title, e.doc_id) for e, keys in self._added.values() if norm in keys
            ]
        extra.sort(key=lambda c: (-c[0], c[1], c[2]))
        held = base.exact(norm, _MAX_SCAN) if base is not None else []
        found = [*extra, *(c for c in held if c[2] not in hidden)]
        out: dict[str, str] = {}
        for _, title, doc_id in sorted(found, key=lambda c: (-c[0], c[1], c[2])):
            out.setdefault(doc_id, title)
            if len(out) >= limit:
                break
        return list(out.items())


def _pool_size(limit: int) -> int:
    return max(limit, min(limit * POOL_FACTOR, _POOL_MAX))


def _foreign(doc_lang: str, want: str) -> bool:
    return doc_lang.strip().lower() not in _NEUTRAL_LANGS and primary_lang(doc_lang) != want


def _rank(cands: Iterable[Candidate], norm: str, lang: str | None, limit: int) -> list[Suggestion]:
    """``limit`` suggestions, one per document and per text (case-folded): documents in ``lang`` (or in
    no language) first, then titles and aliases typed in full before aliases only begun, then by score.
    A suggestion shows what matched: the alias when an alias did."""
    want = primary_lang(lang) if lang and lang.strip() else None

    def order(c: Candidate) -> tuple[bool, bool, float, str, str]:
        foreign = want is not None and _foreign(c.lang, want)
        partial_alias = bool(c.alias) and c.norm != norm
        score = c.score + ALIAS_PENALTY if c.alias else c.score
        return (foreign, partial_alias, -score, c.text, c.doc_id)

    out: list[Suggestion] = []
    seen_docs: set[str] = set()
    seen_texts: set[str] = set()
    for c in sorted(cands, key=order):
        folded = c.text.casefold()
        if c.doc_id in seen_docs or folded in seen_texts:
            continue  # an alias and the title of one doc, or one title in several sources
        seen_docs.add(c.doc_id)
        seen_texts.add(folded)
        out.append(Suggestion(text=c.text, title=c.title, doc_id=DocId(c.doc_id)))
        if len(out) >= limit:
            break
    return out


def suggest_entries_bruteforce(
    entries: Iterable[SuggestEntry], prefix: str, limit: int, lang: str | None = None
) -> list[str]:
    """Reference implementation used by tests: same pool, ranking and dedupe rules, linear scan."""
    norm = normalize_key(prefix)
    if not norm or limit <= 0:
        return []
    found = [
        Candidate(float(np.float32(key_score(e.prior, e.title, alias))), k, e.title, alias, e.lang, e.doc_id)
        for e in entries
        for k, alias in e.keyed()
        if k.startswith(norm)
    ]
    found.sort(key=lambda c: (-c.score, _trie_key(c.norm, c.title, c.alias, c.lang, c.doc_id)))
    exact = [c for c in found if c.norm == norm][:_EXACT_MAX]
    return [s.text for s in _rank([*found[: _pool_size(limit)], *exact], norm, lang, limit)]
