"""Real models on the GPU (``-m gpu``); skipped without CUDA or the local weights."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from oracle_helpers import CORPUS, MODELS, needs_embed_model, needs_rerank_model

from wsp_oracle.config import DEFAULT_EMBED_MODEL, DEFAULT_RERANK_MODEL
from wsp_oracle.vectors import ChunkVectorStore

pytestmark = pytest.mark.gpu


@needs_embed_model
def test_embedder_finds_the_right_passage_across_languages(tmp_path: Path) -> None:
    from wsp_oracle.chunker import chunk_document
    from wsp_oracle.embedder import TransformerEmbedder

    emb = TransformerEmbedder(MODELS / DEFAULT_EMBED_MODEL)
    chunks = [c for d in CORPUS for c in chunk_document(d)]
    store = ChunkVectorStore(tmp_path / "v", emb.dimensions)
    store.add_chunks(chunks, emb.encode_passages([c.embed_text() for c in chunks]))
    for query, want in (
        ("why is the sky blue", "wsp:encyclo/Rayleigh_scattering"),
        ("¿cuál es la capital de Portugal?", "wsp:encyclo/Lisbon"),
        ("wie funktioniert eine Impfung", "wsp:encyclo/Vaccine"),
    ):
        top = store.search_array(emb.encode_queries([query])[0], limit=1)
        assert top[0][0].startswith(want), query
    store.close()


@needs_rerank_model
def test_reranker_orders_and_is_fast() -> None:
    from wsp_oracle.reranker import CrossEncoderReranker

    rr = CrossEncoderReranker(MODELS / DEFAULT_RERANK_MODEL)
    passages = [f"{d.title}\n{d.text}" for d in CORPUS] * 5  # 50 passages
    rr.score("warm up", passages)
    t0 = time.perf_counter()
    scores = rr.score("why is the sky blue", passages)
    ms = (time.perf_counter() - t0) * 1000
    best = max(range(len(CORPUS)), key=lambda i: scores[i])
    assert CORPUS[best].native_id == "Rayleigh_scattering"
    assert all(0.0 <= s <= 1.0 for s in scores)
    assert ms < 150  # budget is 50 ms; generous for a shared GPU
