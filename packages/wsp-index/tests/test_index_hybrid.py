from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest

from wsp_core.ids import DocId, make_doc_id
from wsp_core.model import DocRecord, SearchHit
from wsp_core.ports import Embedder, VectorIndex
from wsp_index import (
    FusionConfig,
    HashingEmbedder,
    HybridSearcher,
    NullVectorIndex,
    NumpyVectorIndex,
    TantivyConfig,
    TantivyIndex,
    embed_documents,
)
from wsp_index_helpers import doc


class FakeLexical:
    def __init__(self, hits: list[SearchHit]) -> None:
        self.hits = hits

    def upsert(self, docs: object) -> int:
        return 0

    def delete(self, doc_ids: object) -> int:
        return 0

    def search(
        self, query: str, *, limit: int = 10, offset: int = 0, sources: Sequence[str] | None = None
    ) -> Sequence[SearchHit]:
        hits = [h for h in self.hits if sources is None or h.source_id in sources]
        return hits[offset : offset + limit]

    def suggest(self, prefix: str, *, limit: int = 10) -> Sequence[str]:
        return []

    def commit(self) -> None:
        return None


class FakeVector:
    def __init__(self, results: list[tuple[str, float]]) -> None:
        self.results = results

    def upsert(self, ids: Sequence[str], vectors: Sequence[Sequence[float]]) -> None:
        return None

    def delete(self, ids: Sequence[str]) -> None:
        return None

    def search(self, vector: Sequence[float], *, limit: int = 50) -> Sequence[tuple[str, float]]:
        return self.results[:limit]


def hit(src: str, native: str) -> SearchHit:
    return SearchHit(make_doc_id(src, native), 1.0, native.upper(), "", src)


def test_rrf_fuses_and_resolves() -> None:
    lexical = FakeLexical([hit("a", "x"), hit("a", "y")])
    vector = FakeVector([(make_doc_id("b", "z"), 0.9), (make_doc_id("a", "y"), 0.8)])
    resolved: dict[str, SearchHit] = {
        make_doc_id("b", "z"): SearchHit(make_doc_id("b", "z"), 0.0, "Zed", "", "b")
    }
    hs = HybridSearcher(lexical, vector, HashingEmbedder(8), resolve=resolved.get)
    out = hs.search("q")
    # y: 1/62 + 1/62 beats x: 1/61 and z: 1/61
    assert [h.doc_id for h in out] == [make_doc_id("a", "y"), make_doc_id("a", "x"), make_doc_id("b", "z")]
    assert out[0].score == pytest.approx(2 / 62)
    assert out[2].title == "Zed"


def test_source_priors_and_unresolved_title() -> None:
    lexical = FakeLexical([hit("a", "x")])
    vector = FakeVector([(make_doc_id("b", "z"), 0.9)])
    cfg = FusionConfig(source_priors={"b": 2.0})
    out = HybridSearcher(lexical, vector, HashingEmbedder(8), config=cfg).search("q")
    assert out[0].doc_id == make_doc_id("b", "z")
    assert out[0].title == make_doc_id("b", "z")
    assert out[0].source_id == "b"


def test_sources_filter_applies_to_dense_hits() -> None:
    lexical = FakeLexical([hit("a", "x")])
    vector = FakeVector([(make_doc_id("b", "z"), 0.9), ("not-a-doc-id", 0.5)])
    hs = HybridSearcher(lexical, vector, HashingEmbedder(8))
    assert [h.source_id for h in hs.search("q", sources=["a"])] == ["a"]
    assert hs.search("q", sources=[]) == []


def test_pagination_and_degenerate_inputs() -> None:
    lexical = FakeLexical([hit("a", str(i)) for i in range(30)])
    hs = HybridSearcher(lexical)
    full = [h.doc_id for h in hs.search("q", limit=30)]
    assert [h.doc_id for h in hs.search("q", limit=10, offset=10)] == full[10:20]
    assert hs.search("  ") == []
    assert hs.search("q", limit=0) == []
    with pytest.raises(ValueError, match="together"):
        HybridSearcher(lexical, NullVectorIndex())


def test_numpy_vector_index() -> None:
    vi = NumpyVectorIndex(3)
    vi.upsert(["a", "b", "c"], [[1, 0, 0], [0, 1, 0], [0.9, 0.1, 0]])
    assert [d for d, _ in vi.search([1, 0, 0], limit=2)] == ["a", "c"]
    vi.upsert(["a"], [[0, 0, 1]])
    assert vi.search([0, 0, 1], limit=1)[0][0] == "a"
    vi.delete(["a", "missing"])
    assert len(vi) == 2
    assert {d for d, _ in vi.search([1, 1, 1], limit=10)} == {"b", "c"}
    assert vi.search([1, 0, 0], limit=0) == []
    with pytest.raises(ValueError, match="dimensions"):
        vi.upsert(["x"], [[1, 2]])
    assert isinstance(vi, VectorIndex)
    assert NumpyVectorIndex(2).search([1, 0]) == []


def test_null_vector_and_hashing_embedder() -> None:
    nv = NullVectorIndex()
    nv.upsert(["a"], [[1.0]])
    nv.delete(["a"])
    assert nv.search([1.0]) == []
    emb = HashingEmbedder(64)
    assert isinstance(emb, Embedder)
    a, b = emb.embed(["Alan Turing", "alan turing"])
    assert a == b
    assert sum(x * x for x in a) == pytest.approx(1.0)
    assert emb.embed([""])[0] == [0.0] * 64


def test_end_to_end_with_tantivy(tmp_path: Path) -> None:
    docs: list[DocRecord] = [
        doc("enc", "turing", "Alan Turing", "computing pioneer and codebreaker"),
        doc("enc", "babbage", "Charles Babbage", "designed the analytical engine"),
        doc("rfc", "http", "HTTP", "hypertext transfer protocol"),
    ]
    with TantivyIndex(tmp_path / "ix", TantivyConfig(writer_heap_bytes=20_000_000, writer_threads=1)) as idx:
        idx.upsert(docs)
        idx.commit()
        emb = HashingEmbedder(128)
        vec = NumpyVectorIndex(128)
        assert embed_documents(docs, emb, vec, batch_size=2) == 3
        hs = HybridSearcher(idx, vec, emb, resolve=idx.lookup)
        out = hs.search("analytical engine")
        assert out[0].doc_id == DocId(make_doc_id("enc", "babbage"))
        assert all(h.title for h in out)
