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

Scale (live library: 256M nodes, 641M edges; memory about 8 bytes a node plus bounded chunks):

1. every document id is reduced to two 64-bit keys, computed vectorized from Arrow's string buffers
   (:func:`key64`): one of the exact id, one of the *loose* id (no ``#fragment``, ``_`` as space,
   lower case). Ids are never held as Python or Arrow strings;
2. aliases (redirects) become loose keys of the doc id the redirect would have, *with* the target's
   language/book prefix (``wsp:wikipedia-top10/en/AC_DC``), pointing at the target node;
3. node keys, aliases and links are spilled to *key partitions* on disk, cut by the top bits of the
   key (a hash, so the partitions are even, ``node_chunk / 2`` nodes each). Each partition of links is
   joined against the same partition of node keys, sorted in memory: exact id first; the misses move
   to the loose-key partitions and are joined against the loose ids, then the aliases. No table of
   all keys is ever held in memory;
4. resolved edges go to per-destination-range bucket files, each sorted and de-duplicated on its own
   into a CSR by destination; out-degrees are counted from the CSR afterwards;
5. every per-node array (groups, degrees, ranks, the sorted id table) is a file in the work directory,
   read and written in slices. Power iteration holds only the weight vectors ``rank / out-degree``
   (gathered at random by the edges, 4 bytes a node each), pulls them through the CSR in bounded
   chunks and sums in float64 (``np.add.reduceat``).

Scratch on disk peaks at about 20 bytes a library link plus 24 bytes a node.

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
import resource
import time
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import ExitStack
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

_NODE_CHUNK = 1 << 22


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
    node_chunk: int = _NODE_CHUNK
    """Nodes per slice of a per-node file, and link records per step of the link resolution (~0.4 GB
    of temporaries); key partitions hold about half as many nodes."""
    batch_size: int = 4_096
    """Documents per Refinery batch while reading ids and links (~150k links of Wikipedia)."""
    bucket_nodes: int = 1 << 18
    """Destination nodes per edge bucket (the CSR is sorted one bucket at a time)."""
    buffer_bytes: int = 64 * 1024 * 1024
    """Records buffered in memory before they are appended to the partition and bucket files."""
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


# --- files in the work directory -----------------------------------------------------------------------

_KINDS = ("intra", "cross")

_GRAPH_FILES = {
    "node_group": "node_group.i32",
    "out_intra": "out_intra.i32",
    "out_cross": "out_cross.i32",
    "indeg_intra": "indeg_intra.i32",
    "indeg_cross": "indeg_cross.i32",
    "id_keys": "ids.keys",
    "id_nodes": "ids.nodes",
    "intra": "intra.src",
    "cross": "cross.src",
    "rank_group": "rank_group.f32",
    "rank_global": "rank_global.f32",
}


def graph_file(workdir: Path, name: str) -> Path:
    """The file of one of a :class:`LinkGraph`'s arrays (``node_group``, ``out_intra``, ``intra`` ...)."""
    return workdir / _GRAPH_FILES[name]


def _clear(workdir: Path) -> None:
    """Remove every scratch file of a graph (and the ``*.pairs`` buckets of older versions)."""
    for name in _GRAPH_FILES.values():
        (workdir / name).unlink(missing_ok=True)
    for pattern in ("*.part", "*.pairs"):
        for path in workdir.glob(pattern):
            path.unlink(missing_ok=True)


type _Read = Callable[[int, int], npt.NDArray[Any]]
"""``read(lo, hi)``: rows ``[lo, hi)`` of a per-node array, in memory or in a file."""


def _steps(n: int, step: int) -> Iterator[tuple[int, int]]:
    for lo in range(0, n, step):
        yield lo, min(n, lo + step)


class _Vec:
    """A per-node vector in a file, read and written in slices with plain file I/O: unlike a memory
    map, the pages read do not stay in the process's resident set, only in the reclaimable cache."""

    __slots__ = ("dtype", "n", "path")

    def __init__(self, path: Path, dtype: npt.DTypeLike, n: int) -> None:
        self.path = path
        self.dtype = np.dtype(dtype)
        self.n = n

    @classmethod
    def create(cls, path: Path, dtype: npt.DTypeLike, n: int) -> _Vec:
        """A new vector of zeros (a sparse file)."""
        vec = cls(path, dtype, n)
        with path.open("wb") as fh:
            fh.truncate(n * vec.dtype.itemsize)
        return vec

    def read(self, lo: int, hi: int) -> npt.NDArray[Any]:
        out = np.empty(hi - lo, dtype=self.dtype)
        if hi > lo:
            with self.path.open("rb") as fh:
                fh.seek(lo * self.dtype.itemsize)
                got = fh.readinto(memoryview(out).cast("B"))
            if got != out.nbytes:
                raise RuntimeError(f"{self.path}: short read at row {lo} (the work directory changed?)")
        return out

    def write(self, lo: int, values: npt.NDArray[Any]) -> None:
        with self.path.open("r+b") as fh:
            fh.seek(lo * self.dtype.itemsize)
            fh.write(memoryview(np.ascontiguousarray(values, dtype=self.dtype)).cast("B"))

    def load(self) -> npt.NDArray[Any]:
        """The whole vector in memory."""
        return self.read(0, self.n)


class _Spill:
    """Records of one dtype spilled to partition files ``<name>.<p>.part``, buffered in memory up to
    ``limit`` bytes. A partition is read back whole (:meth:`take`) or in chunks (:meth:`chunks`), and
    its file is removed once read."""

    def __init__(self, workdir: Path, name: str, dtype: np.dtype[Any], limit: int) -> None:
        self.workdir = workdir
        self.name = name
        self.dtype = dtype
        self.limit = limit
        self.pending: dict[int, list[npt.NDArray[Any]]] = {}
        self.bytes = 0
        self.count = 0

    def path(self, part: int) -> Path:
        return self.workdir / f"{self.name}.{part:05d}.part"

    def add(self, part: I64, records: npt.NDArray[Any]) -> None:
        if not len(records):
            return
        order = np.argsort(part, kind="stable")
        part, records = part[order], records[order]
        cuts = (np.flatnonzero(np.diff(part)) + 1).tolist()
        for lo, hi in zip([0, *cuts], [*cuts, len(part)], strict=True):
            self.pending.setdefault(int(part[lo]), []).append(records[lo:hi])
        self.bytes += records.nbytes
        self.count += len(records)
        if self.bytes >= self.limit:
            self.flush()

    def flush(self) -> None:
        for part, chunks in self.pending.items():
            with self.path(part).open("ab") as fh:
                for c in chunks:
                    fh.write(c.tobytes())
        self.pending.clear()
        self.bytes = 0

    def size(self, part: int) -> int:
        """Records in partition ``part`` (after :meth:`flush`)."""
        path = self.path(part)
        return path.stat().st_size // self.dtype.itemsize if path.is_file() else 0

    def take(self, part: int) -> npt.NDArray[Any]:
        path = self.path(part)
        if not path.is_file():
            return np.zeros(0, dtype=self.dtype)
        out = np.fromfile(path, dtype=self.dtype)
        path.unlink()
        return out

    def chunks(self, part: int, count: int) -> Iterator[npt.NDArray[Any]]:
        path = self.path(part)
        if not path.is_file():
            return
        with path.open("rb") as fh:
            while data := fh.read(max(1, count) * self.dtype.itemsize):
                yield np.frombuffer(data, dtype=self.dtype)
        path.unlink()


# Spilled records (packed, little endian).
_KEY = np.dtype([("key", "<u8"), ("node", "<i4")])
"""A node's exact or loose id key."""
_LINK = np.dtype([("key", "<u8"), ("loose", "<u8"), ("src", "<i4")])
"""A library link: exact and loose key of its target, its source node."""
_MISS = np.dtype([("key", "<u8"), ("src", "<i4")])
"""A link by one key of its target: the loose key after an exact miss (the exact key without aliases)."""
_ALIAS = np.dtype([("key", "<u8"), ("loose", "<u8"), ("seq", "<i8")])
"""A redirect: exact key of its target, loose key of the alias's doc id, input order (first wins)."""
_ALIAS_NODE = np.dtype([("key", "<u8"), ("node", "<i4"), ("seq", "<i8")])
"""A resolved redirect: loose key of the alias's doc id, its target node, input order."""
_EDGE = np.dtype([("src", "<i4"), ("dst", "<i4")])

_MAX_PART_BITS = 12


def _partition_bits(n: int, chunk: int) -> int:
    """Key partitions (by the top bits of the key) of about ``chunk / 2`` nodes each."""
    want = max(1, -(-2 * n // max(1, chunk)))
    return min(_MAX_PART_BITS, (want - 1).bit_length())


def _part(keys: U64, bits: int) -> I64:
    if bits == 0:
        return np.zeros(len(keys), dtype=np.int64)
    return (keys >> np.uint64(64 - bits)).astype(np.int64)


def _records(dtype: np.dtype[Any], **columns: npt.NDArray[Any]) -> npt.NDArray[Any]:
    out = np.empty(len(next(iter(columns.values()))), dtype=dtype)
    for name, values in columns.items():
        out[name] = values
    return out


def _key_table(records: npt.NDArray[Any]) -> KeyTable:
    """A partition's node keys (in node order, so the first node wins among equal keys)."""
    return KeyTable(records["key"], records["node"])


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


# --- the graph -----------------------------------------------------------------------------------------


@dataclass(slots=True)
class LinkGraph:
    """Nodes in scan order plus CSR edge lists (by destination), all in files under ``workdir``.

    ``intra`` holds the edges inside one (source, language) group, ``cross`` the rest; per kind,
    ``<kind>.src`` lists the source node of every edge sorted by destination and ``indeg_<kind>``
    counts them per destination. ``id_keys``/``id_nodes`` (exact id key -> node, sorted by key) are
    what the sidecar is written from. The array properties map a whole file (read-only), for small
    graphs, tests and benchmarks; the build itself streams the files (:func:`graph_file`).
    """

    n: int
    sources: tuple[str, ...]
    groups: tuple[tuple[str, str], ...]
    """``(source_id, primary language)`` of each group."""
    group_sizes: tuple[int, ...]
    """Documents per group."""
    workdir: Path
    edges: int
    intra_edges: int
    unresolved: int
    seconds: float
    resolved: dict[str, int] = field(default_factory=dict[str, int])
    aliases: int = 0

    def _array(self, name: str, dtype: npt.DTypeLike) -> npt.NDArray[Any]:
        path = graph_file(self.workdir, name)
        dt = np.dtype(dtype)
        count = path.stat().st_size // dt.itemsize if path.is_file() else 0
        return _mmap_array(path, dt, count, random=False)

    def vector(self, name: str, dtype: npt.DTypeLike) -> _Vec:
        return _Vec(graph_file(self.workdir, name), dtype, self.n)

    @property
    def node_group(self) -> I32:
        return cast(I32, self._array("node_group", np.int32))

    @property
    def out_intra(self) -> I32:
        return cast(I32, self._array("out_intra", np.int32))

    @property
    def out_cross(self) -> I32:
        return cast(I32, self._array("out_cross", np.int32))

    @property
    def indeg_intra(self) -> I32:
        return cast(I32, self._array("indeg_intra", np.int32))

    @property
    def indeg_cross(self) -> I32:
        return cast(I32, self._array("indeg_cross", np.int32))

    @property
    def id_keys(self) -> U64:
        return cast(U64, self._array("id_keys", np.uint64))

    @property
    def id_nodes(self) -> I32:
        return cast(I32, self._array("id_nodes", np.int32))

    @property
    def node_source(self) -> npt.NDArray[np.int16]:
        group_source = np.array([self.sources.index(s) for s, _ in self.groups], dtype=np.int16)
        return group_source[self.node_group] if len(group_source) else np.zeros(0, np.int16)

    def edge_src(self, kind: str) -> I32:
        """Source node of each edge of ``kind``, sorted by destination (memory-mapped, read-only)."""
        return cast(I32, self._array(kind, np.int32))

    def remove_edges(self) -> None:
        """Remove every scratch file of the graph (edge lists, per-node arrays, ranks)."""
        _clear(self.workdir)


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
    Everything node- or link-sized goes to files under ``workdir`` (local disk); memory holds chunks
    of ``config``'s sizes, plus 4 bytes a node while the edge buckets are sorted and the out-degrees
    counted.
    """
    t0 = time.monotonic()
    workdir.mkdir(parents=True, exist_ok=True)
    _clear(workdir)
    sources = tuple(s for s, _ in readers)
    n = sum(len(r) for _, r in readers)
    if n >= 2**31 - 1:
        raise ValueError(f"too many documents for int32 node ids: {n}")

    def say(msg: str) -> None:
        if progress is not None:
            progress(msg)

    _release()
    bits = _partition_bits(n, config.node_chunk)
    step = max(1, config.node_chunk)
    resolve = config.resolve_aliases
    limit = config.buffer_bytes
    say(f"rank: {n:,} nodes in {len(sources)} source(s), {1 << bits:,} key partition(s)")

    # 1. node keys and groups ------------------------------------------------------------------
    exact_keys = _Spill(workdir, "exact", _KEY, limit)
    loose_keys = _Spill(workdir, "loose", _KEY, limit)
    group_index: dict[tuple[int, str], int] = {}
    group_list: list[tuple[int, str]] = []
    sizes = np.zeros(0, dtype=np.int64)
    prefixed: list[str] = []
    base = 0
    with graph_file(workdir, "node_group").open("wb") as group_out:
        for si, (sid, reader) in enumerate(readers):
            if _prefix_mode(reader):
                prefixed.append(sid)
            for batch in reader.scan(["doc_id", "lang"], batch_size=config.batch_size):
                ids = cast("pa.Array[Any]", batch.column(0))
                k = len(ids)
                if base + k > n:
                    raise RuntimeError("the Refinery changed while rank was reading it")
                nodes = np.arange(base, base + k, dtype=np.int32)
                exact = key64(ids)
                exact_keys.add(_part(exact, bits), _records(_KEY, key=exact, node=nodes))
                if resolve:
                    loose = key64(loose_ids(ids))
                    loose_keys.add(_part(loose, bits), _records(_KEY, key=loose, node=nodes))
                g = _groups_of(cast("pa.Array[Any]", batch.column(1)), si, group_index, group_list)
                group_out.write(g.tobytes())
                counts = np.bincount(g, minlength=len(group_list))
                sizes = np.pad(sizes, (0, len(counts) - len(sizes))) + counts
                base += k
    exact_keys.flush()
    loose_keys.flush()
    if base != n:
        raise RuntimeError(f"id scan saw {base} rows, the readers report {n}; Refinery changed?")
    say(f"rank: {len(group_list):,} source/language group(s)")

    # 2. aliases: the loose key of the doc id each would have, by the exact key of its target ---
    alias_keys = _Spill(workdir, "alias", _ALIAS, limit)
    if resolve:
        _release()
        prefixed_set = pa.array(prefixed, type=pa.string())
        seq = 0
        for chunk in _batched(aliases, 500_000):
            targets = pa.array([t for t, _ in chunk], type=pa.string())
            names = pa.array([a for _, a in chunk], type=pa.string())
            src = pc.replace_substring_regex(targets, pattern=r"^wsp:([^/]*)/.*$", replacement=r"\1")
            is_prefixed = pc.is_in(src, value_set=prefixed_set)
            target = key64(targets)
            alias = key64(loose_ids(_alias_ids(targets, names, is_prefixed)))
            order = np.arange(seq, seq + len(chunk), dtype=np.int64)
            alias_keys.add(_part(target, bits), _records(_ALIAS, key=target, loose=alias, seq=order))
            seq += len(chunk)
        alias_keys.flush()

    # 3. links, by the exact key of their target ------------------------------------------------
    links = _Spill(workdir, "links", _LINK if resolve else _MISS, limit)
    base = batches = 0
    for _sid, reader in readers:
        for batch in reader.scan(["links"], batch_size=config.batch_size):
            column = cast("pa.Array[Any]", batch.column(0))
            rows = len(column)
            flat = cast("pa.Array[Any]", pc.list_flatten(column))
            if len(flat):
                parents = cast(I64, pc.list_parent_indices(column).to_numpy(zero_copy_only=False)).astype(
                    np.int64
                )
                local = cast("pa.Array[Any]", pc.starts_with(flat, pattern="wsp:"))
                keep = local.to_numpy(zero_copy_only=False).astype(bool)
                flat = flat.filter(local)
                if len(flat):
                    target = key64(flat)
                    rec = np.empty(len(flat), dtype=links.dtype)
                    rec["key"] = target
                    rec["src"] = parents[keep] + base
                    if resolve:
                        rec["loose"] = key64(loose_ids(flat))
                    links.add(_part(target, bits), rec)
            base += rows
            batches += 1
            if batches % 16 == 0:
                _release()
            if base % (config.batch_size * 64) < rows:
                say(f"rank: {base:,}/{n:,} documents scanned, {links.count:,} library links")
    links.flush()
    if base != n:
        raise RuntimeError(f"link scan saw {base} rows but the id scan saw {n}; Refinery changed?")
    _release()

    # 4. resolve by exact id, partition by partition; write the key-sorted id table -------------
    shift = max(0, math.ceil(math.log2(max(1, config.bucket_nodes))))
    n_buckets = max(1, (n + (1 << shift) - 1) >> shift)
    found_edges = _Spill(workdir, "edges", _EDGE, limit)
    misses = _Spill(workdir, "miss", _MISS, limit)
    alias_nodes = _Spill(workdir, "aliasnode", _ALIAS_NODE, limit)
    resolved = {"exact": 0, "loose": 0, "alias": 0}
    unresolved = n_alias = 0

    def emit(src: npt.NDArray[Any], dst: I64) -> None:
        found_edges.add(dst >> shift, _records(_EDGE, src=src, dst=dst))

    with (
        graph_file(workdir, "id_keys").open("wb") as keys_out,
        graph_file(workdir, "id_nodes").open("wb") as nodes_out,
    ):
        for p in range(1 << bits):
            by_id = _key_table(exact_keys.take(p))
            keys_out.write(by_id.keys.tobytes())
            nodes_out.write(by_id.nodes.tobytes())
            for rec in alias_keys.chunks(p, step):
                node = by_id.find(rec["key"])
                ok = node >= 0
                n_alias += int(ok.sum())
                loose = rec["loose"][ok]
                alias_nodes.add(
                    _part(loose, bits), _records(_ALIAS_NODE, key=loose, node=node[ok], seq=rec["seq"][ok])
                )
            for rec in links.chunks(p, step):
                tgt = by_id.find(rec["key"])
                hit = tgt >= 0
                resolved["exact"] += int(hit.sum())
                emit(rec["src"][hit], tgt[hit])
                miss = ~hit
                if resolve:
                    loose = rec["loose"][miss]
                    misses.add(_part(loose, bits), _records(_MISS, key=loose, src=rec["src"][miss]))
                else:
                    unresolved += int(miss.sum())
            del by_id
    misses.flush()
    alias_nodes.flush()
    say(
        f"rank: {resolved['exact']:,} of {links.count:,} library links resolved by id"
        + (f", {n_alias:,} aliases resolved to documents" if resolve else "")
    )

    # 5. the misses by loose id, then by alias ----------------------------------------------------
    if resolve:
        for q in range(1 << bits):
            by_loose = _key_table(loose_keys.take(q))
            named = alias_nodes.take(q)
            named = named[np.argsort(named["seq"], kind="stable")]
            by_alias = KeyTable(named["key"], named["node"])
            del named
            for rec in misses.chunks(q, step):
                found = by_loose.find(rec["key"])
                resolved["loose"] += int((found >= 0).sum())
                still = found < 0
                if still.any() and len(by_alias):
                    via = by_alias.find(rec["key"][still])
                    resolved["alias"] += int((via >= 0).sum())
                    found[still] = via
                ok = found >= 0
                unresolved += int((~ok).sum())
                emit(rec["src"][ok], found[ok])
            del by_loose, by_alias
        say(f"rank: {resolved['loose']:,} links resolved by loose id, {resolved['alias']:,} by alias")
    found_edges.flush()
    _release()

    # 6. CSR by destination: each bucket sorted by (destination, source), duplicates dropped -----
    node_group = cast(I32, _Vec(graph_file(workdir, "node_group"), np.int32, n).load())
    edges = intra = 0
    with ExitStack() as stack:
        src_out = {kind: stack.enter_context(graph_file(workdir, kind).open("wb")) for kind in _KINDS}
        deg_out = {
            kind: stack.enter_context(graph_file(workdir, f"indeg_{kind}").open("wb")) for kind in _KINDS
        }
        # sorting a piece costs ~70 bytes an edge: pieces of a quarter of edge_chunk (~300 MB)
        piece_edges = max(1, config.edge_chunk // 4)
        buckets = (
            piece
            for b in range(n_buckets)
            for piece in _bucket(found_edges, b, b << shift, n, shift, piece_edges)
        )
        for lo, hi, pairs in buckets:
            pairs = pairs[pairs["src"] != pairs["dst"]]
            pair = np.unique(
                (pairs["dst"].astype(np.uint64) << np.uint64(32)) | pairs["src"].astype(np.uint64)
            )
            del pairs
            d = (pair >> np.uint64(32)).astype(np.int32)
            s = (pair & np.uint64(0xFFFFFFFF)).astype(np.int32)
            del pair
            same = node_group[s] == node_group[d]
            for kind, mask in (("intra", same), ("cross", ~same)):
                src_out[kind].write(s[mask].tobytes())
                deg_out[kind].write(np.bincount(d[mask] - lo, minlength=hi - lo).astype(np.int32).tobytes())
            edges += len(s)
            intra += int(same.sum())
    del node_group
    _release()
    say(f"rank: {edges:,} edges ({intra:,} within a group), {unresolved:,} links outside the library")
    say("rank: edge lists sorted by destination")

    # 7. out-degrees, one kind at a time ---------------------------------------------------------
    for kind in _KINDS:
        out = np.zeros(n, dtype=np.int32)
        for src in _EdgeFile(graph_file(workdir, kind)).chunks(max(1, config.edge_chunk)):
            nodes_, counts_ = np.unique(src, return_counts=True)
            out[nodes_] += counts_.astype(np.int32)
        out.tofile(graph_file(workdir, f"out_{kind}"))
        del out
    groups = tuple((sources[si], lang) for si, lang in group_list)
    return LinkGraph(
        n=n,
        sources=sources,
        groups=groups,
        group_sizes=tuple(int(c) for c in sizes),
        workdir=workdir,
        edges=edges,
        intra_edges=intra,
        unresolved=unresolved,
        seconds=time.monotonic() - t0,
        resolved=resolved,
        aliases=n_alias,
    )


def _bucket(
    spill: _Spill, part: int, lo: int, n: int, width: int, limit: int
) -> Iterator[tuple[int, int, npt.NDArray[Any]]]:
    """The edges of bucket ``part`` (destinations ``[lo, lo + 2**width)``) as ``(lo, hi, pairs)``
    pieces of at most about ``limit`` edges: a bucket over the limit (in-links concentrate on some
    destinations) is split by destination into sub-buckets, recursively, down to single nodes."""
    hi = min(n, lo + (1 << width))
    count = spill.size(part)
    if count <= limit or width == 0:
        yield lo, hi, spill.take(part)
        return
    bits = min(width, max(1, math.ceil(math.log2(2 * count / limit))))
    sub = _Spill(spill.workdir, f"{spill.name}.{part:05d}", spill.dtype, spill.limit)
    for rec in spill.chunks(part, limit):
        sub.add((rec["dst"].astype(np.int64) - lo) >> (width - bits), rec)
    sub.flush()
    for j in range(1 << bits):
        sub_lo = lo + (j << (width - bits))
        if sub_lo >= hi:
            break
        yield from _bucket(sub, j, sub_lo, n, width - bits, limit)


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


def peak_rss_mib() -> float:
    """This process's peak resident set size so far (MiB)."""
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


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
class _Range:
    """Destination nodes ``[lo, hi)`` and their in-edges in both CSRs (``*_lo``: first edge)."""

    lo: int
    hi: int
    intra_lo: int
    intra: int
    cross_lo: int
    cross: int


def _ranges(indeg_intra: _Vec, indeg_cross: _Vec, chunk: int, window: int) -> list[_Range]:
    """Node ranges of at most ``window`` nodes whose in-edges fit ``chunk`` (a single node may
    exceed it)."""
    out: list[_Range] = []
    n = indeg_intra.n
    lo = e_intra = e_cross = 0
    while lo < n:
        hi = min(n, lo + window)
        a = cast(I32, indeg_intra.read(lo, hi))
        b = cast(I32, indeg_cross.read(lo, hi))
        c = np.cumsum(a, dtype=np.int64) + np.cumsum(b, dtype=np.int64)
        if c[-1] > chunk:
            hi = lo + max(1, int(np.searchsorted(c, chunk, side="right")))
        m_intra = int(a[: hi - lo].sum(dtype=np.int64))
        m_cross = int(b[: hi - lo].sum(dtype=np.int64))
        out.append(_Range(lo, hi, e_intra, m_intra, e_cross, m_cross))
        e_intra += m_intra
        e_cross += m_cross
        lo = hi
    return out


class _EdgeFile:
    """Chunked reads of a ``<kind>.src`` file with ``pread``: unlike a memory map, the pages read do
    not stay in the process's resident set (2.5 GB at 632M edges), only in the reclaimable cache."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.edges = path.stat().st_size // 4 if path.is_file() else 0

    def read(self, lo: int, n: int) -> I32:
        with self.path.open("rb") as fh:
            return np.fromfile(fh, dtype=np.int32, count=n, offset=4 * lo)

    def chunks(self, step: int) -> Iterator[I32]:
        for lo, hi in _steps(self.edges, step):
            yield self.read(lo, hi - lo)


def _pull(src: I32, deg: I32, w: F32, acc: F32) -> None:
    """``acc[t] += sum(w[s] for s -> t)`` for one range's in-edges ``src`` (sums in float64)."""
    nz = np.flatnonzero(deg)
    starts = (np.cumsum(deg, dtype=np.int64) - deg)[nz]
    sums = np.add.reduceat(w[src], starts, dtype=np.float64)
    acc[nz] += sums.astype(np.float32)


def _inverse(counts: I32) -> F32:
    out = np.zeros(len(counts), dtype=np.float32)
    np.divide(1.0, counts, out=out, where=counts > 0)
    return out


def _pagerank(graph: LinkGraph, config: RankConfig) -> tuple[_Vec, _Vec, int, float, float]:
    """:func:`pagerank` into ``rank_group.f32`` / ``rank_global.f32`` of the work directory (one
    file, returned twice, with a single group).

    Memory holds the weight vectors ``rank / out-degree`` (float32, 4 bytes a node each: gathered at
    random by the edges); the ranks, degrees and groups are read and written in slices of
    ``config.node_chunk``, and the edges in ranges of ``config.edge_chunk``.
    """
    n = graph.n
    single = len(graph.groups) <= 1
    step = max(1, config.node_chunk)
    out_intra = graph.vector("out_intra", np.int32)
    out_cross = graph.vector("out_cross", np.int32)
    groups = graph.vector("node_group", np.int32)
    src = {kind: _EdgeFile(graph_file(graph.workdir, kind)) for kind in _KINDS}
    indeg = {kind: graph.vector(f"indeg_{kind}", np.int32) for kind in _KINDS}
    ranges = _ranges(indeg["intra"], indeg["cross"], max(1, config.edge_chunk), min(step, 1 << 20))
    sizes = np.asarray(graph.group_sizes, dtype=np.float64)

    r_g = _Vec.create(graph_file(graph.workdir, "rank_global"), np.float32, n)
    r_s = r_g if single else _Vec.create(graph_file(graph.workdir, "rank_group"), np.float32, n)
    for lo, hi in _steps(n, step):
        r_g.write(lo, np.full(hi - lo, 1.0 / n, dtype=np.float32))
        if not single:
            r_s.write(lo, (1.0 / sizes[groups.read(lo, hi)]).astype(np.float32))
    if n == 0:
        return r_s, r_g, 0, 0.0, 0.0

    d = np.float32(config.damping)
    w_g = np.empty(n, dtype=np.float32)
    w_s = None if single else np.empty(n, dtype=np.float32)
    res_g = res_s = math.inf
    it = 0
    while it < config.max_iterations and max(res_g, res_s) > config.tolerance:
        it += 1
        # weights, and the rank mass of dangling nodes (spread by teleportation)
        leak_g = 0.0
        leak_s = np.zeros(len(sizes), dtype=np.float64)
        for lo, hi in _steps(n, step):
            o_intra = cast(I32, out_intra.read(lo, hi))
            out_g = o_intra.astype(np.int64) + cast(I32, out_cross.read(lo, hi))
            rank = cast(F32, r_g.read(lo, hi))
            w_g[lo:hi] = rank * _inverse(out_g.astype(np.int32))
            leak_g += float(rank[out_g == 0].sum(dtype=np.float64))
            if w_s is not None:
                rank = cast(F32, r_s.read(lo, hi))
                w_s[lo:hi] = rank * _inverse(o_intra)
                dangling = o_intra == 0
                g = cast(I32, groups.read(lo, hi))
                leak_s += np.bincount(g[dangling], weights=rank[dangling], minlength=len(sizes))
        tele_g = np.float32((float(d) * leak_g + (1.0 - float(d))) / n)
        tele_s = ((float(d) * leak_s + (1.0 - float(d))) / np.maximum(sizes, 1.0)).astype(np.float32)
        res_g = res_s = 0.0
        for rng in ranges:
            acc_g = np.zeros(rng.hi - rng.lo, dtype=np.float32)
            acc_s = None if w_s is None else np.zeros(rng.hi - rng.lo, dtype=np.float32)
            if rng.intra:
                edges = src["intra"].read(rng.intra_lo, rng.intra)
                deg = cast(I32, indeg["intra"].read(rng.lo, rng.hi))
                _pull(edges, deg, w_g, acc_g)
                if w_s is not None and acc_s is not None:
                    _pull(edges, deg, w_s, acc_s)
            if rng.cross:
                edges = src["cross"].read(rng.cross_lo, rng.cross)
                _pull(edges, cast(I32, indeg["cross"].read(rng.lo, rng.hi)), w_g, acc_g)
            acc_g *= d
            acc_g += tele_g
            res_g += float(np.abs(acc_g - r_g.read(rng.lo, rng.hi)).sum(dtype=np.float64))
            r_g.write(rng.lo, acc_g)
            if acc_s is not None:
                acc_s *= d
                acc_s += tele_s[groups.read(rng.lo, rng.hi)]
                res_s += float(np.abs(acc_s - r_s.read(rng.lo, rng.hi)).sum(dtype=np.float64))
                r_s.write(rng.lo, acc_s)
        res_s = res_g if single else res_s / max(1, len(sizes))
    return r_s, r_g, it, res_s, res_g


def pagerank(graph: LinkGraph, config: RankConfig = RankConfig()) -> tuple[F32, F32, int, float, float]:  # noqa: B008
    """``(per_group, global, iterations, residual_group, residual_global)`` by power iteration.

    Ranks are float32, in-link sums and residuals float64. With a single group the two ranks are
    identical and only the global one is computed. The ranks are returned in memory (tests and
    benchmarks); :func:`compute_static_rank` streams them from their files instead.
    """
    per_group, global_, it, res_s, res_g = _pagerank(graph, config)
    r_g = cast(F32, global_.load())
    r_s = r_g if per_group is global_ else cast(F32, per_group.load())
    return r_s, r_g, it, res_s, res_g


def _runs(groups: I32) -> Iterator[tuple[int, I64]]:
    """``(group, positions)`` of each group present in ``groups``."""
    order = np.argsort(groups, kind="stable")
    cuts = np.flatnonzero(np.diff(groups[order])) + 1
    for idx in np.split(order, cuts):
        if len(idx):
            yield int(groups[idx[0]]), idx


def _percentile_chunks(
    values: _Read, groups: _Read | None, sizes: I64, n: int, step: int
) -> Iterator[tuple[int, F32]]:
    """:func:`percentiles` in slices ``(lo, out[lo:lo + step])``.

    The values are sorted group by group into one float32 array of ``n`` (the only node-sized
    allocation); each slice is then ranked by binary search in its groups' runs of it.
    """
    sorted_v = np.empty(n, dtype=np.float32)
    start = np.zeros(len(sizes) + 1, dtype=np.int64)
    np.cumsum(sizes, out=start[1:])
    if groups is None:
        for lo, hi in _steps(n, step):
            sorted_v[lo:hi] = values(lo, hi)
        sorted_v.sort()
    else:
        fill = start[:-1].copy()
        for lo, hi in _steps(n, step):
            v = values(lo, hi)
            for g, idx in _runs(cast(I32, groups(lo, hi))):
                sorted_v[fill[g] : fill[g] + len(idx)] = v[idx]
                fill[g] += len(idx)
        for g in range(len(sizes)):
            sorted_v[start[g] : start[g + 1]].sort()
    for lo, hi in _steps(n, step):
        v = values(lo, hi)
        out = np.empty(hi - lo, dtype=np.float32)
        if groups is None:
            out[:] = np.searchsorted(sorted_v, v, side="left") / max(1, n - 1)
        else:
            for g, idx in _runs(cast(I32, groups(lo, hi))):
                run = sorted_v[start[g] : start[g + 1]]
                out[idx] = np.searchsorted(run, v[idx], side="left") / max(1, len(run) - 1)
        yield lo, out


def percentiles(values: F32, groups: I32 | None = None) -> F32:
    """Rank of each value among its group as a fraction in ``[0, 1)`` (ties share the lowest)."""
    out = np.zeros(len(values), dtype=np.float32)
    sizes = np.bincount(groups) if groups is not None else np.array([len(values)], dtype=np.int64)
    by_group = None if groups is None else _slicer(groups)
    for lo, pct in _percentile_chunks(_slicer(values), by_group, sizes, len(values), _NODE_CHUNK):
        out[lo : lo + len(pct)] = pct
    return out


def _slicer(arr: npt.NDArray[Any]) -> _Read:
    return lambda lo, hi: arr[lo:hi]


def _blend(per_group: _Read, global_: _Read, groups: _Read, sizes: I64, n: int, mix: float, step: int) -> F32:
    feature = np.empty(n, dtype=np.float32)
    for lo, pct in _percentile_chunks(per_group, groups, sizes, n, step):
        feature[lo : lo + len(pct)] = (1.0 - mix) * pct
    for lo, pct in _percentile_chunks(global_, None, np.array([n], dtype=np.int64), n, step):
        feature[lo : lo + len(pct)] += mix * pct
    return feature


def blend(per_group: F32, global_: F32, node_group: I32, mix: float) -> F32:
    """The static-rank feature: ``(1 - mix) * pct_group + mix * pct_global``, in ``[0, 1]``."""
    if not len(global_):
        return np.zeros(0, dtype=np.float32)
    sizes = np.bincount(node_group)
    return _blend(
        _slicer(per_group),
        _slicer(global_),
        _slicer(node_group),
        sizes,
        len(global_),
        mix,
        _NODE_CHUNK,
    )


# --- sidecar ------------------------------------------------------------------------------------------


def _write_sidecar(
    path: Path, n: int, keys: _Read, nodes: _Read, columns: Iterable[Callable[[], F32]], step: int
) -> None:
    """Write the sidecar atomically: key-sorted ``keys``, then each column (loaded one at a time,
    indexed by node) gathered in key order through ``nodes``."""
    tmp = path.with_name(path.name + ".partial")
    with tmp.open("wb") as fh:
        fh.write(MAGIC)
        fh.write(np.uint64(n).tobytes())
        for lo, hi in _steps(n, step):
            fh.write(np.ascontiguousarray(keys(lo, hi), dtype=np.uint64).tobytes())
        for load in columns:
            column = load()
            for lo, hi in _steps(n, step):
                fh.write(column[nodes(lo, hi)].astype(np.float32).tobytes())
            del column
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def write_rank_file(path: Path, keys: U64, nodes: I32, feature: F32, per_group: F32, global_: F32) -> int:
    """Write the sidecar atomically from key-sorted ``keys`` and their ``nodes``; returns its size."""
    n = len(keys)
    columns = (lambda: feature, lambda: per_group, lambda: global_)
    _write_sidecar(path, n, _slicer(keys), _slicer(nodes), columns, 1 << 22)
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
    """Build the graph, run PageRank, write the sidecar (``out_path``) and its stats next to it.

    ``workdir`` holds the scratch files (local disk; removed at the end). Memory peaks at about 8
    bytes a node (the two weight vectors of the power iteration, then the percentile sort and the
    feature) plus the chunks of ``config``.
    """
    graph = build_graph(readers, workdir, aliases=aliases, config=config, progress=progress)
    try:
        t1 = time.monotonic()
        per_group, global_, iters, res_s, res_g = _pagerank(graph, config)
        if progress:
            progress(f"rank: {iters} iterations, residual {res_g:.2e} (global) / {res_s:.2e} (per group)")
        n = graph.n
        step = max(1, config.node_chunk)
        groups = graph.vector("node_group", np.int32)
        sizes = np.asarray(graph.group_sizes, dtype=np.int64)

        def feature() -> F32:
            return _blend(per_group.read, global_.read, groups.read, sizes, n, config.mix, step)

        def ranks(vec: _Vec) -> Callable[[], F32]:
            return lambda: cast(F32, vec.load())

        out_path.parent.mkdir(parents=True, exist_ok=True)
        keys = graph.vector("id_keys", np.uint64)
        nodes = graph.vector("id_nodes", np.int32)
        _write_sidecar(out_path, n, keys.read, nodes.read, (feature, ranks(per_group), ranks(global_)), step)
        seconds_iterate = time.monotonic() - t1
    finally:
        graph.remove_edges()
    by_source: dict[str, int] = {}
    for (sid, _), c in zip(graph.groups, graph.group_sizes, strict=True):
        by_source[sid] = by_source.get(sid, 0) + c
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
        groups={f"{s}/{lang}": c for (s, lang), c in zip(graph.groups, graph.group_sizes, strict=True)},
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
    if progress:
        progress(f"rank: peak RSS {peak_rss_mib():,.0f} MiB")
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
