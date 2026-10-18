"""Cut a part out of a remote PMTiles archive, resumably (``wsp maps fetch``; 2026-10-18).

A cut is a zoom window and/or a bounding box of an upstream archive (Mapterhorn's 356 GB planet in
parts of 30-85 GB, its regional archives up to z16). ``pmtiles extract`` did this in one go and lost
29 GB of a 30 GB cut to one HTTP/2 stream reset; this reads the same bytes in chunks that are
checkpointed as they land, so a failure costs one chunk and a rerun continues.

1. **Plan.** The archive's directories are walked (leaf directories outside the zoom window are
   never read) and the tiles inside the cut selected. Their byte spans in the upstream tile data are
   merged into chunks (gaps under :data:`GAP` bytes are read through, chunks stop at
   :data:`CHUNK_BYTES`). A clustered archive stores tiles in id order, lowest zoom first, so a zoom
   window is a few long runs of bytes.
2. **Fetch.** Each chunk is read with a ``Range`` request and written at its place in a staging file
   sized up front; a state file records the chunks done. The plan is recomputed on resume and must
   match the state's fingerprint.
3. **Assemble.** The selected tiles' entries, their offsets moved to the staging file's, become a new
   clustered archive (:func:`wsp_core.pmtiles.assemble_archive`) with the upstream metadata.
"""

from __future__ import annotations

import bisect
import hashlib
import json
import math
import os
import threading
from array import array
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from wsp_core.pmtiles import Archive, Entry, assemble_archive, first_id, id_to_zxy

GAP = 256 * 1024
CHUNK_BYTES = 64 * 1024 * 1024
THREADS = 4

Reader = Callable[[int, int], bytes]


@dataclass(frozen=True, slots=True)
class Window:
    """The tiles to keep: zooms ``min_zoom..max_zoom`` (None: the archive's) inside ``bbox``."""

    min_zoom: int | None = None
    max_zoom: int | None = None
    bbox: tuple[float, float, float, float] | None = None


def _tile_box(z: int, x: int, y: int) -> tuple[float, float, float, float]:
    n = 1 << z

    def lat(yy: int) -> float:
        return math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * yy / n))))

    return (x / n * 360 - 180, lat(y + 1), (x + 1) / n * 360 - 180, lat(y))


def _inside(tile_id: int, bbox: tuple[float, float, float, float] | None) -> bool:
    if bbox is None:
        return True
    w, s, e, n = _tile_box(*id_to_zxy(tile_id))
    return w < bbox[2] and bbox[0] < e and s < bbox[3] and bbox[1] < n


LEAF_SPAN_MAX = 512 * 1024 * 1024


def _preload_leaves(archive: Archive, lo: int, hi: int) -> None:
    """Read every root-level leaf directory the window needs in one request: leaves lie side by side,
    and one request per leaf made a planet's z11 cut spend minutes on directories (2026-10-18)."""
    root = archive.root
    wanted = [
        e
        for i, e in enumerate(root)
        if e.run_length == 0
        and not ((root[i + 1].tile_id if i + 1 < len(root) else math.inf) <= lo or e.tile_id >= hi)
    ]
    if len(wanted) < 8:
        return
    start = wanted[0].offset
    end = wanted[-1].offset + wanted[-1].length
    if end - start > LEAF_SPAN_MAX:
        return
    archive.preload_leaves(start, end - start)


@dataclass
class Plan:
    ids: array[int]
    offsets: array[int]  # in the upstream tile data
    lengths: array[int]
    runs: array[int]
    chunks: list[tuple[int, int]]  # (upstream start, length), by start
    bases: list[int]  # each chunk's offset in the staging file
    total: int

    @property
    def fingerprint(self) -> str:
        h = hashlib.sha256()
        h.update(json.dumps([len(self.ids), self.total, self.chunks[:3], self.chunks[-3:]]).encode())
        return h.hexdigest()[:16]

    def new_offset(self, upstream: int) -> int:
        i = bisect.bisect_right(self.chunks, upstream, key=lambda c: c[0]) - 1
        start, _length = self.chunks[i]
        return self.bases[i] + (upstream - start)


def plan_cut(archive: Archive, window: Window) -> Plan:
    """The tiles of ``archive`` inside ``window`` and the chunks of bytes that hold them."""
    h = archive.header
    lo_z = h.min_zoom if window.min_zoom is None else max(h.min_zoom, window.min_zoom)
    hi_z = h.max_zoom if window.max_zoom is None else min(h.max_zoom, window.max_zoom)
    lo, hi = first_id(lo_z), first_id(hi_z + 1)
    ids, offsets, lengths, runs = array("Q"), array("Q"), array("Q"), array("Q")

    def keep(tile_id: int, offset: int, length: int, run: int) -> None:
        ids.append(tile_id)
        offsets.append(offset)
        lengths.append(length)
        runs.append(run)

    def walk(entries: list[Entry], depth: int) -> None:
        if depth > 4:
            raise ValueError("directories nest too deep")
        for i, e in enumerate(entries):
            following = entries[i + 1].tile_id if i + 1 < len(entries) else math.inf
            if e.run_length == 0:  # a leaf directory: its tiles lie in [e.tile_id, following)
                if following <= lo or e.tile_id >= hi:
                    continue
                walk(archive.leaf(e.offset, e.length), depth + 1)
                continue
            first, last = max(e.tile_id, lo), min(e.tile_id + e.run_length, hi)
            if first >= last:
                continue
            if window.bbox is None:
                keep(first, e.offset, e.length, last - first)
                continue
            for tile_id in range(first, last):  # a run (repeated sea tiles): one entry per kept tile
                if _inside(tile_id, window.bbox):
                    keep(tile_id, e.offset, e.length, 1)

    _preload_leaves(archive, lo, hi)
    walk(archive.root, 0)
    if not ids:
        raise ValueError("the cut holds no tiles")
    spans = sorted({(o, n) for o, n in zip(offsets, lengths, strict=True)})
    chunks: list[tuple[int, int]] = []
    start, end = spans[0][0], spans[0][0] + spans[0][1]
    for o, n in spans[1:]:
        if o - end <= GAP and (max(end, o + n) - start) <= CHUNK_BYTES:
            end = max(end, o + n)
            continue
        chunks.append((start, end - start))
        start, end = o, o + n
    chunks.append((start, end - start))
    bases: list[int] = []
    total = 0
    for _start, length in chunks:
        bases.append(total)
        total += length
    return Plan(ids, offsets, lengths, runs, chunks, bases, total)


@dataclass(frozen=True, slots=True)
class CutResult:
    path: Path
    entries: int
    bytes: int
    chunks: int


def cut_archive(
    read: Reader,
    window: Window,
    dest: Path,
    work: Path,
    *,
    threads: int = THREADS,
    progress: Callable[[int, int, int], None] | None = None,
) -> CutResult:
    """Cut ``window`` out of the archive ``read`` reads into ``dest``, staging in ``work``; resumable.

    ``progress(chunks done, chunks, bytes done)`` is called as chunks land."""
    work.mkdir(parents=True, exist_ok=True)
    archive = Archive(read)
    plan = plan_cut(archive, window)
    data_offset = archive.header.data_offset
    staging = work / (dest.name + ".data")
    state_path = work / (dest.name + ".state.json")
    done: set[int] = set()
    if state_path.is_file() and staging.is_file():
        try:
            state = json.loads(state_path.read_text())
            if state.get("fingerprint") == plan.fingerprint and staging.stat().st_size == plan.total:
                done = {int(i) for i in state.get("done", [])}
        except (OSError, ValueError):
            done = set()
    if not done:
        with staging.open("wb") as f:
            f.truncate(plan.total)
    lock = threading.Lock()
    fd = os.open(staging, os.O_WRONLY)

    def save() -> None:
        tmp = state_path.with_name(state_path.name + ".tmp")
        tmp.write_text(json.dumps({"fingerprint": plan.fingerprint, "done": sorted(done)}))
        tmp.replace(state_path)

    def fetch(i: int) -> None:
        start, length = plan.chunks[i]
        blob = read(data_offset + start, length)
        if len(blob) != length:
            raise OSError(f"chunk {i}: {len(blob)} of {length} bytes")
        written = 0
        view = memoryview(blob)
        while written < length:
            written += os.pwrite(fd, view[written:], plan.bases[i] + written)
        with lock:
            done.add(i)
            if progress:
                progress(len(done), len(plan.chunks), sum(plan.chunks[j][1] for j in done))
            if len(done) % 8 == 0 or len(done) == len(plan.chunks):
                os.fsync(fd)
                save()

    try:
        todo = [i for i in range(len(plan.chunks)) if i not in done]
        with ThreadPoolExecutor(max_workers=max(1, threads), thread_name_prefix="mapcut") as pool:
            for future in [pool.submit(fetch, i) for i in todo]:
                future.result()
        os.fsync(fd)
    finally:
        os.close(fd)
        with lock:
            save()
    entries = sorted(
        (
            Entry(tile_id, plan.new_offset(off), length, run)
            for tile_id, off, length, run in zip(plan.ids, plan.offsets, plan.lengths, plan.runs, strict=True)
        ),
        key=lambda e: e.tile_id,
    )
    h = archive.header
    bbox = window.bbox or h.bounds
    bounds = (
        max(h.min_lon, bbox[0]),
        max(h.min_lat, bbox[1]),
        min(h.max_lon, bbox[2]),
        min(h.max_lat, bbox[3]),
    )
    assemble_archive(
        dest, entries, staging, data_length=plan.total, contents=len(set(plan.offsets)),
        tile_type=h.tile_type, tile_compression=h.tile_compression, metadata=archive.metadata(),
        bounds=bounds,
    )  # fmt: skip
    staging.unlink(missing_ok=True)
    state_path.unlink(missing_ok=True)
    return CutResult(dest, len(entries), dest.stat().st_size, len(plan.chunks))
