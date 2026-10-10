"""``VectorIndex`` for passages: binary HNSW in RAM, int8 rescoring from mmapped files (PLAN §9.4).

Layout of a store directory (local disk only, ADR-0002)::

    meta.sqlite        chunk id <-> integer key, doc id, optional passage text; per-doc content hash
    hnsw-b1.usearch    usearch HNSW graph over sign bits (hamming), 48 bytes per 384-d vector
    int8.bin           int8 vectors, row = key (dimensions bytes per row), memory-mapped
    scale.f32          per-row dequantization scale (float32), memory-mapped

Search: the query's sign bits find ``limit * oversample`` candidates in the binary graph, which are
rescored exactly against their int8 vectors (dot product with the float query) and cut to ``limit``.
For 25M chunks of 384 dimensions that is ~1.2 GB of binary codes plus the graph in RAM and ~9.6 GB
of int8 rows on disk, of which only the candidates' pages are touched per query.

Writes are append-only: an updated chunk gets a fresh key and the old key is removed from the graph
(its int8 row becomes garbage until a rebuild). ``save()`` makes everything durable; a crash between
saves is repaired on open by re-adding keys the graph lost from their int8 rows.
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
import threading
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

import numpy as np
import numpy.typing as npt
from usearch.compiled import MetricKind, ScalarKind
from usearch.index import Index

from wsp_index import ensure_local
from wsp_oracle.chunker import Chunk, doc_of, make_chunk_id

FORMAT = "1"
GRAPH_FILE = "hnsw-b1.usearch"
INT8_FILE = "int8.bin"
SCALE_FILE = "scale.f32"
META_FILE = "meta.sqlite"

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


def _in(prefix: str, marks: str) -> str:
    """``<prefix> (?, ?, ...)``: placeholders only, values are always bound parameters."""
    return f"{prefix} ({marks})"


U64 = npt.NDArray[np.uint64]


class VectorStoreError(RuntimeError):
    pass


def quantize_int8(mat: F32) -> tuple[npt.NDArray[np.int8], F32]:
    """Symmetric per-row int8 quantization: ``row ≈ q * scale``."""
    peak = np.abs(mat).max(axis=1)
    scale = np.where(peak > 0, peak / 127.0, 1.0).astype(np.float32)
    q = np.clip(np.rint(mat / scale[:, None]), -127, 127).astype(np.int8)
    return q, scale


def binarize(mat: F32 | npt.NDArray[np.int8]) -> npt.NDArray[np.uint8]:
    return np.packbits(mat > 0, axis=1)


def _normalize(mat: F32) -> F32:
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return (mat / norms).astype(np.float32, copy=False)


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
        oversample: int = 8,
        max_candidates: int = 2048,
        store_text: bool = True,
        threads: int = 0,
    ) -> None:
        if dimensions < 8 or dimensions % 8:
            raise ValueError("dimensions must be a positive multiple of 8 (binary codes)")
        if mode not in ("load", "view"):
            raise ValueError("mode must be 'load' or 'view'")
        path.mkdir(parents=True, exist_ok=True)
        self.path = ensure_local(path)
        self.dimensions = dimensions
        self.mode = mode
        self.oversample = oversample
        self.max_candidates = max_candidates
        self.store_text = store_text
        self.threads = threads
        self._lock = threading.RLock()
        self._closed = False

        self._db = sqlite3.connect(self.path / META_FILE, check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.executescript(_SCHEMA)
        self._check_meta(model)
        self._db.execute("BEGIN")

        graph = self.path / GRAPH_FILE
        self._index = Index(
            ndim=dimensions,
            metric=MetricKind.Hamming,
            dtype=ScalarKind.B1,
            connectivity=connectivity,
            expansion_add=expansion_add,
            expansion_search=expansion_search,
        )
        if graph.exists():
            if mode == "view":
                self._index.view(str(graph))  # pyright: ignore[reportUnknownMemberType] - PathLike[Unknown] in usearch's hints
            else:
                self._index.load(str(graph))  # pyright: ignore[reportUnknownMemberType] - PathLike[Unknown] in usearch's hints
        self._int8_f = (self.path / INT8_FILE).open("ab+")
        self._scale_f = (self.path / SCALE_FILE).open("ab+")
        self._rows: npt.NDArray[np.int8] = np.zeros((0, dimensions), dtype=np.int8)
        self._scales: F32 = np.zeros(0, dtype=np.float32)
        self._next_key = int(self._meta("next_key") or 0)
        self._remap()
        if self._meta("dirty") == "1" and mode == "load":
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
            self._set_meta("dimensions", str(self.dimensions))
            self._set_meta("format", FORMAT)
            self._set_meta("model", model)
            return
        if int(dims) != self.dimensions or fmt != FORMAT:
            raise VectorStoreError(f"{self.path}: built with {dims} dimensions (format {fmt}); rebuild it")
        if model and stored_model and model != stored_model:
            raise VectorStoreError(
                f"{self.path}: built with model {stored_model!r}, not {model!r}; rebuild it"
            )

    @property
    def model(self) -> str:
        return self._meta("model") or ""

    # --- int8 rows -----------------------------------------------------------------------------

    def _remap(self) -> None:
        self._int8_f.flush()
        self._scale_f.flush()
        n = os.fstat(self._scale_f.fileno()).st_size // 4
        if n == 0:
            self._rows = np.zeros((0, self.dimensions), dtype=np.int8)
            self._scales = np.zeros(0, dtype=np.float32)
            return
        self._rows = np.memmap(self.path / INT8_FILE, dtype=np.int8, mode="r", shape=(n, self.dimensions))
        self._scales = np.memmap(self.path / SCALE_FILE, dtype=np.float32, mode="r", shape=(n,))

    def _append_rows(self, mat: F32) -> U64:
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

    # --- writes --------------------------------------------------------------------------------

    def _writable(self) -> None:
        if self._closed:
            raise VectorStoreError("vector store is closed")
        if self.mode != "load":
            raise VectorStoreError("vector store opened read-only (mode='view')")

    def _mark_dirty(self) -> None:
        if self._meta("dirty") != "1":
            self._set_meta("dirty", "1")

    def _remove_keys(self, keys: Sequence[int]) -> None:
        if keys:
            self._index.remove(np.asarray(keys, dtype=np.uint64))

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
        mat = _normalize(mat)
        with self._lock:
            self._writable()
            self._mark_dirty()
            old = self._keys_of(ids)
            self._remove_keys(list(old.values()))
            keys = self._append_rows(mat)
            self._int8_f.flush()
            self._index.add(keys, binarize(mat), threads=self.threads)
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
            self._mark_dirty()
            old = self._keys_of(ids)
            self._remove_keys(list(old.values()))
            self._db.executemany("DELETE FROM chunks WHERE chunk_id=?", ((c,) for c in old))

    def delete_docs(self, doc_ids: Iterable[str]) -> int:
        """Remove every chunk of these documents (and their recorded embed state)."""
        n = 0
        with self._lock:
            self._writable()
            self._mark_dirty()
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

    def record_docs(self, rows: Iterable[tuple[str, str, str, str, int]]) -> None:
        """``(doc_id, source_id, content_hash, generation, chunks)`` for embedded documents."""
        with self._lock:
            self._writable()
            self._db.executemany("INSERT OR REPLACE INTO docs VALUES (?, ?, ?, ?, ?)", rows)

    def stale_docs(self, source_id: str, generation: str) -> list[str]:
        """Documents of ``source_id`` recorded under any generation other than ``generation``."""
        with self._lock:
            rows = self._db.execute(
                "SELECT doc_id FROM docs WHERE source_id=? AND generation<>?", (source_id, generation)
            ).fetchall()
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

    def search_array(self, query: F32, *, limit: int = 50) -> list[tuple[str, float]]:
        """Top ``limit`` chunk ids by cosine similarity (approximate candidates, exact int8 rescoring)."""
        if limit <= 0 or len(self) == 0:
            return []
        q = _normalize(query.reshape(1, -1).astype(np.float32, copy=False))
        count = min(max(limit * self.oversample, limit), self.max_candidates, len(self))
        matches: Any = self._index.search(binarize(q)[0], count)
        keys = np.asarray(matches.keys, dtype=np.int64)
        if keys.size == 0:
            return []
        rows, scales = self._rows, self._scales
        if int(keys.max()) >= len(scales):
            with self._lock:
                self._remap()
                rows, scales = self._rows, self._scales
        sims = (rows[keys].astype(np.float32) @ q[0]) * scales[keys]
        top = np.argsort(-sims, kind="stable")[:limit]
        chosen = keys[top]
        names = self._names(chosen.tolist())
        return [
            (names[int(k)], float(sims[i]))
            for k, i in zip(chosen.tolist(), top.tolist(), strict=True)
            if int(k) in names
        ]

    def _names(self, keys: Sequence[int]) -> dict[int, str]:
        if not keys:
            return {}
        marks = ",".join("?" * len(keys))
        with self._lock:
            rows = self._db.execute(
                _in("SELECT key, chunk_id FROM chunks WHERE key IN", marks), list(keys)
            ).fetchall()
        return {int(k): str(c) for k, c in rows}

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

    def exact_search(self, query: F32, *, limit: int = 10) -> list[tuple[str, float]]:
        """Brute force over the int8 rows of live keys (recall baseline; O(n))."""
        q = _normalize(query.reshape(1, -1).astype(np.float32, copy=False))[0]
        with self._lock:
            live = np.asarray([int(r[0]) for r in self._db.execute("SELECT key FROM chunks")], dtype=np.int64)
            self._remap()
        if live.size == 0:
            return []
        sims = (self._rows[live].astype(np.float32) @ q) * self._scales[live]
        top = np.argsort(-sims, kind="stable")[:limit]
        names = self._names(live[top].tolist())
        return [(names[int(live[i])], float(sims[i])) for i in top.tolist()]

    # --- durability ----------------------------------------------------------------------------

    def _repair(self) -> None:
        """Make the graph agree with ``meta.sqlite`` after a crash between saves."""
        live = np.asarray([int(r[0]) for r in self._db.execute("SELECT key FROM chunks")], dtype=np.uint64)
        have = np.asarray(self._index.contains(live), dtype=bool) if live.size else np.zeros(0, dtype=bool)
        lost = live[~have]
        if lost.size:
            idx = lost.astype(np.int64)
            self._index.add(lost, binarize(self._rows[idx]), threads=self.threads)
        graph_keys = (
            np.asarray(self._index.keys, dtype=np.uint64) if len(self._index) else np.zeros(0, np.uint64)
        )
        stray = np.setdiff1d(graph_keys, live, assume_unique=False)
        if stray.size:
            self._index.remove(stray)
        self.save()

    def save(self) -> None:
        """Flush rows, write the graph atomically, then commit the id map."""
        with self._lock:
            if self._closed or self.mode != "load":
                return
            for f in (self._int8_f, self._scale_f):
                f.flush()
                os.fsync(f.fileno())
            graph = self.path / GRAPH_FILE
            partial = graph.with_suffix(".partial")
            self._index.save(str(partial))  # pyright: ignore[reportUnknownMemberType] - PathLike[Unknown] in usearch's hints
            os.replace(partial, graph)
            self._set_meta("dirty", "0")
            self._set_meta("next_key", str(self._next_key))
            self._db.execute("COMMIT")
            self._db.execute("BEGIN")
            self._remap()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self.save()
            with contextlib.suppress(sqlite3.Error):
                self._db.execute("COMMIT")
            self._db.close()
            self._int8_f.close()
            self._scale_f.close()
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

    def search_docs(self, query: F32, *, limit: int = 50) -> list[tuple[str, float, str]]:
        """``(doc_id, best similarity, best chunk id)``, best first."""
        if limit <= 0:
            return []
        best: dict[str, tuple[float, str]] = {}
        for chunk_id, sim in self.store.search_array(query, limit=limit * self.fanout):
            doc = doc_of(chunk_id)
            if doc not in best:
                best[doc] = (sim, chunk_id)
        ranked = sorted(best.items(), key=lambda kv: (-kv[1][0], kv[0]))[:limit]
        return [(d, s, c) for d, (s, c) in ranked]
