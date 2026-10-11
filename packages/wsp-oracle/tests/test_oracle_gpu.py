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
    busy = _gpu_busy_elsewhere()
    timings: list[float] = []
    scores: list[float] = []
    for _ in range(5):  # the best of five: one run absorbs a scheduler or clock hiccup
        t0 = time.perf_counter()
        scores = list(rr.score("why is the sky blue", passages))
        timings.append((time.perf_counter() - t0) * 1000)
    best = max(range(len(CORPUS)), key=lambda i: scores[i])
    assert CORPUS[best].native_id == "Rayleigh_scattering"
    assert all(0.0 <= s <= 1.0 for s in scores)
    if busy is not None:  # another process (e.g. a game on the Windows host) holds the GPU
        pytest.skip(f"latency not measurable: GPU {busy}% busy before the test")
    assert min(timings) < 150  # budget is 50 ms; generous for a shared GPU


def _gpu_busy_elsewhere(threshold: int = 50) -> int | None:
    """GPU utilization in percent when it is above ``threshold`` before we load it, else None."""
    import shutil
    import subprocess

    exe = shutil.which("nvidia-smi")
    if exe is None:
        return None
    try:
        out = subprocess.run(
            [exe, "--query-gpu=utilization.gpu", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        ).stdout
        util = max(int(x) for x in out.split() if x.strip().isdigit())
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    return util if util > threshold else None
