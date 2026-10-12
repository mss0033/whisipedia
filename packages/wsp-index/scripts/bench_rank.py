"""Scale benchmarks for the link-analysis static rank (``wsp_index.linkrank``).

    # the whole build on a real Refinery generation, optionally a range of its parts (read-only),
    # with redirects from the refine step's redirects.sqlite:
    systemd-run --user --scope -p MemoryMax=4G nice -n 19 uv run python \\
        packages/wsp-index/scripts/bench_rank.py refinery \\
        --generation ~/.local/share/whisipedia/index/refinery/wikipedia-top10/2026-10-01 --parts 128:336 \\
        --redirects ~/.local/share/whisipedia/index/work/wikipedia-top10/2026-10-01/redirects.sqlite \\
        --out /tmp/rank-bench

    # power iteration alone on a synthetic CSR (41M nodes, 1B edges):
    ... bench_rank.py iterate --nodes 41000000 --edges 1000000000 --groups 10 \\
        --iterations 3 --out /tmp/rank-bench

    # link resolution from a synthetic Refinery:
    ... bench_rank.py graph --docs 300000 --links 20 --out /tmp/rank-bench

Peak RSS is reported from ``getrusage`` (the process's high-water mark). Outputs go to ``--out``,
never to the index root; the Refinery and redirects are only read.
"""

from __future__ import annotations

import argparse
import json
import resource
import shutil
import sqlite3
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import numpy as np
import numpy.typing as npt
import pyarrow as pa
import pyarrow.compute as _pc
import pyarrow.parquet as pq

from wsp_core.model import DocRecord
from wsp_index import RefineryReader, RefineryWriter
from wsp_index.linkrank import (
    LinkGraph,
    RankConfig,
    StaticRank,
    build_graph,
    compute_static_rank,
    graph_file,
    pagerank,
)
from wsp_index.refinery import generation_dir

pc: Any = _pc
pa_: Any = pa  # the array constructors' stubs reject numpy inputs


def rss_mb() -> float:
    """Peak resident set size of this process (MB)."""
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def current_mb() -> float:
    """Current resident set size (MB), from /proc."""
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) / 1024
    except OSError:
        pass
    return 0.0


class PartRange(RefineryReader):
    """A Refinery generation restricted to parts ``[lo, hi)`` (benchmarks on language blocks)."""

    def __init__(self, gen_dir: Path, lo: int, hi: int) -> None:
        super().__init__(gen_dir)
        self.lo, self.hi = lo, min(hi, super().n_parts)
        rows = 0
        for part in range(self.lo, self.hi):
            pf = pq.ParquetFile(self.dir / f"part-{part:05d}.parquet")
            live = self._live(part, pf)  # pyright: ignore[reportPrivateUsage]
            rows += pf.metadata.num_rows if live is None else int(live.sum())
        self._rows = rows

    def __len__(self) -> int:
        return self._rows

    def scan(self, columns: Any = ("doc_id",), *, batch_size: int = 4096) -> Iterator[pa.RecordBatch]:
        for part in range(self.lo, self.hi):
            pf = pq.ParquetFile(self.dir / f"part-{part:05d}.parquet")
            live = self._live(part, pf)  # pyright: ignore[reportPrivateUsage]
            present = self._present(pf, columns)  # pyright: ignore[reportPrivateUsage]
            if live is None:
                yield from pf.iter_batches(batch_size=batch_size, columns=present)  # pyright: ignore[reportUnknownMemberType]
                continue
            table = pf.read(columns=present)  # pyright: ignore[reportUnknownMemberType]
            yield from table.filter(pa.array(live)).to_batches(max_chunksize=batch_size)


def redirect_aliases(path: Path, source_id: str, prefixes: tuple[str, ...]) -> Iterator[tuple[str, str]]:
    """``(target doc id, alias title)`` from a refine step's redirects.sqlite (opened read-only)."""
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        cur = db.execute("SELECT target, alias FROM redirects")
        while rows := cur.fetchmany(200_000):
            for target, alias in rows:
                t = str(target)
                if not prefixes or t.startswith(prefixes):
                    yield f"wsp:{source_id}/{t}", str(alias)
    finally:
        db.close()


def cmd_refinery(args: argparse.Namespace) -> dict[str, object]:
    out: Path = args.out
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    gen: Path = args.generation.expanduser()
    lo, _, hi = str(args.parts or "0:1000000").partition(":")
    reader = PartRange(gen, int(lo), int(hi))
    source_id = gen.parent.name
    prefixes = tuple(p + "/" for p in args.langs.split(",") if p)
    aliases = redirect_aliases(args.redirects.expanduser(), source_id, prefixes) if args.redirects else ()
    log: list[str] = []
    t0 = time.monotonic()
    print(f"reader open: rss {current_mb():.0f} MB, peak {rss_mb():.0f} MB", file=sys.stderr, flush=True)

    def say(msg: str) -> None:
        pool = pa.default_memory_pool().bytes_allocated() / 2**20
        clock = time.monotonic() - t0
        line = f"[{clock:7.1f}s rss {current_mb():6.0f} MB peak {rss_mb():6.0f} arrow {pool:5.0f}] {msg}"
        log.append(line)
        print(line, file=sys.stderr, flush=True)

    cfg = RankConfig(max_iterations=args.iterations)
    stats = compute_static_rank(
        [(source_id, reader)],
        out / "static_rank.bin",
        out / "work",
        aliases=aliases,
        config=cfg,
        progress=say,
    )
    total = time.monotonic() - t0
    rank = StaticRank(out / "static_rank.bin")
    probe = [f"wsp:{source_id}/{t}" for t in args.probe.split(",") if t]
    feats = [float(x) for x in rank.features(probe)] if probe else []
    links = stats.resolved_exact + stats.resolved_loose + stats.resolved_alias + stats.unresolved_links
    return {
        "parts": f"{reader.lo}:{reader.hi}",
        "nodes": stats.nodes,
        "groups": dict(stats.groups),
        "links": links,
        "resolved_exact_pct": round(100 * stats.resolved_exact / max(1, links), 2),
        "resolved_loose_pct": round(100 * stats.resolved_loose / max(1, links), 2),
        "resolved_alias_pct": round(100 * stats.resolved_alias / max(1, links), 2),
        "unresolved_pct": round(100 * stats.unresolved_links / max(1, links), 2),
        "aliases": stats.aliases,
        "edges": stats.edges,
        "intra_edges": stats.intra_edges,
        "iterations": stats.iterations,
        "residual_global": stats.residual_global,
        "residual_group": stats.residual_source,
        "graph_s": round(stats.seconds_graph, 1),
        "iterate_s": round(stats.seconds_iterate, 1),
        "total_s": round(total, 1),
        "peak_rss_mb": round(rss_mb()),
        "probe": dict(zip(probe, feats, strict=True)),
    }


def synthetic_csr(out: Path, nodes: int, edges: int, groups: int, seed: int = 1) -> LinkGraph:
    """A CSR written straight to disk: power-law in-degrees, uniform sources, one kind per group."""
    rng = np.random.default_rng(seed)
    node_group = (np.arange(nodes) * groups // nodes).astype(np.int32)  # contiguous language blocks
    weights = 1.0 / np.arange(1, nodes + 1) ** 0.8
    rng.shuffle(weights)
    indeg = rng.multinomial(edges, weights / weights.sum()).astype(np.int32)
    out_deg = np.zeros(nodes, dtype=np.int32)
    with graph_file(out, "intra").open("wb") as fh:
        step = 1 << 22
        for lo in range(0, nodes, step):
            hi = min(nodes, lo + step)
            m = int(indeg[lo:hi].sum())
            # sources drawn from the destination's own group (all edges intra)
            g = np.repeat(node_group[lo:hi], indeg[lo:hi])
            g_lo = (g.astype(np.int64) * nodes + groups - 1) // groups
            g_hi = ((g.astype(np.int64) + 1) * nodes + groups - 1) // groups
            s = (g_lo + (rng.random(m) * (g_hi - g_lo)).astype(np.int64)).astype(np.int32)
            out_deg += np.bincount(s, minlength=nodes).astype(np.int32)
            fh.write(s.tobytes())
    graph_file(out, "cross").write_bytes(b"")
    zero = np.zeros(nodes, dtype=np.int32)
    for name, values in (
        ("node_group", node_group),
        ("out_intra", out_deg),
        ("out_cross", zero),
        ("indeg_intra", indeg),
        ("indeg_cross", zero),
    ):
        values.tofile(graph_file(out, name))
    return LinkGraph(
        n=nodes,
        sources=("syn",),
        groups=tuple(("syn", f"l{i}") for i in range(groups)),
        group_sizes=tuple(int(c) for c in np.bincount(node_group, minlength=groups)),
        workdir=out,
        edges=edges,
        intra_edges=edges,
        unresolved=0,
        seconds=0.0,
    )


def cmd_iterate(args: argparse.Namespace) -> dict[str, object]:
    out: Path = args.out
    out.mkdir(parents=True, exist_ok=True)
    t0 = time.monotonic()
    graph = synthetic_csr(out, int(args.nodes), int(args.edges), int(args.groups))
    t_gen = time.monotonic() - t0
    rss_after_gen = rss_mb()
    cfg = RankConfig(max_iterations=args.iterations, tolerance=0.0, edge_chunk=args.chunk)
    t1 = time.monotonic()
    r_s, r_g, iters, _, res_g = pagerank(graph, cfg)
    t_it = time.monotonic() - t1
    if not args.keep:
        shutil.rmtree(out, ignore_errors=True)
    return {
        "nodes": args.nodes,
        "edges": graph.edges,
        "groups": args.groups,
        "generate_s": round(t_gen, 1),
        "rss_after_generate_mb": round(rss_after_gen),
        "iterations": iters,
        "iterate_s": round(t_it, 1),
        "per_iteration_s": round(t_it / max(iters, 1), 2),
        "residual_global": res_g,
        "rank_sum": float(r_g.sum(dtype=np.float64)),
        "group_rank_sum": float(r_s.sum(dtype=np.float64)),
        "peak_rss_mb": round(rss_mb()),
    }


def cmd_graph(args: argparse.Namespace) -> dict[str, object]:
    out: Path = args.out
    shutil.rmtree(out, ignore_errors=True)
    rng = np.random.default_rng(2)
    n = int(args.docs)
    n_links = int(args.links)
    t0 = time.monotonic()
    with RefineryWriter(out / "ref", "syn", "g1") as w:
        for start in range(0, n, 10_000):
            batch: list[DocRecord] = []
            for i in range(start, min(n, start + 10_000)):
                targets = np.minimum((n * rng.random(n_links) ** 3).astype(np.int64), n - 1)
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


class SyntheticLibrary:
    """A library generated batch by batch, nothing written: ``docs`` documents ``wsp:syn/<lang>/P<i>``
    in ``langs`` contiguous language blocks, a Poisson number of links each (mean ``links``) to
    power-law targets. Of the links, 8% point outside the library, 4% use another case and a
    ``#fragment`` (resolved by loose id) and 4% a redirect ``Alias_<t>`` (the targets below
    ``redirects`` have one). Duck-types the two members of a reader that the rank build uses."""

    def __init__(self, docs: int, links: float, langs: int, redirects: int, seed: int = 3) -> None:
        self.docs = docs
        self.links = links
        self.langs = [f"l{i}" for i in range(langs)]
        self.redirects = min(redirects, docs)
        self.seed = seed

    def __len__(self) -> int:
        return self.docs

    def _lang(self, nodes: npt.NDArray[np.int64]) -> npt.NDArray[np.int32]:
        return np.minimum(nodes * len(self.langs) // max(1, self.docs), len(self.langs) - 1).astype(np.int32)

    def _ids(
        self, nodes: npt.NDArray[np.int64], variant: npt.NDArray[np.int32] | None = None
    ) -> pa.Array[Any]:
        """``wsp:syn/<lang>/<P|p|Alias_><node>[#x]`` (variant 0, 1 and 2)."""
        var = np.zeros(len(nodes), dtype=np.int32) if variant is None else variant
        heads = [f"wsp:syn/{lang}/{v}" for lang in self.langs for v in ("P", "p", "Alias_")]
        head = pa_.DictionaryArray.from_arrays(pa.array(self._lang(nodes) * 3 + var), pa.array(heads))
        tail = pa_.DictionaryArray.from_arrays(pa.array((var == 1).astype(np.int32)), pa.array(["", "#x"]))
        num = pc.cast(pa.array(nodes), pa.string())
        joined = pc.binary_join_element_wise(head.cast(pa.string()), num, tail.cast(pa.string()), "")
        return cast("pa.Array[Any]", joined)

    def aliases(self) -> Iterator[tuple[str, str]]:
        for t in range(self.redirects):
            yield f"wsp:syn/{self.langs[int(self._lang(np.array([t]))[0])]}/P{t}", f"Alias {t}"

    def scan(self, columns: Any = ("doc_id",), *, batch_size: int = 4096) -> Iterator[pa.RecordBatch]:
        for lo in range(0, self.docs, batch_size):
            nodes = np.arange(lo, min(self.docs, lo + batch_size), dtype=np.int64)
            out: list[pa.Array[Any]] = []
            for name in columns:
                if name == "doc_id":
                    out.append(self._ids(nodes))
                elif name == "native_id":
                    out.append(
                        pc.replace_substring_regex(self._ids(nodes), pattern="^wsp:syn/", replacement="")
                    )
                elif name == "title":
                    out.append(pc.binary_join_element_wise("P", pc.cast(pa.array(nodes), pa.string()), ""))
                elif name == "lang":
                    out.append(
                        pa_.DictionaryArray.from_arrays(
                            pa.array(self._lang(nodes)), pa.array(self.langs)
                        ).cast(pa.string())
                    )
                elif name == "links":
                    out.append(self._links(lo, len(nodes)))
                else:
                    raise KeyError(name)
            yield pa_.RecordBatch.from_arrays(out, names=list(columns))

    def _links(self, lo: int, rows: int) -> pa.Array[Any]:
        rng = np.random.default_rng(self.seed + lo)
        counts = rng.poisson(self.links, rows)
        m = int(counts.sum())
        target = np.minimum((self.docs * rng.random(m) ** 3).astype(np.int64), self.docs - 1)
        kind = rng.random(m)
        variant = np.zeros(m, dtype=np.int32)
        variant[(kind >= 0.08) & (kind < 0.12)] = 1
        alias = (kind >= 0.12) & (kind < 0.16) & (target < self.redirects)
        variant[alias] = 2
        target[kind < 0.08] += self.docs  # outside the library
        offsets = np.zeros(rows + 1, dtype=np.int32)
        np.cumsum(counts, out=offsets[1:])
        return pa_.ListArray.from_arrays(pa.array(offsets), self._ids(target, variant))


def cmd_build(args: argparse.Namespace) -> dict[str, object]:
    """The whole rank build on a :class:`SyntheticLibrary` (with its redirects), scratch in ``--out``."""
    out: Path = args.out
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    lib = SyntheticLibrary(int(args.docs), float(args.links), int(args.langs), int(args.redirects))
    t0 = time.monotonic()
    scratch = 0

    def say(msg: str) -> None:
        nonlocal scratch
        used = sum(p.stat().st_size for p in (out / "work").glob("*") if p.is_file())
        scratch = max(scratch, used)
        clock = time.monotonic() - t0
        line = (
            f"[{clock:7.1f}s rss {current_mb():6.0f} MB peak {rss_mb():6.0f} disk {used / 2**20:6.0f}] {msg}"
        )
        print(line, file=sys.stderr, flush=True)

    cfg = RankConfig(max_iterations=args.iterations)
    stats = compute_static_rank(
        [("syn", cast(RefineryReader, lib))],
        out / "static_rank.bin",
        out / "work",
        aliases=lib.aliases(),
        config=cfg,
        progress=say,
    )
    result: dict[str, object] = {
        "nodes": stats.nodes,
        "edges": stats.edges,
        "intra_edges": stats.intra_edges,
        "resolved": [stats.resolved_exact, stats.resolved_loose, stats.resolved_alias],
        "unresolved": stats.unresolved_links,
        "aliases": stats.aliases,
        "iterations": stats.iterations,
        "graph_s": round(stats.seconds_graph, 1),
        "iterate_s": round(stats.seconds_iterate, 1),
        "total_s": round(time.monotonic() - t0, 1),
        "peak_rss_mb": round(rss_mb()),
        "peak_scratch_mb_at_log_lines": round(scratch / 2**20),
    }
    if not args.keep:
        shutil.rmtree(out, ignore_errors=True)
    return result


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    rf = sub.add_parser("refinery")
    rf.add_argument("--generation", type=Path, required=True)
    rf.add_argument("--parts", default="", help="lo:hi part range (default: all)")
    rf.add_argument("--redirects", type=Path, default=None)
    rf.add_argument("--langs", default="", help="only redirects whose target starts with these prefixes")
    rf.add_argument("--iterations", type=int, default=RankConfig().max_iterations)
    rf.add_argument("--probe", default="", help="comma-separated native ids whose feature to print")
    it = sub.add_parser("iterate")
    it.add_argument("--nodes", type=int, default=7_000_000)
    it.add_argument("--edges", type=int, default=150_000_000)
    it.add_argument("--groups", type=int, default=1)
    it.add_argument("--iterations", type=int, default=4)
    it.add_argument("--chunk", type=int, default=RankConfig().edge_chunk)
    gr = sub.add_parser("graph")
    gr.add_argument("--docs", type=int, default=300_000)
    gr.add_argument("--links", type=int, default=20)
    bd = sub.add_parser("build")
    bd.add_argument("--docs", type=int, default=1_000_000)
    bd.add_argument("--links", type=float, default=3.3)
    bd.add_argument("--langs", type=int, default=4)
    bd.add_argument("--redirects", type=int, default=100_000)
    bd.add_argument("--iterations", type=int, default=RankConfig().max_iterations)
    for p in (rf, it, gr, bd):
        p.add_argument("--out", type=Path, required=True)
        p.add_argument("--keep", action="store_true")
    args = ap.parse_args(argv)
    run = {"refinery": cmd_refinery, "iterate": cmd_iterate, "graph": cmd_graph, "build": cmd_build}[
        cast(str, args.cmd)
    ]
    json.dump(run(args), sys.stdout, indent=1)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
