"""``VectorIndex`` for passages: centered binary HNSW, int8 rescoring from disk (PLAN §9.4).

Layout of a store directory (local disk only, ADR-0002)::

    meta.sqlite        chunk id <-> integer key, doc id, optional passage text; per-doc content hash
    hnsw-b1.usearch    usearch HNSW graph over sign bits (hamming), dimensions/8 bytes per vector
    int8.bin           int8 vectors, row = key (dimensions bytes per row), read with MADV_RANDOM
    scale.f32          per-row dequantization scale (float32), held in RAM (4 bytes a row)
    mean.f32           the mean vector the binary codes are centered on (format 2)

**Codes.** A passage's code is ``sign(x - mean)``, not ``sign(x)``: embeddings share a large common
direction (norm 0.869 for e5-base on this library, 429 of 768 raw bits more than 90% constant), so
raw signs waste most bits. Measured on 60k real passages, recall@10 of the exact top 10 among 80
hamming candidates: 0.573 raw, 0.905 centered (0.953 at 160). The mean is fixed when the graph is
first built (from up to 200k stored rows) and kept in ``mean.f32``; format-1 stores keep raw codes
(mean 0) until they are rebuilt.

**Search.** The query's code finds ``limit * oversample`` candidates in the graph, which are rescored
exactly against their int8 rows (``MADV_RANDOM``: 2048 cold random rows read 9.4 MB instead of
5.7 GB with the kernel's 8 MB readahead) and cut to ``limit``. An ``allow`` predicate (a source
filter) widens the candidate set until enough allowed passages are found.

**Writes and durability.** Writes are append-only: an updated chunk gets a fresh key, the old key
leaves the graph (its int8 row becomes garbage until a rebuild). With ``defer_graph`` (an embed run)
rows and ids are written but the graph is left alone until :meth:`build_graph` adds every missing key
once at the end, instead of rewriting a multi-GB graph every few hundred thousand passages. :meth:`save`
makes rows durable (fsync), then the graph (temporary file, fsync, rename, directory fsync), then the
id map (SQLite commit, ``synchronous=FULL``), and records the graph's size and key count. On open a
store whose graph does not match that record (a crash between the steps) is repaired: keys the id map
lacks leave the graph, keys the graph lacks are re-added from their int8 rows.

**View mode** (the API) never writes: SQLite opens read-only in autocommit, so no read transaction
pins the WAL, and :meth:`refresh` reopens the graph after an embed run replaced it.
"""

from __future__ import annotations

import contextlib
import mmap
import os
import sqlite3
import threading
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

import numpy as np
import numpy.typing as npt
from usearch.compiled import MetricKind, ScalarKind
from usearch.index import Index

from wsp_index import ensure_local
from wsp_oracle.chunker import Chunk, doc_of, make_chunk_id

FORMAT = "2"
FORMATS = ("1", "2")
GRAPH_FILE = "hnsw-b1.usearch"
INT8_FILE = "int8.bin"
SCALE_FILE = "scale.f32"
MEAN_FILE = "mean.f32"
META_FILE = "meta.sqlite"
MEAN_SAMPLE = 200_000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS chunks (
    key INTEGER PRIMARY KEY,
    chunk_id TEXT NOT NULL UNIQUE,
    doc_id TEXT NOT NULL,
    text TEXT
);
CREATE INDEX IF NOT EXISTS chunks_doc ON chunks(doc_id);
CREATE TABLE IF NOT EXISTS docs (
    doc_id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    generation TEXT NOT NULL,
    chunks INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS docs_source ON docs(source_id, generation);
"""

F32 = npt.NDArray[np.float32]
U64 = npt.NDArray[np.uint64]
I64 = npt.NDArray[np.int64]


def _in(prefix: str, marks: str) -> str:
    """``<prefix> (?, ?, ...)``: placeholders only, values are always bound parameters."""
    return f"{prefix} ({marks})"


class VectorStoreError(RuntimeError):
    pass


def quantize_int8(mat: F32) -> tuple[npt.NDArray[np.int8], F32]:
    """Symmetric per-row int8 quantization: ``row ≈ q * scale``."""
    peak = np.abs(mat).max(axis=1)
    scale = np.where(peak > 0, peak / 127.0, 1.0).astype(np.float32)
    q = np.clip(np.rint(mat / scale[:, None]), -127, 127).astype(np.int8)
    return q, scale


def binarize(mat: F32 | npt.NDArray[np.int8], mean: F32 | None = None) -> npt.NDArray[np.uint8]:
    """Sign bits of ``mat - mean`` (``mean`` None: of ``mat``), packed 8 to a byte."""
    if mean is None:
        return np.packbits(mat > 0, axis=1)
    return np.packbits(np.asarray(mat, dtype=np.float32) - mean > 0, axis=1)


def _normalize(mat: F32) -> F32:
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return (mat / norms).astype(np.float32, copy=False)


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _file_stamp(path: Path) -> str:
    try:
        st = path.stat()
    except OSError:
        return "absent"
    return f"{st.st_size}"


class ChunkVectorStore:
    """A ``VectorIndex`` over chunk ids (any string id works; ``doc_of`` maps chunk ids to docs)."""

    def __init__(
        self,
        path: Path,
        dimensions: int,
        *,
        model: str = "",
        mode: str = "load",
        connectivity: int = 16,
        expansion_add: int = 128,
        expansion_search: int = 96,
        oversample: int = 4,
        max_candidates: int = 2048,
        store_text: bool = True,
        threads: int = 0,
        defer_graph: bool = False,
    ) -> None:
        if dimensions < 8 or dimensions % 8:
            raise ValueError("dimensions must be a positive multiple of 8 (binary codes)")
        if mode not in ("load", "view"):
            raise ValueError("mode must be 'load' or 'view'")
        self.view = mode == "view"
        if self.view and not (path / META_FILE).is_file():
            raise VectorStoreError(f"{path}: no vector store to view")
        if not self.view:
            path.mkdir(parents=True, exist_ok=True)
        self.path = ensure_local(path)
        self.dimensions = dimensions
        self.mode = mode
        self.oversample = oversample
        self.max_candidates = max_candidates
        self.store_text = store_text
        self.threads = threads
        self.defer_graph = defer_graph
        self.index_options: dict[str, Any] = {
            "ndim": dimensions,
            "metric": MetricKind.Hamming,
            "dtype": ScalarKind.B1,
            "connectivity": connectivity,
            "expansion_add": expansion_add,
            "expansion_search": expansion_search,
        }
        self._lock = threading.RLock()
        self._closed = False
        self._graph_dirty = False

        if self.view:
            uri = f"file:{self.path / META_FILE}?mode=ro"
            self._db = sqlite3.connect(uri, uri=True, check_same_thread=False, isolation_level=None)
            self._db.execute("PRAGMA query_only=1")
        else:
            self._db = sqlite3.connect(self.path / META_FILE, check_same_thread=False, isolation_level=None)
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=FULL")  # commits happen only in save()
            self._db.executescript(_SCHEMA)
            columns = {str(r[1]) for r in self._db.execute("PRAGMA table_info(docs)")}
            if "lang" not in columns:  # added with per-language embed runs; NULL for older rows
                self._db.execute("ALTER TABLE docs ADD COLUMN lang TEXT")
        self._check_meta(model)
        if not self.view:
            self._db.execute("BEGIN")

        self._index = Index(**self.index_options)
        self._graph_stamp = ""
        self._open_graph()
        self._int8_f = None if self.view else (self.path / INT8_FILE).open("ab+")
        self._scale_f = None if self.view else (self.path / SCALE_FILE).open("ab+")
        self._rows: npt.NDArray[np.int8] = np.zeros((0, dimensions), dtype=np.int8)
        self._scales: F32 = np.zeros(0, dtype=np.float32)
        self._next_key = int(self._meta("next_key") or 0)
        self._mean = self._load_mean()
        self._remap()
        if not self.view and not self._consistent():
            self._repair()

    # --- metadata ------------------------------------------------------------------------------

    def _meta(self, key: str) -> str | None:
        row = self._db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return None if row is None else str(row[0])

    def _set_meta(self, key: str, value: str) -> None:
        self._db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, value))

    def _check_meta(self, model: str) -> None:
        dims, fmt, stored_model = self._meta("dimensions"), self._meta("format"), self._meta("model")
        if dims is None:
            if self.view:
                raise VectorStoreError(f"{self.path}: empty vector store")
            self._set_meta("dimensions", str(self.dimensions))
            self._set_meta("format", FORMAT)
            self._set_meta("model", model)
            return
        if int(dims) != self.dimensions or fmt not in FORMATS:
            raise VectorStoreError(f"{self.path}: built with {dims} dimensions (format {fmt}); rebuild it")
        if model and stored_model and model != stored_model:
            raise VectorStoreError(
                f"{self.path}: built with model {stored_model!r}, not {model!r}; rebuild it"
            )

    @property
    def model(self) -> str:
        return self._meta("model") or ""

    @property
    def format(self) -> str:
        return self._meta("format") or FORMAT

    def _load_mean(self) -> F32 | None:
        path = self.path / MEAN_FILE
        if path.is_file():
            mean = np.fromfile(path, dtype=np.float32)
            if mean.shape == (self.dimensions,):
                return mean
            raise VectorStoreError(f"{path}: {mean.size} values, want {self.dimensions}")
        if self.format == "1" or len(self._index):
            return np.zeros(self.dimensions, dtype=np.float32)  # raw sign codes (format 1)
        return None  # fixed when the graph is first built

    # --- graph ---------------------------------------------------------------------------------

    def _open_graph(self) -> None:
        graph = self.path / GRAPH_FILE
        self._index = Index(**self.index_options)
        if graph.exists():
            if self.view:
                self._index.view(str(graph))  # pyright: ignore[reportUnknownMemberType] - PathLike[Unknown] in usearch's hints
            else:
                self._index.load(str(graph))  # pyright: ignore[reportUnknownMemberType] - PathLike[Unknown] in usearch's hints
        self._graph_stamp = _file_stamp(graph)

    def _consistent(self) -> bool:
        """Does the graph on disk match the one the last committed save recorded?"""
        want_stamp, want_count = self._meta("graph_stamp"), self._meta("graph_count")
        if want_stamp is not None and (
            want_stamp != self._graph_stamp or want_count != str(len(self._index))
        ):
            return False  # the graph on disk is not the one the last commit recorded
        if self._meta("deferred") == "1":
            return True  # an embed run defers the graph: build_graph() completes it
        live = int(self._db.execute("SELECT count(*) FROM chunks").fetchone()[0])
        return live == len(self._index) and self._meta("dirty") != "1"

    def refresh(self) -> bool:
        """View mode: reopen the graph, rows and mean if a writer replaced them; True when it did."""
        with self._lock:
            if _file_stamp(self.path / GRAPH_FILE) == self._graph_stamp and (
                os.path.getsize(self.path / SCALE_FILE) if (self.path / SCALE_FILE).exists() else 0
            ) // 4 == len(self._scales):
                return False
            self._open_graph()
            self._mean = self._load_mean()
            self._remap()
            return True

    def stamp(self) -> tuple[str, int]:
        """What changes when the store's searchable content changes (graph file, row count)."""
        return self._graph_stamp, len(self._scales)

    # --- int8 rows -----------------------------------------------------------------------------

    def _remap(self) -> None:
        if self._int8_f is not None and self._scale_f is not None:
            self._int8_f.flush()
            self._scale_f.flush()
        scale_path = self.path / SCALE_FILE
        n = scale_path.stat().st_size // 4 if scale_path.exists() else 0
        if n == 0:
            self._rows = np.zeros((0, self.dimensions), dtype=np.int8)
            self._scales = np.zeros(0, dtype=np.float32)
            return
        with (self.path / INT8_FILE).open("rb") as fh:
            mm = mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ)
        if hasattr(mmap, "MADV_RANDOM"):
            mm.madvise(mmap.MADV_RANDOM)  # the kernel's readahead would read MBs per candidate row
        n = min(n, len(mm) // self.dimensions)
        self._rows = np.frombuffer(mm, dtype=np.int8, count=n * self.dimensions).reshape(n, self.dimensions)
        self._scales = np.fromfile(scale_path, dtype=np.float32, count=n)  # 4 bytes a row: in RAM

    def _append_rows(self, mat: F32) -> U64:
        if self._int8_f is None or self._scale_f is None:
            raise VectorStoreError("vector store opened read-only (mode='view')")
        q, scale = quantize_int8(mat)
        start = self._next_key
        expect = start * self.dimensions
        self._int8_f.seek(0, os.SEEK_END)
        if self._int8_f.tell() != expect:  # a torn tail from a crash: cut back to the last key
            self._int8_f.truncate(expect)
            self._scale_f.truncate(start * 4)
        self._int8_f.write(q.tobytes())
        self._scale_f.seek(0, os.SEEK_END)
        self._scale_f.write(scale.tobytes())
        self._next_key = start + len(mat)
        return np.arange(start, self._next_key, dtype=np.uint64)

    def _dequantized(self, keys: I64) -> F32:
        return self._rows[keys].astype(np.float32) * self._scales[keys][:, None]

    # --- writes --------------------------------------------------------------------------------

    def _writable(self) -> None:
        if self._closed:
            raise VectorStoreError("vector store is closed")
        if self.view:
            raise VectorStoreError("vector store opened read-only (mode='view')")

    def _remove_keys(self, keys: Sequence[int]) -> None:
        if keys and not self.defer_graph:
            present = np.asarray(keys, dtype=np.uint64)
            present = (
                present[np.asarray(self._index.contains(present), dtype=bool)]
                if len(self._index)
                else present[:0]
            )
            if present.size:
                self._index.remove(present)
                self._graph_dirty = True

    def _ensure_mean(self, first: F32) -> F32:
        if self._mean is None:
            live = self._live_keys()
            if live.size:
                pick = np.sort(
                    np.random.default_rng(0).choice(live, min(MEAN_SAMPLE, live.size), replace=False)
                )
                sample = _normalize(self._dequantized(pick.astype(np.int64)))
            else:
                sample = first
            self._mean = sample.mean(axis=0).astype(np.float32)
            tmp = self.path / (MEAN_FILE + ".partial")
            with tmp.open("wb") as fh:
                fh.write(self._mean.tobytes())
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path / MEAN_FILE)
            _fsync_dir(self.path)
            self._set_meta("format", FORMAT)
        return self._mean

    def upsert(self, ids: Sequence[str], vectors: Sequence[Sequence[float]]) -> None:
        if len(ids) != len(vectors):
            raise ValueError("ids and vectors differ in length")
        if not ids:
            return
        mat = np.asarray(vectors, dtype=np.float32).reshape(len(ids), -1)
        self.upsert_matrix(ids, mat)

    def upsert_matrix(
        self, ids: Sequence[str], mat: F32, *, texts: Sequence[str | None] | None = None
    ) -> None:
        if mat.shape != (len(ids), self.dimensions):
            raise ValueError(f"expected {len(ids)} x {self.dimensions} vectors, got {mat.shape}")
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate ids in one upsert")
        bad = ~np.isfinite(mat).all(axis=1)
        if bad.any():
            raise VectorStoreError(
                f"non-finite embeddings for {int(bad.sum())} passage(s), e.g. {ids[int(np.argmax(bad))]!r}"
            )
        mat = _normalize(mat)
        with self._lock:
            self._writable()
            old = self._keys_of(ids)
            self._remove_keys(list(old.values()))
            keys = self._append_rows(mat)
            if not self.defer_graph:
                self._index.add(keys, binarize(mat, self._ensure_mean(mat)), threads=self.threads)
                self._graph_dirty = True
            rows = (
                (int(k), cid, doc_of(cid), (texts[i] if texts is not None and self.store_text else None))
                for i, (k, cid) in enumerate(zip(keys.tolist(), ids, strict=True))
            )
            self._db.executemany(
                "INSERT INTO chunks(key, chunk_id, doc_id, text) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(chunk_id) DO UPDATE SET key=excluded.key, doc_id=excluded.doc_id, "
                "text=excluded.text",
                rows,
            )
            self._set_meta("next_key", str(self._next_key))

    def add_chunks(self, chunks: Sequence[Chunk], mat: F32) -> None:
        """Upsert passages with their text (kept for the reranker when ``store_text``)."""
        self.upsert_matrix([c.chunk_id for c in chunks], mat, texts=[c.text for c in chunks])

    def _keys_of(self, ids: Sequence[str]) -> dict[str, int]:
        out: dict[str, int] = {}
        for start in range(0, len(ids), 900):
            part = list(ids[start : start + 900])
            marks = ",".join("?" * len(part))
            for cid, key in self._db.execute(
                _in("SELECT chunk_id, key FROM chunks WHERE chunk_id IN", marks), part
            ):
                out[str(cid)] = int(key)
        return out

    def delete(self, ids: Sequence[str]) -> None:
        if not ids:
            return
        with self._lock:
            self._writable()
            old = self._keys_of(ids)
            self._remove_keys(list(old.values()))
            self._db.executemany("DELETE FROM chunks WHERE chunk_id=?", ((c,) for c in old))

    def delete_docs(self, doc_ids: Iterable[str]) -> int:
        """Remove every chunk of these documents (and their recorded embed state)."""
        n = 0
        with self._lock:
            self._writable()
            for doc_id in doc_ids:
                rows = self._db.execute("SELECT key FROM chunks WHERE doc_id=?", (doc_id,)).fetchall()
                self._remove_keys([int(r[0]) for r in rows])
                self._db.execute("DELETE FROM chunks WHERE doc_id=?", (doc_id,))
                self._db.execute("DELETE FROM docs WHERE doc_id=?", (doc_id,))
                n += len(rows)
        return n

    # --- per-document embed state (resume and incremental updates) ----------------------------

    def doc_hashes(self, doc_ids: Sequence[str]) -> dict[str, str]:
        out: dict[str, str] = {}
        with self._lock:
            for start in range(0, len(doc_ids), 900):
                part = list(doc_ids[start : start + 900])
                marks = ",".join("?" * len(part))
                q = _in("SELECT doc_id, content_hash FROM docs WHERE doc_id IN", marks)
                out.update((str(d), str(h)) for d, h in self._db.execute(q, part))
        return out

    def record_docs(
        self, rows: Iterable[tuple[str, str, str, str, int] | tuple[str, str, str, str, int, str | None]]
    ) -> None:
        """``(doc_id, source_id, content_hash, generation, chunks[, lang])`` of embedded documents."""
        full = ((*r, None) if len(r) == 5 else r for r in rows)
        with self._lock:
            self._writable()
            self._db.executemany(
                "INSERT OR REPLACE INTO docs(doc_id, source_id, content_hash, generation, chunks, lang) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                full,
            )

    def stale_docs(self, source_id: str, generation: str, langs: Iterable[str] | None = None) -> list[str]:
        """Documents of ``source_id`` recorded under any generation other than ``generation``;
        with ``langs``, only documents recorded in one of those languages (a ``--langs`` run must
        not remove the other languages' vectors)."""
        q = "SELECT doc_id FROM docs WHERE source_id=? AND generation<>?"
        args: list[str] = [source_id, generation]
        if langs is not None:
            wanted = sorted(langs)
            if not wanted:
                return []
            q += " AND " + _in("lang IN", ",".join("?" * len(wanted)))
            args += wanted
        with self._lock:
            rows = self._db.execute(q, args).fetchall()
        return [str(r[0]) for r in rows]

    def touch_docs(self, doc_ids: Sequence[str], generation: str) -> None:
        with self._lock:
            self._writable()
            self._db.executemany(
                "UPDATE docs SET generation=? WHERE doc_id=?", ((generation, d) for d in doc_ids)
            )

    def doc_count(self, source_id: str | None = None) -> int:
        with self._lock:
            if source_id is None:
                row = self._db.execute("SELECT count(*) FROM docs").fetchone()
            else:
                row = self._db.execute("SELECT count(*) FROM docs WHERE source_id=?", (source_id,)).fetchone()
        return int(row[0])

    # --- reads ---------------------------------------------------------------------------------

    def __len__(self) -> int:
        return int(self._index.size)

    def search(self, vector: Sequence[float], *, limit: int = 50) -> Sequence[tuple[str, float]]:
        q = np.asarray(vector, dtype=np.float32).reshape(1, -1)
        if q.shape[1] != self.dimensions:
            raise ValueError(f"expected {self.dimensions} dimensions, got {q.shape[1]}")
        return self.search_array(q[0], limit=limit)

    def search_array(
        self, query: F32, *, limit: int = 50, allow: Callable[[str], bool] | None = None
    ) -> list[tuple[str, float]]:
        """Top ``limit`` chunk ids by cosine similarity (approximate candidates, exact int8 rescoring).

        ``allow`` (e.g. a source filter) keeps only matching chunk ids; the candidate set doubles
        until ``limit`` of them are found or ``max_candidates`` is reached.
        """
        if limit <= 0 or len(self) == 0:
            return []
        q = _normalize(query.reshape(1, -1).astype(np.float32, copy=False))
        if not np.isfinite(q).all():
            raise ValueError("non-finite query vector")
        mean = self._mean if self._mean is not None else np.zeros(self.dimensions, dtype=np.float32)
        code = binarize(q, mean)[0]
        count = min(max(limit * self.oversample, limit), self.max_candidates, len(self))
        while True:
            found = self._rescored(q[0], code, count, limit, allow)
            if allow is None or len(found) >= limit or count >= min(self.max_candidates, len(self)):
                return found
            count = min(count * 2, self.max_candidates, len(self))

    def _rescored(
        self, q: F32, code: npt.NDArray[np.uint8], count: int, limit: int, allow: Callable[[str], bool] | None
    ) -> list[tuple[str, float]]:
        # Once the candidate set covers the whole store (a filter widened it, or the store is small), scan
        # exactly: an approximate graph over many near-ties can miss nodes even at k == len(self).
        matches: Any = self._index.search(code, count, exact=count >= len(self))
        keys = np.sort(np.asarray(matches.keys, dtype=np.int64))  # file order: fewer, closer page reads
        if keys.size == 0:
            return []
        rows, scales = self._rows, self._scales
        if int(keys.max()) >= len(scales):
            with self._lock:
                self._remap()
                rows, scales = self._rows, self._scales
            keys = keys[keys < len(scales)]
        sims = (rows[keys].astype(np.float32) @ q) * scales[keys]
        order = np.argsort(-sims, kind="stable")
        names = self._names(keys.tolist())
        out: list[tuple[str, float]] = []
        for i in order.tolist():
            name = names.get(int(keys[i]))
            if name is None or (allow is not None and not allow(name)):
                continue  # a key deleted since the graph was written, or filtered out
            out.append((name, float(sims[i])))
            if len(out) >= limit:
                break
        return out

    def _names(self, keys: Sequence[int]) -> dict[int, str]:
        if not keys:
            return {}
        out: dict[int, str] = {}
        with self._lock:
            for start in range(0, len(keys), 900):
                part = list(keys[start : start + 900])
                marks = ",".join("?" * len(part))
                rows = self._db.execute(_in("SELECT key, chunk_id FROM chunks WHERE key IN", marks), part)
                out.update((int(k), str(c)) for k, c in rows)
        return out

    def texts(self, chunk_ids: Sequence[str]) -> dict[str, str]:
        """Stored passage text for these chunk ids (missing when not stored)."""
        if not chunk_ids:
            return {}
        out: dict[str, str] = {}
        with self._lock:
            for start in range(0, len(chunk_ids), 900):
                part = list(chunk_ids[start : start + 900])
                marks = ",".join("?" * len(part))
                q = _in("SELECT chunk_id, text FROM chunks WHERE text IS NOT NULL AND chunk_id IN", marks)
                out.update((str(c), str(t)) for c, t in self._db.execute(q, part))
        return out

    def lead_texts(self, doc_ids: Sequence[str]) -> dict[str, str]:
        """The first passage of each document (for hits that only the lexical retriever found)."""
        found = self.texts([make_chunk_id(d, 0) for d in doc_ids])
        return {doc_of(cid): text for cid, text in found.items()}

    def _live_keys(self, batch: int = 1_000_000) -> I64:
        """Every live key, sorted, as one numpy array (no Python list of millions of ints)."""
        parts: list[I64] = []
        with self._lock:
            cur = self._db.execute("SELECT key FROM chunks ORDER BY key")
            while rows := cur.fetchmany(batch):
                parts.append(np.fromiter((r[0] for r in rows), dtype=np.int64, count=len(rows)))
        return np.concatenate(parts) if parts else np.zeros(0, dtype=np.int64)

    def exact_search(self, query: F32, *, limit: int = 10) -> list[tuple[str, float]]:
        """Brute force over the int8 rows of live keys (recall baseline; O(n))."""
        q = _normalize(query.reshape(1, -1).astype(np.float32, copy=False))[0]
        live = self._live_keys()
        with self._lock:
            self._remap()
        if live.size == 0:
            return []
        sims = (self._rows[live].astype(np.float32) @ q) * self._scales[live]
        top = np.argsort(-sims, kind="stable")[:limit]
        names = self._names(live[top].tolist())
        return [(names[int(live[i])], float(sims[i])) for i in top.tolist()]

    def iter_live(self, batch: int = 100_000) -> Iterator[tuple[I64, F32]]:
        """``(keys, dequantized rows)`` of every live key, in key order."""
        live = self._live_keys()
        for s in range(0, len(live), batch):
            keys = live[s : s + batch]
            yield keys, self._dequantized(keys)

    # --- durability ----------------------------------------------------------------------------

    def _repair(self) -> None:
        """Make the graph agree with ``meta.sqlite`` (after a crash, or a deferred build)."""
        self._remap()
        live = self._live_keys().astype(np.uint64)
        graph_keys = (
            np.asarray(self._index.keys, dtype=np.uint64) if len(self._index) else np.zeros(0, np.uint64)
        )
        stray = np.setdiff1d(graph_keys, live, assume_unique=True)
        if stray.size:
            self._index.remove(stray)
        lost = np.setdiff1d(live, graph_keys, assume_unique=True)
        if lost.size:
            mean = self._ensure_mean(np.zeros((1, self.dimensions), dtype=np.float32))
            for s in range(0, len(lost), 100_000):
                keys = lost[s : s + 100_000]
                rows = _normalize(self._dequantized(keys.astype(np.int64)))
                self._index.add(keys, binarize(rows, mean), threads=self.threads)
        self._graph_dirty = (
            self._graph_dirty or bool(stray.size or lost.size) or self._meta("graph_stamp") is None
        )
        self.save()

    def build_graph(self) -> int:
        """Add every key the graph lacks (a deferred embed run's passages), remove deleted ones, save.

        Returns the number of keys in the graph."""
        with self._lock:
            self._writable()
            deferred, self.defer_graph = self.defer_graph, False
            try:
                self._repair()
            finally:
                self.defer_graph = deferred
            return len(self)

    def save(self) -> None:
        """Rows (fsync), then the graph (temp file, fsync, rename, dir fsync), then the id map."""
        with self._lock:
            if self._closed or self.view:
                return
            if self._int8_f is None or self._scale_f is None:
                raise VectorStoreError("vector store opened read-only (mode='view')")
            for f in (self._int8_f, self._scale_f):
                f.flush()
                os.fsync(f.fileno())
            graph = self.path / GRAPH_FILE
            if self._graph_dirty or not graph.exists():
                partial = graph.with_suffix(".partial")
                self._index.save(str(partial))  # pyright: ignore[reportUnknownMemberType] - PathLike[Unknown] in usearch's hints
                fd = os.open(partial, os.O_RDONLY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
                os.replace(partial, graph)
                _fsync_dir(self.path)
                self._graph_dirty = False
            self._graph_stamp = _file_stamp(graph)
            self._set_meta("graph_stamp", self._graph_stamp)
            self._set_meta("graph_count", str(len(self._index)))
            self._set_meta("dirty", "0")
            self._set_meta("deferred", "1" if self.defer_graph else "0")
            self._set_meta("next_key", str(self._next_key))
            self._db.execute("COMMIT")
            self._db.execute("BEGIN")
            self._remap()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            if not self.view:
                self.save()
                with contextlib.suppress(sqlite3.Error):
                    self._db.execute("COMMIT")
            self._db.close()
            for f in (self._int8_f, self._scale_f):
                if f is not None:
                    f.close()
            self._closed = True

    def stats(self) -> Mapping[str, int | str]:
        with self._lock:
            chunks = int(self._db.execute("SELECT count(*) FROM chunks").fetchone()[0])
        return {
            "chunks": chunks,
            "graph_size": len(self),
            "rows_on_disk": self._next_key,
            "dimensions": self.dimensions,
            "model": self.model,
            "format": self.format,
            "memory_bytes": int(self._index.memory_usage),
        }


class DocVectorIndex:
    """Document-level ``VectorIndex`` over a :class:`ChunkVectorStore`.

    ``search`` takes ``limit * fanout`` passages and keeps each document's best one, which is what
    the document-level ``HybridSearcher`` fuses. ``upsert`` stores a whole-document vector as
    chunk ``#0`` so the port stays usable without the chunker.
    """

    def __init__(self, store: ChunkVectorStore, *, fanout: int = 3) -> None:
        self.store = store
        self.fanout = fanout
        self.dimensions = store.dimensions

    def upsert(self, ids: Sequence[str], vectors: Sequence[Sequence[float]]) -> None:
        if len(ids) != len(vectors):
            raise ValueError("ids and vectors differ in length")
        self.store.delete_docs(ids)
        self.store.upsert([make_chunk_id(d, 0) for d in ids], vectors)

    def delete(self, ids: Sequence[str]) -> None:
        self.store.delete_docs(ids)

    def search(self, vector: Sequence[float], *, limit: int = 50) -> Sequence[tuple[str, float]]:
        q = np.asarray(vector, dtype=np.float32)
        return [(d, s) for d, s, _ in self.search_docs(q, limit=limit)]

    def search_docs(
        self, query: F32, *, limit: int = 50, allow: Callable[[str], bool] | None = None
    ) -> list[tuple[str, float, str]]:
        """``(doc_id, best similarity, best chunk id)``, best first; ``allow`` filters chunk ids."""
        if limit <= 0:
            return []
        best: dict[str, tuple[float, str]] = {}
        for chunk_id, sim in self.store.search_array(query, limit=limit * self.fanout, allow=allow):
            doc = doc_of(chunk_id)
            if doc not in best:
                best[doc] = (sim, chunk_id)
        ranked = sorted(best.items(), key=lambda kv: (-kv[1][0], kv[0]))[:limit]
        return [(d, s, c) for d, (s, c) in ranked]
