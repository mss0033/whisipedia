"""Contract suite for ``VectorIndex`` (PLAN §9.3-9.4, §12.2).

Subclass and provide an ``index`` fixture returning an empty index of ``DIMS`` dimensions::

    class TestMyIndex(VectorIndexContract):
        @pytest.fixture
        def index(self, tmp_path): return MyIndex(tmp_path, VectorIndexContract.DIMS)

Semantics pinned here: ``search`` returns at most ``limit`` distinct ids, most similar first, with
non-increasing scores; ``upsert`` replaces by id (no duplicates); ``delete`` removes and ignores
unknown ids; mismatched ``ids``/``vectors`` lengths raise ``ValueError``; an empty index and
``limit <= 0`` return ``[]``. Vectors need not be normalized: similarity is by direction (cosine).
Approximate indexes must still be exact on these small, well-separated cases. Pure Python, so it
runs without numpy.
"""

from __future__ import annotations

import random
from collections.abc import Sequence

import pytest

from wsp_core.ports import VectorIndex


def basis(i: int, dims: int, *, scale: float = 1.0) -> list[float]:
    """A ±1 pattern unique to ``i`` (well separated even after binary quantization)."""
    rng = random.Random(1000 + i)  # noqa: S311 - test data, not security
    return [scale * (1.0 if rng.random() < 0.5 else -1.0) for _ in range(dims)]


def near(v: Sequence[float], flips: int, seed: int = 0) -> list[float]:
    """``v`` with ``flips`` coordinates negated (cosine 1 - 2 * flips / dims)."""
    out = list(v)
    rng = random.Random(seed)  # noqa: S311 - test data, not security
    for j in rng.sample(range(len(v)), flips):
        out[j] = -out[j]
    return out


def _ids(results: Sequence[tuple[str, float]]) -> list[str]:
    return [i for i, _ in results]


class VectorIndexContract:
    DIMS = 64

    @pytest.fixture
    def index(self) -> VectorIndex:
        raise NotImplementedError("provide an `index` fixture returning an empty VectorIndex of DIMS")

    def test_empty_index_returns_nothing(self, index: VectorIndex) -> None:
        assert list(index.search(basis(0, self.DIMS), limit=5)) == []

    def test_nearest_first(self, index: VectorIndex) -> None:
        ids = [f"doc-{i}" for i in range(20)]
        index.upsert(ids, [basis(i, self.DIMS) for i in range(20)])
        for i in (0, 7, 19):
            res = index.search(near(basis(i, self.DIMS), 4, seed=i), limit=3)
            assert _ids(res)[0] == f"doc-{i}"

    def test_limit_scores_and_distinct_ids(self, index: VectorIndex) -> None:
        index.upsert([f"d{i}" for i in range(30)], [basis(i, self.DIMS) for i in range(30)])
        res = list(index.search(basis(3, self.DIMS), limit=10))
        assert 1 <= len(res) <= 10
        assert len(set(_ids(res))) == len(res)
        scores = [s for _, s in res]
        assert scores == sorted(scores, reverse=True)
        assert list(index.search(basis(3, self.DIMS), limit=0)) == []

    def test_scale_does_not_matter(self, index: VectorIndex) -> None:
        index.upsert(["a", "b"], [basis(1, self.DIMS, scale=10.0), basis(2, self.DIMS, scale=0.1)])
        assert _ids(index.search(basis(2, self.DIMS), limit=1)) == ["b"]

    def test_upsert_replaces_by_id(self, index: VectorIndex) -> None:
        index.upsert(["x", "y"], [basis(1, self.DIMS), basis(2, self.DIMS)])
        index.upsert(["x"], [basis(3, self.DIMS)])
        res = list(index.search(basis(3, self.DIMS), limit=5))
        assert _ids(res)[0] == "x"
        assert _ids(res).count("x") == 1
        assert "x" not in _ids(index.search(basis(1, self.DIMS), limit=1))

    def test_delete(self, index: VectorIndex) -> None:
        index.upsert(["x", "y", "z"], [basis(i, self.DIMS) for i in range(3)])
        index.delete(["y", "never-added"])
        found = _ids(index.search(basis(1, self.DIMS), limit=5))
        assert "y" not in found
        assert set(found) <= {"x", "z"}
        index.delete(["x", "z"])
        assert list(index.search(basis(0, self.DIMS), limit=5)) == []

    def test_readd_after_delete(self, index: VectorIndex) -> None:
        index.upsert(["x"], [basis(1, self.DIMS)])
        index.delete(["x"])
        index.upsert(["x"], [basis(2, self.DIMS)])
        assert _ids(index.search(basis(2, self.DIMS), limit=1)) == ["x"]

    def test_length_mismatch_raises(self, index: VectorIndex) -> None:
        with pytest.raises(ValueError, match=r"."):
            index.upsert(["a", "b"], [basis(0, self.DIMS)])

    def test_empty_upsert_is_a_no_op(self, index: VectorIndex) -> None:
        index.upsert([], [])
        assert list(index.search(basis(0, self.DIMS), limit=3)) == []
