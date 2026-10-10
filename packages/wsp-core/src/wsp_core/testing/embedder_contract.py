"""Contract suite for ``Embedder`` (PLAN §9.3, §12.2).

Subclass and provide an ``embedder`` fixture::

    class TestMyEmbedder(EmbedderContract):
        @pytest.fixture
        def embedder(self): return MyEmbedder()

Semantics pinned here: ``dimensions`` is a positive int and every vector has exactly that many
finite components; one vector per input, in input order; an empty batch gives ``[]``; results are
deterministic and independent of batch composition (within ``TOLERANCE``, for fp16 kernels);
vectors are L2-normalized (or all-zero for text with no content); different texts differ.
Set ``SEMANTIC = True`` for real models to also require that a paraphrase is closer than an
unrelated sentence. Pure Python, so it runs without numpy.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

import pytest

from wsp_core.ports import Embedder

TEXTS = (
    "Alan Turing worked on mathematics and computer science.",
    "Lisbon is the capital of Portugal, on the Tagus estuary.",
    "HTTP caching stores responses for reuse by clients.",
)


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return sum(x * y for x, y in zip(a, b, strict=True)) / (na * nb) if na and nb else 0.0


class EmbedderContract:
    TOLERANCE = 1e-3
    SEMANTIC = False

    @pytest.fixture
    def embedder(self) -> Embedder:
        raise NotImplementedError("provide an `embedder` fixture")

    def test_dimensions_and_shape(self, embedder: Embedder) -> None:
        assert isinstance(embedder.dimensions, int)
        assert embedder.dimensions > 0
        out = list(embedder.embed(list(TEXTS)))
        assert len(out) == len(TEXTS)
        for v in out:
            assert len(v) == embedder.dimensions
            assert all(math.isfinite(x) for x in v)

    def test_empty_batch(self, embedder: Embedder) -> None:
        assert list(embedder.embed([])) == []

    def test_normalized(self, embedder: Embedder) -> None:
        for v in embedder.embed(list(TEXTS)):
            norm = math.sqrt(sum(x * x for x in v))
            assert abs(norm - 1.0) < 1e-2

    def test_deterministic_and_batch_independent(self, embedder: Embedder) -> None:
        batch = list(embedder.embed(list(TEXTS)))
        (again,) = embedder.embed([TEXTS[1]])
        assert all(abs(x - y) <= self.TOLERANCE for x, y in zip(batch[1], again, strict=True))

    def test_different_texts_differ(self, embedder: Embedder) -> None:
        a, b, _ = embedder.embed(list(TEXTS))
        assert cosine(a, b) < 0.999

    def test_paraphrase_is_closer(self, embedder: Embedder) -> None:
        if not self.SEMANTIC:
            pytest.skip("not a semantic embedder")
        q, para, other = embedder.embed(
            [
                "What is the capital city of Portugal?",
                "Lisbon is the capital of Portugal.",
                "Photosynthesis converts light into chemical energy.",
            ]
        )
        assert cosine(q, para) > cosine(q, other)
