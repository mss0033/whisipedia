"""Link-analysis static rank: PageRank correctness on synthetic graphs, link resolution, per-language
groups, 64-bit keys, the sidecar, and its use in lexical ranking, fusion and the featured article."""

from __future__ import annotations

import dataclasses
import json
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

import numpy as np
import numpy.typing as npt
import pyarrow as pa
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
    LinkGraph,
    RankConfig,
    StaticRank,
    alias_doc_id,
    blend,
    build_graph,
    compute_static_rank,
    key64,
    key64_one,
    pagerank,
    percentiles,
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


def scan_ids(readers: Sequence[tuple[str, RefineryReader]]) -> list[str]:
    """Doc ids in node order (the order build_graph numbers them)."""
    return [
        d for _, r in readers for b in r.scan(["doc_id"]) for d in cast(list[str], b.column(0).to_pylist())
    ]


def edge_pairs(graph: LinkGraph, ids: Sequence[str]) -> set[tuple[str, str]]:
    pairs: set[tuple[str, str]] = set()
    for kind, indeg in (("intra", graph.indeg_intra), ("cross", graph.indeg_cross)):
        dst = np.repeat(np.arange(graph.n), indeg)
        pairs |= {(ids[int(s)], ids[int(t)]) for s, t in zip(graph.edge_src(kind), dst, strict=True)}
    return pairs


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
    # tiny chunks, buckets and batches: the result must not depend on any boundary
    cfg = RankConfig(edge_chunk=17, tolerance=1e-7, max_iterations=500, bucket_nodes=8, batch_size=7)
    graph = build_graph(readers, tmp_path / "work", config=cfg)
    assert graph.n == 60
    assert graph.edges == len(set(edges))
    ids = scan_ids(readers)
    assert edge_pairs(graph, ids) == {(f"wsp:enc/N{s:04d}", f"wsp:enc/N{t:04d}") for s, t in edges}
    per_source, global_, iters, _, res = pagerank(graph, cfg)
    assert iters < 500
    assert res < 1e-6
    want = reference_pagerank(60, edges)
    order = [int(x.split("N")[-1]) for x in ids]
    np.testing.assert_allclose(global_, want[order], rtol=1e-4)  # float32 ranks, float64 sums
    np.testing.assert_allclose(per_source, global_, rtol=1e-9)  # one group: both are the same
    assert float(global_.sum(dtype=np.float64)) == pytest.approx(1.0, rel=1e-5)
    graph.remove_edges()


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
    per_source, global_, *_ = pagerank(graph, RankConfig(tolerance=1e-8, max_iterations=300))
    ids = scan_ids(readers)
    for source in ("wiki", "rfc"):
        rows = [i for i, d in enumerate(ids) if d.startswith(f"wsp:{source}/")]
        assert float(per_source[rows].sum()) == pytest.approx(1.0, rel=1e-5)
    assert float(global_.sum()) == pytest.approx(1.0, rel=1e-5)
    hub = ids.index("wsp:wiki/Hub")
    rfc1 = ids.index("wsp:rfc/1")
    # cross-source citations lift the hub globally; inside rfc, RFC 1 is the best-linked page
    assert global_[hub] == global_.max()
    rfc_rows = [i for i, d in enumerate(ids) if d.startswith("wsp:rfc/")]
    assert per_source[rfc1] == per_source[rfc_rows].max()
    feature = blend(per_source, global_, graph.node_group, 0.5)
    assert feature[hub] == feature.max()
    assert float(feature.min()) >= 0
    assert float(feature.max()) <= 1
    assert graph.node_source.tolist() == [graph.sources.index(d[4:].split("/")[0]) for d in ids]


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
    assert graph.resolved == {"exact": 1, "loose": 1, "alias": 1}  # the self-link resolves exactly
    no_alias = build_graph(readers, tmp_path / "w2", config=RankConfig(resolve_aliases=False))
    assert (no_alias.edges, no_alias.unresolved) == (0, 3)
    assert alias_doc_id("wsp:w/A/Alan_Turing", "Alan M. Turing", prefixed=True) == "wsp:w/A/Alan_M._Turing"
    assert alias_doc_id("wsp:rfc/9110", "HTTP Semantics", prefixed=False) == "wsp:rfc/HTTP_Semantics"
    assert alias_doc_id("wsp:w/en/AC/DC", "AC DC", prefixed=True) == "wsp:w/en/AC_DC"
    assert alias_doc_id("wsp:w/AC/DC", "ACDC", prefixed=False) == "wsp:w/ACDC"


def test_redirect_aliases_keep_the_language_prefix(tmp_path: Path) -> None:
    """A multi-wiki source: redirects resolve in their own language, titles may contain "/"."""
    docs = [
        doc("w", "en/Alan_Turing", "Alan Turing"),
        doc("w", "de/Alan_Turing", "Alan Turing", lang="de"),
        doc("w", "en/AC/DC", "AC/DC"),
        doc(
            "w", "en/Enigma", "Enigma", links=("wsp:w/en/Turing", "wsp:w/en/AC_DC", "wsp:w/en/Bon_Scott_band")
        ),
        doc("w", "de/Enigma", "Enigma", lang="de", links=("wsp:w/de/Turing",)),
    ]
    readers = write(tmp_path / "ref", docs)
    aliases = [
        ("wsp:w/en/Alan_Turing", "Turing"),
        ("wsp:w/de/Alan_Turing", "Turing"),
        ("wsp:w/en/AC/DC", "AC DC"),
        ("wsp:w/en/AC/DC", "Bon Scott band"),
        ("wsp:w/en/Missing", "Whatever"),  # redirect to a page the generation does not have
    ]
    graph = build_graph(readers, tmp_path / "work", aliases=aliases)
    assert graph.aliases == 4
    assert graph.resolved["alias"] == 4
    assert graph.unresolved == 0
    assert edge_pairs(graph, scan_ids(readers)) == {
        ("wsp:w/en/Enigma", "wsp:w/en/Alan_Turing"),
        ("wsp:w/en/Enigma", "wsp:w/en/AC/DC"),
        ("wsp:w/de/Enigma", "wsp:w/de/Alan_Turing"),
    }
    assert sorted(graph.groups) == [("w", "de"), ("w", "en")]
    assert graph.intra_edges == 3
    graph.remove_edges()


def _loose(doc_id: str) -> str:
    return re.sub("#.*$", "", doc_id).replace("_", " ").lower()


def _mixed_library(seed: int) -> tuple[list[DocRecord], list[tuple[str, str]]]:
    """Two sources, two languages, every way a link resolves or does not (and conflicts in each)."""
    rng = np.random.default_rng(seed)
    natives = [("w", f"en/T{i}", "en") for i in range(40)] + [("w", f"de/T{i}", "de") for i in range(30)]
    natives += [("w", "en/Foo_bar", "en"), ("w", "en/foo_bar", "en")]  # one loose id, two documents
    natives += [("enc", f"N{i}", "en") for i in range(30)]
    ids = [f"wsp:{s}/{n}" for s, n, _ in natives]
    aliases = [
        ("wsp:w/en/T1", "Alias one"),
        ("wsp:w/en/T2", "Alias one"),  # the same alias again: the first one wins
        ("wsp:w/de/T3", "Drei"),
        ("wsp:enc/N4", "Four"),
        ("wsp:w/en/Missing", "Gone"),  # a redirect to a page the library does not have
    ]
    forms = [
        "wsp:w/en/Alias_one",
        "wsp:w/de/Drei",
        "wsp:enc/Four",
        "wsp:w/en/FOO_BAR#Section",
        "wsp:w/en/Gone",
        "wsp:w/en/Nope",
        "https://example.org/",
    ]
    docs: list[DocRecord] = []
    for i, (source, native, lang) in enumerate(natives):
        links: list[str] = []
        for _ in range(int(rng.integers(0, 9))):
            pick = int(rng.integers(0, len(ids) + len(forms)))
            if pick < len(ids):
                target = ids[pick]
                links.append(target if rng.random() < 0.7 else target.lower() + "#Part")
            else:
                links.append(forms[pick - len(ids)])
        if i % 7 == 0:
            links += [ids[i], *links[:1]]  # a self-link and a duplicate
        title = native.partition("/")[2] if source == "w" else native
        docs.append(doc(source, native, title, lang=lang, links=tuple(links)))
    return docs, aliases


def _reference(
    ids: Sequence[str], docs: Sequence[DocRecord], aliases: Sequence[tuple[str, str]]
) -> tuple[set[tuple[int, int]], dict[str, int], int]:
    """Edges, resolution counts and unresolved links by plain dictionaries (first node wins)."""
    exact: dict[str, int] = {}
    loose: dict[str, int] = {}
    for node, doc_id in enumerate(ids):
        exact.setdefault(doc_id, node)
        loose.setdefault(_loose(doc_id), node)
    by_alias: dict[str, int] = {}
    for target, name in aliases:
        if target in exact:
            alias = alias_doc_id(target, name, prefixed=target.startswith("wsp:w/"))
            by_alias.setdefault(_loose(alias), exact[target])
    edges: set[tuple[int, int]] = set()
    counts = {"exact": 0, "loose": 0, "alias": 0}
    unresolved = 0
    by_id: dict[str, DocRecord] = {d.doc_id: d for d in docs}
    for s, doc_id in enumerate(ids):
        for link in by_id[doc_id].links:
            if not link.startswith("wsp:"):
                continue
            if link in exact:
                counts["exact"] += 1
                t = exact[link]
            elif _loose(link) in loose:
                counts["loose"] += 1
                t = loose[_loose(link)]
            elif _loose(link) in by_alias:
                counts["alias"] += 1
                t = by_alias[_loose(link)]
            else:
                unresolved += 1
                continue
            if s != t:
                edges.add((s, t))
    return edges, counts, unresolved


def _reference_ranks(
    n: int, edges: set[tuple[int, int]], groups: npt.NDArray[np.int32], d: float = 0.85
) -> tuple[npt.NDArray[np.float64], npt.NDArray[np.float64]]:
    """Float64 power iteration: per group (edges inside a group, mass kept in it) and global."""

    def iterate(pairs: list[tuple[int, int]], group: npt.NDArray[np.integer[Any]]) -> npt.NDArray[np.float64]:
        size = np.bincount(group).astype(np.float64)
        src = np.array([s for s, _ in pairs], dtype=np.int64)
        dst = np.array([t for _, t in pairs], dtype=np.int64)
        out = np.bincount(src, minlength=n).astype(np.float64)
        r = 1.0 / size[group]
        for _ in range(2000):
            acc = np.zeros(n)
            np.add.at(acc, dst, r[src] / out[src])
            leak = np.bincount(group[out == 0], weights=r[out == 0], minlength=len(size))
            r = d * acc + (d * leak + 1 - d)[group] / size[group]
        return r

    intra = [(s, t) for s, t in edges if groups[s] == groups[t]]
    return iterate(intra, groups), iterate(sorted(edges), np.zeros(n, dtype=np.int64))


def test_bounded_memory_build_matches_an_in_memory_reference(tmp_path: Path) -> None:
    """Tiny chunks, partitions, buckets and buffers send every step through its spill files: the
    graph must equal a dictionary-resolved reference, the ranks a float64 reference, and the sidecar
    must be byte for byte the one of the default sizes."""
    docs, aliases = _mixed_library(11)
    readers = write(tmp_path / "ref", docs)
    ids = scan_ids(readers)
    tiny = RankConfig(
        tolerance=1e-7,
        max_iterations=300,
        node_chunk=3,  # 64 key partitions, 3-node slices
        edge_chunk=5,
        bucket_nodes=4,
        buffer_bytes=40,
        batch_size=5,
    )
    graph = build_graph(readers, tmp_path / "w", aliases=aliases, config=tiny)
    want_edges, want_counts, want_unresolved = _reference(ids, docs, aliases)
    assert {(ids.index(s), ids.index(t)) for s, t in edge_pairs(graph, ids)} == want_edges
    assert graph.resolved == want_counts
    assert want_counts["loose"]  # the library exercises every path
    assert want_counts["alias"]
    assert (graph.edges, graph.unresolved, graph.aliases) == (len(want_edges), want_unresolved, 4)
    node_group = np.array(graph.node_group)
    assert graph.group_sizes == tuple(np.bincount(node_group).tolist())
    out_intra = np.zeros(graph.n, dtype=np.int64)
    out_all = np.zeros(graph.n, dtype=np.int64)
    for s, t in want_edges:
        out_all[s] += 1
        out_intra[s] += node_group[s] == node_group[t]
    assert np.array_equal(graph.out_intra, out_intra)
    assert np.array_equal(np.asarray(graph.out_intra) + graph.out_cross, out_all)
    assert graph.intra_edges == int(out_intra.sum())
    assert np.array_equal(graph.id_keys, np.sort(key64(ids)))
    per_group, global_, iterations, *_ = pagerank(graph, tiny)
    assert iterations < tiny.max_iterations
    graph.remove_edges()
    assert not list((tmp_path / "w").iterdir())
    want_group, want_global = _reference_ranks(graph.n, want_edges, node_group)
    np.testing.assert_allclose(per_group, want_group, rtol=2e-4)
    np.testing.assert_allclose(global_, want_global, rtol=2e-4)

    small = tmp_path / "small" / RANK_FILE
    default = tmp_path / "default" / RANK_FILE
    stats = compute_static_rank(readers, small, tmp_path / "w", aliases=aliases, config=tiny)
    big = dataclasses.replace(tiny, node_chunk=RankConfig().node_chunk, edge_chunk=RankConfig().edge_chunk)
    compute_static_rank(readers, default, tmp_path / "w2", aliases=aliases, config=big)
    assert small.read_bytes() == default.read_bytes()  # no result depends on a chunk boundary
    assert stats.edges == len(want_edges)
    rank = StaticRank(small)
    found = rank.pageranks(ids)
    assert found is not None
    np.testing.assert_array_equal(found[0], per_group)
    np.testing.assert_array_equal(found[1], global_)
    # the feature: percentiles by hand (values strictly below / group size - 1)
    pct_group = np.array(
        [
            (per_group[node_group == g] < v).sum() / max(1, (node_group == g).sum() - 1)
            for v, g in zip(per_group, node_group, strict=True)
        ]
    )
    pct_global = np.array([(global_ < v).sum() / max(1, len(global_) - 1) for v in global_])
    want_feature = (0.5 * pct_group.astype(np.float32) + 0.5 * pct_global.astype(np.float32)).astype(
        np.float32
    )
    np.testing.assert_array_equal(rank.features(ids), want_feature)
    np.testing.assert_array_equal(blend(per_group, global_, node_group, 0.5), want_feature)


def test_languages_are_ranked_among_themselves(tmp_path: Path) -> None:
    """A big clique of interlinked stubs in one language must not outrank another language's pages."""
    stubs = [
        doc(
            "w", f"ceb/S{i}", f"S{i}", lang="ceb", links=tuple(f"wsp:w/ceb/S{j}" for j in range(40) if j != i)
        )
        for i in range(40)
    ]
    en = [doc("w", f"en/P{i}", f"P{i}", links=("wsp:w/en/Hub",)) for i in range(5)] + [
        doc("w", "en/Hub", "Hub")
    ]
    readers = write(tmp_path / "ref", [*stubs, *en])
    out = tmp_path / RANK_FILE
    stats = compute_static_rank(readers, out, tmp_path / "work")
    assert stats.groups == {"w/ceb": 40, "w/en": 6}
    rank = StaticRank(out)
    hub, stub, leaf = rank.features(["wsp:w/en/Hub", "wsp:w/ceb/S3", "wsp:w/en/P1"]).tolist()
    assert hub > stub  # the best English page beats a stub of a 40-page clique
    assert leaf == 0.0  # nobody links to it
    found = rank.pageranks(["wsp:w/en/Hub", "wsp:w/ceb/S3"])
    assert found is not None
    per, _ = found
    assert per[0] > per[1]  # inside its own language the hub holds a larger share


def test_key64() -> None:
    import wsp_index.linkrank as lr

    words = [f"wsp:w/en/T{i}" for i in range(50_000)] + ["", "a", "é", "東京", "x" * 10_000]
    keys = key64(pa.array(words))
    assert len(set(keys.tolist())) == len(words)  # no collisions, the empty string included
    assert key64_one("wsp:w/en/T7") == int(keys[7])
    assert np.array_equal(key64(pa.array(words, type=pa.large_string())), keys)
    assert np.array_equal(key64(pa.array(words).slice(3, 10)), keys[3:13])  # Arrow offsets honoured
    assert np.array_equal(key64(pa.chunked_array([words[:5], words[5:9]])), keys[:9])
    assert np.array_equal(key64(pa.array(["a", None, "b"])), key64(pa.array(["a", "", "b"])))
    assert np.array_equal(key64(["a", "b"]), key64(pa.array(["a", "b"])))  # a plain sequence
    assert key64(pa.array([], type=pa.string())).shape == (0,)
    old = lr._CHUNK_BYTES  # pyright: ignore[reportPrivateUsage]
    try:
        lr._CHUNK_BYTES = 64  # pyright: ignore[reportPrivateUsage] - every chunking path, oversized rows too
        assert np.array_equal(key64(pa.array(words)), keys)
    finally:
        lr._CHUNK_BYTES = old  # pyright: ignore[reportPrivateUsage]
    with pytest.raises(TypeError):
        key64(pa.array([1, 2]))


def test_percentiles() -> None:
    v = np.array([0.1, 0.5, 0.5, 0.9, 0.2, 0.3], dtype=np.float32)
    assert percentiles(v).tolist() == pytest.approx([0.0, 0.6, 0.6, 1.0, 0.2, 0.4])
    g = np.array([0, 0, 0, 0, 1, 1], dtype=np.int32)
    assert percentiles(v, g).tolist() == pytest.approx([0.0, 1 / 3, 1 / 3, 1.0, 0.0, 1.0])


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
    assert rank.version == 2
    feats = rank.features([d.doc_id for d in docs] + ["wsp:enc/missing"])
    assert feats[-1] == 0.0
    assert (feats >= 0).all()
    assert (feats <= 1).all()
    assert float(feats.max()) > 0.9
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
    z = np.zeros(0, np.float32)
    write_rank_file(out, np.zeros(0, np.uint64), np.zeros(0, np.int32), z, z, z)
    rank = StaticRank(out)
    assert len(rank) == 0
    assert rank.features(["wsp:a/b"]).tolist() == [0.0]
    assert rank.pagerank("wsp:a/b") is None
    stats = compute_static_rank([], tmp_path / "e" / RANK_FILE, tmp_path / "work")
    assert stats.nodes == 0


def test_version_1_sidecar_still_reads(tmp_path: Path) -> None:
    """Files from before the percentile feature: blake2b keys, the log feature squashed into [0, 1)."""
    from wsp_index.refinery import id_hash

    ids = ["wsp:a/x", "wsp:a/y"]
    h = np.array([id_hash(d) for d in ids], dtype=np.uint64)
    order = np.argsort(h)
    feat = np.array([3.0, 0.5], dtype=np.float32)[order]
    body = h[order].tobytes() + feat.tobytes() * 3
    out = tmp_path / RANK_FILE
    out.write_bytes(b"WSPRANK1" + np.uint64(2).tobytes() + body)
    rank = StaticRank(out)
    assert rank.version == 1
    assert rank.features(ids).tolist() == pytest.approx([0.75, 0.5 / 1.5])


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
        lex, config=FusionConfig(static_rank=_Static({"wsp:a/2": 1.0}), static_weight=0.1)
    )
    assert boosted.search("q")[0].doc_id == "wsp:a/2"
    zero = HybridSearcher(lex, config=FusionConfig(static_rank=_Static({"wsp:a/2": 1.0})))
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
