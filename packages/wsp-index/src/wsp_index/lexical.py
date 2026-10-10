"""``LexicalIndex`` on Tantivy: BM25 over per-language fields, snippets, source filter, suggest.

On-disk layout under the index directory (local disk only, ADR-0002)::

    tantivy/        Tantivy index
    meta.sqlite     doc_id -> (source, title, lang, prior) and aliases; feeds suggest and ``lookup``
    suggest/        mmapped MARISA tries (see ``wsp_index.suggest``)

Per-language analysis: each configured language ``L`` gets ``title_L``/``text_L`` fields analyzed
with ``wsp_L`` (Snowball stemmer where Tantivy has one); every other language goes to
``title_any``/``text_any`` (no stemming). Queries run against all of them, so a mixed-language
library is searched in one pass and adding a language only needs a reindex of that language.

Writes are buffered by Tantivy and become visible on ``commit()`` (batched commits; optionally
automatic every ``commit_every`` operations). ``upsert`` is delete-by-term on ``doc_id`` plus add,
which Tantivy orders by opstamp, so repeated upserts of one id in a batch keep only the last.
"""

from __future__ import annotations

import html
import json
import sqlite3
import threading
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Self

import tantivy

from wsp_core.ids import DocId
from wsp_core.model import DocRecord, SearchHit
from wsp_index.localfs import ensure_local
from wsp_index.suggest import (
    DEFAULT_HEAVY_THRESHOLD,
    DEFAULT_TOP_K,
    SuggestEntry,
    Suggester,
    TitleTrie,
)
from wsp_index.text import FALLBACK_LANG, build_analyzer, primary_lang, sanitize_query

_META_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS titles (
    doc_id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL,
    title TEXT NOT NULL,
    lang TEXT NOT NULL,
    prior REAL NOT NULL DEFAULT 0
) WITHOUT ROWID;
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
    writer_heap_bytes: int = 256 * 1024 * 1024
    writer_threads: int = 0  # 0 = Tantivy default (one per core, capped at 8)
    commit_every: int | None = None
    """Commit automatically after this many buffered upserts/deletes (``None``: only explicit)."""
    snippet_chars: int = 200
    store_text: bool = True
    """Store body text in Tantivy (needed for highlighted snippets; costs roughly 40% of text size)."""
    suggest_rebuild_threshold: int = 2_000
    """Rebuild the suggest trie at commit when more than this many titles changed since the last build."""
    suggest_heavy_threshold: int = DEFAULT_HEAVY_THRESHOLD
    suggest_top_k: int = DEFAULT_TOP_K

    def schema_signature(self) -> str:
        return json.dumps({"languages": sorted(self.languages), "store_text": self.store_text})


class SchemaMismatchError(RuntimeError):
    pass


class TantivyIndex:
    """Reference ``LexicalIndex``. One writer per index directory; searches are thread-safe."""

    def __init__(self, path: Path, config: TantivyConfig | None = None) -> None:
        self.config = config or TantivyConfig()
        self.path = ensure_local(path)
        self._langs: tuple[str, ...] = tuple(dict.fromkeys(primary_lang(x) for x in self.config.languages))
        self._routes: tuple[str, ...] = (*self._langs, FALLBACK_LANG)
        self._lock = threading.RLock()

        self._db = sqlite3.connect(self.path / "meta.sqlite", check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
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

        self._suggest_dir = self.path / "suggest"
        self._suggester = Suggester()
        self._pending_suggest: list[SuggestEntry] = []
        self._pending_removed: list[str] = []
        self._suggest_stale = False
        if TitleTrie.exists(self._suggest_dir) and self._meta("suggest_dirty") != "1":
            self._suggester.replace_base(TitleTrie.open(self._suggest_dir))
        else:
            self._rebuild_suggest()

    # --- setup ---------------------------------------------------------------------------------

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
        return self._writer

    # --- LexicalIndex: writes ------------------------------------------------------------------

    def upsert(self, docs: Iterable[DocRecord]) -> int:
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

    def commit(self) -> None:
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
            if self._suggest_stale:
                self._suggest_stale = False
                self._rebuild_suggest()

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
                self._rebuild_suggest()
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
        cur = self._db.execute(
            "SELECT t.doc_id, t.title, t.prior, group_concat(a.alias, char(30)) "
            "FROM titles t LEFT JOIN aliases a ON a.doc_id = t.doc_id GROUP BY t.doc_id"
        )
        for d, title, prior, aliases in cur:
            yield SuggestEntry(d, title, tuple(aliases.split("\x1e")) if aliases else (), float(prior))

    def _rebuild_suggest(self) -> None:
        trie = TitleTrie.build(
            self._all_entries(),
            self._suggest_dir,
            heavy_threshold=self.config.suggest_heavy_threshold,
            top_k=self.config.suggest_top_k,
        )
        self._suggester.replace_base(trie)
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
        return self._suggester.suggest(prefix, limit=limit)

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
        text = sanitize_query(query)
        if not text:
            return None
        fields = [f"{kind}_{lang}" for lang in self._routes for kind in ("title", "text")]
        boosts = {f"title_{lang}": self.config.title_boost for lang in self._routes}
        parsed, _errors = self._index.parse_query_lenient(text, fields, field_boosts=boosts)
        return parsed

    def search(
        self, query: str, *, limit: int = 10, offset: int = 0, sources: Sequence[str] | None = None
    ) -> Sequence[SearchHit]:
        if limit <= 0 or offset < 0:
            return []
        parsed = self.parse(query)
        if parsed is None:
            return []
        q = parsed
        if sources is not None:
            if not sources:
                return []
            source_filter = tantivy.Query.term_set_query(self._schema, "source_id", list(sources))
            q = tantivy.Query.boolean_query(
                [(tantivy.Occur.Must, parsed), (tantivy.Occur.Must, source_filter)]
            )
        searcher = self._index.searcher()
        result = searcher.search(q, limit, count=False, offset=offset)
        generators: dict[str, tantivy.SnippetGenerator] = {}
        hits: list[SearchHit] = []
        for score, address in result.hits:
            doc = searcher.doc(address)
            route = self.route(str(doc.get_first("lang") or ""))
            title = str(doc.get_first(f"title_{route}") or "")
            snippet = ""
            if self.config.store_text:
                gen = generators.get(route)
                if gen is None:
                    gen = tantivy.SnippetGenerator.create(searcher, parsed, self._schema, f"text_{route}")
                    gen.set_max_num_chars(self.config.snippet_chars)
                    generators[route] = gen
                snippet = gen.snippet_from_doc(doc).to_html()
                if not snippet:
                    snippet = _lead(str(doc.get_first(f"text_{route}") or ""), self.config.snippet_chars)
            hits.append(
                SearchHit(
                    doc_id=DocId(str(doc.get_first("doc_id"))),
                    score=float(score),
                    title=title,
                    snippet=snippet,
                    source_id=str(doc.get_first("source_id") or ""),
                )
            )
        return hits


def _lead(text: str, max_chars: int) -> str:
    """First ``max_chars`` of ``text`` cut at a word boundary, HTML-escaped (snippet fallback)."""
    if len(text) <= max_chars:
        return html.escape(text)
    cut = text[:max_chars]
    space = cut.rfind(" ")
    return html.escape(cut[: space if space > max_chars // 2 else max_chars]) + "…"
