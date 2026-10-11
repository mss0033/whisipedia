"""Cluster-ordered reading of ZIM archives (audit 2026-10-11 C2).

Reading a ZIM in path order through libzim decompresses about one cluster per article: a
2,000-entry path-ordered slice of English Wikipedia touches ~900 distinct clusters, and every
redirect costs two random reads (3.4 ms each over SMB). This module reads the archive the way it is
laid out instead:

1. **Scan** (parallel, one task per window of :data:`WINDOW_ENTRIES` directory entries): the path
   pointer list and the directory entries it points at are read in large sequential pieces and
   parsed with numpy (no per-entry Python). Each window writes its entries' kind, namespace, MIME
   index, cluster/blob (or redirect target), path and title to ``.npy`` files on local disk.
2. **Content** (parallel, one task per run of clusters holding at most :data:`TASK_BYTES`
   compressed bytes and :data:`TASK_ENTRIES` HTML entries): the worker reads its clusters with one
   ``read`` per run of consecutive cluster numbers, decompresses each cluster once and slices every
   HTML blob it holds. Output is in ``(cluster, blob, entry index)`` order.
3. **Redirects** (parallel, :data:`REDIRECT_TASK` per task, in entry-index order): source and
   target paths come from the scan files; nothing is read from the archive.

Format reference: https://wiki.openzim.org/wiki/ZIM_file_format. The header is 80 bytes
(``magic, major, minor, uuid, entryCount, clusterCount, pathPtrPos, titlePtrPos, clusterPtrPos,
mimeListPos, mainPage, layoutPage, checksumPos``); the MIME list is NUL-terminated strings ending
with an empty one; the path pointer list holds ``entryCount`` u64 offsets of directory entries
sorted by ``(namespace, path)``. A content entry is ``u16 mime, u8 parameterLen, u8 namespace, u32
revision, u32 cluster, u32 blob, path\\0, title\\0, parameters``; a redirect has ``mime = 0xffff``
and ``u32 redirectIndex`` in place of cluster and blob; ``0xfffe``/``0xfffd`` (link target, deleted;
old archives only) have neither. A cluster starts with an info byte (compression in the low
nibble: 1 none, 2 zlib, 3 bzip2, 4 xz, 5 zstd; ``0x10`` = 64-bit blob offsets) followed by the
(compressed) blob offset table and blobs.

Entries are exposed as libzim exposes them: the user entries are namespace ``C`` and paths carry
no namespace (the new namespace scheme, ``minor >= 1``); a title is the stored title, or the path
when the stored title is empty. Archives with the old scheme (``minor == 0``, none in the vault as
of 2026-10-11, and not produced by Kiwix since 2021) raise :class:`ZimFormatError` rather than be
read by an untested path.
"""

from __future__ import annotations

import bz2
import lzma
import os
import shutil
import struct
import zlib
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

import numpy as np
import numpy.typing as npt
import zstandard

MAGIC = 72_173_914
_HEADER = struct.Struct("<IHH16sIIQQQQIIQ")
REDIRECT_MIME = 0xFFFF
LINKTARGET_MIME = 0xFFFE
DELETED_MIME = 0xFFFD
KIND_CONTENT, KIND_REDIRECT, KIND_OTHER = 0, 1, 2

WINDOW_ENTRIES = 1 << 19  # directory entries per scan task (~25-60 MB of dirents)
# Compressed cluster bytes per content task. 64 MiB tasks returned ~150 MB of text each on
# Wikibooks, and with 2 x workers results in flight the parent peaked at 1.5 GB; 16 MiB still
# amortizes reads (clusters are ~1 MiB) and keeps a result in the tens of MB.
TASK_BYTES = 16 << 20
TASK_ENTRIES = 20_000  # HTML entries per content task (bounds the result a worker sends back)
REDIRECT_TASK = 100_000  # redirects per redirect task
_SEGMENT_GAP = 4 << 20  # dirent offsets further apart than this are read separately
_SEGMENT_SPAN = 64 << 20  # and no single read spans more than this
_TAIL = 4096  # bytes read past the last dirent start of a segment (grown when a string is longer)

U8 = npt.NDArray[np.uint8]
I64 = npt.NDArray[np.int64]
U32 = npt.NDArray[np.uint32]


class ZimFormatError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ZimHeader:
    major: int
    minor: int
    entry_count: int
    cluster_count: int
    path_ptr_pos: int
    title_ptr_pos: int
    cluster_ptr_pos: int
    mime_list_pos: int
    checksum_pos: int
    file_size: int

    @property
    def new_namespace_scheme(self) -> bool:
        return self.minor >= 1


def read_header(fh: BinaryIO) -> ZimHeader:
    raw = fh.read(_HEADER.size)
    if len(raw) != _HEADER.size:
        raise ZimFormatError("short ZIM header")
    magic, major, minor, _uuid, entries, clusters, path_ptr, title_ptr, cluster_ptr, mime, _m, _l, csum = (
        _HEADER.unpack(raw)
    )
    if magic != MAGIC:
        raise ZimFormatError(f"not a ZIM archive (magic {magic:#x})")
    size = fh.seek(0, os.SEEK_END)
    return ZimHeader(major, minor, entries, clusters, path_ptr, title_ptr, cluster_ptr, mime, csum, size)


def read_mimes(fh: BinaryIO, pos: int) -> tuple[str, ...]:
    """The MIME type list: NUL-terminated strings up to an empty one."""
    fh.seek(pos)
    data = b""
    out: list[str] = []
    at = 0
    while True:
        end = data.find(b"\0", at)
        if end < 0:
            chunk = fh.read(4096)
            if not chunk:
                raise ZimFormatError("unterminated MIME list")
            data += chunk
            continue
        if end == at:
            return tuple(out)
        out.append(data[at:end].decode("utf-8", "replace"))
        at = end + 1


def _pointer(fh: BinaryIO, header: ZimHeader, index: int) -> int:
    fh.seek(header.path_ptr_pos + 8 * index)
    return struct.unpack("<Q", fh.read(8))[0]


def _namespace(fh: BinaryIO, header: ZimHeader, index: int) -> int:
    fh.seek(_pointer(fh, header, index) + 3)
    return fh.read(1)[0]


def user_range(fh: BinaryIO, header: ZimHeader) -> tuple[int, int]:
    """Entry indexes libzim exposes as user entries: namespace ``C``."""
    if not header.new_namespace_scheme:
        raise ZimFormatError(f"old namespace scheme (ZIM {header.major}.{header.minor}) is not supported")

    def lower_bound(ns: int) -> int:
        lo, hi = 0, header.entry_count
        while lo < hi:
            mid = (lo + hi) // 2
            if _namespace(fh, header, mid) < ns:
                lo = mid + 1
            else:
                hi = mid
        return lo

    return lower_bound(ord("C")), lower_bound(ord("C") + 1)


def cluster_offsets(fh: BinaryIO, header: ZimHeader, *, dirents_at: int = 0) -> npt.NDArray[np.uint64]:
    """``cluster_count + 1`` offsets: cluster ``c`` occupies ``[off[c], off[c + 1])``.

    The last cluster ends at the next section: whichever of the header's sections, or the first
    directory entry (``dirents_at``; libzim writes the directory right after the clusters), comes
    first after it.
    """
    fh.seek(header.cluster_ptr_pos)
    raw = fh.read(8 * header.cluster_count)
    if len(raw) != 8 * header.cluster_count:
        raise ZimFormatError("short cluster pointer list")
    offs = np.frombuffer(raw, dtype="<u8").astype(np.uint64)
    if offs.size and np.any(np.diff(offs.astype(np.int64)) <= 0):
        raise ZimFormatError("cluster pointers are not increasing")
    last = int(offs[-1]) if offs.size else 0
    sections = (
        header.checksum_pos,
        header.path_ptr_pos,
        header.title_ptr_pos,
        header.cluster_ptr_pos,
        header.mime_list_pos,
        header.file_size,
        dirents_at,
    )
    end = min(s for s in sections if s > last)
    return np.append(offs, np.uint64(end))


# --- scan --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ScanTask:
    zim: Path
    out: Path  # this window's directory
    first: int  # global entry index of the window's first entry
    count: int
    path_ptr_pos: int


def _u16(buf: U8, at: I64) -> npt.NDArray[np.uint16]:
    return (buf[at].astype(np.uint16) | (buf[at + 1].astype(np.uint16) << 8)).astype(np.uint16)


def _u32(buf: U8, at: I64) -> U32:
    out = buf[at].astype(np.uint32)
    for k in (1, 2, 3):
        out |= buf[at + k].astype(np.uint32) << (8 * k)
    return out


def _parse_segment(buf: U8, at: I64) -> tuple[npt.NDArray[np.uint16], U32, U32, I64, I64, I64] | None:
    """Fixed fields and string bounds of the dirents starting at ``at`` (``None``: buffer too short)."""
    if int(at.max()) + 16 > buf.size:
        return None
    mime = _u16(buf, at)
    a = _u32(buf, at + 8)
    b = _u32(buf, at + 12)
    path_start = at + np.where(mime == REDIRECT_MIME, 12, np.where(mime >= DELETED_MIME, 8, 16))
    nul = np.flatnonzero(buf == 0)
    pi = np.searchsorted(nul, path_start)
    if pi.size and int(pi.max()) >= nul.size:
        return None
    path_end = nul[pi]
    ti = np.searchsorted(nul, path_end + 1)
    if ti.size and int(ti.max()) >= nul.size:
        return None
    return mime, a, b, path_start, path_end, nul[ti]


def scan_window(task: ScanTask) -> int:
    """Worker: parse ``task.count`` directory entries from ``task.first`` into ``task.out``.

    The raw dirent bytes are kept (``dirents.bin``, about the window's share of the directory)
    with each entry's path and title bounds, so no string is copied out entry by entry. Peak
    memory is one segment (at most ``_SEGMENT_SPAN`` plus a tail) and its NUL positions.
    """
    out = task.out
    out.mkdir(parents=True, exist_ok=True)
    n = task.count
    mime = np.zeros(n, np.uint16)
    a = np.zeros(n, np.uint32)
    b = np.zeros(n, np.uint32)
    path_start = np.zeros(n, np.int64)
    path_end = np.zeros(n, np.int64)
    title_end = np.zeros(n, np.int64)
    with task.zim.open("rb") as fh, (out / "dirents.bin").open("wb") as raw_out:
        file_end = fh.seek(0, os.SEEK_END)
        fh.seek(task.path_ptr_pos + 8 * task.first)
        ptrs = np.frombuffer(fh.read(8 * n), dtype="<u8").astype(np.int64)
        if ptrs.size != n:
            raise ZimFormatError("short path pointer list")
        order = np.argsort(ptrs, kind="stable")
        sorted_ptrs = ptrs[order]
        # Segments: split at gaps, then every _SEGMENT_SPAN bytes inside a segment (bounded reads).
        seg_id = np.concatenate(([0], np.cumsum(np.diff(sorted_ptrs) > _SEGMENT_GAP)))
        seg_base = sorted_ptrs[np.concatenate(([0], np.flatnonzero(np.diff(seg_id)) + 1))][seg_id]
        key = seg_id * (1 << 40) + (sorted_ptrs - seg_base) // _SEGMENT_SPAN
        written = 0
        for seg in np.split(np.arange(n), np.flatnonzero(np.diff(key)) + 1):
            pos = sorted_ptrs[seg]
            base = int(pos[0])
            tail = _TAIL
            while True:
                fh.seek(base)
                buf = np.frombuffer(fh.read(int(pos[-1]) - base + tail), dtype=np.uint8)
                parsed = _parse_segment(buf, pos - base)
                if parsed is not None:
                    break
                if base + buf.size >= file_end:
                    raise ZimFormatError("directory entry runs past the end of the file")
                tail *= 4
            m, fa, fb, ps, pe, te = parsed
            idx = order[seg]
            mime[idx], a[idx], b[idx] = m, fa, fb
            path_start[idx], path_end[idx], title_end[idx] = ps + written, pe + written, te + written
            used = int(te.max()) + 1  # keep the segment up to its last title's NUL
            raw_out.write(buf[:used].tobytes())
            written += used
    kind = np.where(
        mime == REDIRECT_MIME, KIND_REDIRECT, np.where(mime >= DELETED_MIME, KIND_OTHER, KIND_CONTENT)
    )
    content = np.flatnonzero(kind == KIND_CONTENT)
    by_cluster = content[np.lexsort((content, b[content], a[content]))]
    arrays: dict[str, npt.NDArray[np.generic]] = {
        "kind": kind.astype(np.uint8),
        "mime": mime,
        "a": a,
        "b": b,
        "path_start": path_start,
        "path_end": path_end,
        "title_end": title_end,
        "content": by_cluster.astype(np.uint32),  # local indexes sorted by (cluster, blob, index)
        "content_cluster": a[by_cluster],
        "redirects": np.flatnonzero(kind == KIND_REDIRECT).astype(np.uint32),
        "ptr_min": np.array([int(sorted_ptrs[0])], dtype=np.int64),
    }
    for name, arr in arrays.items():
        np.save(out / f"{name}.npy", arr)
    return n


# --- the scanned archive ----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Scan:
    """A scanned archive: windows of entry arrays on local disk."""

    zim: Path
    root: Path
    header: ZimHeader
    mimes: tuple[str, ...]
    first: int  # first user entry (global index)
    stop: int  # one past the last user entry
    window: int  # entries per window

    @property
    def windows(self) -> range:
        return range((self.stop - self.first + self.window - 1) // self.window)

    def window_dir(self, w: int) -> Path:
        return self.root / f"w{w:06d}"

    def load(self, w: int, name: str) -> npt.NDArray[Any]:
        return np.load(self.window_dir(w) / f"{name}.npy", mmap_mode="r")

    def dirents_at(self) -> int:
        """Offset of the first directory entry (after the windows are scanned)."""
        return min((int(self.load(w, "ptr_min")[0]) for w in self.windows), default=0)


def scan_tasks(zim: Path, root: Path, *, window: int | None = None) -> tuple[Scan, list[ScanTask]]:
    window = window or WINDOW_ENTRIES
    with zim.open("rb") as fh:
        header = read_header(fh)
        mimes = read_mimes(fh, header.mime_list_pos)
        first, stop = user_range(fh, header)
    scan = Scan(zim, root, header, mimes, first, stop, window)
    tasks = [
        ScanTask(
            zim,
            scan.window_dir(w),
            first + w * window,
            min(window, stop - first - w * window),
            header.path_ptr_pos,
        )
        for w in scan.windows
    ]
    return scan, tasks


def remove_scan(root: Path) -> None:
    shutil.rmtree(root, ignore_errors=True)


# --- content ------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ClusterTask:
    scan: Scan
    first_cluster: int  # inclusive
    stop_cluster: int  # exclusive
    html_mimes: tuple[int, ...]
    offsets: tuple[int, ...]  # cluster_offsets[first_cluster : stop_cluster + 1]


@dataclass(frozen=True, slots=True)
class Blob:
    """One HTML entry's bytes and identity, in cluster order."""

    index: int  # global entry index
    path: str
    title: str
    content: bytes


def content_tasks(
    scan: Scan,
    offsets: npt.NDArray[np.uint64],
    *,
    task_bytes: int = TASK_BYTES,
    task_entries: int = TASK_ENTRIES,
) -> list[ClusterTask]:
    """Runs of clusters holding HTML, each at most ``task_bytes`` compressed / ``task_entries`` entries."""
    html = tuple(i for i, m in enumerate(scan.mimes) if m.startswith("text/html"))
    html_arr = np.array(html, dtype=np.uint16)
    counts = np.zeros(scan.header.cluster_count, dtype=np.int64)
    for w in scan.windows:
        content = np.asarray(scan.load(w, "content"))
        mime = np.asarray(scan.load(w, "mime"))[content]
        clusters = np.asarray(scan.load(w, "content_cluster"))[np.isin(mime, html_arr)]
        if clusters.size and int(clusters.max()) >= scan.header.cluster_count:
            raise ZimFormatError("directory entry names a cluster past the cluster count")
        counts += np.bincount(clusters, minlength=scan.header.cluster_count)
    sizes = np.diff(offsets.astype(np.int64))
    needed = np.flatnonzero(counts)
    tasks: list[ClusterTask] = []
    start = 0
    while start < needed.size:
        csum = np.cumsum(sizes[needed[start:]])
        nsum = np.cumsum(counts[needed[start:]])
        fit = int(np.searchsorted(csum, task_bytes, side="right"))
        fit = min(fit, int(np.searchsorted(nsum, task_entries, side="right")))
        stop = start + max(1, fit)
        c0, c1 = int(needed[start]), int(needed[stop - 1]) + 1
        tasks.append(ClusterTask(scan, c0, c1, html, tuple(int(x) for x in offsets[c0 : c1 + 1])))
        start = stop
    return tasks


def _decompress(raw: bytes) -> tuple[bytes, bool]:
    info = raw[0]
    kind, extended = info & 0x0F, bool(info & 0x10)
    body = memoryview(raw)[1:]
    if kind in (0, 1):
        return bytes(body), extended
    if kind == 5:
        return zstandard.ZstdDecompressor().stream_reader(body).read(), extended
    if kind == 4:
        return lzma.LZMADecompressor().decompress(body), extended
    if kind == 2:
        return zlib.decompressobj().decompress(body), extended
    if kind == 3:
        return bz2.BZ2Decompressor().decompress(body), extended
    raise ZimFormatError(f"unknown cluster compression {kind}")


def _blob_offsets(data: bytes, extended: bool) -> npt.NDArray[np.uint64]:
    width = 8 if extended else 4
    first = int.from_bytes(data[:width], "little")
    if first % width or first < width or first > len(data):
        raise ZimFormatError("corrupt cluster blob table")
    return np.frombuffer(data[:first], dtype="<u8" if extended else "<u4").astype(np.uint64)


def read_clusters(task: ClusterTask) -> Iterator[Blob]:
    """Worker: every HTML blob of the task's clusters, in ``(cluster, blob, index)`` order.

    Each run of consecutive cluster numbers is read with one ``read`` and every cluster is
    decompressed once, however many entries it holds.
    """
    scan = task.scan
    html = np.array(task.html_mimes, dtype=np.uint16)
    cls: list[npt.NDArray[np.uint32]] = []
    bls: list[npt.NDArray[np.uint32]] = []
    gls: list[npt.NDArray[np.int64]] = []
    for w in scan.windows:
        clusters = scan.load(w, "content_cluster")
        lo = int(np.searchsorted(clusters, task.first_cluster, side="left"))
        hi = int(np.searchsorted(clusters, task.stop_cluster, side="left"))
        if lo == hi:
            continue
        local = np.asarray(scan.load(w, "content")[lo:hi]).astype(np.int64)
        keep = np.isin(np.asarray(scan.load(w, "mime"))[local], html)
        local = local[keep]
        cls.append(np.asarray(clusters[lo:hi])[keep])
        bls.append(np.asarray(scan.load(w, "b"))[local])
        gls.append(local + scan.first + w * scan.window)
    if not cls:
        return
    cl, bl, gi = np.concatenate(cls), np.concatenate(bls), np.concatenate(gls)
    order = np.lexsort((gi, bl, cl))
    cl, bl, gi = cl[order], bl[order], gi[order]
    uniq, first_pos = np.unique(cl, return_index=True)
    bounds = np.append(first_pos, cl.size)
    runs = np.split(np.arange(uniq.size), np.flatnonzero(np.diff(uniq.astype(np.int64)) != 1) + 1)
    strings: dict[int, _Strings] = {}
    with scan.zim.open("rb") as fh:
        for run in runs:
            base = task.offsets[int(uniq[run[0]]) - task.first_cluster]
            stop = task.offsets[int(uniq[run[-1]]) + 1 - task.first_cluster]
            fh.seek(base)
            raw = fh.read(stop - base)
            if len(raw) != stop - base:
                raise ZimFormatError("short cluster read")
            for k in run.tolist():
                c = int(uniq[k])
                lo_off = task.offsets[c - task.first_cluster] - base
                hi_off = task.offsets[c + 1 - task.first_cluster] - base
                data, extended = _decompress(raw[lo_off:hi_off])
                table = _blob_offsets(data, extended)
                for j in range(int(bounds[k]), int(bounds[k + 1])):
                    blob_no, g = int(bl[j]), int(gi[j])
                    if blob_no + 1 >= table.size:
                        raise ZimFormatError(f"cluster {c} has no blob {blob_no}")
                    w, local = divmod(g - scan.first, scan.window)
                    if w not in strings:
                        strings[w] = _Strings(scan, w)
                    path, title = strings[w].path_title(local)
                    yield Blob(g, path, title, data[int(table[blob_no]) : int(table[blob_no + 1])])


class _Strings:
    """Paths and titles of one scanned window (memory-mapped raw dirents)."""

    def __init__(self, scan: Scan, w: int) -> None:
        raw = scan.window_dir(w) / "dirents.bin"
        self.raw = np.memmap(raw, dtype=np.uint8, mode="r") if raw.stat().st_size else np.zeros(0, np.uint8)
        self.ps = scan.load(w, "path_start")
        self.pe = scan.load(w, "path_end")
        self.te = scan.load(w, "title_end")

    def path(self, local: int) -> str:
        return self.raw[int(self.ps[local]) : int(self.pe[local])].tobytes().decode("utf-8", "replace")

    def path_title(self, local: int) -> tuple[str, str]:
        path = self.path(local)
        start, end = int(self.pe[local]) + 1, int(self.te[local])
        title = self.raw[start:end].tobytes().decode("utf-8", "replace")
        return path, title or path


# --- redirects ------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RedirectTask:
    scan: Scan
    window: int
    start: int  # positions in the window's redirect list
    stop: int


def redirect_tasks(scan: Scan, *, size: int | None = None) -> list[RedirectTask]:
    size = size or REDIRECT_TASK
    tasks: list[RedirectTask] = []
    for w in scan.windows:
        n = int(scan.load(w, "redirects").shape[0])
        tasks.extend(RedirectTask(scan, w, s, min(s + size, n)) for s in range(0, n, size))
    return tasks


def read_redirects(task: RedirectTask) -> list[tuple[str, str]]:
    """Worker: ``(path, target path)`` of the task's redirects, in entry-index order."""
    scan = task.scan
    local = np.asarray(scan.load(task.window, "redirects")[task.start : task.stop]).astype(np.int64)
    targets = np.asarray(scan.load(task.window, "a"))[local].astype(np.int64)
    strings: dict[int, _Strings] = {task.window: _Strings(scan, task.window)}
    out: list[tuple[str, str]] = []
    fh: BinaryIO | None = None
    try:
        for loc, target in zip(local.tolist(), targets.tolist(), strict=True):
            path = strings[task.window].path(loc)
            if scan.first <= target < scan.stop:
                w, tl = divmod(target - scan.first, scan.window)
                if w not in strings:
                    strings[w] = _Strings(scan, w)
                out.append((path, strings[w].path(tl)))
            else:  # a target outside namespace C (not written by libzim): read its dirent directly
                if fh is None:
                    fh = scan.zim.open("rb")
                out.append((path, _dirent_path(fh, scan, target)))
    finally:
        if fh is not None:
            fh.close()
    return out


def _dirent_path(fh: BinaryIO, scan: Scan, index: int) -> str:
    if not 0 <= index < scan.header.entry_count:
        raise ZimFormatError(f"redirect to entry {index} of {scan.header.entry_count}")
    ptr = _pointer(fh, scan.header, index)
    fh.seek(ptr)
    head = fh.read(16)
    mime = int.from_bytes(head[:2], "little")
    start = 12 if mime == REDIRECT_MIME else (8 if mime >= DELETED_MIME else 16)
    fh.seek(ptr + start)
    data = b""
    while b"\0" not in data:
        chunk = fh.read(1024)
        if not chunk:
            raise ZimFormatError("unterminated path")
        data += chunk
    return data.split(b"\0", 1)[0].decode("utf-8", "replace")
