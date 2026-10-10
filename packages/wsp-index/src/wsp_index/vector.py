"""Reference ``VectorIndex`` and ``Embedder`` implementations (the Phase 3 seam).

Real ANN (binary-quantized, mmapped int8 rescoring) and a real embedding model replace these by
config; the hybrid searcher only sees the ports.
"""

from __future__ import annotations

import hashlib
import re
import threading
from collections.abc import Sequence

import numpy as np
import numpy.typing as npt

_TOKEN = re.compile(r"\w+", re.UNICODE)


class NullVectorIndex:
    """Accepts and forgets everything; ``search`` is always empty. For lexical-only deployments."""

    def upsert(self, ids: Sequence[str], vectors: Sequence[Sequence[float]]) -> None:
        if len(ids) != len(vectors):
            raise ValueError("ids and vectors differ in length")

    def delete(self, ids: Sequence[str]) -> None:
        return None

    def search(self, vector: Sequence[float], *, limit: int = 50) -> Sequence[tuple[str, float]]:
        return []


class NumpyVectorIndex:
    """Exact cosine search by brute force over an in-memory float32 matrix.

    O(n·d) per query: fine for tests and up to ~10^5 vectors; the ground truth for ANN recall.
    """

    def __init__(self, dimensions: int) -> None:
        if dimensions < 1:
            raise ValueError("dimensions must be positive")
        self.dimensions = dimensions
        self._ids: list[str] = []
        self._row: dict[str, int] = {}
        self._mat: npt.NDArray[np.float32] = np.empty((0, dimensions), dtype=np.float32)
        self._lock = threading.Lock()

    def __len__(self) -> int:
        return len(self._ids)

    def _normalize(self, vectors: Sequence[Sequence[float]]) -> npt.NDArray[np.float32]:
        mat = np.asarray(vectors, dtype=np.float32).reshape(len(vectors), -1)
        if mat.shape[1] != self.dimensions:
            raise ValueError(f"expected {self.dimensions} dimensions, got {mat.shape[1]}")
        norms = np.linalg.norm(mat, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return mat / norms

    def upsert(self, ids: Sequence[str], vectors: Sequence[Sequence[float]]) -> None:
        if len(ids) != len(vectors):
            raise ValueError("ids and vectors differ in length")
        if not ids:
            return
        mat = self._normalize(vectors)
        with self._lock:
            new_rows: list[npt.NDArray[np.float32]] = []
            for i, doc_id in enumerate(ids):
                row = self._row.get(doc_id)
                if row is not None:
                    self._mat[row] = mat[i]
                else:
                    self._row[doc_id] = len(self._ids)
                    self._ids.append(doc_id)
                    new_rows.append(mat[i])
            if new_rows:
                self._mat = np.vstack([self._mat, np.stack(new_rows)])

    def delete(self, ids: Sequence[str]) -> None:
        with self._lock:
            for doc_id in ids:
                row = self._row.pop(doc_id, None)
                if row is None:
                    continue
                last = len(self._ids) - 1
                if row != last:  # swap-remove keeps the matrix dense
                    moved = self._ids[last]
                    self._ids[row] = moved
                    self._mat[row] = self._mat[last]
                    self._row[moved] = row
                self._ids.pop()
                self._mat = self._mat[:last]

    def search(self, vector: Sequence[float], *, limit: int = 50) -> Sequence[tuple[str, float]]:
        if limit <= 0:
            return []
        q = self._normalize([vector])[0]
        with self._lock:
            mat, ids = self._mat, list(self._ids)
        if not ids:
            return []
        sims = mat @ q
        k = min(limit, len(ids))
        top = np.argpartition(-sims, k - 1)[:k]
        top = top[np.argsort(-sims[top], kind="stable")]
        return [(ids[int(i)], float(sims[i])) for i in top]


class HashingEmbedder:
    """Deterministic bag-of-words feature hashing (signed), L2-normalized. Not semantic.

    A dependency-free stand-in for tests and wiring; it exercises the dense path end to end.
    """

    def __init__(self, dimensions: int = 256) -> None:
        self.dimensions = dimensions

    def _slot(self, token: str) -> tuple[int, float]:
        h = int.from_bytes(hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest(), "little")
        return h % self.dimensions, (1.0 if (h >> 63) & 1 else -1.0)

    def embed(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        out = np.zeros((len(texts), self.dimensions), dtype=np.float32)
        for i, text in enumerate(texts):
            for tok in _TOKEN.findall(text.casefold()):
                slot, sign = self._slot(tok)
                out[i, slot] += sign
        norms = np.linalg.norm(out, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return (out / norms).tolist()
