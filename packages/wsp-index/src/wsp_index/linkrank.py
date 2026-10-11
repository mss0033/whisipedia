"""Link-analysis static rank: PageRank over the library's inter-document link graph (PLAN §9.3).

Built offline at index time (``wsp index rank``, also the last step of ``wsp index build``) from the
Refinery's ``DocRecord.links``, and stored as one static value per document in a sidecar file that
the lexical index, the fusion stage, suggest and the portal read.

Two PageRanks come out of one pass over the edges:

* **per group**: a group is one *(source, language)* pair. The graph restricted to links inside a
  group, with teleportation and dangling mass kept inside it, so every group's ranks sum to 1: a
  German article is compared with German articles, an RFC with RFCs. Mixing languages let six
  million Cebuano bot stubs, which link to each other, outrank English and German articles;
* **global**: the whole cross-source, cross-language graph.

The feature is a blend of *percentiles*, ``(1 - mix) * pct_group + mix * pct_global`` in ``[0, 1)``:
the best page of each group scores near 1 and every unlinked page 0, whatever the group's size, so
the boost a ranker derives from it is capped (``1 + w * feature``) and comparable across languages.

Scale (wikipedia-top10: 32M nodes; target 41M nodes and ~1B edges in about 3 GB):

1. every document id is reduced to two 64-bit keys, computed vectorized from Arrow's string buffers
   (:func:`key64`): one of the exact id, one of the *loose* id (no ``#fragment``, ``_`` as space,
   lower case). Ids are never held as Python or Arrow strings;
2. aliases (redirects) become loose keys of the doc id the redirect would have, *with* the target's
   language/book prefix (``wsp:wikipedia-top10/en/AC_DC``), pointing at the target node;
3. each link is resolved by binary search: exact id, then loose id, then alias. Edges go to
   per-destination-range bucket files, which are then sorted into a CSR by destination;
4. power iteration pulls float32 weights through the CSR in bounded chunks and sums them in float64
   (``np.add.reduceat``), so no node-sized temporary is allocated per chunk.

Sidecar format (``<index>/lexical/static_rank.bin``, little endian, replaced atomically)::

    b"WSPRANK2" | u64 n | u64 key[n] (sorted key64 of doc ids) | f32 feature[n]
    | f32 pr_group[n] | f32 pr_global[n]

``WSPRANK1`` files (blake2b-64 keys, the unbounded log feature) stay readable until the next
``wsp index rank``. A 64-bit key collision (p ≈ 5e-5 at 41M documents) can only swap two documents'
priors or mis-resolve a link.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import json
import math
import mmap
import os
import time
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from functools import cache
from pathlib import Path
from typing import Any, cast

import numpy as np
import numpy.typing as npt
import pyarrow as pa
import pyarrow.compute as _pc

from wsp_index.refinery import RefineryReader, id_hash
from wsp_index.text import primary_lang

pc: Any = _pc  # the compute stubs are too partial for strict checking; results are cast at use

RANK_FILE = "static_rank.bin"
STATS_FILE = "static_rank.json"
MAGIC = b"WSPRANK2"
MAGIC_V1 = b"WSPRANK1"
_HEADER = len(MAGIC) + 8

F64 = npt.NDArray[np.float64]
F32 = npt.NDArray[np.float32]
I32 = npt.NDArray[np.int32]
I64 = npt.NDArray[np.int64]
U64 = npt.NDArray[np.uint64]

# --- 64-bit string keys ---------------------------------------------------------------------------

_BASE = 0x100000001B3  # odd, so invertible modulo 2**64
_BASE_INV = pow(_BASE, -1, 2**64)
_LEN_SALT = np.uint64(0x9E3779B97F4A7C15)
_CHUNK_BYTES = 1 << 22
_powers: dict[str, U64] = {}


def _power_table(name: str, base: int, size: int) -> U64:
    have = _powers.get(name)
    if have is not None and len(have) >= size:
        return have
    size = max(size, _CHUNK_BYTES)
    out = np.empty(size, dtype=np.uint64)
    out[0] = 1
    step = 1
    while step < size:  # doubling: out[step:2*step] = out[:step] * base**step (mod 2**64)
        span = min(step, size - step)
        factor: int = pow(base, step, 1 << 64)
        out[step : step + span] = out[:span] * np.uint64(factor)
        step *= 2
    _powers[name] = out
    return out


def _mix(h: U64) -> U64:
    """splitmix64's finalizer: spreads the polynomial hash's low-entropy high bits."""
    h = h ^ (h >> np.uint64(30))
    h = h * np.uint64(0xBF58476D1CE4E5B9)
    h = h ^ (h >> np.uint64(27))
    h = h * np.uint64(0x94D049BB133111EB)
    return h ^ (h >> np.uint64(31))


def _string_buffers(arr: pa.Array[Any]) -> tuple[I64, npt.NDArray[np.uint8]]:
    if arr.null_count:
        arr = cast("pa.Array[Any]", pc.fill_null(arr, ""))
    large = pa.types.is_large_string(arr.type) or pa.types.is_large_binary(arr.type)
    if not (large or pa.types.is_string(arr.type) or pa.types.is_binary(arr.type)):
        raise TypeError(f"key64 needs strings, got {arr.type}")
    bufs = arr.buffers()
    width = 8 if large else 4
    offsets = np.frombuffer(
        cast(Any, bufs[1]),
        dtype=np.int64 if large else np.int32,
        count=len(arr) + 1,
        offset=arr.offset * width,
    ).astype(np.int64)
    data = np.frombuffer(cast(Any, bufs[2]), dtype=np.uint8) if bufs[2] is not None else np.zeros(0, np.uint8)
    return offsets, data


def key64(values: pa.Array[Any] | pa.ChunkedArray[Any] | Sequence[str]) -> U64:
    """64-bit key of each string's UTF-8 bytes, vectorized over Arrow's buffers.

    ``mix(sum_j (b_j + 1) * B**j  xor  len * salt)`` modulo 2**64. The sum is computed with *global*
    byte positions (one table of powers, one ``reduceat``) and shifted back to each string's start by
    multiplying with the inverse power, so the cost is three passes over the bytes with no
    per-byte row index. Strings are cut into chunks of at most 4 MB of bytes (bounded temporaries).
    """
    if isinstance(values, pa.ChunkedArray):
        parts = [key64(c) for c in values.chunks]
        return np.concatenate(parts) if parts else np.zeros(0, dtype=np.uint64)
    arr = values if isinstance(values, pa.Array) else pa.array(list(values), type=pa.string())
    n = len(arr)
    out = np.zeros(n, dtype=np.uint64)
    if n == 0:
        return out
    offsets, data = _string_buffers(arr)
    lens = np.diff(offsets)
    row = 0
    while row < n:
        # rows [row, end) whose bytes fit one chunk (at least one row)
        limit = offsets[row] + _CHUNK_BYTES
        cut = int(np.searchsorted(offsets, int(limit), side="right"))
        end = max(row + 1, cut - 1)
        end = min(end, n)
        lo, hi = int(offsets[row]), int(offsets[end])
        total = hi - lo
        if total:
            pw = _power_table("fwd", _BASE, total)
            inv = _power_table("inv", _BASE_INV, total)
            terms = (data[lo:hi].astype(np.uint64) + np.uint64(1)) * pw[:total]
            seg_len = lens[row:end]
            nz = seg_len > 0
            starts = (offsets[row:end] - lo)[nz]
            sums = np.add.reduceat(terms, starts)
            h = out[row:end]
            h[nz] = sums * inv[starts]
        row = end
    out ^= lens.astype(np.uint64) * _LEN_SALT
    return _mix(out)


def key64_one(text: str) -> int:
    return int(key64(pa.array([text], type=pa.string()))[0])


def loose_ids(arr: pa.Array[Any]) -> pa.Array[Any]:
    """Loose id: no ``#fragment``, ``_`` as space, lower case (``wsp:src/A/Alan_Turing#Life``)."""
    s = pc.replace_substring_regex(arr, pattern="#.*$", replacement="")
    s = pc.replace_substring(s, pattern="_", replacement=" ")
    return cast("pa.Array[Any]", pc.utf8_lower(s))


class KeyTable:
    """Sorted u64 keys -> int32 node numbers (first node wins among equal keys)."""

    def __init__(self, keys: U64, nodes: I32) -> None:
        order = np.argsort(keys, kind="stable")
        self.keys: U64 = keys[order]
        self.nodes: I32 = nodes[order]
        del order

    @classmethod
    def empty(cls) -> KeyTable:
        return cls(np.zeros(0, dtype=np.uint64), np.zeros(0, dtype=np.int32))

    def __len__(self) -> int:
        return len(self.keys)

    @property
    def nbytes(self) -> int:
        return int(self.keys.nbytes + self.nodes.nbytes)

    def find(self, keys: U64) -> I64:
        """Node of each key, -1 when absent."""
        out = np.full(len(keys), -1, dtype=np.int64)
        if not len(self.keys) or not len(keys):
            return out
        pos = np.searchsorted(self.keys, keys)
        pos = np.minimum(pos, len(self.keys) - 1)
        hit = self.keys[pos] == keys
        out[hit] = self.nodes[pos[hit]]
        return out


# --- configuration and statistics --------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RankConfig:
    damping: float = 0.85
    max_iterations: int = 60
    tolerance: float = 1e-4
    """Stop when the L1 change of both rank vectors drops below this (ranks sum to 1). The error
    shrinks by about ``damping`` per iteration, so 1e-4 needs at most ~57 iterations; 1e-6 was not
    reachable in the old 50."""
    mix: float = 0.5
    """Weight of the global percentile in the feature; the rest is the per-group percentile."""
    edge_chunk: int = 16_000_000
    """Edges gathered per step of the power iteration (temporaries ~200 MB)."""
    batch_size: int = 4_096
    """Documents per Refinery batch while reading ids and links (~150k links of Wikipedia)."""
    bucket_nodes: int = 1 << 18
    """Destination nodes per edge bucket (the CSR is sorted one bucket at a time)."""
    buffer_bytes: int = 64 * 1024 * 1024
    """Edge pairs buffered in memory before they are appended to the bucket files."""
    resolve_aliases: bool = True
    """Resolve links through loose ids (case, ``_`` vs space, ``#fragment``) and redirects."""


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
    groups: Mapping[str, int] = field(default_factory=dict[str, int])
    """Documents per ``source/lang`` group."""
    resolved_exact: int = 0
    resolved_loose: int = 0
    resolved_alias: int = 0
    aliases: int = 0
    damping: float = 0.85
    mix: float = 0.5
    format: int = 2

    def line(self) -> str:
        return (
            f"{self.nodes:,} nodes in {len(self.groups):,} source/language groups, {self.edges:,} edges "
            f"({self.intra_edges:,} within a group, {self.unresolved_links:,} links outside the library), "
            f"{self.iterations} iterations, graph {self.seconds_graph:.1f}s, "
            f"iterate {self.seconds_iterate:.1f}s"
        )


# --- the graph -----------------------------------------------------------------------------------------

_KINDS = ("intra", "cross")


@dataclass(slots=True)
class LinkGraph:
    """Nodes in scan order plus CSR edge lists (by destination) under ``workdir``.

    ``intra`` holds the edges inside one (source, language) group, ``cross`` the rest; per kind,
    ``<kind>.src`` lists the source node of every edge sorted by destination and ``indeg_<kind>``
    counts them per destination. ``id_keys``/``id_nodes`` (exact id key -> node, sorted by key) are
    what the sidecar is written from.
    """

    n: int
    sources: tuple[str, ...]
    groups: tuple[tuple[str, str], ...]
    """``(source_id, primary language)`` of each group."""
    node_group: I32
    out_intra: I32
    out_cross: I32
    indeg_intra: I32
    indeg_cross: I32
    id_keys: U64
    id_nodes: I32
    workdir: Path
    edges: int
    intra_edges: int
    unresolved: int
    seconds: float
    resolved: dict[str, int] = field(default_factory=dict[str, int])
    aliases: int = 0

    @property
    def node_source(self) -> npt.NDArray[np.int16]:
        group_source = np.array([self.sources.index(s) for s, _ in self.groups], dtype=np.int16)
        return group_source[self.node_group] if len(group_source) else np.zeros(0, np.int16)

    def edge_src(self, kind: str) -> I32:
        """Source node of each edge of ``kind``, sorted by destination (memory-mapped, read-only)."""
        m = int(
            self.indeg_intra.sum(dtype=np.int64) if kind == "intra" else self.indeg_cross.sum(dtype=np.int64)
        )
        if m == 0:
            return np.zeros(0, dtype=np.int32)
        return cast(I32, _mmap_array(self.workdir / f"{kind}.src", np.dtype(np.int32), m, random=False))

    def remove_edges(self) -> None:
        for p in self.workdir.glob("*.src"):
            p.unlink(missing_ok=True)
        for p in self.workdir.glob("*.pairs"):
            p.unlink(missing_ok=True)


def _mmap_array(
    path: Path, dtype: np.dtype[Any], count: int, *, offset: int = 0, random: bool
) -> npt.NDArray[Any]:
    """A read-only numpy view of ``count`` items at ``offset`` of ``path`` (MADV_RANDOM when asked)."""
    if count == 0:
        return np.zeros(0, dtype=dtype)
    with path.open("rb") as fh:
        mm = mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ)
    if random and hasattr(mmap, "MADV_RANDOM"):
        mm.madvise(mmap.MADV_RANDOM)
    return np.frombuffer(mm, dtype=dtype, count=count, offset=offset)


def _prefix_mode(reader: RefineryReader) -> bool:
    """True when the source's native ids are ``<prefix>/<title>`` (a language or book prefix).

    Read from the first row group: a native id equal to its title (``_`` as space) means no prefix,
    one whose rest after the first ``/`` equals the title means a prefix (``en/AC/DC``, ``A/Foo``).
    """
    for batch in reader.scan(["native_id", "title"], batch_size=256):
        natives = cast(list[str], batch.column(0).to_pylist())
        titles = cast(list[str], batch.column(1).to_pylist())
        plain = prefixed = 0
        for native, title in zip(natives, titles, strict=True):
            t = title.replace(" ", "_")
            if native == t:
                plain += 1
            elif native.partition("/")[2] == t:
                prefixed += 1
        return prefixed > plain
    return False


def alias_doc_id(target_doc_id: str, alias: str, *, prefixed: bool) -> str:
    """The doc id a link to ``alias`` (a redirect title) would use: the target's source and prefix.

    ``prefixed``: the source's native ids carry a language/book prefix (:func:`_prefix_mode`), so
    ``wsp:wikipedia-top10/en/AC/DC`` + ``AC DC`` -> ``wsp:wikipedia-top10/en/AC_DC``.
    """
    head, _, native = target_doc_id.partition("/")
    prefix = native.partition("/")[0] + "/" if prefixed and "/" in native else ""
    return f"{head}/{prefix}{alias.strip().replace(' ', '_')}"


def _alias_ids(targets: pa.Array[Any], names: pa.Array[Any], prefixed: pa.Array[Any]) -> pa.Array[Any]:
    """Vectorized :func:`alias_doc_id` (``prefixed`` per row)."""
    with_prefix = pc.replace_substring_regex(targets, pattern=r"^(wsp:[^/]*/[^/]*/).*$", replacement=r"\1")
    bare = pc.replace_substring_regex(targets, pattern=r"^(wsp:[^/]*/).*$", replacement=r"\1")
    # a prefixed source's target without a second "/" has no prefix to keep
    has_two = pc.match_substring_regex(targets, pattern=r"^wsp:[^/]*/[^/]*/")
    head = pc.if_else(pc.and_(prefixed, has_two), with_prefix, bare)
    tail = pc.replace_substring(pc.utf8_trim_whitespace(names), pattern=" ", replacement="_")
    return cast("pa.Array[Any]", pc.binary_join_element_wise(head, tail, ""))


class _Buckets:
    """Edge pairs bucketed by destination range, buffered and appended to ``<kind>.<b>.pairs``."""

    def __init__(self, workdir: Path, n_buckets: int, shift: int, limit: int) -> None:
        self.workdir = workdir
        self.shift = shift
        self.limit = limit
        self.pending: dict[tuple[str, int], list[npt.NDArray[np.int32]]] = {}
        self.bytes = 0
        self.n_buckets = n_buckets

    def path(self, kind: str, b: int) -> Path:
        return self.workdir / f"{kind}.{b:05d}.pairs"

    def add(self, kind: str, s: I32, d: I32) -> None:
        if not len(s):
            return
        b = d >> self.shift
        order = np.argsort(b, kind="stable")
        s, d, b = s[order], d[order], b[order]
        cuts = np.flatnonzero(np.diff(b)) + 1
        for ss, dd in zip(np.split(s, cuts), np.split(d, cuts), strict=True):
            pair = np.empty(2 * len(ss), dtype=np.int32)
            pair[0::2], pair[1::2] = ss, dd
            self.pending.setdefault((kind, int(dd[0]) >> self.shift), []).append(pair)
            self.bytes += pair.nbytes
        if self.bytes >= self.limit:
            self.flush()

    def flush(self) -> None:
        for (kind, b), chunks in self.pending.items():
            with self.path(kind, b).open("ab") as fh:
                for c in chunks:
                    fh.write(c.tobytes())
        self.pending.clear()
        self.bytes = 0


def _groups_of(
    langs: pa.Array[Any], source: int, index: dict[tuple[int, str], int], groups: list[tuple[int, str]]
) -> I32:
    uniq = cast(list[str | None], pc.unique(langs).to_pylist())
    table = np.empty(len(uniq), dtype=np.int32)
    for i, raw in enumerate(uniq):
        key = (source, primary_lang(raw or ""))
        g = index.get(key)
        if g is None:
            g = index[key] = len(groups)
            groups.append(key)
        table[i] = g
    pos = cast("pa.Array[Any]", pc.index_in(langs, value_set=pa.array(uniq, type=langs.type)))
    return table[pos.to_numpy(zero_copy_only=False).astype(np.int64)]


def build_graph(
    readers: Sequence[tuple[str, RefineryReader]],
    workdir: Path,
    *,
    aliases: Iterable[tuple[str, str]] = (),
    config: RankConfig = RankConfig(),  # noqa: B008 - immutable
    progress: Callable[[str], None] | None = None,
) -> LinkGraph:
    """Resolve every ``wsp:`` link of every document to a node and write the CSR edge lists.

    ``readers`` are ``(source_id, reader)`` of the current generation of each source; ``aliases`` are
    ``(target doc id, alias title)`` pairs (redirects), used to resolve links to alternative names.
    """
    t0 = time.monotonic()
    workdir.mkdir(parents=True, exist_ok=True)
    for stale in (*workdir.glob("*.pairs"), *workdir.glob("*.src")):
        stale.unlink()
    sources = tuple(s for s, _ in readers)
    n = sum(len(r) for _, r in readers)
    if n >= 2**31 - 1:
        raise ValueError(f"too many documents for int32 node ids: {n}")

    def say(msg: str) -> None:
        if progress is not None:
            progress(msg)

    _release()
    say(f"rank: {n:,} nodes in {len(sources)} source(s)")

    # 1. node keys and groups ------------------------------------------------------------------
    exact = np.empty(n, dtype=np.uint64)
    loose = np.empty(n, dtype=np.uint64) if config.resolve_aliases else np.zeros(0, dtype=np.uint64)
    node_group = np.empty(n, dtype=np.int32)
    group_index: dict[tuple[int, str], int] = {}
    group_list: list[tuple[int, str]] = []
    prefixed: list[str] = []
    base = 0
    for si, (sid, reader) in enumerate(readers):
        if _prefix_mode(reader):
            prefixed.append(sid)
        for batch in reader.scan(["doc_id", "lang"], batch_size=config.batch_size):
            ids = cast("pa.Array[Any]", batch.column(0))
            k = len(ids)
            if base + k > n:
                raise RuntimeError("the Refinery changed while rank was reading it")
            exact[base : base + k] = key64(ids)
            if config.resolve_aliases:
                loose[base : base + k] = key64(loose_ids(ids))
            node_group[base : base + k] = _groups_of(
                cast("pa.Array[Any]", batch.column(1)), si, group_index, group_list
            )
            base += k
    if base != n:
        raise RuntimeError(f"id scan saw {base} rows, the readers report {n}; Refinery changed?")
    by_id = KeyTable(exact, np.arange(n, dtype=np.int32))
    del exact
    by_loose = KeyTable(loose, np.arange(n, dtype=np.int32)) if config.resolve_aliases else KeyTable.empty()
    del loose
    say(
        f"rank: {len(group_list):,} source/language group(s); "
        f"key tables {(by_id.nbytes + by_loose.nbytes) / 2**20:,.0f} MiB"
    )

    # 2. aliases -------------------------------------------------------------------------------
    _release()
    by_alias = KeyTable.empty()
    n_alias = 0
    if config.resolve_aliases:
        prefixed_set = pa.array(prefixed, type=pa.string())
        a_keys: list[U64] = []
        a_nodes: list[I32] = []
        for chunk in _batched(aliases, 500_000):
            targets = pa.array([t for t, _ in chunk], type=pa.string())
            names = pa.array([a for _, a in chunk], type=pa.string())
            node = by_id.find(key64(targets))
            ok = node >= 0
            if not ok.any():
                continue
            mask = pa.array(ok)
            targets, names = targets.filter(mask), names.filter(mask)
            src = pc.replace_substring_regex(targets, pattern=r"^wsp:([^/]*)/.*$", replacement=r"\1")
            is_prefixed = pc.is_in(src, value_set=prefixed_set)
            a_keys.append(key64(loose_ids(_alias_ids(targets, names, is_prefixed))))
            a_nodes.append(node[ok].astype(np.int32))
            n_alias += int(ok.sum())
        if a_keys:
            by_alias = KeyTable(np.concatenate(a_keys), np.concatenate(a_nodes))
        del a_keys, a_nodes
        say(f"rank: {n_alias:,} aliases resolved to documents")

    # 3. links -> bucketed edges ---------------------------------------------------------------
    shift = max(0, math.ceil(math.log2(max(1, config.bucket_nodes))))
    n_buckets = max(1, (n + (1 << shift) - 1) >> shift)
    buckets = _Buckets(workdir, n_buckets, shift, config.buffer_bytes)
    out_intra = np.zeros(n, dtype=np.int32)
    out_cross = np.zeros(n, dtype=np.int32)
    resolved = {"exact": 0, "loose": 0, "alias": 0}
    edges = intra = unresolved = batches = 0
    base = 0
    for _sid, reader in readers:
        for batch in reader.scan(["links"], batch_size=config.batch_size):
            links = cast("pa.Array[Any]", batch.column(0))
            rows = len(links)
            flat = cast("pa.Array[Any]", pc.list_flatten(links))
            if len(flat):
                parents = cast(I64, pc.list_parent_indices(links).to_numpy(zero_copy_only=False)).astype(
                    np.int64
                )
                local = cast("pa.Array[Any]", pc.starts_with(flat, pattern="wsp:"))
                keep = local.to_numpy(zero_copy_only=False).astype(bool)
                flat = flat.filter(local)
                parents = parents[keep]
                tgt = by_id.find(key64(flat))
                resolved["exact"] += int((tgt >= 0).sum())
                missing = tgt < 0
                if config.resolve_aliases and missing.any():
                    rest_keys = key64(loose_ids(flat.filter(pa.array(missing))))
                    found = by_loose.find(rest_keys)
                    resolved["loose"] += int((found >= 0).sum())
                    still = found < 0
                    if still.any() and len(by_alias):
                        via = by_alias.find(rest_keys[still])
                        resolved["alias"] += int((via >= 0).sum())
                        found[still] = via
                    tgt[missing] = found
                ok = tgt >= 0
                unresolved += int((~ok).sum())
                s = parents[ok] + base
                d = tgt[ok]
                loops = s != d
                s, d = s[loops], d[loops]
                if len(s):
                    pair = np.unique((s << 32) | d)
                    s32 = (pair >> 32).astype(np.int32)
                    d32 = (pair & 0xFFFFFFFF).astype(np.int32)
                    same = node_group[s32] == node_group[d32]
                    buckets.add("intra", s32[same], d32[same])
                    buckets.add("cross", s32[~same], d32[~same])
                    local_s = s32 - base
                    out_intra[base : base + rows] += np.bincount(local_s[same], minlength=rows).astype(
                        np.int32
                    )
                    out_cross[base : base + rows] += np.bincount(local_s[~same], minlength=rows).astype(
                        np.int32
                    )
                    edges += len(s32)
                    intra += int(same.sum())
            base += rows
            batches += 1
            if batches % 16 == 0:
                _release()
            if base % (config.batch_size * 64) < rows:
                say(f"rank: {base:,}/{n:,} documents scanned, {edges:,} edges")
    buckets.flush()
    if base != n:
        raise RuntimeError(f"link scan saw {base} rows but the id scan saw {n}; Refinery changed?")
    del by_loose, by_alias
    _release()
    say(f"rank: {edges:,} edges ({intra:,} within a group), {unresolved:,} links outside the library")

    # The id table is only needed again for the sidecar: keep it on disk from here on.
    id_keys = _spill(workdir / "ids.keys.src", by_id.keys)
    id_nodes = _spill(workdir / "ids.nodes.src", by_id.nodes)
    del by_id

    # 4. CSR by destination --------------------------------------------------------------------
    indeg = {kind: np.zeros(n, dtype=np.int32) for kind in _KINDS}
    for kind in _KINDS:
        with (workdir / f"{kind}.src").open("wb") as out:
            for b in range(n_buckets):
                path = buckets.path(kind, b)
                if not path.is_file():
                    continue
                pairs = np.fromfile(path, dtype=np.int32)
                path.unlink()
                s, d = pairs[0::2], pairs[1::2]
                order = np.argsort(d, kind="stable")
                out.write(s[order].tobytes())
                lo = b << shift
                hi = min(n, lo + (1 << shift))
                indeg[kind][lo:hi] = np.bincount(d - lo, minlength=hi - lo).astype(np.int32)
                del pairs, s, d, order
    say("rank: edge lists sorted by destination")
    groups = tuple((sources[si], lang) for si, lang in group_list)
    return LinkGraph(
        n=n,
        sources=sources,
        groups=groups,
        node_group=node_group,
        out_intra=out_intra,
        out_cross=out_cross,
        indeg_intra=indeg["intra"],
        indeg_cross=indeg["cross"],
        id_keys=cast(U64, id_keys),
        id_nodes=cast(I32, id_nodes),
        workdir=workdir,
        edges=edges,
        intra_edges=intra,
        unresolved=unresolved,
        seconds=time.monotonic() - t0,
        resolved=resolved,
        aliases=n_alias,
    )


def _spill(path: Path, arr: npt.NDArray[Any]) -> npt.NDArray[Any]:
    """Write ``arr`` to ``path`` and return a read-only memory map of it (frees the RAM copy)."""
    arr.tofile(path)
    return _mmap_array(path, arr.dtype, len(arr), random=False)


@cache
def _malloc_trim() -> Callable[[int], int] | None:
    try:
        return cast(
            Callable[[int], int], ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6").malloc_trim
        )
    except (OSError, AttributeError):
        return None


def _release() -> None:
    """Hand freed pages back to the OS. Arrow's mimalloc pool and glibc's heap both keep them
    otherwise: opening the 32M-row wikipedia-top10 id index leaves 0.7 GB retained, and a link scan
    grows RSS by gigabytes of numpy and Arrow temporaries that are already free."""
    pa.default_memory_pool().release_unused()
    trim = _malloc_trim()
    if trim is not None:
        trim(0)


def _batched[T](items: Iterable[T], size: int) -> Iterator[list[T]]:
    chunk: list[T] = []
    for item in items:
        chunk.append(item)
        if len(chunk) >= size:
            yield chunk
            chunk = []
    if chunk:
        yield chunk


# --- power iteration ----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Segment:
    lo: int
    hi: int
    edge_lo: int
    edges: int


def _segments(indeg: I32, chunk: int, window: int = 1 << 20) -> list[_Segment]:
    """Node ranges whose in-edges fit ``chunk`` (a single node may exceed it)."""
    out: list[_Segment] = []
    n = len(indeg)
    lo = e = 0
    while lo < n:
        hi = min(n, lo + window)
        c = np.cumsum(indeg[lo:hi], dtype=np.int64)
        if c[-1] > chunk:
            hi = lo + max(1, int(np.searchsorted(c, chunk, side="right")))
            c = c[: hi - lo]
        m = int(c[-1])
        out.append(_Segment(lo, hi, e, m))
        e += m
        lo = hi
    return out


class _EdgeFile:
    """Chunked reads of a ``<kind>.src`` file with ``pread``: unlike a memory map, the pages read do
    not stay in the process's resident set (2.5 GB at 632M edges), only in the reclaimable cache."""

    def __init__(self, path: Path, edges: int) -> None:
        self.path = path
        self.edges = edges

    def read(self, lo: int, n: int) -> I32:
        with self.path.open("rb") as fh:
            return np.fromfile(fh, dtype=np.int32, count=n, offset=4 * lo)


def _pull(src: _EdgeFile, indeg: I32, segments: Sequence[_Segment], w: F32, acc: F32) -> None:
    """``acc[t] += sum(w[s] for s -> t)`` over one CSR, chunk by chunk (sums in float64)."""
    for seg in segments:
        if not seg.edges:
            continue
        deg = indeg[seg.lo : seg.hi]
        nz = np.flatnonzero(deg)
        starts = (np.cumsum(deg, dtype=np.int64) - deg)[nz]
        vals = w[src.read(seg.edge_lo, seg.edges)]
        sums = np.add.reduceat(vals, starts, dtype=np.float64)
        acc[seg.lo + nz] += sums.astype(np.float32)


def _inverse(counts: I32) -> F32:
    out = np.zeros(len(counts), dtype=np.float32)
    np.divide(1.0, counts, out=out, where=counts > 0)
    return out


def pagerank(graph: LinkGraph, config: RankConfig = RankConfig()) -> tuple[F32, F32, int, float, float]:  # noqa: B008
    """``(per_group, global, iterations, residual_group, residual_global)`` by power iteration.

    Vectors are float32 (8 of them: ~1.3 GB at 41M nodes); in-link sums and residuals are float64.
    With a single group the two ranks are identical and only the global one is computed.
    """
    n = graph.n
    if n == 0:
        z = np.zeros(0, dtype=np.float32)
        return z, z, 0, 0.0, 0.0
    d = np.float32(config.damping)
    single = len(graph.groups) <= 1
    indeg_sum = {
        "intra": int(graph.indeg_intra.sum(dtype=np.int64)),
        "cross": int(graph.indeg_cross.sum(dtype=np.int64)),
    }
    src = {kind: _EdgeFile(graph.workdir / f"{kind}.src", indeg_sum[kind]) for kind in _KINDS}
    indeg = {"intra": graph.indeg_intra, "cross": graph.indeg_cross}
    segs = {kind: _segments(indeg[kind], max(1, config.edge_chunk)) for kind in _KINDS}
    groups = graph.node_group
    sizes = np.bincount(groups, minlength=len(graph.groups)).astype(np.float64)

    out_g = graph.out_intra.astype(np.int64) + graph.out_cross
    inv_g = _inverse(out_g.astype(np.int32))
    dangling_g = np.flatnonzero(out_g == 0)
    del out_g
    inv_s = _inverse(graph.out_intra)
    dangling_s = np.flatnonzero(graph.out_intra == 0)

    r_g = np.full(n, 1.0 / n, dtype=np.float32)
    r_s = r_g if single else (1.0 / sizes[groups]).astype(np.float32)
    acc_g = np.empty(n, dtype=np.float32)
    acc_s = None if single else np.empty(n, dtype=np.float32)
    res_g = res_s = math.inf
    it = 0
    while it < config.max_iterations and max(res_g, res_s) > config.tolerance:
        it += 1
        w = r_g * inv_g
        acc_g.fill(0.0)
        _pull(src["intra"], indeg["intra"], segs["intra"], w, acc_g)
        _pull(src["cross"], indeg["cross"], segs["cross"], w, acc_g)
        leak_g = float(r_g[dangling_g].sum(dtype=np.float64))
        acc_g *= d
        acc_g += np.float32((float(d) * leak_g + (1.0 - float(d))) / n)
        res_g = float(np.abs(acc_g - r_g).sum(dtype=np.float64))
        r_g, acc_g = acc_g, r_g
        if acc_s is None:
            r_s, res_s = r_g, res_g
            continue
        w = r_s * inv_s
        acc_s.fill(0.0)
        _pull(src["intra"], indeg["intra"], segs["intra"], w, acc_s)
        leak_s = np.bincount(groups[dangling_s], weights=r_s[dangling_s], minlength=len(sizes))
        tele = ((float(d) * leak_s + (1.0 - float(d))) / np.maximum(sizes, 1.0)).astype(np.float32)
        acc_s *= d
        acc_s += tele[groups]
        res_s = float(np.abs(acc_s - r_s).sum(dtype=np.float64)) / max(1, len(sizes))
        r_s, acc_s = acc_s, r_s
    return r_s, r_g, it, res_s, res_g


def percentiles(values: F32, groups: I32 | None = None) -> F32:
    """Rank of each value among its group as a fraction in ``[0, 1)`` (ties share the lowest)."""
    out = np.zeros(len(values), dtype=np.float32)
    if groups is None:
        sorted_v = np.sort(values)
        out[:] = np.searchsorted(sorted_v, values, side="left") / max(1, len(values) - 1)
        return out
    order = np.argsort(groups, kind="stable")
    cuts = np.flatnonzero(np.diff(groups[order])) + 1
    for idx in np.split(order, cuts):
        if not len(idx):
            continue
        v = values[idx]
        out[idx] = np.searchsorted(np.sort(v), v, side="left") / max(1, len(v) - 1)
    return out


def blend(per_group: F32, global_: F32, node_group: I32, mix: float) -> F32:
    """The static-rank feature: ``(1 - mix) * pct_group + mix * pct_global``, in ``[0, 1]``."""
    if not len(global_):
        return np.zeros(0, dtype=np.float32)
    feature = (1.0 - mix) * percentiles(per_group, node_group) + mix * percentiles(global_)
    return feature.astype(np.float32)


# --- sidecar ------------------------------------------------------------------------------------------


def write_rank_file(path: Path, keys: U64, nodes: I32, feature: F32, per_group: F32, global_: F32) -> int:
    """Write the sidecar atomically from key-sorted ``keys`` and their ``nodes``; returns its size."""
    n = len(keys)
    tmp = path.with_name(path.name + ".partial")
    step = 1 << 22
    with tmp.open("wb") as fh:
        fh.write(MAGIC)
        fh.write(np.uint64(n).tobytes())
        fh.write(np.ascontiguousarray(keys, dtype=np.uint64).tobytes())
        for column in (feature, per_group, global_):
            for i in range(0, n, step):
                fh.write(column[nodes[i : i + step]].astype(np.float32).tobytes())
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    return n


class StaticRank:
    """Read side of the sidecar: memory-mapped (``MADV_RANDOM``), binary search by doc-id key."""

    def __init__(self, path: Path) -> None:
        self.path = path
        with path.open("rb") as fh:
            head = fh.read(_HEADER)
            size = os.fstat(fh.fileno()).st_size
        if len(head) < _HEADER or head[: len(MAGIC)] not in (MAGIC, MAGIC_V1):
            raise ValueError(f"not a static rank file: {path}")
        self.version = 2 if head[: len(MAGIC)] == MAGIC else 1
        n = int(np.frombuffer(head[len(MAGIC) :], dtype=np.uint64)[0])
        expected = _HEADER + n * (8 + 4 + 4 + 4)
        if size != expected:
            raise ValueError(f"truncated static rank file: {path} ({size} bytes, want {expected})")
        off = _HEADER
        self._keys = cast(U64, _mmap_array(path, np.dtype(np.uint64), n, offset=off, random=True))
        off += 8 * n
        self._feature = cast(F32, _mmap_array(path, np.dtype(np.float32), n, offset=off, random=True))
        off += 4 * n
        self._per_group = cast(F32, _mmap_array(path, np.dtype(np.float32), n, offset=off, random=True))
        off += 4 * n
        self._global = cast(F32, _mmap_array(path, np.dtype(np.float32), n, offset=off, random=True))
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

    def _key(self, doc_ids: Sequence[str]) -> U64:
        if self.version == 1:
            return np.fromiter((id_hash(d) for d in doc_ids), dtype=np.uint64, count=len(doc_ids))
        return key64(pa.array(list(doc_ids), type=pa.string()))

    def _rows(self, doc_ids: Sequence[str]) -> tuple[npt.NDArray[np.intp], npt.NDArray[np.bool_]]:
        h = self._key(doc_ids)
        pos = np.searchsorted(self._keys, h)
        pos = np.minimum(pos, max(0, self.size - 1))
        found = (self._keys[pos] == h) if self.size else np.zeros(len(h), dtype=bool)
        return pos, found

    def features(self, doc_ids: Sequence[str]) -> F32:
        """Feature per doc id in ``[0, 1]`` (0.0 for documents without a rank).

        A version-1 file's unbounded ``log1p`` feature is squashed to ``[0, 1)`` (``x / (1 + x)``)
        so a ranker's capped boost holds until the next ``wsp index rank``."""
        if not doc_ids or self.size == 0:
            return np.zeros(len(doc_ids), dtype=np.float32)
        pos, found = self._rows(doc_ids)
        raw = np.where(found, self._feature[pos], 0.0).astype(np.float32)
        return (raw / (1.0 + raw)).astype(np.float32) if self.version == 1 else raw

    def feature(self, doc_id: str) -> float:
        return float(self.features([doc_id])[0])

    def pagerank(self, doc_id: str) -> tuple[float, float] | None:
        """``(per_group, global)`` PageRank of one document, or None."""
        found = self.pageranks([doc_id])
        return None if found is None or math.isnan(found[0][0]) else (float(found[0][0]), float(found[1][0]))

    def pageranks(self, doc_ids: Sequence[str]) -> tuple[F32, F32] | None:
        """``(per_group, global)`` PageRank arrays (NaN for unknown documents); None when empty."""
        if self.size == 0:
            return None
        pos, found = self._rows(doc_ids)
        per = np.where(found, self._per_group[pos], np.nan).astype(np.float32)
        glob = np.where(found, self._global[pos], np.nan).astype(np.float32)
        return per, glob


# --- whole build --------------------------------------------------------------------------------------


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
    try:
        t1 = time.monotonic()
        per_group, global_, iters, res_s, res_g = pagerank(graph, config)
        if progress:
            progress(f"rank: {iters} iterations, residual {res_g:.2e} (global) / {res_s:.2e} (per group)")
        feature = blend(per_group, global_, graph.node_group, config.mix)
        seconds_iterate = time.monotonic() - t1
        out_path.parent.mkdir(parents=True, exist_ok=True)
        write_rank_file(out_path, graph.id_keys, graph.id_nodes, feature, per_group, global_)
    finally:
        graph.remove_edges()
    counts = np.bincount(graph.node_group, minlength=len(graph.groups))
    by_source: dict[str, int] = {}
    for (sid, _), c in zip(graph.groups, counts, strict=True):
        by_source[sid] = by_source.get(sid, 0) + int(c)
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
        sources=by_source,
        groups={f"{s}/{lang}": int(c) for (s, lang), c in zip(graph.groups, counts, strict=True)},
        resolved_exact=graph.resolved.get("exact", 0),
        resolved_loose=graph.resolved.get("loose", 0),
        resolved_alias=graph.resolved.get("alias", 0),
        aliases=graph.aliases,
        damping=config.damping,
        mix=config.mix,
    )
    stats_path = out_path.with_name(STATS_FILE)
    tmp = stats_path.with_name(STATS_FILE + ".partial")
    tmp.write_text(json.dumps(asdict(stats), indent=1) + "\n", encoding="utf-8")
    os.replace(tmp, stats_path)
    return stats


def top_documents(reader: RefineryReader, rank: StaticRank, k: int) -> list[tuple[str, float]]:
    """The ``k`` best-ranked documents of one generation by feature (scans its doc ids)."""
    best: list[tuple[str, float]] = []
    for batch in reader.scan(["doc_id"], batch_size=65536):
        ids = cast(list[str], batch.column(0).to_pylist())
        f = rank.features(ids)
        take = np.argsort(-f, kind="stable")[:k]
        best.extend((ids[int(i)], float(f[int(i)])) for i in take)
        best.sort(key=lambda t: -t[1])
        del best[k:]
    return best
