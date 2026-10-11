"""Scale benchmark for the link-analysis static rank (``wsp_index.linkrank``) on synthetic graphs.

    # power iteration at English-Wikipedia scale (7M nodes, 150M edges), a few iterations timed:
    WSP_MAX_WORKERS=4 OMP_NUM_THREADS=4 nice -n 19 ionice -c 3 uv run python \
        packages/wsp-index/scripts/bench_rank.py iterate --nodes 7000000 --edges 150000000 \
        --iterations 4 --out /tmp/rank-bench

    # link resolution from Refinery Parquet (Arrow index_in) on a smaller library:
    ... bench_rank.py graph --docs 300000 --links 20 --out /tmp/rank-bench

The edge list for ``iterate`` is written straight to disk (no Parquet), with in-degrees following a
power law like real link graphs. Peak RSS is reported from ``getrusage``. Run it on a scratch
directory, never the real index root.
"""

from __future__ import annotations

import argparse
import json
import resource
import shutil
import sys
import time
from pathlib import Path

import numpy as np
import pyarrow as pa

from wsp_core.model import DocRecord
from wsp_index import RefineryReader, RefineryWriter
from wsp_index.linkrank import LinkGraph, RankConfig, build_graph, pagerank
from wsp_index.refinery import generation_dir


def rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def synthetic_edges(out: Path, nodes: int, edges: int, seed: int = 1, chunk: int = 10_000_000) -> int:
    """Uniform sources, power-law targets (a few hubs get most links); self-loops dropped."""
    rng = np.random.default_rng(seed)
    written = 0
    with (out / "cross.src").open("wb") as fs, (out / "cross.dst").open("wb") as fd:
        left = edges
        while left > 0:
            m = min(chunk, left)
            s = rng.integers(0, nodes, size=m, dtype=np.int64)
            d = np.minimum((nodes * rng.random(m) ** 3).astype(np.int64), nodes - 1)
            keep = s != d
            fs.write(s[keep].astype(np.int32).tobytes())
            fd.write(d[keep].astype(np.int32).tobytes())
            written += int(keep.sum())
            left -= m
    return written


def cmd_iterate(args: argparse.Namespace) -> dict[str, object]:
    out: Path = args.out
    out.mkdir(parents=True, exist_ok=True)
    t0 = time.monotonic()
    m = synthetic_edges(out, args.nodes, args.edges)
    t_gen = time.monotonic() - t0
    n_sources = args.sources
    node_source = (np.arange(args.nodes) % n_sources).astype(np.int16)
    graph = LinkGraph(
        doc_ids=pa.nulls(args.nodes, type=pa.string()),  # pagerank only needs the node count
        node_source=node_source,
        sources=tuple(f"s{i}" for i in range(n_sources)),
        workdir=out,
        edges=m,
        intra_edges=0,
        unresolved=0,
        seconds=t_gen,
    )
    cfg = RankConfig(max_iterations=args.iterations, tolerance=0.0, edge_chunk=args.chunk)
    t1 = time.monotonic()
    _, r_g, iters, _, res_g = pagerank(graph, cfg)
    t_it = time.monotonic() - t1
    if not args.keep:
        shutil.rmtree(out, ignore_errors=True)
    return {
        "nodes": args.nodes,
        "edges": m,
        "generate_s": round(t_gen, 1),
        "iterations": iters,
        "iterate_s": round(t_it, 1),
        "per_iteration_s": round(t_it / max(iters, 1), 2),
        "residual_global": res_g,
        "rank_sum": float(r_g.sum()),
        "peak_rss_mb": round(rss_mb()),
    }


def cmd_graph(args: argparse.Namespace) -> dict[str, object]:
    out: Path = args.out
    shutil.rmtree(out, ignore_errors=True)
    rng = np.random.default_rng(2)
    n = args.docs
    t0 = time.monotonic()
    with RefineryWriter(out / "ref", "syn", "g1") as w:
        for start in range(0, n, 10_000):
            batch: list[DocRecord] = []
            for i in range(start, min(n, start + 10_000)):
                targets = np.minimum((n * rng.random(args.links) ** 3).astype(np.int64), n - 1)
                links = tuple(f"wsp:syn/P{int(t):08d}" for t in targets)
                batch.append(
                    DocRecord(
                        doc_id=f"wsp:syn/P{i:08d}",  # pyright: ignore[reportArgumentType]
                        source_id="syn",
                        native_id=f"P{i:08d}",
                        title=f"P{i}",
                        lang="en",
                        license="CC0-1.0",
                        text="x",
                        links=links,
                    )
                )
            w.write(batch)
    t_write = time.monotonic() - t0
    reader = RefineryReader(generation_dir(out / "ref", "syn", "g1"))
    t1 = time.monotonic()
    graph = build_graph([("syn", reader)], out / "work")
    t_graph = time.monotonic() - t1
    t2 = time.monotonic()
    _, _, iters, _, res = pagerank(graph)
    t_it = time.monotonic() - t2
    if not args.keep:
        shutil.rmtree(out, ignore_errors=True)
    return {
        "docs": n,
        "links_per_doc": args.links,
        "edges": graph.edges,
        "write_refinery_s": round(t_write, 1),
        "build_graph_s": round(t_graph, 1),
        "links_per_s": round(n * args.links / max(t_graph, 1e-9)),
        "iterations": iters,
        "iterate_s": round(t_it, 2),
        "residual": res,
        "peak_rss_mb": round(rss_mb()),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    it = sub.add_parser("iterate")
    it.add_argument("--nodes", type=int, default=7_000_000)
    it.add_argument("--edges", type=int, default=150_000_000)
    it.add_argument("--sources", type=int, default=1)
    it.add_argument("--iterations", type=int, default=4)
    it.add_argument("--chunk", type=int, default=RankConfig().edge_chunk)
    gr = sub.add_parser("graph")
    gr.add_argument("--docs", type=int, default=300_000)
    gr.add_argument("--links", type=int, default=20)
    for p in (it, gr):
        p.add_argument("--out", type=Path, required=True)
        p.add_argument("--keep", action="store_true")
    args = ap.parse_args(argv)
    result = cmd_iterate(args) if args.cmd == "iterate" else cmd_graph(args)
    json.dump(result, sys.stdout, indent=1)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
