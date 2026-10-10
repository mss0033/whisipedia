"""Title autocomplete (PLAN §9.3: < 10 ms).

Structure (chosen by ``scripts/bench_index.py``; see ``docs/components/index.md``):

* A **MARISA trie** (``marisa-trie``, mmapped) holds one key per title or alias,
  ``"<normalized>\\x1f<display title>\\x1f<doc_id>"`` with a float32 score record. Prefix
  enumeration is a trie walk; the trie is several times smaller than a Python list of the same keys.
* Short prefixes ("a", "the ") match millions of keys, so enumerating and sorting them would blow
  the budget. At build time every prefix whose completion set exceeds ``heavy_threshold`` gets its
  top-``top_k`` keys precomputed (a second trie). Each depth partitions the keys, so there are at most
  ``depth * n / heavy_threshold`` such prefixes: small.
* Other prefixes have at most ``heavy_threshold`` completions, enumerated and sorted on the fly.
* A small in-memory **overlay** absorbs incremental upserts/deletes between rebuilds.

Ranking: ``score = prior + 1 / (1 + len(title))``. ``prior`` is a popularity signal such as
``log1p(inbound links)``; the length term prefers "Paris" over "Paris Commune" when priors tie.
"""

from __future__ import annotations

import bisect
import heapq
import itertools
import os
import threading
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast

import marisa_trie  # pyright: ignore[reportMissingTypeStubs]  # untyped C extension; wrapped below
import numpy as np

from wsp_index.text import normalize_key

SEP = "\x1f"
_LIST_SEP = "\x1e"
TRIE_FILE = "titles.marisa"
HEAVY_FILE = "heavy.marisa"
DEFAULT_HEAVY_THRESHOLD = 512
DEFAULT_TOP_K = 32


class _RecordTrie(Protocol):
    def items(self, prefix: str = "") -> list[tuple[str, tuple[float]]]: ...

    def __getitem__(self, key: str) -> list[tuple[float]]: ...

    def __len__(self) -> int: ...

    def save(self, path: str) -> None: ...


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
    """One suggestible document: its display title plus optional aliases, and a popularity prior."""

    doc_id: str
    title: str
    aliases: tuple[str, ...] = ()
    prior: float = 0.0

    @property
    def score(self) -> float:
        return entry_score(self.prior, self.title)

    def match_keys(self) -> list[str]:
        """Normalized match keys (title first), deduplicated, empty ones dropped."""
        seen: dict[str, None] = {}
        for raw in (self.title, *self.aliases):
            key = normalize_key(raw)
            if key:
                seen.setdefault(key, None)
        return list(seen)


def entry_score(prior: float, title: str) -> float:
    return prior + 1.0 / (1.0 + len(title))


def _trie_key(norm: str, title: str, doc_id: str) -> str:
    return f"{norm}{SEP}{title.replace(SEP, ' ')}{SEP}{doc_id}"


def _split_key(key: str) -> tuple[str, str]:
    """``(display title, doc_id)`` of a trie key."""
    _, title, doc_id = key.split(SEP, 2)
    return title, doc_id


Candidate = tuple[float, str, str]  # (score, title, doc_id)


class TitleTrie:
    """Immutable prefix index over titles and aliases, stored as two mmapped MARISA tries."""

    def __init__(self, trie: _RecordTrie, heavy: _BytesTrie) -> None:
        self._trie = trie
        self._heavy = heavy

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
            s = e.score
            for norm in e.match_keys():
                keys.append(_trie_key(norm, e.title, e.doc_id))
                scores_list.append(s)
        order = sorted(range(len(keys)), key=keys.__getitem__)
        keys = [keys[i] for i in order]
        scores = np.asarray(scores_list, dtype=np.float32)[np.asarray(order, dtype=np.int64)]
        heavy = _heavy_prefixes(keys, scores, heavy_threshold, top_k)

        directory.mkdir(parents=True, exist_ok=True)
        trie = _record_trie(zip(keys, ((float(s),) for s in scores.tolist()), strict=True))
        heavy_trie = _bytes_trie(
            (prefix, _LIST_SEP.join(top).encode("utf-8")) for prefix, top in heavy.items()
        )
        for obj, name in ((trie, TRIE_FILE), (heavy_trie, HEAVY_FILE)):
            partial = directory / (name + ".partial")
            obj.save(str(partial))
            os.replace(partial, directory / name)
        return cls.open(directory)

    @classmethod
    def open(cls, directory: Path) -> TitleTrie:
        return cls(_mmap_record_trie(directory / TRIE_FILE), _mmap_bytes_trie(directory / HEAVY_FILE))

    @classmethod
    def exists(cls, directory: Path) -> bool:
        return (directory / TRIE_FILE).is_file() and (directory / HEAVY_FILE).is_file()

    def _score(self, key: str) -> float:
        return self._trie[key][0][0]

    def candidates(self, prefix: str) -> Iterator[Candidate]:
        """Completions of a *normalized* prefix in descending score order (lazy)."""
        packed = self._heavy.get(prefix)
        if packed:
            top = packed[0].decode("utf-8").split(_LIST_SEP)
            for key in top:
                yield (self._score(key), *_split_key(key))
            # Rarely reached: consumer filtered out more than top_k (deletions, duplicates).
            seen = set(top)
            rest = [(s[0], k) for k, s in self._trie.items(prefix) if k not in seen]
        else:
            rest = [(s[0], k) for k, s in self._trie.items(prefix)]
        rest.sort(key=lambda sk: (-sk[0], sk[1]))
        for score, key in rest:
            yield (score, *_split_key(key))


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
            end = bisect.bisect_left(keys, prefix + chr(ord(ch) + 1), pos, hi)
            if ch != SEP:
                stack.append((prefix + ch, pos, end))
            pos = end
    return out


class Suggester:
    """``TitleTrie`` plus an in-memory overlay for changes since the trie was built. Thread-safe."""

    def __init__(self, base: TitleTrie | None = None) -> None:
        self._base = base
        self._lock = threading.Lock()
        self._added: dict[str, tuple[SuggestEntry, list[str]]] = {}
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
                self._added[e.doc_id] = (e, e.match_keys())

    def remove(self, doc_ids: Iterable[str]) -> None:
        with self._lock:
            for d in doc_ids:
                self._hidden.add(d)
                self._added.pop(d, None)

    def suggest(self, prefix: str, *, limit: int = 10) -> list[str]:
        norm = normalize_key(prefix)
        if not norm or limit <= 0:
            return []
        with self._lock:
            base = self._base
            hidden = frozenset(self._hidden) if self._hidden else frozenset[str]()
            extra: list[Candidate] = [
                (e.score, e.title, e.doc_id)
                for e, keys in self._added.values()
                if any(k.startswith(norm) for k in keys)
            ]
        extra.sort(key=lambda c: (-c[0], c[1]))
        streams: list[Iterator[Candidate]] = [iter(extra)]
        if base is not None:
            streams.append(c for c in base.candidates(norm) if c[2] not in hidden)
        out: list[str] = []
        seen_titles: set[str] = set()
        seen_docs: set[str] = set()
        for _, title, doc_id in heapq.merge(*streams, key=lambda c: -c[0]):
            if doc_id in seen_docs or title in seen_titles:
                continue  # an alias and the title of one doc, or one title in several sources
            seen_docs.add(doc_id)
            seen_titles.add(title)
            out.append(title)
            if len(out) >= limit:
                break
        return out


def suggest_entries_bruteforce(entries: Iterable[SuggestEntry], prefix: str, limit: int) -> list[str]:
    """Reference implementation used by tests: same ranking and dedupe rules, linear scan."""
    norm = normalize_key(prefix)
    if not norm:
        return []
    matches = sorted(
        (e for e in entries if any(k.startswith(norm) for k in e.match_keys())),
        key=lambda e: (-np.float32(e.score), e.title),
    )
    titles: dict[str, None] = {}
    for e in matches:
        titles.setdefault(e.title, None)
    return list(itertools.islice(titles, limit))
