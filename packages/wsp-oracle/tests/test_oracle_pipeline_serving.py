"""Serving behaviour (rank-oracle #16, #22, #23, #25, #27): source filters in dense retrieval, removed
documents never shown, stable pages, cache invalidation on a rebuilt store, the matched passage."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from pathlib import Path

import pytest
from oracle_helpers import CORPUS

from wsp_core.ids import DocId
from wsp_core.model import SearchHit
from wsp_index import FusionConfig, HashingEmbedder, HybridSearcher, TantivyConfig, TantivyIndex
from wsp_oracle.chunker import chunk_document
from wsp_oracle.embedder import encode_passages
from wsp_oracle.pipeline import OracleIndex, OracleSearcher
from wsp_oracle.vectors import ChunkVectorStore, DocVectorIndex

DIMS = 64
SMALL = TantivyConfig(writer_heap_bytes=20_000_000, writer_threads=1)


@pytest.fixture
def lexical(tmp_path: Path) -> Iterator[TantivyIndex]:
    idx = TantivyIndex(tmp_path / "lexical", SMALL)
    idx.upsert(CORPUS)
    idx.commit()
    yield idx
    idx.close()


@pytest.fixture
def store(tmp_path: Path) -> Iterator[ChunkVectorStore]:
    s = ChunkVectorStore(tmp_path / "v", DIMS)
    emb = HashingEmbedder(DIMS)
    chunks = [c for d in CORPUS for c in chunk_document(d, target_words=50)]
    s.add_chunks(chunks, encode_passages(emb, [c.embed_text() for c in chunks], DIMS))
    s.save()
    yield s
    s.close()


def searcher(lexical: TantivyIndex, store: ChunkVectorStore) -> OracleSearcher:
    return OracleSearcher(
        lexical,
        embedder=HashingEmbedder(DIMS),
        vectors=DocVectorIndex(store),
        resolve=lexical.lookup,
        passage_text=store.texts,
    )


def test_dense_retrieval_honours_the_source_filter(lexical: TantivyIndex, store: ChunkVectorStore) -> None:
    s = searcher(lexical, store)
    res = s.run("how do vaccines train the immune system", sources=["rfc"])
    assert {h.source_id for h in res.hits} <= {"rfc"}
    assert "dense" in res.timings_ms


def test_removed_documents_never_leak(lexical: TantivyIndex, store: ChunkVectorStore) -> None:
    lexical.delete([DocId("wsp:encyclo/Vaccine")])
    lexical.commit()
    res = searcher(lexical, store).run("vaccine immunity pathogen")
    assert "wsp:encyclo/Vaccine" not in {h.doc_id for h in res.hits}
    assert all(h.title != h.doc_id for h in res.hits)


class _Lex:
    def __init__(self, n: int) -> None:
        self.hits = [
            SearchHit(DocId(f"wsp:a/{i}"), 1.0 / (i + 1), f"T{i}", f"<b>m{i}</b>", "a") for i in range(n)
        ]

    def search(
        self, query: str, *, limit: int = 10, offset: int = 0, sources: Sequence[str] | None = None
    ) -> Sequence[SearchHit]:
        return self.hits[offset : offset + limit]

    def upsert(self, docs: object) -> int:
        return 0

    def delete(self, doc_ids: object) -> int:
        return 0

    def suggest(self, prefix: str, *, limit: int = 10) -> Sequence[str]:
        return []

    def commit(self) -> None: ...

    def close(self) -> None: ...


def test_hybrid_drops_unresolvable_dense_hits() -> None:
    class Vec:
        def search(self, vector: Sequence[float], *, limit: int = 50) -> Sequence[tuple[str, float]]:
            return [("wsp:a/gone", 0.9), ("wsp:a/kept", 0.8)]

        def upsert(self, ids: object, vectors: object) -> None: ...

        def delete(self, ids: object) -> None: ...

    def resolve(doc_id: str) -> SearchHit | None:
        return SearchHit(DocId(doc_id), 0.0, "Kept", "", "a") if doc_id.endswith("kept") else None

    hybrid = HybridSearcher(_Lex(1), Vec(), HashingEmbedder(8), config=FusionConfig(), resolve=resolve)
    ids = [h.doc_id for h in hybrid.search("q", limit=5)]
    assert "wsp:a/gone" not in ids
    assert "wsp:a/kept" in ids


def test_deeper_pages_keep_the_order_already_shown() -> None:
    lex = _Lex(200)
    s = OracleSearcher(lex, rerank_top=1, fusion=FusionConfig(depth=10))
    index = OracleIndex(s)
    first = [h.doc_id for h in index.search("q", limit=10)]
    lex.hits.reverse()  # the library changed between the two requests
    deeper = [h.doc_id for h in index.search("q", limit=10, offset=10)]
    again = [h.doc_id for h in index.search("q", limit=10)]
    assert again == first  # page 1 is what it was
    assert not set(first) & set(deeper)


def test_a_rebuilt_store_clears_the_cache() -> None:
    lex = _Lex(5)
    changed = {"now": False}
    clock = {"t": 0.0}
    index = OracleIndex(
        OracleSearcher(lex),
        refresh=lambda: changed["now"],
        refresh_seconds=1.0,
        clock=lambda: clock["t"],
    )
    a = index.search("q", limit=3)
    lex.hits.reverse()
    assert index.search("q", limit=3) == a  # cached
    changed["now"] = True
    clock["t"] = 2.0
    assert [h.doc_id for h in index.search("q", limit=3)] != [h.doc_id for h in a]
    changed["now"] = False
    clock["t"] = 10_000.0  # and results expire anyway
    assert index.search("q", limit=3)


def test_reranker_scores_the_matched_passage(lexical: TantivyIndex, store: ChunkVectorStore) -> None:
    seen: dict[str, str] = {}

    class Spy:
        def rerank(self, query: str, hits: Sequence[SearchHit]) -> Sequence[SearchHit]:
            seen.update((h.doc_id, h.snippet) for h in hits)
            return list(hits)

    s = OracleSearcher(lexical, reranker=Spy(), passage_text=store.texts)  # lexical only
    s.run("Rayleigh scattering blue sky")
    passage = seen["wsp:encyclo/Rayleigh_scattering"]
    assert "scatter" in passage.casefold()  # BM25's matching snippet, not merely the lead passage
    assert "<b>" not in passage
