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

    def close(self) -> None:
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
    lexical = FakeLexical([hit("a", "x"), hit("a", "w"), hit("a", "y")])
    vector = FakeVector([(make_doc_id("a", "y"), 0.9), (make_doc_id("b", "z"), 0.8)])
    resolved: dict[str, SearchHit] = {
        make_doc_id("b", "z"): SearchHit(make_doc_id("b", "z"), 0.0, "Zed", "", "b")
    }
    hs = HybridSearcher(lexical, vector, HashingEmbedder(8), resolve=resolved.get)
    out = hs.search("q")
    # x: 2/61 (no dense rank: its lexical rank counts); y: 1/63 + 1/61 (lifted by its dense rank) beats
    # w: 2/62; z (dense only): 1/62
    assert [h.doc_id for h in out] == [make_doc_id("a", n) for n in ("x", "y", "w")] + [make_doc_id("b", "z")]
    assert out[1].score == pytest.approx(1 / 63 + 1 / 61)
    assert out[3].title == "Zed"


def test_source_priors_and_unresolved_title() -> None:
    lexical = FakeLexical([hit("a", "x")])
    vector = FakeVector([(make_doc_id("b", "z"), 0.9)])
    cfg = FusionConfig(source_priors={"b": 3.0})
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


def test_sources_without_embeddings_are_not_outvoted() -> None:
    # Lexical: wiki/best first, med/second. Only "med" is embedded and dense ranks med/second first.
    lexical = FakeLexical([hit("wiki", "best"), hit("med", "second")])
    vector = FakeVector([(make_doc_id("med", "second"), 0.9)])
    fair = HybridSearcher(lexical, vector, HashingEmbedder(8)).search("q")
    assert [h.doc_id for h in fair] == [make_doc_id("wiki", "best"), make_doc_id("med", "second")]
    assert fair[0].score == pytest.approx(2 / 61)  # its lexical rank, counted on the dense side too


def test_the_embedded_source_is_not_penalised() -> None:
    # Audit 2026-10-12 #5: mdwiki (the only embedded source) holds the best lexical match, which dense
    # retrieval missed. Its missing dense vote sank it to rank 183, out of the reranked head of 50.
    best = SearchHit(make_doc_id("mdwiki", "Snakebite"), 1.0, "Snakebite", "", "mdwiki")
    lexical = FakeLexical([best, *(hit("pubmed", str(i)) for i in range(199))])
    vector = FakeVector([(make_doc_id("mdwiki", f"other{i}"), 0.9 - i / 1000) for i in range(200)])
    cfg = FusionConfig(depth=200, dense_weight=2.0)
    out = HybridSearcher(lexical, vector, HashingEmbedder(8), config=cfg).search("snake bite", limit=200)
    assert out[0].doc_id == best.doc_id  # as for any source: no dense rank, so its lexical rank counts
    # and both sides are treated alike: the pubmed hits keep their lexical order behind it
    pubmed = [h.doc_id for h in out if h.source_id == "pubmed"]
    assert pubmed[:10] == [make_doc_id("pubmed", str(i)) for i in range(10)]


def test_fusion_says_whether_it_holds_every_candidate() -> None:
    few = FakeLexical([hit("a", str(i)) for i in range(5)])
    assert HybridSearcher(few).fuse("q", limit=10).exhaustive
    assert not HybridSearcher(few).fuse("q", limit=3).exhaustive  # cut at the page
    full = FakeLexical([hit("a", str(i)) for i in range(60)])
    assert not HybridSearcher(full, config=FusionConfig(depth=50)).fuse("q", limit=50).exhaustive
    # dense retrieval filled its depth (the five lexical hits and 45 others): it may hold more
    dense = FakeVector(
        [(str(h.doc_id), 0.5) for h in few.hits] + [(make_doc_id("b", str(i)), 0.4) for i in range(45)]
    )
    fused = HybridSearcher(few, dense, HashingEmbedder(8), config=FusionConfig(depth=50)).fuse("q", limit=50)
    assert len(fused.hits) == 50
    assert not fused.exhaustive


def test_a_lexical_window_cut_to_its_every_word_lead_is_not_exhaustive(tmp_path: Path) -> None:
    from wsp_index.lexical import MIN_TIER_HITS

    docs = [doc("s", f"both{i}", f"Tide {i}", "tide tables for the harbour") for i in range(MIN_TIER_HITS)]
    docs += [doc("s", f"one{i}", f"Tide {i}", "tide only") for i in range(10)]
    with TantivyIndex(tmp_path / "ix", TantivyConfig(writer_heap_bytes=20_000_000, writer_threads=1)) as idx:
        idx.upsert(docs)
        idx.commit()
        lead = HybridSearcher(idx, snippet_free=True, partial=False).fuse("tide harbour", limit=100)
        assert len(lead.hits) == MIN_TIER_HITS
        assert not lead.exhaustive
        every = HybridSearcher(idx, snippet_free=True).fuse("tide harbour", limit=100)
        assert len(every.hits) == MIN_TIER_HITS + 10
        assert every.exhaustive
