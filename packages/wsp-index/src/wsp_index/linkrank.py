"""Link-analysis static rank: PageRank over the library's inter-document link graph (PLAN §9.3).

Built offline at index time (``wsp index rank``, also the last step of ``wsp index build``) from the
Refinery's ``DocRecord.links``, and stored as one static value per document in a sidecar file that
the lexical index, the fusion stage, suggest and the portal read.

Two PageRanks come out of one pass over the edges:

* **per source**: the graph restricted to links inside each source, with teleportation and dangling
  mass kept inside that source, so every source's ranks sum to 1 (a Wikipedia article is compared
  with Wikipedia articles, an RFC with RFCs);
* **global**: the whole cross-source graph (a Wikipedia article cited from RFCs and books gains).

Both are turned into a relative score (``pr * N``: 1 is an average page) and blended into one
feature, ``(1 - mix) * log1p(rel_source) + mix * log1p(rel_global)``. A page nobody links to scores
about 0.14, an average page 0.69, the most linked pages 8-10.

Scale (English Wikipedia: ~7M nodes, ~150M edges) is handled with bounded memory:

1. node ids are read column-wise from Parquet into one Arrow string array (~35 bytes per node);
2. link targets are resolved to node numbers in C++ with ``pyarrow.compute.index_in`` (exact doc
   id first, then a normalized key that also covers redirects/aliases), batch by batch, and appended
   to an on-disk edge list (two int32 files, deduplicated per batch, self-links dropped);
3. power iteration streams the edge list through ``np.memmap`` in fixed-size chunks
   (``np.bincount`` of gathered weights), so peak memory is a few float64 vectors of N plus one
   chunk, never the whole graph. No SciPy is needed.

Sidecar format (``<index>/lexical/static_rank.bin``, little endian, replaced atomically)::

    b"WSPRANK1" | u64 n | u64 hashes[n] (sorted blake2b-64 of doc ids) | f32 feature[n]
    | f32 pr_source[n] | f32 pr_global[n]

plus ``static_rank.json`` with the build statistics. Lookups hash the doc id and binary-search;
a 64-bit hash collision (p ≈ 1e-6 at 7M documents) can only swap two documents' priors.
"""

from __future__ import annotations

import json
import math
import os
import re
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, cast

import numpy as np
import numpy.typing as npt
import pyarrow as pa
import pyarrow.compute as pc

from wsp_index.refinery import RefineryReader, id_hash

RANK_FILE = "static_rank.bin"
STATS_FILE = "static_rank.json"
MAGIC = b"WSPRANK1"
_HEADER = len(MAGIC) + 8
_NAMESPACE = re.compile(r"^[A-Za-z]/")
EDGE_FILES = ("intra.src", "intra.dst", "cross.src", "cross.dst")

F64 = npt.NDArray[np.float64]
F32 = npt.NDArray[np.float32]
I32 = npt.NDArray[np.int32]


@dataclass(frozen=True, slots=True)
class RankConfig:
    damping: float = 0.85
    max_iterations: int = 50
    tolerance: float = 1e-6
    """Stop when the L1 change of both rank vectors drops below this (ranks sum to 1)."""
    mix: float = 0.5
    """Weight of the global (cross-source) rank in the blended feature; the rest is per source."""
    edge_chunk: int = 8_000_000
    """Edges per streamed chunk during power iteration (temporaries ~130 MB)."""
    batch_size: int = 8192
    resolve_aliases: bool = True
    """Second lookup pass with normalized keys (case, ``_`` vs space, ``#fragment``, redirects)."""


@dataclass(frozen=True, slots=True)
class RankStats:
    nodes: int
    edges: int
    intra_edges: int
    unresolved_links: int
    iterations: int
    residual_global: float
    residual_source: float
    seconds_graph: float
    seconds_iterate: float
    sources: Mapping[str, int] = field(default_factory=dict[str, int])
    damping: float = 0.85
    mix: float = 0.5

    def line(self) -> str:
        return (
            f"{self.nodes:,} nodes, {self.edges:,} edges ({self.intra_edges:,} within a source, "
            f"{self.unresolved_links:,} links outside the library), {self.iterations} iterations, "
            f"graph {self.seconds_graph:.1f}s, iterate {self.seconds_iterate:.1f}s"
        )


@dataclass(frozen=True, slots=True)
class LinkGraph:
    """Nodes in scan order plus an on-disk edge list under ``workdir``.

    Edges inside one source go to ``intra.src``/``intra.dst``, edges between sources to
    ``cross.src``/``cross.dst`` (int32 node numbers), so the per-source rank streams only the
    first pair and the global rank streams both, without re-testing sources per edge.
    """

    doc_ids: pa.Array[Any]
    node_source: npt.NDArray[np.int16]
    sources: tuple[str, ...]
    workdir: Path
    edges: int
    intra_edges: int
    unresolved: int
    seconds: float

    @property
    def n(self) -> int:
        return len(self.doc_ids)

    def edge_arrays(self, kind: str) -> tuple[I32, I32]:
        """``(src, dst)`` memory maps of the ``intra`` or ``cross`` edge list."""
        m = self.intra_edges if kind == "intra" else self.edges - self.intra_edges
        if m == 0:
            empty = np.zeros(0, dtype=np.int32)
            return empty, empty
        src = np.memmap(self.workdir / f"{kind}.src", dtype=np.int32, mode="r", shape=(m,))
        dst = np.memmap(self.workdir / f"{kind}.dst", dtype=np.int32, mode="r", shape=(m,))
        return cast(I32, src), cast(I32, dst)

    def remove_edges(self) -> None:
        for name in EDGE_FILES:
            (self.workdir / name).unlink(missing_ok=True)


def _norm(arr: pa.Array[Any]) -> pa.Array[Any]:
    """Loose key: no ``#fragment``, ``_`` as space, lower case (``wsp:src/A/Alan_Turing#Life``)."""
    s = pc.replace_substring_regex(arr, pattern="#.*$", replacement="")  # pyright: ignore[reportUnknownMemberType]
    s = pc.replace_substring(s, pattern="_", replacement=" ")  # pyright: ignore[reportUnknownMemberType]
    return pc.utf8_lower(s)  # pyright: ignore[reportUnknownMemberType]


def _as_array(chunks: Sequence[pa.Array[Any]], typ: pa.DataType) -> pa.Array[Any]:
    if not chunks:
        return pa.array([], type=typ)
    return pa.concat_arrays(list(chunks))  # pyright: ignore[reportUnknownMemberType]


def _positions(values: pa.Array[Any], value_set: pa.Array[Any]) -> npt.NDArray[np.int64]:
    """Index of each value in ``value_set`` (first occurrence), -1 when absent."""
    pos = pc.index_in(values, value_set=value_set)  # pyright: ignore[reportUnknownMemberType]
    filled = cast("pa.Array[Any]", pc.fill_null(pos, pa.scalar(-1, pa.int32())))  # pyright: ignore[reportUnknownMemberType]
    return filled.to_numpy(zero_copy_only=False).astype(np.int64)


def _batched[T](items: Iterable[T], size: int) -> Iterable[list[T]]:
    chunk: list[T] = []
    for item in items:
        chunk.append(item)
        if len(chunk) >= size:
            yield chunk
            chunk = []
    if chunk:
        yield chunk


def _iter_strings(arr: pa.Array[Any], step: int = 100_000) -> Iterable[str]:
    """The strings of an Arrow array, materialized ``step`` at a time (bounded Python heap)."""
    for i in range(0, len(arr), step):
        yield from cast(list[str], arr.slice(i, step).to_pylist())


def alias_doc_id(target_doc_id: str, alias: str) -> str:
    """The doc id a link to ``alias`` (a redirect title) would use: same source and namespace."""
    head, _, native = target_doc_id.partition("/")
    ns = m.group(0) if (m := _NAMESPACE.match(native)) else ""
    return f"{head}/{ns}{alias.strip().replace(' ', '_')}"


def build_graph(
    readers: Sequence[tuple[str, RefineryReader]],
    workdir: Path,
    *,
    aliases: Iterable[tuple[str, str]] = (),
    config: RankConfig = RankConfig(),  # noqa: B008 - immutable
    progress: Callable[[str], None] | None = None,
) -> LinkGraph:
    """Resolve every ``wsp:`` link of every document to a node and write the edge list.

    ``readers`` are ``(source_id, reader)`` of the current generation of each source; ``aliases`` are
    ``(target doc id, alias title)`` pairs (redirects), used to resolve links to alternative names.
    """
    t0 = time.monotonic()
    workdir.mkdir(parents=True, exist_ok=True)
    sources = tuple(s for s, _ in readers)
    id_chunks: list[pa.Array[Any]] = []
    src_of: list[npt.NDArray[np.int16]] = []
    for si, (_, reader) in enumerate(readers):
        for batch in reader.scan(["doc_id"], batch_size=config.batch_size):
            col = cast("pa.Array[Any]", batch.column(0))
            id_chunks.append(col)
            src_of.append(np.full(len(col), si, dtype=np.int16))
    doc_ids = _as_array(id_chunks, pa.string())
    node_source = np.concatenate(src_of) if src_of else np.zeros(0, dtype=np.int16)
    n = len(doc_ids)
    if n >= 2**31 - 1:
        raise ValueError(f"too many documents for int32 node ids: {n}")
    if progress:
        progress(f"rank: {n:,} nodes in {len(sources)} source(s)")

    alias_keys: pa.Array[Any] | None = None
    alias_idx: I32 | None = None
    if config.resolve_aliases:
        keys = [_norm(doc_ids)]
        idx = [np.arange(n, dtype=np.int32)]
        for chunk in _batched(aliases, 200_000):
            targets = pa.array([t for t, _ in chunk], type=pa.string())
            pos = _positions(targets, doc_ids)
            found = pos >= 0
            names = pa.array([alias_doc_id(t, a) for t, a in chunk], type=pa.string())
            keys.append(_norm(cast("pa.Array[Any]", names.filter(pa.array(found)))))
            idx.append(pos[found].astype(np.int32))
        alias_keys = _as_array(keys, pa.string())
        alias_idx = np.concatenate(idx)

    edges = intra = unresolved = 0
    base = 0
    files = {name: (workdir / name).open("wb") for name in EDGE_FILES}
    try:
        for _, reader in readers:
            for batch in reader.scan(["links"], batch_size=config.batch_size):
                links = cast("pa.ListArray[Any]", batch.column(0))
                rows = len(links)
                parents = cast(
                    "npt.NDArray[np.int64]",
                    pc.list_parent_indices(links).to_numpy(zero_copy_only=False),  # pyright: ignore[reportUnknownMemberType]
                )
                flat = cast("pa.Array[Any]", pc.list_flatten(links))  # pyright: ignore[reportUnknownMemberType]
                if len(flat):
                    local = cast("pa.Array[Any]", pc.starts_with(flat, pattern="wsp:"))  # pyright: ignore[reportUnknownMemberType]
                    keep = local.to_numpy(zero_copy_only=False).astype(bool)
                    flat = flat.filter(local)
                    parents = parents[keep]
                    tgt = _positions(flat, doc_ids)
                    missing = tgt < 0
                    if alias_keys is not None and alias_idx is not None and missing.any():
                        rest = flat.filter(pa.array(missing))
                        p2 = _positions(_norm(rest), alias_keys)
                        hit = p2 >= 0
                        resolved = np.full(len(p2), -1, dtype=np.int64)
                        resolved[hit] = alias_idx[p2[hit]]
                        tgt[missing] = resolved
                    ok = tgt >= 0
                    unresolved += int((~ok).sum())
                    s = parents[ok] + base
                    d = tgt[ok]
                    loops = s != d
                    s, d = s[loops], d[loops]
                    if len(s):
                        pair = np.unique((s.astype(np.int64) << 32) | d.astype(np.int64))
                        s32 = (pair >> 32).astype(np.int32)
                        d32 = (pair & 0xFFFFFFFF).astype(np.int32)
                        same = node_source[s32] == node_source[d32]
                        files["intra.src"].write(s32[same].tobytes())
                        files["intra.dst"].write(d32[same].tobytes())
                        files["cross.src"].write(s32[~same].tobytes())
                        files["cross.dst"].write(d32[~same].tobytes())
                        edges += len(s32)
                        intra += int(same.sum())
                base += rows
                if progress and base % (config.batch_size * 128) < config.batch_size:
                    progress(f"rank: {base:,}/{n:,} documents scanned, {edges:,} edges")
    finally:
        for fh in files.values():
            fh.close()
    if base != n:
        raise RuntimeError(f"link scan saw {base} rows but the id scan saw {n}; Refinery changed?")
    return LinkGraph(doc_ids, node_source, sources, workdir, edges, intra, unresolved, time.monotonic() - t0)


def _chunks(pair: tuple[I32, I32], step: int) -> Iterable[tuple[I32, I32]]:
    src, dst = pair
    for i in range(0, len(src), step):
        yield np.asarray(src[i : i + step]), np.asarray(dst[i : i + step])


def pagerank(graph: LinkGraph, config: RankConfig = RankConfig()) -> tuple[F64, F64, int, float, float]:  # noqa: B008
    """``(per_source, global, iterations, residual_source, residual_global)`` by power iteration.

    Per iteration the intra-source edges are streamed once (feeding both ranks) and the cross-source
    edges once (global rank only). With a single source the two ranks are identical and only the
    global one is computed.
    """
    n = graph.n
    if n == 0:
        z = np.zeros(0, dtype=np.float64)
        return z, z, 0, 0.0, 0.0
    d = config.damping
    step = max(1, config.edge_chunk)
    single = len(graph.sources) <= 1
    intra, cross = graph.edge_arrays("intra"), graph.edge_arrays("cross")
    groups = graph.node_source.astype(np.int64)
    n_src = np.bincount(groups, minlength=len(graph.sources)).astype(np.float64)
    size_of = n_src[groups]

    out_s = np.zeros(n, dtype=np.float64)
    for s, _ in _chunks(intra, step):
        out_s += np.bincount(s, minlength=n)
    out_g = out_s.copy()
    for s, _ in _chunks(cross, step):
        out_g += np.bincount(s, minlength=n)
    dangling_g, dangling_s = out_g == 0, out_s == 0
    inv_g = np.divide(1.0, out_g, out=np.zeros(n), where=~dangling_g)
    inv_s = np.divide(1.0, out_s, out=np.zeros(n), where=~dangling_s)
    del out_g, out_s

    r_g = np.full(n, 1.0 / n)
    r_s = r_g if single else 1.0 / size_of
    res_g = res_s = math.inf
    it = 0
    while it < config.max_iterations and max(res_g, res_s) > config.tolerance:
        it += 1
        w_g = r_g * inv_g
        w_s = None if single else r_s * inv_s
        acc_g = np.zeros(n)
        acc_s = None if single else np.zeros(n)
        for s, t in _chunks(intra, step):
            acc_g += np.bincount(t, weights=w_g[s], minlength=n)
            if acc_s is not None and w_s is not None:
                acc_s += np.bincount(t, weights=w_s[s], minlength=n)
        for s, t in _chunks(cross, step):
            acc_g += np.bincount(t, weights=w_g[s], minlength=n)
        leak_g = float(r_g[dangling_g].sum())
        new_g = d * acc_g + (d * leak_g + (1.0 - d)) / n
        res_g = float(np.abs(new_g - r_g).sum())
        r_g = new_g
        if acc_s is None:
            r_s, res_s = r_g, res_g
        else:
            leak_s = np.bincount(groups, weights=np.where(dangling_s, r_s, 0.0), minlength=len(n_src))
            new_s = d * acc_s + (d * leak_s[groups] + (1.0 - d)) / size_of
            res_s = float(np.abs(new_s - r_s).sum()) / max(1, len(graph.sources))
            r_s = new_s
    return r_s, r_g, it, res_s, res_g


def blend(per_source: F64, global_: F64, node_source: npt.NDArray[np.int16], mix: float) -> F32:
    """The static-rank feature: ``(1 - mix) * log1p(pr_s * N_s) + mix * log1p(pr_g * N)``."""
    n = len(global_)
    if n == 0:
        return np.zeros(0, dtype=np.float32)
    groups = node_source.astype(np.int64)
    size_of = np.bincount(groups).astype(np.float64)[groups]
    feature = (1.0 - mix) * np.log1p(per_source * size_of) + mix * np.log1p(global_ * n)
    return feature.astype(np.float32)


# --- sidecar -------------------------------------------------------------------------------------


def write_rank_file(path: Path, doc_ids: Iterable[str], feature: F32, per_source: F64, global_: F64) -> int:
    """Write the sidecar atomically (``.partial`` + rename); returns the number of documents."""
    hashes = np.fromiter((id_hash(d) for d in doc_ids), dtype=np.uint64, count=len(feature))
    order = np.argsort(hashes, kind="stable")
    n = len(hashes)
    tmp = path.with_name(path.name + ".partial")
    with tmp.open("wb") as fh:
        fh.write(MAGIC)
        fh.write(np.uint64(n).tobytes())
        fh.write(hashes[order].tobytes())
        fh.write(feature[order].astype(np.float32).tobytes())
        fh.write(per_source[order].astype(np.float32).tobytes())
        fh.write(global_[order].astype(np.float32).tobytes())
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    return n


class StaticRank:
    """Read side of the sidecar: memory-mapped, binary search by doc-id hash."""

    def __init__(self, path: Path) -> None:
        self.path = path
        raw = np.memmap(path, dtype=np.uint8, mode="r")
        if bytes(raw[: len(MAGIC)]) != MAGIC:
            raise ValueError(f"not a static rank file: {path}")
        n = int(np.frombuffer(raw[len(MAGIC) : _HEADER], dtype=np.uint64)[0])
        expected = _HEADER + n * (8 + 4 + 4 + 4)
        if len(raw) != expected:
            raise ValueError(f"truncated static rank file: {path} ({len(raw)} bytes, want {expected})")
        off = _HEADER
        self._hashes: npt.NDArray[np.uint64] = raw[off : off + 8 * n].view(np.uint64)
        off += 8 * n
        self._feature: F32 = raw[off : off + 4 * n].view(np.float32)
        off += 4 * n
        self._per_source: F32 = raw[off : off + 4 * n].view(np.float32)
        off += 4 * n
        self._global: F32 = raw[off : off + 4 * n].view(np.float32)
        self.size = n

    @classmethod
    def open(cls, path: Path) -> StaticRank | None:
        """The sidecar at ``path``, or None if it does not exist (or is unreadable)."""
        try:
            return cls(path) if path.is_file() else None
        except (OSError, ValueError):
            return None

    def __len__(self) -> int:
        return self.size

    def _rows(self, doc_ids: Sequence[str]) -> tuple[npt.NDArray[np.intp], npt.NDArray[np.bool_]]:
        h = np.fromiter((id_hash(d) for d in doc_ids), dtype=np.uint64, count=len(doc_ids))
        pos = np.searchsorted(self._hashes, h)
        pos = np.minimum(pos, max(0, self.size - 1))
        found = (self._hashes[pos] == h) if self.size else np.zeros(len(h), dtype=bool)
        return pos, found

    def features(self, doc_ids: Sequence[str]) -> F32:
        """Blended feature per doc id (0.0 for documents without a rank)."""
        if not doc_ids or self.size == 0:
            return np.zeros(len(doc_ids), dtype=np.float32)
        pos, found = self._rows(doc_ids)
        return np.where(found, self._feature[pos], 0.0).astype(np.float32)

    def feature(self, doc_id: str) -> float:
        return float(self.features([doc_id])[0])

    def pagerank(self, doc_id: str) -> tuple[float, float] | None:
        """``(per_source, global)`` PageRank of one document, or None."""
        if self.size == 0:
            return None
        pos, found = self._rows([doc_id])
        if not found[0]:
            return None
        return float(self._per_source[pos[0]]), float(self._global[pos[0]])


# --- whole build ---------------------------------------------------------------------------------


def compute_static_rank(
    readers: Sequence[tuple[str, RefineryReader]],
    out_path: Path,
    workdir: Path,
    *,
    aliases: Iterable[tuple[str, str]] = (),
    config: RankConfig = RankConfig(),  # noqa: B008 - immutable
    progress: Callable[[str], None] | None = None,
) -> RankStats:
    """Build the graph, run PageRank, write the sidecar (``out_path``) and its stats next to it."""
    graph = build_graph(readers, workdir, aliases=aliases, config=config, progress=progress)
    t1 = time.monotonic()
    per_source, global_, iters, res_s, res_g = pagerank(graph, config)
    feature = blend(per_source, global_, graph.node_source, config.mix)
    seconds_iterate = time.monotonic() - t1
    out_path.parent.mkdir(parents=True, exist_ok=True)
    write_rank_file(out_path, _iter_strings(graph.doc_ids), feature, per_source, global_)
    counts = np.bincount(graph.node_source.astype(np.int64), minlength=len(graph.sources))
    stats = RankStats(
        nodes=graph.n,
        edges=graph.edges,
        intra_edges=graph.intra_edges,
        unresolved_links=graph.unresolved,
        iterations=iters,
        residual_global=res_g,
        residual_source=res_s,
        seconds_graph=graph.seconds,
        seconds_iterate=seconds_iterate,
        sources={s: int(c) for s, c in zip(graph.sources, counts, strict=True)},
        damping=config.damping,
        mix=config.mix,
    )
    stats_path = out_path.with_name(STATS_FILE)
    tmp = stats_path.with_name(STATS_FILE + ".partial")
    tmp.write_text(json.dumps(asdict(stats), indent=1) + "\n", encoding="utf-8")
    os.replace(tmp, stats_path)
    graph.remove_edges()
    return stats


def top_documents(reader: RefineryReader, rank: StaticRank, k: int) -> list[tuple[str, float]]:
    """The ``k`` best-ranked documents of one generation (scans its doc ids)."""
    best: list[tuple[str, float]] = []
    for batch in reader.scan(["doc_id"], batch_size=65536):
        ids = cast(list[str], batch.column(0).to_pylist())
        f = rank.features(ids)
        take = np.argsort(-f, kind="stable")[:k]
        best.extend((ids[int(i)], float(f[int(i)])) for i in take)
        best.sort(key=lambda t: -t[1])
        del best[k:]
    return best
