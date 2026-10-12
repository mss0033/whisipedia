"""``LexicalIndex`` on Tantivy: BM25 over per-language fields, snippets, source filter, suggest.

On-disk layout under the index directory (local disk only, ADR-0002)::

    tantivy/        Tantivy index
    meta.sqlite     doc_id -> (source, title, lang, prior) and aliases; feeds suggest and ``lookup``
    suggest/        mmapped MARISA tries (see ``wsp_index.suggest``)
    static_rank.bin link-analysis static rank (``wsp_index.linkrank``), written by ``wsp index rank``
    spelling/       vocabulary for "did you mean" (``wsp_index.spelling``), written by ``wsp index spell``

Static rank: when ``static_rank.bin`` exists, the top ``rank_depth`` BM25 hits are re-sorted by
``bm25 + rank_weight * feature`` (feature ≈ 0.14 for an unlinked page, 0.69 average, up to ~10).
Deeper hits keep their BM25 order, so paging stays consistent. The file is re-checked every few
seconds, so a rank rebuilt by another process is picked up without a restart.

Per-language analysis: each configured language ``L`` gets ``title_L``/``text_L`` fields analyzed
with ``wsp_L`` (Snowball stemmer where Tantivy has one); every other language goes to
``title_any``/``text_any`` (no stemming). Queries run against all of them, so a mixed-language
library is searched in one pass and adding a language only needs a reindex of that language.

Writes are buffered by Tantivy and become visible on ``commit()`` (batched commits; optionally
automatic every ``commit_every`` operations). ``upsert`` is delete-by-term on ``doc_id`` plus add,
which Tantivy orders by opstamp, so repeated upserts of one id in a batch keep only the last.
"""

from __future__ import annotations

import contextlib
import html
import json
import sqlite3
import threading
import time
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType
from typing import Self

import tantivy

from wsp_core.ids import DocId
from wsp_core.model import DocRecord, SearchHit
from wsp_index.linkrank import RANK_FILE, StaticRank
from wsp_index.localfs import ensure_local
from wsp_index.suggest import (
    DEFAULT_HEAVY_THRESHOLD,
    DEFAULT_TOP_K,
    TRIE_FILE,
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

_META_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS titles (
    doc_id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL,
    title TEXT NOT NULL,
    lang TEXT NOT NULL,
    prior REAL NOT NULL DEFAULT 0
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS titles_prior ON titles(prior);
CREATE TABLE IF NOT EXISTS aliases (
    doc_id TEXT NOT NULL,
    alias TEXT NOT NULL,
    PRIMARY KEY (doc_id, alias)
) WITHOUT ROWID;
"""


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
            {"languages": sorted(self.languages), "store_text": self.store_text, "analyzer": ANALYZER_VERSION}
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
    ) -> None:
        self.config = config or TantivyConfig()
        # Document text by id for snippets when the index stores none (the Refinery document store).
        self.texts = texts
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

        index_dir = self.path / "tantivy"
        index_dir.mkdir(exist_ok=True)
        if tantivy.Index.exists(str(index_dir)):
            self._index = tantivy.Index.open(str(index_dir))
        else:
            self._index = tantivy.Index(self._build_schema(), path=str(index_dir))
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
            self._suggest_stale = dirty
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
        """Few enough titles that a suggest rebuild costs milliseconds, not minutes and gigabytes."""
        cap = self.config.suggest_open_rebuild_titles
        return self._title_count(cap + 1) <= cap

    def _title_count(self, cap: int) -> int:
        """Number of titles, counting at most ``cap`` (cheap on a huge table)."""
        row = self._db.execute("SELECT COUNT(*) FROM (SELECT 1 FROM titles LIMIT ?)", (cap,)).fetchone()
        return int(row[0])

    def has_source(self, source_id: str) -> bool:
        """Whether any document of ``source_id`` is indexed (a primary-key range probe)."""
        lo = f"wsp:{source_id}/"
        hi = f"wsp:{source_id}0"  # "0" sorts right after "/"
        with self._lock:
            row = self._db.execute(
                "SELECT 1 FROM titles WHERE doc_id >= ? AND doc_id < ? LIMIT 1", (lo, hi)
            ).fetchone()
        return row is not None

    def source_doc_ids(self, source_id: str, *, batch: int = 50_000) -> Iterator[list[str]]:
        """Indexed doc ids of ``source_id`` in batches (primary-key order, keyset-paged)."""
        last, hi = f"wsp:{source_id}/", f"wsp:{source_id}0"
        while True:
            with self._lock:
                rows = self._db.execute(
                    "SELECT doc_id FROM titles WHERE doc_id > ? AND doc_id < ? ORDER BY doc_id LIMIT ?",
                    (last, hi, batch),
                ).fetchall()
            if not rows:
                return
            ids = [str(r[0]) for r in rows]
            yield ids
            last = ids[-1]

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
            raise SchemaMismatchError(f"index at {self.path} was built with {existing}, config wants {sig}")

    def _build_schema(self) -> tantivy.Schema:
        sb = tantivy.SchemaBuilder()
        sb.add_text_field("doc_id", stored=True, tokenizer_name="raw")
        sb.add_text_field("source_id", stored=True, tokenizer_name="raw")
        sb.add_text_field("lang", stored=True, tokenizer_name="raw")
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
            rows: list[tuple[str, str, str, str]] = []
            aliases: list[tuple[str, str]] = []
            for doc in docs:
                route = self.route(doc.lang)
                tdoc = tantivy.Document()
                tdoc.add_text("doc_id", doc.doc_id)
                tdoc.add_text("source_id", doc.source_id)
                tdoc.add_text("lang", doc.lang)
                tdoc.add_text(f"title_{route}", doc.title)
                tdoc.add_text(f"text_{route}", doc.text)
                if replace:
                    writer.delete_documents_by_term("doc_id", doc.doc_id)
                writer.add_document(tdoc)
                rows.append((doc.doc_id, doc.source_id, doc.title, doc.lang))
                aliases.extend((doc.doc_id, a) for a in doc.aliases if a.strip())
                n += 1
            self._db.executemany(
                "INSERT INTO titles(doc_id, source_id, title, lang) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(doc_id) DO UPDATE SET source_id=excluded.source_id, title=excluded.title, "
                "lang=excluded.lang",
                rows,
            )
            # DocRecord.aliases add to (never replace) aliases set with set_aliases().
            self._db.executemany("INSERT OR IGNORE INTO aliases(doc_id, alias) VALUES (?, ?)", aliases)
            self._stage_suggest([r[0] for r in rows])
            self._after_write(n)
        return n

    def delete(self, doc_ids: Iterable[DocId]) -> int:
        """Delete by id. Returns the number of ids processed (unknown ids are not an error)."""
        ids = list(dict.fromkeys(doc_ids))
        with self._lock:
            writer = self._get_writer()
            for doc_id in ids:
                writer.delete_documents_by_term("doc_id", doc_id)
            self._db.executemany("DELETE FROM titles WHERE doc_id=?", [(d,) for d in ids])
            self._db.executemany("DELETE FROM aliases WHERE doc_id=?", [(d,) for d in ids])
            if self._suggest_room(len(ids)):
                gone = set(ids)
                # Commit applies removals before upserts, so drop staged upserts of these ids.
                self._pending_suggest = [e for e in self._pending_suggest if e.doc_id not in gone]
                self._pending_removed.extend(ids)
            self._after_write(len(ids))
        return len(ids)

    def set_priors(self, priors: Mapping[str, float]) -> int:
        """Set suggest popularity priors (e.g. ``log1p(inlinks)``); visible after ``commit()``."""
        with self._lock:
            cur = self._db.executemany(
                "UPDATE titles SET prior=? WHERE doc_id=?", [(float(v), k) for k, v in priors.items()]
            )
            self._stage_suggest(list(priors))
            return cur.rowcount

    def set_aliases(self, aliases: Mapping[str, Sequence[str]]) -> None:
        """Replace the alias list of each given document (redirects, alternative names)."""
        with self._lock:
            ids = [(d,) for d in aliases]
            self._db.executemany("DELETE FROM aliases WHERE doc_id=?", ids)
            self._db.executemany(
                "INSERT OR IGNORE INTO aliases(doc_id, alias) VALUES (?, ?)",
                [(d, a) for d, names in aliases.items() for a in names if a.strip()],
            )
            self._stage_suggest(list(aliases))

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

    def _stage_suggest(self, doc_ids: list[str]) -> None:
        if self._suggest_room(len(doc_ids)):
            self._pending_suggest.extend(self._load_entries(doc_ids))

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

    def _load_entries(self, doc_ids: Sequence[str]) -> list[SuggestEntry]:
        out: list[SuggestEntry] = []
        for i in range(0, len(doc_ids), 500):
            chunk = doc_ids[i : i + 500]
            marks = ",".join("?" * len(chunk))  # only placeholders are interpolated below
            alias_sql = f"SELECT doc_id, alias FROM aliases WHERE doc_id IN ({marks})"  # noqa: S608
            title_sql = f"SELECT doc_id, title, prior FROM titles WHERE doc_id IN ({marks})"  # noqa: S608
            aliases: dict[str, list[str]] = {}
            for d, a in self._db.execute(alias_sql, chunk):
                aliases.setdefault(d, []).append(a)
            for d, title, prior in self._db.execute(title_sql, chunk):
                out.append(SuggestEntry(d, title, tuple(aliases.get(d, ())), float(prior)))
        return out

    def _all_entries(self) -> Iterable[SuggestEntry]:
        """Titles with their aliases, best prior first, until ``suggest_max_keys`` keys (streamed:
        the ``titles_prior`` index walks in order without sorting 40M rows)."""
        budget = self.config.suggest_max_keys
        cur = self._db.cursor()
        cur.execute("SELECT doc_id, title, prior FROM titles ORDER BY prior DESC")
        while budget > 0 and (rows := cur.fetchmany(2_000)):
            ids = [r[0] for r in rows]
            marks = ",".join("?" * len(ids))  # only placeholders are interpolated
            aliases: dict[str, list[str]] = {}
            for d, a in self._db.execute(f"SELECT doc_id, alias FROM aliases WHERE doc_id IN ({marks})", ids):  # noqa: S608
                aliases.setdefault(d, []).append(a)
            for d, title, prior in rows:
                entry = SuggestEntry(d, title, tuple(aliases.get(d, ())), float(prior))
                budget -= 1 + len(entry.aliases)
                yield entry
                if budget <= 0:
                    break
        cur.close()

    def _rebuild_suggest(self) -> None:
        trie = TitleTrie.build(
            self._all_entries(),
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
        try:
            st = (self._suggest_dir / TRIE_FILE).stat()
        except OSError:
            return (0, 0, 0)
        return (st.st_ino, st.st_mtime_ns, st.st_size)

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

    def iter_aliases(self, *, batch: int = 50_000) -> Iterator[tuple[str, str]]:
        """Every ``(doc_id, alias)`` pair (redirects and alternative names), in batches."""
        last = ("", "")
        while True:
            with self._lock:
                rows = self._db.execute(
                    "SELECT doc_id, alias FROM aliases WHERE (doc_id, alias) > (?, ?) "
                    "ORDER BY doc_id, alias LIMIT ?",
                    (*last, batch),
                ).fetchall()
            if not rows:
                return
            for d, a in rows:
                yield str(d), str(a)
            last = (str(rows[-1][0]), str(rows[-1][1]))

    # --- reads ---------------------------------------------------------------------------------

    @property
    def num_docs(self) -> int:
        return self._index.searcher().num_docs

    def lookup(self, doc_id: str) -> SearchHit | None:
        """Title and source of an indexed document (for fusing hits from other retrievers)."""
        with self._lock:
            row = self._db.execute("SELECT title, source_id FROM titles WHERE doc_id=?", (doc_id,)).fetchone()
        return None if row is None else SearchHit(DocId(doc_id), 0.0, str(row[0]), "", str(row[1]))

    def parse(self, query: str) -> tantivy.Query | None:
        text = drop_stopwords(sanitize_query(query))
        if not text:
            return None
        fields = [f"{kind}_{lang}" for lang in self._routes for kind in ("title", "text")]
        boosts = {f"title_{lang}": self.config.title_boost for lang in self._routes}
        parsed, _errors = self._index.parse_query_lenient(text, fields, field_boosts=boosts)
        words = text.split()
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

    def search(
        self, query: str, *, limit: int = 10, offset: int = 0, sources: Sequence[str] | None = None
    ) -> Sequence[SearchHit]:
        return self._search(query, limit=limit, offset=offset, sources=sources, snippets=True)

    def search_hits(
        self, query: str, *, limit: int = 10, offset: int = 0, sources: Sequence[str] | None = None
    ) -> Sequence[SearchHit]:
        """Like :meth:`search` without snippets: much cheaper for wide windows (totals, facets)."""
        return self._search(query, limit=limit, offset=offset, sources=sources, snippets=False)

    def _search(
        self, query: str, *, limit: int, offset: int, sources: Sequence[str] | None, snippets: bool
    ) -> Sequence[SearchHit]:
        if limit <= 0 or offset < 0:
            return []
        parsed = self.parse(query)
        if parsed is None:
            return []
        clauses: list[tuple[tantivy.Occur, tantivy.Query]] = [(tantivy.Occur.Must, parsed)]
        if sources is not None:
            if not sources:
                return []
            source_filter = tantivy.Query.term_set_query(self._schema, "source_id", list(sources))
            clauses.append((tantivy.Occur.Must, source_filter))
        clauses += self._prior_clauses
        q = parsed if len(clauses) == 1 else tantivy.Query.boolean_query(clauses)
        searcher = self._index.searcher()
        rank = self.static_rank() if self.config.rank_weight > 0 else None
        depth = self.config.rank_depth
        if rank is None or depth <= 0 or offset >= depth:
            result = searcher.search(q, limit, count=False, offset=offset)
            scored = [(float(sc), searcher.doc(a)) for sc, a in result.hits]
        else:
            result = searcher.search(q, max(depth, offset + limit), count=False, offset=0)
            pool = [(float(sc), searcher.doc(a)) for sc, a in result.hits]
            head, tail = pool[:depth], pool[depth:]
            feats = rank.features([str(d.get_first("doc_id")) for _, d in head])
            w = self.config.rank_weight
            head = [(sc + w * float(f), d) for (sc, d), f in zip(head, feats, strict=True)]
            head.sort(key=lambda t: -t[0])
            scored = [*head, *tail][offset : offset + limit]
        cut = self._snippet_map(searcher, parsed, [d for _, d in scored]) if snippets else {}
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
        return hits

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
                gen = tantivy.SnippetGenerator.create(searcher, parsed, self._schema, f"text_{route}")
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
