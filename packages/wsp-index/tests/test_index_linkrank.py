"""Link-analysis static rank: PageRank correctness on synthetic graphs, link resolution, per-language
groups, 64-bit keys, the sidecar, and its use in lexical ranking, fusion and the featured article."""

from __future__ import annotations

import dataclasses
import json
import os
import re
from collections.abc import Iterator, Sequence
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
    LINK_SCAN_MAX_AGE_DAYS,
    RANK_FILE,
    LinkGraph,
    RankConfig,
    RankInput,
    StaticRank,
    alias_doc_id,
    blend,
    build_graph,
    compute_static_rank,
    key64,
    key64_one,
    link_free_sources,
    pagerank,
    percentiles,
    rank_due,
    top_documents,
    write_rank_file,
)
from wsp_index.refinery import (
    SUCCESS_FILE,
    compose_set,
    generation_dir,
    library_links,
    part_dir,
    seal_stamp,
    set_members,
)
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


def test_the_fence_finds_exactly_what_a_full_binary_search_finds(tmp_path: Path) -> None:
    """A lookup through the in-memory fence (one page of the key array per block) gives the same
    positions as a binary search over the whole key array, for present and absent keys."""
    import os

    from wsp_index.linkrank import FENCE_STRIDE, fence_path, write_fence

    rng = np.random.default_rng(7)
    n = FENCE_STRIDE * 37 + 123  # a ragged last block
    keys = np.unique(rng.integers(1, 2**63, size=n + 50, dtype=np.uint64))[:n]
    f = rng.random(n).astype(np.float32)
    out = tmp_path / "static_rank.bin"
    write_rank_file(out, keys, np.arange(n, dtype=np.int32), f, f, f)
    assert fence_path(out).is_file()  # written with the sidecar
    fenced = StaticRank(out)
    assert fenced._fence is not None  # pyright: ignore[reportPrivateUsage]
    probes = np.concatenate(
        [
            keys[rng.integers(0, n, 3000)],
            rng.integers(0, 2**63, 3000, dtype=np.uint64),
            np.array([0, keys[0], keys[-1], np.uint64(2**64 - 1)], dtype=np.uint64),
        ]
    )
    assert np.array_equal(fenced._fenced(probes), np.searchsorted(keys, probes))  # pyright: ignore[reportPrivateUsage]
    # A stale fence (older than the sidecar) is ignored, not trusted.
    os.utime(fence_path(out), ns=(1, 1))
    assert StaticRank(out)._fence is None  # pyright: ignore[reportPrivateUsage]
    write_fence(out)
    assert StaticRank(out)._fence is not None  # pyright: ignore[reportPrivateUsage]


def test_the_fence_is_in_place_before_the_new_sidecar_appears(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """2026-10-26 review #104: an API reloading the sidecar the moment it appeared found no fence yet
    and ran unfenced until restarted."""
    from wsp_index import linkrank

    path = tmp_path / "static_rank.bin"
    real = os.replace
    seen: list[bool] = []

    def replace(src: str | Path, dst: str | Path) -> None:
        if Path(dst) == path:
            seen.append(linkrank.fence_path(path).is_file())
        real(src, dst)

    monkeypatch.setattr(os, "replace", replace)
    keys = np.arange(2000, dtype=np.uint64)
    one = np.ones(2000, dtype=np.float32)
    linkrank.write_rank_file(path, keys, np.arange(2000, dtype=np.int32), one, one, one)
    assert seen == [True]
    rank = linkrank.StaticRank.open(path)
    assert rank is not None
    assert rank._fence is not None  # pyright: ignore[reportPrivateUsage]


# --- whether to compute it again (2026-11-05 deep dive) -------------------------------------------------


def _inputs(readers: Sequence[tuple[str, RefineryReader]]) -> dict[str, RankInput]:
    out: dict[str, RankInput] = {}
    for sid, r in readers:
        links = library_links(r.dir)
        unknown = () if links is not None else (r.generation_id,)
        out[sid] = RankInput(r.generation_id, seal_stamp(r.dir), links or 0, False, unknown)
    return out


def test_the_rank_records_what_each_source_brought_to_the_graph(tmp_path: Path) -> None:
    """The seal counts the library links a generation carries; the rank's statistics record each
    source's generation, seal, links and aliases, which is what rank_due compares."""
    wiki = [doc("w", f"en/P{i}", f"P{i}", links=("wsp:w/en/Hub", "https://example.org/x")) for i in range(3)]
    wiki.append(doc("w", "en/Hub", "Hub", links=("wsp:pubmed/1",)))
    papers = [doc("pubmed", str(i), f"Paper {i}", links=("https://doi.org/10.1/x",)) for i in range(3)]
    readers = write(tmp_path / "ref", [*wiki, *papers])
    assert [library_links(r.dir) for _, r in readers] == [0, 4]  # pubmed, w
    out = tmp_path / RANK_FILE
    stats = compute_static_rank(readers, out, tmp_path / "work", aliases=[("wsp:w/en/Hub", "Centre")])
    recorded = json.loads(out.with_name("static_rank.json").read_text())
    seals = _inputs(readers)
    assert recorded["inputs"] == {
        "pubmed": {"generation": "g1", "seal": seals["pubmed"].seal, "links": 0, "aliases": 0, "members": []},
        "w": {"generation": "g1", "seal": seals["w"].seal, "links": 4, "aliases": 1, "members": []},
    }
    assert recorded["computed_at"] == stats.computed_at > 0


def test_a_rank_is_kept_while_only_link_free_sources_change(tmp_path: Path) -> None:
    """The nightly build ran PageRank (4-12 h on the NAS) whenever anything was indexed, while PubMed
    updates, which link nowhere, were all that changed: 8 new edges in 13 days."""
    wiki = [doc("w", f"en/P{i}", f"P{i}", links=("wsp:w/en/Hub",)) for i in range(3)]
    wiki.append(doc("w", "en/Hub", "Hub"))
    papers = [doc("pubmed", str(i), f"Paper {i}") for i in range(3)]
    readers = write(tmp_path / "ref", [*wiki, *papers])
    out = tmp_path / RANK_FILE
    stats = compute_static_rank(readers, out, tmp_path / "work")
    stats_path = out.with_name("static_rank.json")
    now = stats.computed_at + 3600
    current = _inputs(readers)
    assert not rank_due(stats_path, current, now=now).due  # nothing changed...
    assert not rank_due(stats_path, current, now=now + 30 * 86400).due  # ...however old

    # A PubMed update: a new generation that carries no library link and has no aliases.
    (pubmed2,) = write(tmp_path / "ref", [*papers, doc("pubmed", "9", "Paper 9")], gen="g2")
    update = {**current, **_inputs([pubmed2])}
    kept = rank_due(stats_path, update, now=now)
    assert not kept.due
    assert "pubmed" in kept.reason
    assert rank_due(stats_path, update, now=stats.computed_at + 7 * 86400).due  # the weekly refresh
    assert rank_due(stats_path, update, now=stats.computed_at - 60).due  # a clock set back: recount
    assert rank_due(stats_path, update, config=RankConfig(mix=0.3), now=now).due
    # Alias rows alone change nothing while no link names the source (the kept rank records which
    # sources links name); test_a_link_free_source_that_links_name_makes_the_rank_due has the others.
    aliased = dataclasses.replace(update["pubmed"], aliases=True)
    assert not rank_due(stats_path, {**update, "pubmed": aliased}, now=now).due
    # Its seal does not say whether it carries links (sealed before the count).
    unknown = rank_due(
        stats_path, {**update, "pubmed": dataclasses.replace(update["pubmed"], unknown=("g2",))}, now=now
    )
    assert unknown.due
    assert "does not record" in unknown.reason
    # A new link-free source: kept; one that links: due.
    (news,) = write(tmp_path / "ref", [doc("news", "1", "Headline")])
    assert not rank_due(stats_path, {**current, **_inputs([news])}, now=now).due
    (linking,) = write(tmp_path / "ref", [doc("blog", "1", "Post", links=("wsp:w/en/Hub",))])
    assert rank_due(stats_path, {**current, **_inputs([linking])}, now=now).due

    # The linking source changed (even refined again under the same id: another seal), or left.
    (wiki_again,) = write(tmp_path / "ref2", wiki)
    assert wiki_again[1].generation_id == current["w"].generation
    assert rank_due(stats_path, {**current, **_inputs([wiki_again])}, now=now).due
    assert rank_due(stats_path, {"pubmed": current["pubmed"]}, now=now).due
    assert not rank_due(stats_path, {"w": current["w"]}, now=now).due  # a link-free source left

    # No record of the inputs (a rank computed before 2026-11-05), or no statistics at all.
    recorded = json.loads(stats_path.read_text())
    del recorded["inputs"]
    stats_path.write_text(json.dumps(recorded))
    assert rank_due(stats_path, current, now=now).due
    stats_path.unlink()
    assert rank_due(stats_path, current, now=now).due


def test_a_set_generation_records_its_members_links(tmp_path: Path) -> None:
    root = tmp_path / "ref"
    for gid, links in (("2026.a", ("wsp:s/x",)), ("2026.b", ())):
        with RefineryWriter(root, "s", gid, directory=part_dir(root, "s", gid)) as w:
            w.write([doc("s", gid, gid, links=links)])
    members = [part_dir(root, "s", g) for g in ("2026.a", "2026.b")]
    assert library_links(generation_dir(root, "s", compose_set(root, "s", members))) == 1
    # A member sealed before the count: the set does not know either.
    seal = members[1] / SUCCESS_FILE
    meta = json.loads(seal.read_text())
    del meta["library_links"]
    seal.write_text(json.dumps(meta))
    with RefineryWriter(root, "s", "2026.c", directory=part_dir(root, "s", "2026.c")) as w:
        w.write([doc("s", "c", "c")])
    later = compose_set(root, "s", [*members, part_dir(root, "s", "2026.c")])
    assert library_links(generation_dir(root, "s", later)) is None


def test_a_set_keeps_the_rank_when_only_a_new_link_free_partition_joins(tmp_path: Path) -> None:
    """nara-catalog adds a partition a night to dozens sealed before seals counted links: the old ones
    are known to carry none when the rank read them in a source that brought none."""
    root = tmp_path / "ref"
    for gid in ("2026.a", "2026.b"):
        with RefineryWriter(root, "s", gid, directory=part_dir(root, "s", gid)) as w:
            w.write([doc("s", gid, gid)])
    first = compose_set(root, "s", [part_dir(root, "s", g) for g in ("2026.a", "2026.b")])
    gen_dir = generation_dir(root, "s", first)
    out = tmp_path / RANK_FILE
    stats = compute_static_rank([("s", RefineryReader(gen_dir))], out, tmp_path / "work")
    stats_path = out.with_name("static_rank.json")
    assert json.loads(stats_path.read_text())["inputs"]["s"]["members"] == ["2026.a", "2026.b"]
    # The next night's set: a third member, counted; the first two sealed before the count.
    with RefineryWriter(root, "s", "2026.c", directory=part_dir(root, "s", "2026.c")) as w:
        w.write([doc("s", "c", "c")])
    later = compose_set(root, "s", [part_dir(root, "s", g) for g in ("2026.a", "2026.b", "2026.c")])
    seal = seal_stamp(generation_dir(root, "s", later))
    now = stats.computed_at + 60
    parts = ("2026.a", "2026.b", "2026.c")
    old_members = RankInput(later, seal, links=0, aliases=False, unknown=("2026.a", "2026.b"), members=parts)
    assert not rank_due(stats_path, {"s": old_members}, now=now).due
    # A member the rank never read whose seal does not count: it may carry links.
    stranger = RankInput(later, seal, links=0, aliases=False, unknown=("2026.a", "2026.x"), members=parts)
    assert rank_due(stats_path, {"s": stranger}, now=now).due
    # The new member brings links: due.
    assert rank_due(
        stats_path, {"s": RankInput(later, seal, 3, False, ("2026.a", "2026.b"), parts)}, now=now
    ).due


# --- alias rows without a link that names the source (2026-11-10 trawl, search-index-01) ---------------


def _lanes_library(tmp_path: Path) -> tuple[list[tuple[str, RefineryReader]], Path, float]:
    """A wiki that links to itself, to ``named`` and to a source that is not there; ``lane`` carries
    alias rows and nothing links to it (cia-reading-room: 0 links, 1,100 aliases)."""
    wiki = [doc("w", f"en/P{i}", f"P{i}", links=("wsp:w/en/Hub", "wsp:Named/1#top")) for i in range(3)]
    wiki.append(doc("w", "en/Hub", "Hub", links=("wsp:gone/x", "https://example.org/wsp:lane/1")))
    lane = [doc("lane", str(i), f"Report {i}") for i in range(3)]
    named = [doc("named", str(i), f"Named {i}") for i in range(2)]
    readers = write(tmp_path / "ref", [*wiki, *lane, *named])
    out = tmp_path / RANK_FILE
    aliases = [("wsp:lane/1", "First report"), ("wsp:named/1", "One")]
    stats = compute_static_rank(readers, out, tmp_path / "work", aliases=aliases)
    return readers, out.with_name("static_rank.json"), stats.computed_at


def test_the_rank_records_which_sources_links_name(tmp_path: Path) -> None:
    """Every library link is counted by the source its target id names, lower case as loose ids are,
    resolved or not, and from every source."""
    _, stats_path, _ = _lanes_library(tmp_path)
    recorded = json.loads(stats_path.read_text())
    assert recorded["link_targets"] == {"w": 3, "named": 3, "gone": 1}
    assert recorded["inputs"]["lane"]["aliases"] == 1  # alias rows, and no link names it
    assert recorded["links_scanned_at"] == recorded["computed_at"]


def test_link_targets_count_what_the_rank_would_resolve(tmp_path: Path) -> None:
    """The name is cut the way the links resolve: an id with a newline, a head without a ``/``, a name
    no source can have. A link that reaches a document is always counted under that document's source."""
    docs = [
        doc("a", "1", "One", links=("wsp:B/x\ny", "wsp:b", "wsp:not_a_source/1", "wsp:/1", "wsp:B.c-1/z")),
        # The Kelvin sign lower-cases to "k", as it does in a loose id; a "#" before the "/" cuts the
        # loose id short of any document; bare titles are not one name each.
        doc("a", "2", "Two", links=("wsp:\u212a/Page", "wsp:b#top/x", "wsp:Some_Title", "wsp:Other Title")),
        doc("b", "x\ny", "Odd"),
        doc("k", "page", "Page"),
    ]
    readers = write(tmp_path / "ref", docs)
    graph = build_graph(readers, tmp_path / "work", config=RankConfig(batch_size=1))
    assert graph.link_targets == {"b": 2, "b.c-1": 1, "k": 1}
    # wsp:B/x\ny reached wsp:b/x\ny and the Kelvin link wsp:k/page: each counted under its source.
    assert graph.resolved["loose"] == 2
    assert graph.edges == 2


def test_too_many_named_sources_are_not_recorded_at_all(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A name left out would read as "no link names it": past the bound nothing is recorded, and
    rank_due keeps the older rule."""
    from wsp_index import linkrank

    monkeypatch.setattr(linkrank, "MAX_LINK_TARGETS", 2)
    docs = [doc("a", str(i), str(i), links=(f"wsp:s{i}/x",)) for i in range(3)]
    readers = write(tmp_path / "ref", docs)
    out = tmp_path / RANK_FILE
    stats = compute_static_rank(readers, out, tmp_path / "work", config=RankConfig(batch_size=1))
    assert stats.link_targets is None
    assert json.loads(out.with_name("static_rank.json").read_text())["link_targets"] is None


def test_an_alias_only_source_no_link_names_keeps_the_rank(tmp_path: Path) -> None:
    """2026-11-10: 125 of 235 sources carry alias rows and no link; each lane release made the nightly
    build compute PageRank (6-8 h) although an alias can only resolve a link that names its source."""
    readers, stats_path, at = _lanes_library(tmp_path)
    current = {
        sid: dataclasses.replace(i, aliases=sid in ("lane", "named")) for sid, i in _inputs(readers).items()
    }
    now = at + 3600
    assert not rank_due(stats_path, current, now=now).due
    (lane2,) = write(tmp_path / "ref", [doc("lane", str(i), f"Report {i}") for i in range(5)], gen="g2")
    update = {**current, "lane": dataclasses.replace(_inputs([lane2])["lane"], aliases=True)}
    kept = rank_due(stats_path, update, now=now)
    assert not kept.due
    assert "only link-free sources changed (lane)" in kept.reason
    assert rank_due(stats_path, update, now=at + 7 * 86400).due  # the weekly refresh still counts it
    # It left, with its aliases: nothing named it, nothing changes.
    assert not rank_due(stats_path, {k: v for k, v in current.items() if k != "lane"}, now=now).due
    # A new source with alias rows that no link names.
    (new,) = write(tmp_path / "ref", [doc("fresh", "1", "Fresh")])
    fresh = dataclasses.replace(_inputs([new])["fresh"], aliases=True)
    assert not rank_due(stats_path, {**current, "fresh": fresh}, now=now).due
    # What still makes it due: links of its own, a seal that does not count them.
    linking = dataclasses.replace(update["lane"], links=1)
    assert "brings links" in rank_due(stats_path, {**current, "lane": linking}, now=now).reason
    uncounted = dataclasses.replace(update["lane"], unknown=("g2",))
    assert "does not record" in rank_due(stats_path, {**current, "lane": uncounted}, now=now).reason


def test_a_link_free_source_that_links_name_makes_the_rank_due(tmp_path: Path) -> None:
    """A source some link names can gain or lose an edge with every document and alias: changed, gone
    or new, the rank is computed again, with or without alias rows."""
    readers, stats_path, at = _lanes_library(tmp_path)
    current = _inputs(readers)
    now = at + 3600
    assert not rank_due(stats_path, current, now=now).due
    (named2,) = write(tmp_path / "ref", [doc("named", "7", "Seven")], gen="g2")
    changed = rank_due(stats_path, {**current, **_inputs([named2])}, now=now)
    assert changed.due
    assert changed.reason == "named changed and 3 link(s) name it"
    left = rank_due(stats_path, {k: v for k, v in current.items() if k != "named"}, now=now)
    assert left.due
    assert left.reason == "named left and 3 link(s) name it"
    # A link pointed at a source that was not in the library: it arrives.
    (gone,) = write(tmp_path / "ref", [doc("gone", "x", "Back")])
    arrived = rank_due(stats_path, {**current, **_inputs([gone])}, now=now)
    assert arrived.due
    assert arrived.reason == "gone is new and 1 link(s) name it"


def test_a_rank_without_link_targets_uses_the_older_rule(tmp_path: Path) -> None:
    """A guard, true before the change too: a kept rank that does not record which sources links name
    (computed before 2026-11-11), one whose record is not what this code writes, and a source id the
    record cannot speak for all leave an aliased change due."""
    readers, stats_path, at = _lanes_library(tmp_path)
    current = _inputs(readers)
    now = at + 3600
    (lane2,) = write(tmp_path / "ref", [doc("lane", "9", "Report 9")], gen="g2")
    aliased = {**current, "lane": dataclasses.replace(_inputs([lane2])["lane"], aliases=True)}
    recorded = json.loads(stats_path.read_text())
    wrong: list[object] = ["absent", None, [], {"w": "3"}, {"w": -1}, {"w": True}, {"w": 1.5}]
    for broken in wrong:
        if broken == "absent":
            recorded.pop("link_targets", None)
        else:
            recorded["link_targets"] = broken
        stats_path.write_text(json.dumps(recorded))
        assert rank_due(stats_path, aliased, now=now).due, broken
        gone = {k: v for k, v in current.items() if k != "lane"}
        assert rank_due(stats_path, gone, now=now).due, broken  # it left with aliases
    # An id that is not a registry id (or longer than the record's names): the older rule for it alone.
    recorded["link_targets"] = {"w": 3}
    recorded["inputs"]["Odd_Id"] = {"generation": "g0", "seal": "0", "links": 0, "aliases": 2, "members": []}
    stats_path.write_text(json.dumps(recorded))
    odd = dataclasses.replace(aliased["lane"], aliases=True)
    assert rank_due(stats_path, {**current, "Odd_Id": odd}, now=now).reason.startswith("Odd_Id changed")
    assert rank_due(stats_path, current, now=now).reason == "Odd_Id left with links or aliases"
    del recorded["inputs"]["Odd_Id"]
    stats_path.write_text(json.dumps(recorded))
    long_id = "x" * 65
    assert "brings links or aliases" in rank_due(stats_path, {**current, long_id: odd}, now=now).reason


# --- the link scan of sources that hold no link (search-index-03a) --------------------------------------


class _NoLinkScan(RefineryReader):
    """A reader whose ``links`` column must not be read."""

    def scan(
        self, columns: Sequence[str] = ("doc_id",), *, batch_size: int = 4096
    ) -> Iterator[pa.RecordBatch]:
        if "links" in columns:
            raise AssertionError(f"the links of {self.dir} were read")
        return super().scan(columns, batch_size=batch_size)


def _guarded(
    readers: Sequence[tuple[str, RefineryReader]], free: frozenset[str]
) -> list[tuple[str, RefineryReader]]:
    return [(sid, _NoLinkScan(r.dir) if sid in free else r) for sid, r in readers]


def test_the_link_scan_skips_sources_the_kept_rank_found_link_free(tmp_path: Path) -> None:
    """The rank read the ``links`` column of every source over the NAS, 48 of 69 minutes in sources
    that hold none: a source the kept rank found link-free, unchanged since, is not read again, and
    the rank is the same to the byte."""
    readers, stats_path, at = _lanes_library(tmp_path)
    out = tmp_path / RANK_FILE
    first = out.read_bytes()
    before = json.loads(stats_path.read_text())
    free = link_free_sources(stats_path, _inputs(readers), now=at + 7 * 86400)
    assert free == {"lane", "named"}
    said: list[str] = []
    aliases = [("wsp:lane/1", "First report"), ("wsp:named/1", "One")]
    stats = compute_static_rank(
        _guarded(readers, free), out, tmp_path / "work", aliases=aliases, link_free=free, progress=said.append
    )
    assert out.read_bytes() == first
    assert "rank: link scan skipped for 2 link-free source(s), 5 documents" in said
    after = json.loads(stats_path.read_text())
    assert after["inputs"] == before["inputs"]
    assert after["inputs"]["lane"]["links"] == 0
    assert after["link_targets"] == before["link_targets"]
    assert (after["edges"], after["nodes"]) == (before["edges"], before["nodes"])
    assert not rank_due(stats_path, _inputs(readers), now=at + 27 * 86400).due
    # When every source was last read is carried over, not renewed: the belief runs out.
    assert after["links_scanned_at"] == before["links_scanned_at"] == at
    assert stats.computed_at >= at
    # A rank that reads them all (nothing skipped) renews it.
    full = compute_static_rank(readers, out, tmp_path / "work", aliases=aliases)
    assert full.links_scanned_at == full.computed_at
    assert out.read_bytes() == first


def test_a_link_free_belief_runs_out(tmp_path: Path) -> None:
    """A rank that skips a source records 0 links for it again, so a wrong belief would confirm itself
    at every rank: once every source's links were last read 28 days ago, they are all read."""
    readers, stats_path, at = _lanes_library(tmp_path)
    current = _inputs(readers)
    day = 86400
    assert link_free_sources(stats_path, current, now=at + (LINK_SCAN_MAX_AGE_DAYS - 0.1) * day)
    assert link_free_sources(stats_path, current, now=at + LINK_SCAN_MAX_AGE_DAYS * day) == frozenset()
    assert link_free_sources(stats_path, current, now=at - 60) == frozenset()  # a clock set back
    # Three weekly ranks skip, the fourth reads everything: the time is carried through each skip.
    out = tmp_path / RANK_FILE
    for week in (1, 2, 3):
        free = link_free_sources(stats_path, current, now=at + week * 7 * day)
        assert free == {"lane", "named"}
        compute_static_rank(readers, out, tmp_path / "work", link_free=free)
    assert link_free_sources(stats_path, current, now=at + 28 * day) == frozenset()
    # ...and by the calendar: a rank that skipped is due once the links were last all read 28 days
    # ago, with no source changed (a rank that read them all is kept however old).
    assert not rank_due(stats_path, current, now=at + 27.9 * day).due
    late = rank_due(stats_path, current, now=at + 28 * day)
    assert late.due
    assert late.reason == "the links of every source were last read 28.0 days ago"
    assert rank_due(stats_path, current, now=at - 60).due  # a clock set back
    # A rank that does not say when the links were last all read (before 2026-11-11), or says nonsense.
    recorded = json.loads(stats_path.read_text())
    for broken in ("absent", 0, -5.0, None, "yesterday", True, float("nan")):
        if broken == "absent":
            recorded.pop("links_scanned_at")
        else:
            recorded["links_scanned_at"] = broken
        stats_path.write_text(json.dumps(recorded))
        assert link_free_sources(stats_path, current, now=at + 60) == frozenset(), broken
    # A skip decided from such a record leaves the time unknown: the rank is due, and reads them all.
    compute_static_rank(readers, out, tmp_path / "work", link_free={"lane"})
    assert json.loads(stats_path.read_text())["links_scanned_at"] == 0.0
    unknown = rank_due(stats_path, current, now=at + 60)
    assert unknown.due
    assert "at an unknown time" in unknown.reason
    full = compute_static_rank(readers, out, tmp_path / "work")
    assert not rank_due(stats_path, current, now=full.computed_at + 400 * day).due


def test_a_changed_generation_is_scanned_unless_its_seals_say_zero(tmp_path: Path) -> None:
    readers, stats_path, at = _lanes_library(tmp_path)
    current = _inputs(readers)
    now = at + 60

    def free(**changed: RankInput) -> frozenset[str]:
        return link_free_sources(stats_path, {**current, **changed}, now=now)

    assert free() == {"lane", "named"}  # "w" has links: always read
    # A new generation whose seal counts 0 links: skipped. One that counts some, or does not count: read.
    (lane2,) = write(tmp_path / "ref", [doc("lane", "9", "Report 9")], gen="g2")
    new = _inputs([lane2])["lane"]
    assert (new.links, new.unknown) == (0, ())
    assert "lane" in free(lane=new)
    assert "lane" not in free(lane=dataclasses.replace(new, links=2))
    assert "lane" not in free(lane=dataclasses.replace(new, unknown=("g2",)))
    # The linking source under a new seal that says 0: skipped (the seal counts every row written).
    assert "w" in free(w=dataclasses.replace(current["w"], seal="another", links=0))
    assert "w" not in free(w=dataclasses.replace(current["w"], seal="another"))
    # A source the kept rank never saw: by its seals alone.
    (fresh,) = write(tmp_path / "ref", [doc("fresh", "1", "Fresh")])
    assert "fresh" in free(fresh=_inputs([fresh])["fresh"])
    assert "fresh" not in free(fresh=dataclasses.replace(_inputs([fresh])["fresh"], unknown=("g1",)))
    # The kept record is not a count of 0: nothing is concluded from it.
    recorded = json.loads(stats_path.read_text())
    uncounted = dataclasses.replace(current["lane"], unknown=("g1",))
    assert "lane" in free(lane=uncounted)  # the kept rank read these very files and found none
    recorded = json.loads(stats_path.read_text())
    for broken in (None, False, "0", 3):
        recorded["inputs"]["lane"]["links"] = broken
        stats_path.write_text(json.dumps(recorded))
        assert "lane" not in free(lane=uncounted), broken
        # The kept rank read these very files and did not record 0: its word over the seal's 0.
        assert "lane" not in free(), broken


def _uncounted(member: Path) -> None:
    """A member as sealed before 2026-11-05: its seal does not count its links."""
    seal = member / SUCCESS_FILE
    meta = json.loads(seal.read_text())
    del meta["library_links"]
    seal.write_text(json.dumps(meta))


def _set_input(root: Path, sid: str, set_id: str) -> RankInput:
    """As Indexer._rank_inputs builds it for a set generation."""
    gen_dir = generation_dir(root, sid, set_id)
    members = set_members(gen_dir) or ()
    counts = [(m, library_links(part_dir(root, sid, m))) for m in members]
    return RankInput(
        set_id,
        seal_stamp(gen_dir),
        links=sum(n for _, n in counts if n is not None),
        aliases=False,
        unknown=tuple(m for m, n in counts if n is None),
        members=members,
    )


def test_a_member_that_left_a_set_has_its_links_read_again(tmp_path: Path) -> None:
    """Review 2026-11-11: the scan skips rows a later member shadows. When that member leaves, the rows
    it hid are live again, links and all, and the kept rank never read them: "the rank read these
    members in a source that brought none" says nothing about them."""
    root = tmp_path / "ref"
    with RefineryWriter(root, "s", "2026.a", directory=part_dir(root, "s", "2026.a")) as w:
        w.write([doc("s", "page", "Page", links=("wsp:s/other",)), doc("s", "other", "Other")])
    with RefineryWriter(root, "s", "2026.b", directory=part_dir(root, "s", "2026.b")) as w:
        w.write([doc("s", "page", "Page")])  # the newer row of the same document: no link
    a, b = (part_dir(root, "s", g) for g in ("2026.a", "2026.b"))
    _uncounted(a)
    _uncounted(b)
    both = compose_set(root, "s", [a, b])
    out = tmp_path / RANK_FILE
    stats = compute_static_rank(
        [("s", RefineryReader(generation_dir(root, "s", both)))], out, tmp_path / "work"
    )
    stats_path = out.with_name("static_rank.json")
    assert json.loads(stats_path.read_text())["inputs"]["s"]["links"] == 0  # the linking row is shadowed
    now = stats.computed_at + 60
    assert link_free_sources(stats_path, {"s": _set_input(root, "s", both)}, now=now) == {"s"}
    # A third member joins: what was hidden stays hidden, the set is still link-free.
    with RefineryWriter(root, "s", "2026.c", directory=part_dir(root, "s", "2026.c")) as w:
        w.write([doc("s", "third", "Third")])
    three = compose_set(root, "s", [a, b, part_dir(root, "s", "2026.c")])
    assert link_free_sources(stats_path, {"s": _set_input(root, "s", three)}, now=now) == {"s"}
    # 2026.b leaves: the linking row of 2026.a is live again.
    alone = compose_set(root, "s", [a])
    current = {"s": _set_input(root, "s", alone)}
    assert current["s"].unknown == ("2026.a",)
    free = link_free_sources(stats_path, current, now=now)
    assert free == frozenset()
    # The rank is due at once, not at the weekly refresh: the graph has an edge it never had.
    assert not rank_due(stats_path, {"s": _set_input(root, "s", three)}, now=now).due
    lost = rank_due(stats_path, current, now=now)
    assert lost.due
    assert "lost a member" in lost.reason
    reader = RefineryReader(generation_dir(root, "s", alone))
    again = compute_static_rank([("s", reader)], out, tmp_path / "work", link_free=free)
    assert json.loads(stats_path.read_text())["inputs"]["s"]["links"] == 1
    assert again.edges == 1


def test_no_kept_rank_skips_nothing(tmp_path: Path) -> None:
    readers, stats_path, at = _lanes_library(tmp_path)
    current = _inputs(readers)
    assert link_free_sources(stats_path, current, now=at + 60)
    recorded = stats_path.read_text()
    for text in ("", "[]", "{not json", json.dumps({**json.loads(recorded), "format": 3})):
        stats_path.write_text(text)
        assert link_free_sources(stats_path, current, now=at + 60) == frozenset(), text[:20]
    no_inputs = json.loads(recorded)
    no_inputs["inputs"] = ["lane"]
    stats_path.write_text(json.dumps(no_inputs))
    assert link_free_sources(stats_path, current, now=at + 60) == frozenset()
    stats_path.unlink()
    assert link_free_sources(stats_path, current, now=at + 60) == frozenset()


def test_a_source_reached_only_through_an_alias_is_named(tmp_path: Path) -> None:
    """The edge exists only because of an alias row of a source without links: the link that uses the
    alias names that source, so its change (the alias may be gone) makes the rank due."""
    docs = [doc("w", "en/A", "A", links=("wsp:lane/First_report",)), doc("lane", "1", "Report 1")]
    readers = write(tmp_path / "ref", docs)
    out = tmp_path / RANK_FILE
    stats = compute_static_rank(readers, out, tmp_path / "work", aliases=[("wsp:lane/1", "First report")])
    assert (stats.edges, stats.resolved_alias) == (1, 1)
    assert stats.link_targets == {"lane": 1}
    current = _inputs(readers)
    now = stats.computed_at + 60
    assert not rank_due(out.with_name("static_rank.json"), current, now=now).due
    (lane2,) = write(tmp_path / "ref", [doc("lane", "1", "Report 1")], gen="g2")  # without the alias row
    changed = rank_due(out.with_name("static_rank.json"), {**current, **_inputs([lane2])}, now=now)
    assert changed.due
    assert changed.reason == "lane changed and 1 link(s) name it"
