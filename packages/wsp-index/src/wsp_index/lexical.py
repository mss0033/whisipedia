"""``LexicalIndex`` on Tantivy: BM25 over per-language fields, snippets, source filter, suggest.

On-disk layout under the index directory (local disk; ADR-0002, ADR-0011)::

    tantivy/        Tantivy index, unless TANTIVY.txt names another directory (a generation on the NAS)
    TANTIVY.txt     optional: absolute path of the Tantivy index in use
    meta.sqlite     settings and the source numbers of the ``source_key`` column
    suggest/        mmapped MARISA tries (see ``wsp_index.suggest``)
    static_rank.bin link-analysis static rank (``wsp_index.linkrank``), written by ``wsp index rank``
    spelling/       vocabulary for "did you mean" (``wsp_index.spelling``), written by ``wsp index spell``

Static rank: when ``static_rank.bin`` exists, the top ``rank_depth`` BM25 hits are re-sorted by
``bm25 + rank_weight * feature``. The feature is a percentile in ``[0, 1]`` (``wsp_index.linkrank``),
so with ``rank_weight = 1`` it adds at most 1 to BM25 scores of roughly 5-40 (plus source priors of up
to 6): it breaks near-ties, it does not reorder clear matches. Deeper hits keep their BM25 order, so
paging stays consistent. The file is re-checked every few seconds, so a rank rebuilt by another
process is picked up without a restart.

Every-word matches first: a query of several words is first run as an AND (every word must match,
in any language's fields), which is cheap. With at least :data:`MIN_TIER_HITS` such matches they lead
the result list, best first, and the documents matching only some of the words follow; with fewer,
the list is the any-word query's (which boosts every-word matches; ``coordination_boost``). Neither
query is limited to the query's language: a German page titled "Berliner Mauer" is a match for
``Berliner Mauer`` even though the query has no German function words (audit 2026-10-12 #1); a
preference for the query's language is a ranking matter, left to the Oracle. The list does not depend
on the page asked for, so pages of any size tile it.

Per-language analysis: each configured language ``L`` gets ``title_L``/``text_L`` fields analyzed
with ``wsp_L`` (Snowball stemmer where Tantivy has one); every other language goes to
``title_any``/``text_any`` (no stemming). Queries run against all of them, so a mixed-language
library is searched in one pass and adding a language only needs a reindex of that language.

Writes are buffered by Tantivy and become visible on ``commit()`` (batched commits; optionally
automatic every ``commit_every`` operations). ``upsert`` is delete-by-term on ``doc_id`` plus add,
which Tantivy orders by opstamp, so repeated upserts of one id in a batch keep only the last.

Titles, aliases and suggest priors are not copied into a database (ADR-0011): titles are stored in
Tantivy, aliases and titles for the suggest trie come from :class:`~wsp_index.titles.TitleSources`
(the Refinery and the redirect tables), priors from the static rank. Two fast columns spare stored
document reads on the hot path (each one a disk or network round trip): ``doc_key`` (the static rank's
key of the doc id) for ranking a window, ``source_key`` (a small per-index number) for the source cap.
"""

from __future__ import annotations

import contextlib
import html
import json
import os
import sqlite3
import threading
import time
from collections import Counter, OrderedDict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType
from typing import Any, Self, cast

import numpy as np
import numpy.typing as npt
import tantivy

from wsp_core.ids import DocId
from wsp_core.model import DocRecord, SearchHit
from wsp_index.linkrank import RANK_FILE, StaticRank, key64
from wsp_index.localfs import ensure_local
from wsp_index.suggest import (
    DEFAULT_HEAVY_THRESHOLD,
    DEFAULT_TOP_K,
    SuggestEntry,
    Suggester,
    TitleTrie,
)
from wsp_index.text import (
    ANALYZER_VERSION,
    FALLBACK_LANG,
    build_analyzer,
    drop_stopwords,
    primary_lang,
    sanitize_query,
)
from wsp_index.titles import TitleSources

_META_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS source_keys (source_id TEXT PRIMARY KEY, key INTEGER NOT NULL UNIQUE);
"""
SCHEMA_FIELDS = 2
"""Version of the field set: 2 added the ``doc_key`` and ``source_key`` fast columns (ADR-0011)."""
TANTIVY_POINTER = "TANTIVY.txt"


def tantivy_dir(lexical: Path) -> Path:
    """The Tantivy index of the lexical directory ``lexical``: the one ``TANTIVY.txt`` names, else
    ``lexical/tantivy``."""
    pointer = lexical / TANTIVY_POINTER
    if pointer.is_file():
        text = pointer.read_text(encoding="utf-8").strip()
        if text:
            return Path(text)
    return lexical / "tantivy"


def point_tantivy(lexical: Path, target: Path) -> None:
    """Make ``target`` (an absolute path, e.g. a copy on the NAS) the Tantivy index of ``lexical``."""
    if not target.is_absolute():
        raise ValueError(f"the Tantivy pointer must be absolute: {target}")
    partial = lexical / (TANTIVY_POINTER + ".partial")
    partial.write_text(f"{target}\n", encoding="utf-8")
    with partial.open("rb") as fh:
        os.fsync(fh.fileno())
    os.replace(partial, lexical / TANTIVY_POINTER)


LOOKUP_CACHE = 100_000  # lookup() answers kept (~300 bytes each)
LOOKUP_STAMP_EVERY = 5.0  # seconds between checks that the index on disk is still the same
MIN_TIER_HITS = 30
"""Every-word matches enough to lead the result list on their own (module docstring)."""
DIVERSE_WIDTH = 3
"""A shared window (``per_source``) is asked this many times deeper in one run, so that the sources a
crowding source hides are usually in it already and the query is not run again (:meth:`_diverse`)."""
DIVERSE_ROUNDS = 2
"""Queries run again at most this many times to share a window among sources (``per_source``). Each
round reads the source of up to a window of new hits from the ``source_key`` fast column. Two rounds
cover one crowding source after another (Open Library's editions, then Stack Exchange users named
"Huckleberry Finn"); measured on the live index (2026-10-12), a third cost 0.3-1.5 s more per query and
brought no title into the window that two did not."""


class _DocMemo:
    """A searcher whose ``doc`` reads each stored document once for one query (each read is 1-3 ms from
    a cold local disk, more from the NAS). Everything else goes to the searcher."""

    def __init__(self, searcher: tantivy.Searcher) -> None:
        self._searcher = searcher
        self._docs: dict[tuple[int, int], tantivy.Document] = {}

    def doc(self, address: tantivy.DocAddress) -> tantivy.Document:
        key = (address.segment_ord, address.doc)
        found = self._docs.get(key)
        if found is None:
            found = self._docs[key] = self._searcher.doc(address)
        return found

    @property
    def native(self) -> tantivy.Searcher:
        """The searcher itself, for Tantivy functions that take one (``SnippetGenerator.create``)."""
        return self._searcher

    def __getattr__(self, name: str) -> Any:
        return getattr(self._searcher, name)


@dataclass(frozen=True, slots=True)
class LexicalWindow:
    """The first hits of a query's result list (:meth:`TantivyIndex.search_window`)."""

    hits: Sequence[SearchHit]
    exhaustive: bool
    """``hits`` are every match of the query: a deeper window would find nothing more."""


@dataclass(frozen=True, slots=True)
class TantivyConfig:
    languages: tuple[str, ...] = ("en",)
    """Languages with their own analyzer (BCP-47 primary subtags); others use the fallback."""
    title_boost: float = 3.0
    writer_heap_bytes: int = 1024 * 1024 * 1024
    writer_threads: int = 4
    """4 threads with >= 256 MB each: measured 144k docs/s against 34k for Tantivy's default (8 threads
    sharing 256 MB). The heap is the writer's whole budget, split between its threads."""
    commit_every: int | None = None
    """Commit automatically after this many buffered upserts/deletes (``None``: only explicit)."""
    snippet_chars: int = 200
    store_text: bool = False
    """Store body text in Tantivy too. Off: snippets are cut from the Refinery's copy (the ``texts``
    source of :class:`TantivyIndex`). Storing measured 1.4 KB a document (105 GB for 76M on
    2026-10-11), and every ranked candidate loaded its whole stored text, which made search slow."""
    suggest_rebuild_threshold: int = 2_000
    suggest_rebuild_seconds: float = 12 * 3600
    """A writer folds a small overlay into a fresh trie only when the trie is this old: a full rebuild
    reads every title of the library (1.5 h over the NAS on 2026-10-14), and one ran after each of a
    run of 36 small Kiwix releases. The nightly build, with the trie a day old, rebuilds it."""
    """Rebuild the suggest trie at commit when more than this many titles changed since the last build."""
    suggest_max_keys: int = 3_000_000
    """Completion keys in the suggest trie (titles and aliases of the best-ranked documents first):
    bounds the rebuild's memory (~300 bytes a key while building) on a 40M-title library."""
    suggest_open_rebuild_titles: int = 200_000
    """Opening an index without a trie builds one only below this many titles; bigger libraries
    leave it to the writer (``wsp index build``), never to a reader such as the API."""
    suggest_heavy_threshold: int = DEFAULT_HEAVY_THRESHOLD
    suggest_top_k: int = DEFAULT_TOP_K
    source_priors: Mapping[str, float] = field(default_factory=dict[str, float])
    """Constant score added to matches from a source (PLAN §9.3 source-quality priors): lets curated
    references outrank sheer volume, e.g. an encyclopedia article over millions of dictionary stubs."""
    rank_weight: float = 1.0
    """Weight of the link-analysis static rank added to BM25 (0 switches it off)."""
    rank_depth: int = 100
    """BM25 hits re-sorted with the static rank; deeper hits keep their BM25 order."""
    coordination_boost: float = 1.0
    """Extra BM25 weight for documents that match every query word (0 switches it off). Matching is
    any-word, so without it a page holding one rare word plus a source prior can outrank pages about
    the whole query (``alan turing`` ranked a syndrome named after Allan first; E2E-6)."""

    def schema_signature(self) -> str:
        """Everything that changes how documents are analyzed: an index built otherwise is refused
        (it would silently miss matches) and must be rebuilt."""
        return json.dumps(
            {
                "languages": sorted(self.languages),
                "store_text": self.store_text,
                "analyzer": ANALYZER_VERSION,
                "fields": SCHEMA_FIELDS,
            }
        )


class SchemaMismatchError(RuntimeError):
    pass


class TantivyIndex:
    """Reference ``LexicalIndex``. One writer per index directory; searches are thread-safe."""

    def __init__(
        self,
        path: Path,
        config: TantivyConfig | None = None,
        *,
        texts: Callable[[Sequence[str]], Mapping[str, str]] | None = None,
        titles: TitleSources | Callable[[], TitleSources] | None = None,
    ) -> None:
        """``titles``: where the suggest trie's titles and aliases come from on a full rebuild (built on
        first use when a callable). Without it the trie is built from the stored titles, which reads
        every document: only for small indexes (tests, samples)."""
        self.config = config or TantivyConfig()
        # Document text by id for snippets when the index stores none (the Refinery document store).
        self.texts = texts
        self._titles = titles
        self.path = ensure_local(path)
        self._langs: tuple[str, ...] = tuple(dict.fromkeys(primary_lang(x) for x in self.config.languages))
        self._routes: tuple[str, ...] = (*self._langs, FALLBACK_LANG)
        self._lock = threading.RLock()

        self._db = sqlite3.connect(self.path / "meta.sqlite", check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.execute("PRAGMA busy_timeout=60000")  # another process's commit, not an error
        self._db.execute("PRAGMA journal_size_limit=67108864")  # the WAL shrinks back after a bulk load
        self._db.execute("PRAGMA cache_size=-131072")  # 128 MiB page cache (default: 2 MB)
        self._db.executescript(_META_SCHEMA)
        self._check_signature()

        self.source_keys: dict[str, int] = {}
        self._source_names: dict[int, str] = {}
        self._reload_source_keys()
        index_dir = self.tantivy_dir = tantivy_dir(self.path)  # may be on the NAS (ADR-0011)
        if index_dir == self.path / "tantivy":
            index_dir.mkdir(parents=True, exist_ok=True)
        elif not index_dir.parent.is_dir():
            # Never create directories under a pointer: with the NAS unmounted, that made an empty
            # index on the root disk and the API served an empty library (2026-10-13 review).
            raise RuntimeError(
                f"{TANTIVY_POINTER} names {index_dir}, whose parent does not exist (not mounted?)"
            )
        if index_dir.is_dir() and tantivy.Index.exists(str(index_dir)):
            self._index = tantivy.Index.open(str(index_dir))
        elif index_dir.is_dir() and any(index_dir.iterdir()):
            raise RuntimeError(f"{TANTIVY_POINTER} names {index_dir}, which holds no Tantivy index")
        else:
            index_dir.mkdir(exist_ok=True)
            self._index = tantivy.Index(self._build_schema(), path=str(index_dir))
        self._lookups: OrderedDict[str, SearchHit | None] = OrderedDict()  # lookup(): doc id -> hit
        self._lookups_stamp: tuple[int, int, int] | None = None
        self._lookups_checked = -1e9
        self._lookups_lock = threading.Lock()
        for lang in self._routes:
            self._index.register_tokenizer(f"wsp_{lang}", build_analyzer(lang))
        self._schema = self._index.schema
        self._writer: tantivy.IndexWriter | None = None
        self._buffered = 0
        self._closed = False

        self._prior_clauses = self._build_prior_clauses()
        self._rank_path = self.path / RANK_FILE
        self._rank: StaticRank | None = StaticRank.open(self._rank_path)
        self._rank_stamp = _stamp(self._rank_path)
        self._rank_checked = time.monotonic()
        self._suggest_dir = self.path / "suggest"
        self._suggester = Suggester()
        self._pending_suggest: list[SuggestEntry] = []
        self._pending_removed: list[str] = []
        self._suggest_stale = False
        self._trie_stamp: tuple[int, int, int] = (0, 0, 0)
        self._trie_checked = 0.0
        # Opening never rebuilds a big trie: a stale one is served until a writer's commit replaces it
        # (2026-10-11: a refine step that merely opened the index rebuilt it and took 12.8 GB).
        dirty = self._meta("suggest_dirty") == "1" or not TitleTrie.exists(self._suggest_dir)
        if dirty and self._small():
            self._rebuild_suggest()
        elif TitleTrie.exists(self._suggest_dir):
            self._suggester.replace_base(TitleTrie.open(self._suggest_dir))
            self._trie_stamp = self._trie_file_stamp()
            # Dirty at open (an earlier process's overlay, persisted only as the flag) is not stale:
            # this process keeps its own overlay, and suggest_rebuild_due() ages the flag. Treating it
            # as stale made every release step after a small one rebuild the whole trie (review
            # 2026-10-14 #1a).
        else:
            self._suggest_stale = True

    # --- setup ---------------------------------------------------------------------------------

    def _build_prior_clauses(self) -> list[tuple[tantivy.Occur, tantivy.Query]]:
        by_bonus: dict[float, list[str]] = {}
        for source_id, bonus in self.config.source_priors.items():
            if bonus > 0:
                by_bonus.setdefault(float(bonus), []).append(source_id)
        return [
            (
                tantivy.Occur.Should,
                tantivy.Query.const_score_query(
                    tantivy.Query.term_set_query(self._schema, "source_id", sorted(ids)), bonus
                ),
            )
            for bonus, ids in sorted(by_bonus.items())
        ]

    def _small(self) -> bool:
        """Few enough documents that a suggest rebuild costs milliseconds, not minutes and gigabytes."""
        return self._index.searcher().num_docs <= self.config.suggest_open_rebuild_titles

    def has_source(self, source_id: str) -> bool:
        """Whether any live document of ``source_id`` is indexed."""
        q = tantivy.Query.term_query(self._schema, "source_id", source_id)
        return bool(self._index.searcher().search(q, 1, count=False).hits)

    def present(self, doc_ids: Sequence[str]) -> set[str]:
        """The live indexed documents among ``doc_ids``."""
        out: set[str] = set()
        ids = list(dict.fromkeys(doc_ids))
        searcher = self._index.searcher()
        for i in range(0, len(ids), 10_000):
            chunk = ids[i : i + 10_000]
            q = tantivy.Query.term_set_query(self._schema, "doc_id", chunk)
            for _, a in searcher.search(q, len(chunk), count=False).hits:
                out.add(str(searcher.doc(a).get_first("doc_id")))
        return out

    def delete_source(self, source_id: str) -> None:
        """Delete every document of ``source_id`` (one delete term; visible after ``commit()``)."""
        with self._lock:
            self._get_writer().delete_documents_by_term("source_id", source_id)
            self._suggest_stale = True
            self._after_write(1)

    def _reload_source_keys(self) -> None:
        with self._lock:
            rows = self._db.execute("SELECT source_id, key FROM source_keys").fetchall()
            self.source_keys = {str(sid): int(k) for sid, k in rows}
            self._source_names = {k: sid for sid, k in self.source_keys.items()}

    def _source_key(self, source_id: str) -> int:
        """The source's number in the ``source_key`` column (assigned on first sight, never reused)."""
        key = self.source_keys.get(source_id)
        if key is None:
            key = max(self.source_keys.values(), default=0) + 1
            self._db.execute("INSERT INTO source_keys(source_id, key) VALUES (?, ?)", (source_id, key))
            self._db.commit()  # numbers are never rolled back: a rolled-back document leaves an unused one
            self.source_keys[source_id] = key
            self._source_names[key] = source_id
        return key

    def _meta(self, key: str) -> str | None:
        row = self._db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return None if row is None else str(row[0])

    def _set_meta(self, key: str, value: str) -> None:
        self._db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, value))

    def _check_signature(self) -> None:
        sig = self.config.schema_signature()
        existing = self._meta("schema")
        if existing is None:
            self._set_meta("schema", sig)
            self._db.commit()
        elif existing != sig:
            raise SchemaMismatchError(
                f"index at {self.path} was built with {existing}, config wants {sig}: remove it together "
                "with index-state.json and run `wsp index build` (docs/runbooks/rebuild-indexes.md)"
            )

    def _build_schema(self) -> tantivy.Schema:
        sb = tantivy.SchemaBuilder()
        sb.add_text_field("doc_id", stored=True, tokenizer_name="raw")
        sb.add_text_field("source_id", stored=True, tokenizer_name="raw")
        sb.add_text_field("lang", stored=True, tokenizer_name="raw")
        sb.add_unsigned_field("doc_key", fast=True)  # the static rank's key of doc_id (ranking, no doc read)
        sb.add_unsigned_field("source_key", fast=True)  # small source number (the source cap, no doc read)
        for lang in self._routes:
            sb.add_text_field(f"title_{lang}", stored=True, tokenizer_name=f"wsp_{lang}")
            sb.add_text_field(f"text_{lang}", stored=self.config.store_text, tokenizer_name=f"wsp_{lang}")
        return sb.build()

    def route(self, lang: str) -> str:
        p = primary_lang(lang)
        return p if p in self._langs else FALLBACK_LANG

    def _get_writer(self) -> tantivy.IndexWriter:
        if self._writer is None:
            self._writer = self._index.writer(
                heap_size=self.config.writer_heap_bytes, num_threads=self.config.writer_threads
            )
            # Segment files of a crashed, never committed run are only collected on request (the
            # 2026-10-11 kill left 80 GB of them).
            self._writer.garbage_collect_files()
        return self._writer

    # --- LexicalIndex: writes ------------------------------------------------------------------

    def upsert(self, docs: Iterable[DocRecord], *, replace: bool = True) -> int:
        """Add or replace documents. ``replace=False`` skips the delete-by-id: only for documents known
        not to be in the index (a first load of a source), where it saves a delete term per document."""
        n = 0
        with self._lock:
            writer = self._get_writer()
            staged: list[SuggestEntry] = []
            batch = list(docs)
            keys = key64([d.doc_id for d in batch]) if batch else np.zeros(0, np.uint64)
            for doc, key in zip(batch, keys.tolist(), strict=True):
                route = self.route(doc.lang)
                tdoc = tantivy.Document()
                tdoc.add_text("doc_id", doc.doc_id)
                tdoc.add_text("source_id", doc.source_id)
                tdoc.add_text("lang", doc.lang)
                tdoc.add_unsigned("doc_key", int(key))
                tdoc.add_unsigned("source_key", self._source_key(doc.source_id))
                tdoc.add_text(f"title_{route}", doc.title)
                tdoc.add_text(f"text_{route}", doc.text)
                if replace:
                    writer.delete_documents_by_term("doc_id", doc.doc_id)
                writer.add_document(tdoc)
                if doc.title:
                    aliases = tuple(a for a in doc.aliases if a.strip())
                    staged.append(SuggestEntry(doc.doc_id, doc.title, aliases, 0.0))
                n += 1
            if self._suggest_room(len(staged)):
                self._pending_suggest.extend(self._with_priors(staged))
            self._after_write(n)
        return n

    def delete(self, doc_ids: Iterable[DocId]) -> int:
        """Delete by id. Returns the number of ids processed (unknown ids are not an error)."""
        ids = list(dict.fromkeys(doc_ids))
        with self._lock:
            writer = self._get_writer()
            for doc_id in ids:
                writer.delete_documents_by_term("doc_id", doc_id)
            if self._suggest_room(len(ids)):
                gone = set(ids)
                # Commit applies removals before upserts, so drop staged upserts of these ids.
                self._pending_suggest = [e for e in self._pending_suggest if e.doc_id not in gone]
                self._pending_removed.extend(ids)
            self._after_write(len(ids))
        return len(ids)

    def mark_suggest_stale(self) -> None:
        """Rebuild the whole suggest trie at the next commit (new ranks, new redirect tables)."""
        with self._lock:
            self._suggest_stale = True
            self._pending_suggest.clear()
            self._pending_removed.clear()

    def _suggest_room(self, n: int) -> bool:
        """False (and mark the trie stale) once the overlay would exceed the rebuild threshold."""
        if self._suggest_stale:
            return False
        pending = len(self._pending_suggest) + len(self._pending_removed) + self._suggester.pending
        if pending + n > self.config.suggest_rebuild_threshold:
            self._suggest_stale = True  # cheaper to rebuild at commit than to track individually
            self._pending_suggest.clear()
            self._pending_removed.clear()
            return False
        return True

    def _after_write(self, n: int) -> None:
        self._buffered += n
        if self.config.commit_every is not None and self._buffered >= self.config.commit_every:
            self.commit()

    def commit(self, *, rebuild_suggest: bool = True) -> None:
        """Make writes visible. ``rebuild_suggest=False`` (bulk loads) leaves a stale suggest trie
        marked dirty for one rebuild later instead of rebuilding it at every commit."""
        with self._lock:
            if self._writer is not None:
                self._writer.commit()
            dirty = self._suggest_stale or bool(self._pending_suggest or self._pending_removed)
            if dirty:
                self._set_meta("suggest_dirty", "1")
            self._db.commit()
            self._index.reload()
            self._buffered = 0
            if self._suggest_stale:
                if rebuild_suggest:
                    self._rebuild_suggest()
            else:
                self._suggester.remove(self._pending_removed)
                self._suggester.upsert(self._pending_suggest)
                self._pending_removed.clear()
                self._pending_suggest.clear()

    def rollback(self) -> None:
        """Discard uncommitted writes."""
        with self._lock:
            if self._writer is not None:
                self._writer.rollback()
            self._db.rollback()
            self._pending_suggest.clear()
            self._pending_removed.clear()
            self._buffered = 0
            # A stale trie stays marked dirty; rebuilding it here could take minutes.

    def close(self) -> None:
        """Release the writer lock and persist the suggest trie. Uncommitted writes are discarded.

        Idempotent: closing twice is a no-op."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            if self._writer is not None:
                self._writer.rollback()
                self._writer.wait_merging_threads()
                self._writer = None
            self._db.rollback()
            if self._suggester.pending:
                if self._small():
                    self._rebuild_suggest()
                else:  # never a full rebuild on close of a big library: the next writer's rebuild does it
                    self._set_meta("suggest_dirty", "1")
                    self._db.commit()
            with contextlib.suppress(sqlite3.Error):
                self._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self._db.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        if exc_type is None:
            self.commit()
        self.close()

    # --- suggest -------------------------------------------------------------------------------

    def _with_priors(self, entries: list[SuggestEntry]) -> list[SuggestEntry]:
        """``entries`` with their static-rank prior (an overlay entry until the next full rebuild; redirect
        aliases join it then)."""
        rank = self.static_rank()
        if rank is None or not entries:
            return entries
        feats = rank.features([e.doc_id for e in entries])
        return [
            SuggestEntry(e.doc_id, e.title, e.aliases, float(f)) for e, f in zip(entries, feats, strict=True)
        ]

    def _suggest_entries(self) -> list[SuggestEntry]:
        """Every entry of a full trie rebuild, at most ``suggest_max_keys`` keys."""
        titles = self._titles() if callable(self._titles) else self._titles
        budget = self.config.suggest_max_keys
        if titles is not None:
            return titles.suggest_entries(self.static_rank(), budget)
        # No title sources (tests, the sample library): the stored titles, best-ranked first.
        searcher = self._index.searcher()
        found = searcher.search(tantivy.Query.all_query(), max(1, searcher.num_docs), count=False)
        entries: list[SuggestEntry] = []
        for _, a in found.hits:
            doc = searcher.doc(a)
            route = self.route(str(doc.get_first("lang") or ""))
            title = str(doc.get_first(f"title_{route}") or "")
            if title:
                entries.append(SuggestEntry(str(doc.get_first("doc_id")), title))
        entries = sorted(self._with_priors(entries), key=lambda e: (-e.prior, e.doc_id))
        out: list[SuggestEntry] = []
        used = 0
        for e in entries:
            if used >= budget:
                break
            out.append(e)
            used += 1 + len(e.aliases)
        return out

    def _rebuild_suggest(self) -> None:
        trie = TitleTrie.build(
            self._suggest_entries(),
            self._suggest_dir,
            heavy_threshold=self.config.suggest_heavy_threshold,
            top_k=self.config.suggest_top_k,
        )
        self._suggester.replace_base(trie)
        self._trie_stamp = self._trie_file_stamp()
        self._suggest_stale = False
        self._pending_suggest.clear()
        self._pending_removed.clear()
        self._set_meta("suggest_dirty", "0")
        self._db.commit()

    def suggest_rebuild_due(self, now: float | None = None) -> bool:
        """Whether :meth:`rebuild_suggest` would be worth its full scan now: the trie is missing or
        stale beyond the overlay's threshold, or it is dirty and older than ``suggest_rebuild_seconds``.
        A small dirty overlay on a fresh trie is left for a later writer (the nightly build)."""
        with self._lock:
            if not TitleTrie.exists(self._suggest_dir) or self._suggest_stale:
                return True
            pending = len(self._pending_suggest) + len(self._pending_removed) + self._suggester.pending
            if pending > self.config.suggest_rebuild_threshold:
                return True
            dirty = pending > 0 or self._meta("suggest_dirty") == "1"
            if not dirty:
                return False
            age = (time.time() if now is None else now) - self._trie_file_stamp()[1] / 1e9  # mtime_ns
            return age >= self.config.suggest_rebuild_seconds

    def rebuild_suggest(self) -> None:
        """Commit, then fold the overlay into a fresh trie (e.g. on a schedule)."""
        with self._lock:
            self.commit()
            if self._suggester.pending:
                self._rebuild_suggest()

    def suggest(self, prefix: str, *, limit: int = 10) -> Sequence[str]:
        self._reload_trie_if_rebuilt()
        return self._suggester.suggest(prefix, limit=limit)

    def _trie_file_stamp(self) -> tuple[int, int, int]:
        """Identity of the trie file on disk: a rebuild changes at least one of these."""
        return TitleTrie.stamp(self._suggest_dir)

    def _reload_trie_if_rebuilt(self, *, every: float = 5.0) -> None:
        """Pick up a trie rebuilt by another process (e.g. ``wsp index build`` while ``wsp serve`` runs).

        Tantivy readers reload on commit by themselves; the mmapped trie does not. Checked at most every
        ``every`` seconds; a process with its own unsaved overlay keeps it.
        """
        now = time.monotonic()
        if now - self._trie_checked < every:
            return
        self._trie_checked = now
        stamp = self._trie_file_stamp()
        if stamp == self._trie_stamp or stamp == (0, 0, 0) or self._suggester.pending:
            return
        with self._lock:
            self._suggester.replace_base(TitleTrie.open(self._suggest_dir))
            self._trie_stamp = stamp

    # --- static rank ---------------------------------------------------------------------------

    @property
    def rank_path(self) -> Path:
        return self._rank_path

    def static_rank(self) -> StaticRank | None:
        """The link-analysis rank sidecar (reloaded when ``wsp index rank`` replaced it)."""
        now = time.monotonic()
        if now - self._rank_checked >= 5.0:
            self._rank_checked = now
            stamp = _stamp(self._rank_path)
            if stamp != self._rank_stamp:
                self._rank_stamp = stamp
                self._rank = StaticRank.open(self._rank_path)
        return self._rank

    def reload_static_rank(self) -> StaticRank | None:
        self._rank_checked = 0.0
        self._rank_stamp = (-1, -1, -1)
        return self.static_rank()

    # --- reads ---------------------------------------------------------------------------------

    @property
    def num_docs(self) -> int:
        return self._index.searcher().num_docs

    def commit_stamp(self) -> tuple[int, int, int]:
        """Identity of the last commit on disk, by any process (Tantivy replaces ``meta.json`` on each):
        a cache of search results is stale once it changes."""
        return _stamp(self.tantivy_dir / "meta.json")

    def lookup(self, doc_id: str) -> SearchHit | None:
        """Title, source and language of an indexed document (for fusing hits from other retrievers,
        and the title of every change-feed row and directory entry).

        A stored-document read, on the NAS a round trip or more: the last :data:`LOOKUP_CACHE` answers
        are kept, dropped when the index on disk changes (checked every :data:`LOOKUP_STAMP_EVERY` s).
        """
        now = time.monotonic()
        with self._lookups_lock:
            if now - self._lookups_checked >= LOOKUP_STAMP_EVERY:
                self._lookups_checked = now
                stamp = self.commit_stamp()
                if stamp != self._lookups_stamp:
                    self._lookups_stamp = stamp
                    self._lookups.clear()
            if doc_id in self._lookups:
                self._lookups.move_to_end(doc_id)
                return self._lookups[doc_id]
        hit = self._lookup(doc_id)
        with self._lookups_lock:
            self._lookups[doc_id] = hit
            while len(self._lookups) > LOOKUP_CACHE:
                self._lookups.popitem(last=False)
        return hit

    def _lookup(self, doc_id: str) -> SearchHit | None:
        searcher = self._index.searcher()
        found = searcher.search(tantivy.Query.term_query(self._schema, "doc_id", doc_id), 1, count=False)
        if not found.hits:
            return None
        doc = searcher.doc(found.hits[0][1])
        lang = str(doc.get_first("lang") or "")
        title = str(doc.get_first(f"title_{self.route(lang)}") or "")
        return SearchHit(DocId(doc_id), 0.0, title, "", str(doc.get_first("source_id") or ""), lang)

    def parse(self, query: str, *, require_all: bool = False) -> tantivy.Query | None:
        """The query over every language's title and text fields; ``require_all``: every word must
        match (an AND, much cheaper than the OR on large indexes)."""
        text = drop_stopwords(sanitize_query(query))
        if not text:
            return None
        fields = [f"{kind}_{lang}" for lang in self._routes for kind in ("title", "text")]
        boosts = {f"title_{lang}": self.config.title_boost for lang in self._routes}
        words = text.split()
        if require_all and len(words) > 1 and '"' not in text:
            every, _errors = self._index.parse_query_lenient(
                " ".join(f"+{w}" for w in words), fields, field_boosts=boosts
            )
            return every
        parsed, _errors = self._index.parse_query_lenient(text, fields, field_boosts=boosts)
        if self.config.coordination_boost <= 0 or len(words) < 2 or '"' in text:
            return parsed
        every, _errors = self._index.parse_query_lenient(
            " ".join(f"+{w}" for w in words), fields, field_boosts=boosts
        )
        return tantivy.Query.boolean_query(
            [
                (tantivy.Occur.Should, parsed),
                (tantivy.Occur.Should, tantivy.Query.boost_query(every, self.config.coordination_boost)),
            ]
        )

    def _every_word(self, query: str) -> tantivy.Query | None:
        """The every-word query, or ``None`` when it is the any-word one (a single word, a phrase)."""
        text = drop_stopwords(sanitize_query(query))
        if len(text.split()) < 2 or '"' in text:
            return None
        return self.parse(query, require_all=True)

    def search(
        self, query: str, *, limit: int = 10, offset: int = 0, sources: Sequence[str] | None = None
    ) -> Sequence[SearchHit]:
        return self._search(query, limit=limit, offset=offset, sources=sources, snippets=True).hits

    def search_hits(
        self, query: str, *, limit: int = 10, offset: int = 0, sources: Sequence[str] | None = None
    ) -> Sequence[SearchHit]:
        """Like :meth:`search` without snippets: much cheaper for wide windows (totals, facets)."""
        return self._search(query, limit=limit, offset=offset, sources=sources, snippets=False).hits

    def search_window(
        self,
        query: str,
        *,
        limit: int,
        sources: Sequence[str] | None = None,
        partial: bool = True,
        per_source: int | None = None,
    ) -> LexicalWindow:
        """The first ``limit`` hits without snippets, and whether they are every match. For a ranker that
        wants a pool of good candidates (the Oracle).

        ``partial=False``: when every-word matches lead the list (at least :data:`MIN_TIER_HITS`), the
        window ends with them instead of going on with documents that match only some of the words.
        That spares the any-word query (0.4-3 s on 130M documents, against ~20 ms; 2026-10-12), and
        such a window is never exhaustive.

        ``per_source``: no source takes more than this many places while other sources have matches
        (:meth:`_diverse`): on the full library 198 Open Library editions of "Huckleberry Finn" filled
        a window of 200, and the book itself, the Wikisource text and every other site were never
        candidates (2026-10-12).
        """
        return self._search(
            query,
            limit=limit,
            offset=0,
            sources=sources,
            snippets=False,
            partial=partial,
            per_source=per_source,
        )

    def _top(
        self,
        searcher: tantivy.Searcher,
        parsed: tantivy.Query,
        n: int,
        sources: Sequence[str] | None,
        *,
        per_source: int | None = None,
        without: Sequence[str] = (),
    ) -> list[tuple[float, tantivy.DocAddress]]:
        """The ``n`` best matches of ``parsed`` within ``sources`` and not in ``without``, source priors
        added (an exclusion adds no score, so the scores of different exclusions compare)."""
        if per_source is not None:
            return self._diverse(searcher, parsed, n, sources, per_source)
        clauses: list[tuple[tantivy.Occur, tantivy.Query]] = [(tantivy.Occur.Must, parsed)]
        if sources is not None:
            source_filter = tantivy.Query.term_set_query(self._schema, "source_id", list(sources))
            clauses.append((tantivy.Occur.Must, source_filter))
        if without:
            excluded = tantivy.Query.term_set_query(self._schema, "source_id", list(without))
            clauses.append((tantivy.Occur.MustNot, excluded))
        clauses += self._prior_clauses
        q = parsed if len(clauses) == 1 else tantivy.Query.boolean_query(clauses)
        return [(float(sc), a) for sc, a in searcher.search(q, n, count=False, offset=0).hits]

    def _diverse(
        self,
        searcher: tantivy.Searcher,
        parsed: tantivy.Query,
        n: int,
        sources: Sequence[str] | None,
        per_source: int,
    ) -> list[tuple[float, tantivy.DocAddress]]:
        """The ``n`` best matches with no source above ``per_source`` places while others have matches.

        The query is run once for a window :data:`DIVERSE_WIDTH` times deeper (a top-k heap over the
        same postings: on the live index the deeper window cost about 5% more, a second run of a cold
        query 2-3 s; 2026-10-13). Only when a source fills more than its share of the ``n`` best of
        that wider window too is it excluded and the query run again, at most :data:`DIVERSE_ROUNDS`
        times. The window is then each source's best ``per_source`` matches by score, followed by the
        overflow of the crowded sources by score (deferred, never dropped: a single-source window
        keeps its order). Each round is one more top-``k`` query, never a scan.
        """
        wide = n * DIVERSE_WIDTH
        hits = self._top(searcher, parsed, wide, sources)
        if len(hits) < n or (sources is not None and len(set(sources)) < 2):
            return hits[:n]  # every match, or one source: nothing to share
        pool = self._with_sources(searcher, hits)
        crowded: set[str] = set()
        for _ in range(DIVERSE_ROUNDS):
            if len(hits) < wide:
                break  # the wider window held every match: nothing is hidden behind a crowded source
            best = sorted(pool, key=lambda t: -t[0])[:n]
            counts = Counter(src for _, _, src in best)
            over = {src for src, k in counts.items() if k > per_source} - crowded
            if not over:
                break
            crowded |= over
            hits = self._top(searcher, parsed, wide, sources, without=sorted(crowded))
            pool += self._with_sources(searcher, hits)
        pool.sort(key=lambda t: -t[0])  # stable: equal scores keep the order Tantivy gave them
        taken: Counter[str] = Counter()
        head: list[tuple[float, tantivy.DocAddress]] = []
        overflow: list[tuple[float, tantivy.DocAddress]] = []
        for sc, a, src in pool:
            taken[src] += 1
            (head if taken[src] <= per_source else overflow).append((sc, a))
        return [*head, *overflow][:n]

    def _with_sources(
        self, searcher: tantivy.Searcher, hits: Sequence[tuple[float, tantivy.DocAddress]]
    ) -> list[tuple[float, tantivy.DocAddress, str]]:
        """Each hit with its source id, from the ``source_key`` fast column (no stored document read)."""
        keys = searcher.fast_field_values("source_key", [a for _, a in hits]) if hits else []
        if any(k is not None and int(k) not in self._source_names for k in keys):
            self._reload_source_keys()  # numbered by the writer since this reader opened the index
        by_key = self._source_names
        out: list[tuple[float, tantivy.DocAddress, str]] = []
        for (sc, a), key in zip(hits, keys, strict=True):
            src = by_key.get(int(key)) if key is not None else None
            if src is None:  # a source numbered by another process since this one opened the index
                src = str(searcher.doc(a).get_first("source_id") or "")
            out.append((sc, a, src))
        return out

    def _rank_features(
        self, searcher: tantivy.Searcher, rank: StaticRank, addrs: Sequence[tantivy.DocAddress]
    ) -> npt.NDArray[np.float32]:
        """Static-rank feature of each address, from the ``doc_key`` fast column (no stored document read
        for a version-2 rank file)."""
        if not addrs:
            return np.zeros(0, dtype=np.float32)
        keys = searcher.fast_field_values("doc_key", list(addrs))
        if all(k is not None for k in keys):
            found = rank.features_by_key(np.asarray(keys, dtype=np.uint64))
            if found is not None:
                return found
        return rank.features([str(searcher.doc(a).get_first("doc_id")) for a in addrs])

    def _candidates(
        self,
        searcher: tantivy.Searcher,
        query: str,
        n: int,
        sources: Sequence[str] | None,
        *,
        partial: bool,
        per_source: int | None = None,
    ) -> tuple[list[tuple[float, tantivy.DocAddress]], int, bool]:
        """The first ``n`` entries of the query's result list (module docstring), how many of them are
        the every-word lead, and whether they are every match.

        The list does not depend on ``n``: whether every-word matches lead is decided by how many there
        are, known exactly from a window of ``max(n, MIN_TIER_HITS)``, never by the page asked for
        (pages cut from different lists repeated some documents and skipped others; audit 2026-10-12 #6).
        """
        anyq = self.parse(query)
        if anyq is None:
            return [], 0, True
        every = self._every_word(query)
        if every is not None:
            first = self._top(searcher, every, max(n, MIN_TIER_HITS), sources, per_source=per_source)
            if len(first) >= MIN_TIER_HITS:
                if len(first) >= n:
                    return first[:n], n, False
                if not partial:
                    return first, len(first), False
                # ``first`` holds every every-word match; the documents matching some words follow.
                rest = self._top(searcher, anyq, n, sources, per_source=per_source)
                lead = {(a.segment_ord, a.doc) for _, a in first}  # DocAddress is not hashable
                tail = [(sc, a) for sc, a in rest if (a.segment_ord, a.doc) not in lead][: n - len(first)]
                return [*first, *tail], len(first), len(rest) < n
        hits = self._top(searcher, anyq, n, sources, per_source=per_source)
        return hits, 0, len(hits) < n

    def _search(
        self,
        query: str,
        *,
        limit: int,
        offset: int,
        sources: Sequence[str] | None,
        snippets: bool,
        partial: bool = True,
        per_source: int | None = None,
    ) -> LexicalWindow:
        if limit <= 0 or offset < 0 or (sources is not None and not sources):
            return LexicalWindow([], True)
        searcher = cast(tantivy.Searcher, _DocMemo(self._index.searcher()))  # each document read once
        rank = self.static_rank() if self.config.rank_weight > 0 else None
        depth = self.config.rank_depth
        ranked = not (rank is None or depth <= 0 or offset >= depth)
        need = max(depth, offset + limit) if ranked else offset + limit
        pool, lead, exhaustive = self._candidates(
            searcher, query, need, sources, partial=partial, per_source=per_source
        )
        if not ranked or rank is None:
            chosen = pool[offset : offset + limit]
        else:
            head = pool[:depth]
            feats = self._rank_features(searcher, rank, [a for _, a in head])
            w = self.config.rank_weight
            boosted = [(sc + w * float(f), a) for (sc, a), f in zip(head, feats, strict=True)]
            # Re-sorted within the every-word lead and within the rest: their scores come from different
            # queries, and the lead stays first.
            order = sorted(range(len(boosted)), key=lambda i: (i >= lead, -boosted[i][0]))
            chosen = [*(boosted[i] for i in order), *pool[depth : offset + limit]][offset : offset + limit]
        scored = [(sc, searcher.doc(a)) for sc, a in chosen]  # stored documents: only the hits returned
        parsed = self.parse(query) if snippets else None
        cut = {} if parsed is None else self._snippet_map(searcher, parsed, [d for _, d in scored])
        hits: list[SearchHit] = []
        for score, doc in scored:
            route = self.route(str(doc.get_first("lang") or ""))
            doc_id = str(doc.get_first("doc_id"))
            hits.append(
                SearchHit(
                    doc_id=DocId(doc_id),
                    score=float(score),
                    title=str(doc.get_first(f"title_{route}") or ""),
                    snippet=cut.get(doc_id, ""),
                    source_id=str(doc.get_first("source_id") or ""),
                    lang=str(doc.get_first("lang") or ""),
                )
            )
        return LexicalWindow(hits, exhaustive and len(pool) <= offset + limit)

    def snippets(self, query: str, doc_ids: Sequence[str]) -> dict[str, str]:
        """Highlighted snippets of ``query`` in each of ``doc_ids`` (for hits ranked without them: the
        Oracle ranks a window of 200 but shows ten). Ids not in the index are left out."""
        parsed = self.parse(query) if doc_ids else None
        if parsed is None:
            return {}
        searcher = self._index.searcher()
        by_id = tantivy.Query.term_set_query(self._schema, "doc_id", list(dict.fromkeys(doc_ids)))
        found = searcher.search(by_id, len(doc_ids), count=False)
        return self._snippet_map(searcher, parsed, [searcher.doc(a) for _, a in found.hits])

    def _snippet_map(
        self, searcher: tantivy.Searcher, parsed: tantivy.Query, docs: Sequence[tantivy.Document]
    ) -> dict[str, str]:
        """doc id -> snippet, from the stored text or, when none is stored, from ``texts``."""
        if not docs:
            return {}
        ids = [str(d.get_first("doc_id")) for d in docs]
        external: Mapping[str, str] = {}
        if not self.config.store_text:
            if self.texts is None:
                return {}
            external = self.texts(ids)
        generators: dict[str, tantivy.SnippetGenerator] = {}
        out: dict[str, str] = {}
        for doc_id, doc in zip(ids, docs, strict=True):
            route = self.route(str(doc.get_first("lang") or ""))
            source_doc = doc
            if not self.config.store_text:
                text = external.get(doc_id)
                if text is None:
                    continue
                source_doc = tantivy.Document(**{f"text_{route}": text})
            gen = generators.get(route)
            if gen is None:
                native = searcher.native if isinstance(searcher, _DocMemo) else searcher  # Rust wants its own
                gen = tantivy.SnippetGenerator.create(native, parsed, self._schema, f"text_{route}")
                gen.set_max_num_chars(self.config.snippet_chars)
                generators[route] = gen
            snippet = gen.snippet_from_doc(source_doc).to_html()
            out[doc_id] = snippet or _lead(
                str(source_doc.get_first(f"text_{route}") or ""), self.config.snippet_chars
            )
        return out


def _stamp(path: Path) -> tuple[int, int, int]:
    try:
        st = path.stat()
    except OSError:
        return (0, 0, 0)
    return (st.st_ino, st.st_mtime_ns, st.st_size)


def _lead(text: str, max_chars: int) -> str:
    """First ``max_chars`` of ``text`` cut at a word boundary, HTML-escaped (snippet fallback)."""
    if len(text) <= max_chars:
        return html.escape(text)
    cut = text[:max_chars]
    space = cut.rfind(" ")
    return html.escape(cut[: space if space > max_chars // 2 else max_chars]) + "…"
