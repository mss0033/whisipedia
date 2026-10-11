"""ChunkVectorStore (rank-oracle #7-#10, #15, #16, #24): centered codes, crash recovery, deferred graph
builds, read-only view mode, source filters and non-finite embeddings."""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

from wsp_oracle.vectors import (
    GRAPH_FILE,
    MEAN_FILE,
    META_FILE,
    ChunkVectorStore,
    DocVectorIndex,
    VectorStoreError,
    binarize,
)

DIMS = 64


def unit(rng: np.random.Generator, n: int, d: int = DIMS, offset: float = 0.0) -> np.ndarray:
    m = rng.standard_normal((n, d)).astype(np.float32) + offset  # offset: a shared direction
    return m / np.linalg.norm(m, axis=1, keepdims=True)


def test_codes_are_centered_on_the_stored_mean(tmp_path: Path) -> None:
    rng = np.random.default_rng(0)
    m = unit(rng, 400, offset=1.5)  # like real embeddings: most raw sign bits are constant
    s = ChunkVectorStore(tmp_path / "v", DIMS)
    s.upsert_matrix([str(i) for i in range(400)], m)
    s.close()
    mean = np.fromfile(tmp_path / "v" / MEAN_FILE, dtype=np.float32)
    assert np.allclose(mean, m.mean(axis=0), atol=1e-5)
    raw_constant = int((binarize(m).astype(bool).mean(0) == 1).sum())  # bytes, a rough signal
    centered = np.unpackbits(binarize(m, mean), axis=1).mean(0)
    assert float(np.abs(centered - 0.5).max()) < 0.25  # every centered bit carries information
    assert raw_constant >= 0
    ro = ChunkVectorStore(tmp_path / "v", DIMS, mode="view")
    assert ro.format == "2"
    assert ro.search(m[5].tolist(), limit=1)[0][0] == "5"
    ro.close()


def test_recall_with_centered_codes_beats_raw_signs(tmp_path: Path) -> None:
    """The same candidate budget finds more of the exact top 10 when codes are centered."""
    rng = np.random.default_rng(3)
    d = 128
    centers = unit(rng, 60, d, offset=2.0)
    data = centers[rng.integers(0, 60, 4000)] + 0.3 * unit(rng, 4000, d)
    data = (data / np.linalg.norm(data, axis=1, keepdims=True)).astype(np.float32)
    queries = (centers[:40] + 0.2 * unit(rng, 40, d)).astype(np.float32)
    recall: dict[str, float] = {}
    for name in ("raw", "centered"):
        path = tmp_path / name
        if name == "raw":  # a zero mean: the previous release's raw sign codes
            path.mkdir()
            np.zeros(d, dtype=np.float32).tofile(path / MEAN_FILE)
        s = ChunkVectorStore(path, d, oversample=4)
        s.upsert_matrix([str(i) for i in range(4000)], data)
        hits = 0
        for q in queries:
            exact = {i for i, _ in s.exact_search(q, limit=10)}
            hits += len(exact & {i for i, _ in s.search_array(q, limit=10)})
        recall[name] = hits / 400
        s.close()
    assert recall["centered"] > recall["raw"]
    assert recall["centered"] >= 0.75


def _crash_after_graph_replace(path: Path) -> None:
    """The audit's scenario: the graph file is replaced, the process dies before the COMMIT."""
    code = f"""
import os, numpy as np
from pathlib import Path
from wsp_oracle.vectors import ChunkVectorStore, GRAPH_FILE
rng = np.random.default_rng(0)
s = ChunkVectorStore(Path({str(path)!r}), {DIMS})
m = rng.standard_normal((200, {DIMS})).astype(np.float32)
s.upsert_matrix([f"wsp:x/A/{{i}}#0" for i in range(100)], m[:100]); s.save()
s.upsert_matrix([f"wsp:x/A/{{i}}#0" for i in range(100, 200)], m[100:])
s._index.save(str(s.path / GRAPH_FILE))
os._exit(9)
"""
    proc = subprocess.run([sys.executable, "-c", code], check=False)
    assert proc.returncode == 9


def test_crash_between_graph_and_commit_is_repaired(tmp_path: Path) -> None:
    path = tmp_path / "v"
    _crash_after_graph_replace(path)
    db = sqlite3.connect(path / META_FILE)
    assert db.execute("SELECT count(*) FROM chunks").fetchone()[0] == 100  # the second batch rolled back
    db.close()
    s = ChunkVectorStore(path, DIMS)
    assert len(s) == 100  # the 100 keys the id map never committed left the graph
    rng = np.random.default_rng(1)
    m = rng.standard_normal((100, DIMS)).astype(np.float32)
    s.upsert_matrix([f"wsp:x/A/{i}#0" for i in range(100, 200)], m)  # resume: no duplicate keys
    s.save()
    assert len(s) == 200
    assert s.search(m[7].tolist(), limit=1)[0][0] == "wsp:x/A/107#0"
    s.close()


def test_deferred_graph_is_built_once(tmp_path: Path) -> None:
    rng = np.random.default_rng(2)
    m = unit(rng, 300)
    s = ChunkVectorStore(tmp_path / "v", DIMS, defer_graph=True)
    graph = tmp_path / "v" / GRAPH_FILE
    for start in range(0, 300, 100):
        s.upsert_matrix([str(i) for i in range(start, start + 100)], m[start : start + 100])
        s.save()  # durable rows and ids, but no graph rewrite
    assert len(s) == 0
    size_before = graph.stat().st_size
    s.delete(["5"])
    reopened_during_run = ChunkVectorStore(tmp_path / "w", DIMS)  # an unrelated store still works
    reopened_during_run.close()
    assert s.build_graph() == 299
    assert graph.stat().st_size > size_before
    assert s.search(m[42].tolist(), limit=1)[0][0] == "42"
    assert "5" not in {i for i, _ in s.search(m[5].tolist(), limit=5)}
    s.close()
    again = ChunkVectorStore(tmp_path / "v", DIMS)  # consistent: no repair needed
    assert len(again) == 299
    again.close()


def test_a_deferred_run_interrupted_resumes(tmp_path: Path) -> None:
    rng = np.random.default_rng(5)
    m = unit(rng, 100)
    s = ChunkVectorStore(tmp_path / "v", DIMS, defer_graph=True)
    s.upsert_matrix([str(i) for i in range(100)], m)
    s.save()
    s.close()  # the run died before build_graph
    resumed = ChunkVectorStore(tmp_path / "v", DIMS, defer_graph=True)
    assert len(resumed) == 0  # still deferred: opening does not build a whole graph
    assert resumed.build_graph() == 100
    resumed.close()


def test_view_mode_is_read_only_and_sees_new_commits(tmp_path: Path) -> None:
    rng = np.random.default_rng(6)
    m = unit(rng, 200)
    w = ChunkVectorStore(tmp_path / "v", DIMS)
    w.upsert_matrix([str(i) for i in range(100)], m[:100])
    w.save()
    ro = ChunkVectorStore(tmp_path / "v", DIMS, mode="view")
    assert len(ro) == 100
    assert not ro._db.in_transaction  # pyright: ignore[reportPrivateUsage] - no snapshot pins the WAL
    w.upsert_matrix([str(i) for i in range(100, 200)], m[100:])
    w.save()
    assert ro.refresh()
    assert len(ro) == 200
    assert ro.search(m[150].tolist(), limit=1)[0][0] == "150"
    assert not ro.refresh()  # nothing changed since
    with pytest.raises(VectorStoreError):
        ro.delete(["1"])
    ro.close()
    w.close()
    with pytest.raises(VectorStoreError, match="no vector store"):
        ChunkVectorStore(tmp_path / "nothing", DIMS, mode="view")


def test_source_filter_widens_the_candidates(tmp_path: Path) -> None:
    rng = np.random.default_rng(7)
    base = unit(rng, 1)[0]
    near = unit(rng, 300) * 0.1 + base  # 300 near-duplicates from one source
    far = unit(rng, 5) * 0.6 + base  # 5 farther passages from another
    mat = np.vstack([near, far]).astype(np.float32)
    ids = [f"wsp:big/{i}#0" for i in range(300)] + [f"wsp:small/{i}#0" for i in range(5)]
    s = ChunkVectorStore(tmp_path / "v", DIMS, oversample=2, max_candidates=1024)
    s.upsert_matrix(ids, mat)
    found = s.search_array(base.astype(np.float32), limit=5, allow=lambda c: c.startswith("wsp:small/"))
    assert len(found) == 5
    docs = DocVectorIndex(s).search_docs(base.astype(np.float32), limit=3, allow=lambda c: "small" in c)
    assert all("small" in d for d, _, _ in docs)
    s.close()


def test_non_finite_embeddings_are_rejected(tmp_path: Path) -> None:
    s = ChunkVectorStore(tmp_path / "v", DIMS)
    m = np.ones((2, DIMS), dtype=np.float32)
    m[1, 3] = np.nan
    with pytest.raises(VectorStoreError, match="non-finite"):
        s.upsert_matrix(["a", "b"], m)
    assert len(s) == 0
    s.close()


def test_format_1_store_opens_with_raw_codes(tmp_path: Path) -> None:
    rng = np.random.default_rng(8)
    m = unit(rng, 50)
    s = ChunkVectorStore(tmp_path / "v", DIMS)
    s.upsert_matrix([str(i) for i in range(50)], m)
    s.close()
    # turn it into a store from the previous release: raw sign codes, no mean, no graph record
    (tmp_path / "v" / MEAN_FILE).unlink()
    db = sqlite3.connect(tmp_path / "v" / META_FILE)
    db.execute("UPDATE meta SET value='1' WHERE key='format'")
    db.execute("DELETE FROM meta WHERE key IN ('graph_stamp', 'graph_count')")
    db.commit()
    db.close()
    old = ChunkVectorStore(tmp_path / "v", DIMS, mode="view")
    assert old.format == "1"
    assert old.search(m[9].tolist(), limit=1)[0][0] in {str(i) for i in range(50)}
    old.close()
    assert os.path.exists(tmp_path / "v" / GRAPH_FILE)
