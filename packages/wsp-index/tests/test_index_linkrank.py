"""Link-analysis static rank: PageRank correctness on synthetic graphs, link resolution, the sidecar,
and its use in lexical ranking, fusion and the portal's featured article."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import cast

import numpy as np
import pytest

from wsp_core.ids import DocId
from wsp_core.model import DocRecord, SearchHit
from wsp_index import (
    FusionConfig,
    HybridSearcher,
    RefineryReader,
    RefineryWriter,
    TantivyConfig,
    TantivyIndex,
)
from wsp_index.catalog import CatalogConfig, build_source_catalog, refresh_featured
from wsp_index.linkrank import (
    RANK_FILE,
    RankConfig,
    StaticRank,
    alias_doc_id,
    blend,
    build_graph,
    compute_static_rank,
    pagerank,
    top_documents,
    write_rank_file,
)
from wsp_index.refinery import generation_dir
from wsp_index_helpers import doc

SMALL = TantivyConfig(writer_heap_bytes=20_000_000, writer_threads=1)


def write(root: Path, docs: Sequence[DocRecord], gen: str = "g1") -> list[tuple[str, RefineryReader]]:
    by: dict[str, list[DocRecord]] = {}
    for d in docs:
        by.setdefault(d.source_id, []).append(d)
    out: list[tuple[str, RefineryReader]] = []
    for source, recs in sorted(by.items()):
        with RefineryWriter(root, source, gen) as w:
            w.write(recs)
        out.append((source, RefineryReader(generation_dir(root, source, gen))))
    return out


def reference_pagerank(n: int, edges: list[tuple[int, int]], d: float = 0.85) -> np.ndarray:
    """Dense textbook PageRank (dangling nodes spread uniformly) for checking the streamed one."""
    m = np.zeros((n, n))
    for s, t in set(edges):
        m[t, s] = 1.0
    out = m.sum(axis=0)
    for j in range(n):
        m[:, j] = m[:, j] / out[j] if out[j] else 1.0 / n
    r = np.full(n, 1.0 / n)
    for _ in range(500):
        r = d * m @ r + (1 - d) / n
    return r


def random_library(seed: int, n: int, m: int) -> tuple[list[DocRecord], list[tuple[int, int]]]:
    rng = np.random.default_rng(seed)
    edges = [(int(a), int(b)) for a, b in rng.integers(0, n, size=(m, 2)) if a != b]
    links: dict[int, list[str]] = {}
    for s, t in edges:
        links.setdefault(s, []).append(f"wsp:enc/N{t:04d}")
    docs = [doc("enc", f"N{i:04d}", f"Node {i}", links=tuple(links.get(i, ()))) for i in range(n)]
    return docs, edges


def test_streamed_pagerank_matches_the_dense_reference(tmp_path: Path) -> None:
    docs, edges = random_library(7, 60, 240)
    readers = write(tmp_path / "ref", docs)
    # tiny chunks: the streamed power iteration must not depend on chunk boundaries
    cfg = RankConfig(edge_chunk=17, tolerance=1e-12, max_iterations=500)
    graph = build_graph(readers, tmp_path / "work", config=cfg)
    assert graph.n == 60
    assert graph.edges == len(set(edges))
    per_source, global_, iters, _, res = pagerank(graph, cfg)
    assert iters < 500
    assert res < 1e-10
    want = reference_pagerank(60, edges)
    order = [int(x.split("N")[-1]) for x in cast(list[str], graph.doc_ids.to_pylist())]
    np.testing.assert_allclose(global_, want[order], rtol=1e-6)
    np.testing.assert_allclose(per_source, global_, rtol=1e-9)  # one source: both are the same
    assert global_.sum() == pytest.approx(1.0)


def test_per_source_rank_is_computed_inside_each_source(tmp_path: Path) -> None:
    docs = [
        doc("wiki", "Hub", "Hub"),
        doc("wiki", "A", "A", links=("wsp:wiki/Hub",)),
        doc("wiki", "B", "B", links=("wsp:wiki/Hub", "wsp:rfc/1")),
        doc("rfc", "1", "RFC 1"),
        doc("rfc", "2", "RFC 2", links=("wsp:rfc/1", "wsp:wiki/Hub")),
        doc("rfc", "3", "RFC 3", links=("wsp:wiki/Hub",)),
    ]
    readers = write(tmp_path / "ref", docs)
    graph = build_graph(readers, tmp_path / "w")
    assert (graph.edges, graph.intra_edges) == (6, 3)
    per_source, global_, *_ = pagerank(graph, RankConfig(tolerance=1e-12, max_iterations=300))
    ids = cast(list[str], graph.doc_ids.to_pylist())
    for source in ("wiki", "rfc"):
        rows = [i for i, d in enumerate(ids) if d.startswith(f"wsp:{source}/")]
        assert per_source[rows].sum() == pytest.approx(1.0)
    assert global_.sum() == pytest.approx(1.0)
    hub = ids.index("wsp:wiki/Hub")
    rfc1 = ids.index("wsp:rfc/1")
    # cross-source citations lift the hub globally; inside rfc, RFC 1 is the best-linked page
    assert global_[hub] == global_.max()
    rfc_rows = [i for i, d in enumerate(ids) if d.startswith("wsp:rfc/")]
    assert per_source[rfc1] == per_source[rfc_rows].max()
    feature = blend(per_source, global_, graph.node_source, 0.5)
    assert feature[hub] == feature.max()
    assert float(feature.min()) > 0


def test_links_resolve_through_case_fragments_and_redirects(tmp_path: Path) -> None:
    docs = [
        doc("w", "A/Alan_Turing", "Alan Turing"),
        doc(
            "w",
            "A/Enigma",
            "Enigma",
            links=(
                "wsp:w/A/alan_turing#Early_life",  # case and fragment
                "wsp:w/A/Turing",  # a redirect
                "wsp:w/A/Nowhere",  # not in the library
                "https://example.org/",  # not a library link at all
                "wsp:w/A/Enigma",  # self-link, dropped
            ),
        ),
    ]
    readers = write(tmp_path / "ref", docs)
    graph = build_graph(readers, tmp_path / "w", aliases=[("wsp:w/A/Alan_Turing", "Turing")])
    assert (graph.edges, graph.unresolved) == (1, 1)  # both resolved links are the same edge
    no_alias = build_graph(readers, tmp_path / "w2", config=RankConfig(resolve_aliases=False))
    assert (no_alias.edges, no_alias.unresolved) == (0, 3)
    assert alias_doc_id("wsp:w/A/Alan_Turing", "Alan M. Turing") == "wsp:w/A/Alan_M._Turing"
    assert alias_doc_id("wsp:rfc/9110", "HTTP Semantics") == "wsp:rfc/HTTP_Semantics"


def test_sidecar_round_trip_and_corruption(tmp_path: Path) -> None:
    docs, _ = random_library(3, 40, 120)
    readers = write(tmp_path / "ref", docs)
    out = tmp_path / "lexical" / RANK_FILE
    stats = compute_static_rank(readers, out, tmp_path / "work")
    assert stats.nodes == 40
    assert stats.sources == {"enc": 40}
    assert "40 nodes" in stats.line()
    assert json.loads(out.with_name("static_rank.json").read_text())["edges"] == stats.edges
    assert not list((tmp_path / "work").iterdir()), "the edge list is scratch"
    rank = StaticRank(out)
    assert len(rank) == 40
    feats = rank.features([d.doc_id for d in docs] + ["wsp:enc/missing"])
    assert feats[-1] == 0.0
    assert (feats[:-1] > 0).all()
    pr = rank.pagerank(docs[0].doc_id)
    assert pr is not None
    assert pr[0] == pytest.approx(pr[1], rel=1e-5)
    assert rank.pagerank("wsp:enc/missing") is None
    best = top_documents(readers[0][1], rank, 3)
    assert [f for _, f in best] == sorted((f for _, f in best), reverse=True)
    assert best[0][1] == pytest.approx(float(feats[:-1].max()))

    out.write_bytes(out.read_bytes()[:-3])
    assert StaticRank.open(out) is None
    assert StaticRank.open(tmp_path / "absent.bin") is None
    (tmp_path / "junk.bin").write_bytes(b"x" * 64)
    assert StaticRank.open(tmp_path / "junk.bin") is None


def test_empty_library(tmp_path: Path) -> None:
    out = tmp_path / RANK_FILE
    write_rank_file(out, [], np.zeros(0, np.float32), np.zeros(0), np.zeros(0))
    rank = StaticRank(out)
    assert len(rank) == 0
    assert rank.features(["wsp:a/b"]).tolist() == [0.0]


def _same_text_corpus() -> list[DocRecord]:
    """Ten pages with identical text; page 7 is linked from all the others."""
    text = "a page about lighthouses and their keepers"
    return [
        doc("enc", f"P{i}", f"Lighthouse {i}", text, links=() if i == 7 else ("wsp:enc/P7",))
        for i in range(10)
    ]


def test_lexical_search_blends_the_static_rank(tmp_path: Path) -> None:
    docs = _same_text_corpus()
    readers = write(tmp_path / "ref", docs)
    idx = TantivyIndex(tmp_path / "lexical", SMALL)
    idx.upsert(docs)
    idx.commit()
    before = [h.doc_id for h in idx.search("lighthouses keepers", limit=10)]
    assert before[0] != "wsp:enc/P7"
    compute_static_rank(readers, idx.rank_path, tmp_path / "work")
    assert idx.reload_static_rank() is not None
    after = idx.search("lighthouses keepers", limit=10)
    assert after[0].doc_id == "wsp:enc/P7"
    assert after[0].score > after[1].score
    # paging is consistent with the re-sorted order
    page2 = idx.search("lighthouses keepers", limit=5, offset=5)
    assert [h.doc_id for h in after[5:]] == [h.doc_id for h in page2]
    assert [h.doc_id for h in idx.search_hits("lighthouses keepers", limit=1)] == ["wsp:enc/P7"]
    idx.close()

    off = TantivyIndex(tmp_path / "lexical", TantivyConfig(writer_threads=1, rank_weight=0.0))
    assert [h.doc_id for h in off.search("lighthouses keepers", limit=10)] == before
    shallow = TantivyIndex(tmp_path / "lexical", TantivyConfig(writer_threads=1, rank_depth=0))
    assert [h.doc_id for h in shallow.search("lighthouses keepers", limit=10)] == before
    off.close()
    shallow.close()


class _Static:
    def __init__(self, scores: dict[str, float]) -> None:
        self.scores = scores

    def __call__(self, ids: Sequence[str]) -> Sequence[float]:
        return [self.scores.get(i, 0.0) for i in ids]


class _Lexical:
    def __init__(self, hits: list[SearchHit]) -> None:
        self.hits = hits

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


def test_fusion_static_boost() -> None:
    hits = [SearchHit(DocId(f"wsp:a/{i}"), 1.0, f"T{i}", "", "a") for i in range(3)]
    lex = _Lexical(hits)
    plain = HybridSearcher(lex, config=FusionConfig())
    assert [h.doc_id for h in plain.search("q")] == ["wsp:a/0", "wsp:a/1", "wsp:a/2"]
    boosted = HybridSearcher(
        lex, config=FusionConfig(static_rank=_Static({"wsp:a/2": 5.0}), static_weight=0.1)
    )
    assert boosted.search("q")[0].doc_id == "wsp:a/2"
    zero = HybridSearcher(lex, config=FusionConfig(static_rank=_Static({"wsp:a/2": 5.0})))
    assert zero.search("q")[0].doc_id == "wsp:a/0"  # weight 0: off


def test_featured_articles_follow_the_static_rank(tmp_path: Path) -> None:
    long = " ".join(["word"] * 400)
    docs = [doc("enc", f"P{i}", f"Page {i}", long, links=() if i == 3 else ("wsp:enc/P3",)) for i in range(6)]
    root = tmp_path / "ref"
    readers = write(root, docs)
    out_root = tmp_path / "catalog"
    rank_path = tmp_path / RANK_FILE
    compute_static_rank(readers, rank_path, tmp_path / "work")
    rank = StaticRank(rank_path)
    cfg = CatalogConfig(featured=True, min_featured_chars=100, rank=rank)
    path = build_source_catalog(root, "enc", "g1", out_root, cfg)
    assert json.loads(path.read_text())["featured"][0] == "wsp:enc/P3"
    payload = json.loads(path.read_text())
    payload["featured"] = []
    path.write_text(json.dumps(payload))
    assert refresh_featured(root, "enc", "g1", out_root, cfg)
    assert json.loads(path.read_text())["featured"][0] == "wsp:enc/P3"
    assert not refresh_featured(root, "enc", "other-generation", out_root, cfg)
    assert not refresh_featured(root, "nope", "g1", out_root, cfg)
