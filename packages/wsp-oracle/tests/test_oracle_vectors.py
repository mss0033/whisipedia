"""ChunkVectorStore: persistence, view mode, crash repair, recall vs brute force, doc state."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from wsp_oracle.chunker import Chunk
from wsp_oracle.vectors import ChunkVectorStore, DocVectorIndex, VectorStoreError, binarize, quantize_int8

DIMS = 64


def unit(rng: np.random.Generator, n: int, d: int = DIMS) -> np.ndarray:
    m = rng.standard_normal((n, d)).astype(np.float32)
    return m / np.linalg.norm(m, axis=1, keepdims=True)


def test_quantization_roundtrip() -> None:
    m = unit(np.random.default_rng(0), 50)
    q, scale = quantize_int8(m)
    back = q.astype(np.float32) * scale[:, None]
    assert float(np.abs(back - m).max()) < 0.01
    assert binarize(m).shape == (50, DIMS // 8)


def test_persistence_and_view_mode(tmp_path: Path) -> None:
    rng = np.random.default_rng(1)
    m = unit(rng, 200)
    ids = [f"wsp:s/d{i}#0" for i in range(200)]
    with_store = ChunkVectorStore(tmp_path / "v", DIMS, model="m1")
    with_store.upsert_matrix(ids, m, texts=[f"text {i}" for i in range(200)])
    with_store.close()

    ro = ChunkVectorStore(tmp_path / "v", DIMS, mode="view")
    assert len(ro) == 200
    assert ro.search(m[17].tolist(), limit=1)[0][0] == ids[17]
    assert ro.texts([ids[3]]) == {ids[3]: "text 3"}
    assert ro.lead_texts(["wsp:s/d4"]) == {"wsp:s/d4": "text 4"}
    with pytest.raises(VectorStoreError, match="read-only"):
        ro.upsert(["x"], [m[0].tolist()])
    ro.close()

    with pytest.raises(VectorStoreError, match="model"):
        ChunkVectorStore(tmp_path / "v", DIMS, model="other")
    with pytest.raises(VectorStoreError, match="dimensions"):
        ChunkVectorStore(tmp_path / "v", 128)


def test_crash_between_saves_is_repaired(tmp_path: Path) -> None:
    rng = np.random.default_rng(2)
    m = unit(rng, 60)
    s = ChunkVectorStore(tmp_path / "v", DIMS)
    s.upsert_matrix([f"a{i}" for i in range(30)], m[:30])
    s.save()
    s.upsert_matrix([f"b{i}" for i in range(30)], m[30:])
    s.delete(["a0"])
    # simulate a crash: commit the id map but never write the graph again
    s._db.execute("COMMIT")  # pyright: ignore[reportPrivateUsage]
    s._db.close()  # pyright: ignore[reportPrivateUsage]
    s._int8_f.close()  # pyright: ignore[reportPrivateUsage]
    s._scale_f.close()  # pyright: ignore[reportPrivateUsage]

    again = ChunkVectorStore(tmp_path / "v", DIMS)
    assert len(again) == 59
    assert again.search(m[45].tolist(), limit=1)[0][0] == "b15"
    assert "a0" not in [i for i, _ in again.search(m[0].tolist(), limit=5)]
    again.close()


def test_recall_against_brute_force(tmp_path: Path) -> None:
    rng = np.random.default_rng(3)
    d = 128
    centers = unit(rng, 50, d)
    data = centers[rng.integers(0, 50, 3000)] + 0.35 * unit(rng, 3000, d)
    data = data / np.linalg.norm(data, axis=1, keepdims=True)
    s = ChunkVectorStore(tmp_path / "v", d, oversample=10)
    s.upsert_matrix([str(i) for i in range(3000)], data.astype(np.float32))
    hits = 0
    queries = unit(rng, 30, d) * 0.3 + centers[:30]
    for q in queries:
        exact = {i for i, _ in s.exact_search(q.astype(np.float32), limit=10)}
        approx = {i for i, _ in s.search_array(q.astype(np.float32), limit=10)}
        hits += len(exact & approx)
    assert hits / 300 >= 0.9
    s.close()


def test_doc_state_and_doc_index(tmp_path: Path) -> None:
    rng = np.random.default_rng(4)
    s = ChunkVectorStore(tmp_path / "v", DIMS)
    chunks = [Chunk(f"wsp:s/a#{n}", "wsp:s/a", n, "A", "", f"a{n}") for n in range(3)]
    chunks += [Chunk("wsp:s/b#0", "wsp:s/b", 0, "B", "", "b0")]
    m = unit(rng, 4)
    s.add_chunks(chunks, m)
    s.record_docs([("wsp:s/a", "s", "h1", "g1", 3), ("wsp:s/b", "s", "h2", "g1", 1)])
    assert s.doc_hashes(["wsp:s/a", "wsp:s/zz"]) == {"wsp:s/a": "h1"}
    s.touch_docs(["wsp:s/a"], "g2")
    assert s.stale_docs("s", "g2") == ["wsp:s/b"]
    assert s.doc_count("s") == 2

    idx = DocVectorIndex(s)
    top = idx.search_docs(m[1], limit=2)
    assert top[0][0] == "wsp:s/a"
    assert top[0][2] == "wsp:s/a#1"
    assert len({d for d, _, _ in top}) == len(top)
    assert s.delete_docs(["wsp:s/a"]) == 3
    assert [d for d, _ in idx.search(m[1].tolist(), limit=5)] == ["wsp:s/b"]
    assert s.stats()["chunks"] == 1
    s.close()
    s.close()  # idempotent


def test_rejects_bad_input(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="multiple of 8"):
        ChunkVectorStore(tmp_path / "v", 12)
    s = ChunkVectorStore(tmp_path / "w", DIMS)
    with pytest.raises(ValueError, match="duplicate"):
        s.upsert(["a", "a"], [[1.0] * DIMS, [1.0] * DIMS])
    with pytest.raises(ValueError, match="dimensions"):
        s.search([1.0] * 8)
    s.close()
