"""PMTiles v3: one file of map tiles addressed by z/x/y (https://github.com/protomaps/PMTiles, spec v3).

The Atlas's tilesets (``docs/components/maps.md``) are PMTiles archives in the vault: the OpenStreetMap
basemap (Protomaps), terrain (Mapterhorn) and imagery. :class:`Archive` reads one through a
``read(offset, length)`` callable, so the same code serves a local file, a file on the NAS or (in
tests) bytes in memory. :func:`write_archive` writes one; the imagery renderer and the tests use it.

Layout: a 127-byte header; a root directory; optional metadata (JSON); leaf directories; tile data.
A directory is a list of entries (tile id, offset, length, run length) stored column by column as
varints, the tile ids and (for contiguous tiles) the offsets as deltas, compressed as a whole
(``internal_compression``). An entry with run length 0 points at a leaf directory instead of a tile.
Tile ids number every tile of every zoom along a Hilbert curve, zoom 0 first, so a clustered archive
holds a zoom's tiles after all the tiles of lower zooms.
"""

from __future__ import annotations

import bisect
import gzip
import json
import struct
import threading
from collections import OrderedDict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path
from typing import BinaryIO, cast

MAGIC = b"PMTiles"
HEADER_LEN = 127
MAX_ZOOM = 31  # tile ids are 64 bits: 4**z tiles per zoom fit up to z31


class Compression(IntEnum):
    UNKNOWN = 0
    NONE = 1
    GZIP = 2
    BROTLI = 3
    ZSTD = 4


class TileType(IntEnum):
    UNKNOWN = 0
    MVT = 1
    PNG = 2
    JPEG = 3
    WEBP = 4
    AVIF = 5


MEDIA_TYPES: Mapping[TileType, str] = {
    TileType.MVT: "application/vnd.mapbox-vector-tile",
    TileType.PNG: "image/png",
    TileType.JPEG: "image/jpeg",
    TileType.WEBP: "image/webp",
    TileType.AVIF: "image/avif",
}


class PMTilesError(ValueError):
    """Not a readable PMTiles v3 archive."""


@dataclass(frozen=True, slots=True)
class Header:
    root_offset: int
    root_length: int
    metadata_offset: int
    metadata_length: int
    leaf_offset: int
    leaf_length: int
    data_offset: int
    data_length: int
    addressed_tiles: int
    tile_entries: int
    tile_contents: int
    clustered: bool
    internal_compression: Compression
    tile_compression: Compression
    tile_type: TileType
    min_zoom: int
    max_zoom: int
    min_lon: float
    min_lat: float
    max_lon: float
    max_lat: float
    center_zoom: int
    center_lon: float
    center_lat: float

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        return (self.min_lon, self.min_lat, self.max_lon, self.max_lat)


_HEADER = struct.Struct("<7sB11QBBBBBBiiiiBii")


def parse_header(raw: bytes) -> Header:
    if len(raw) < HEADER_LEN or raw[:7] != MAGIC:
        raise PMTilesError("not a PMTiles archive")
    if raw[7] != 3:
        raise PMTilesError(f"PMTiles version {raw[7]} is not supported (only 3)")
    f = _HEADER.unpack(raw[:HEADER_LEN])
    try:
        return Header(
            root_offset=f[2], root_length=f[3], metadata_offset=f[4], metadata_length=f[5],
            leaf_offset=f[6], leaf_length=f[7], data_offset=f[8], data_length=f[9],
            addressed_tiles=f[10], tile_entries=f[11], tile_contents=f[12],
            clustered=bool(f[13]), internal_compression=Compression(f[14]),
            tile_compression=Compression(f[15]), tile_type=TileType(f[16]),
            min_zoom=f[17], max_zoom=f[18],
            min_lon=f[19] / 1e7, min_lat=f[20] / 1e7, max_lon=f[21] / 1e7, max_lat=f[22] / 1e7,
            center_zoom=f[23], center_lon=f[24] / 1e7, center_lat=f[25] / 1e7,
        )  # fmt: skip
    except ValueError as exc:  # an unknown compression or tile type
        raise PMTilesError(str(exc)) from None


def serialize_header(h: Header) -> bytes:
    return _HEADER.pack(
        MAGIC, 3, h.root_offset, h.root_length, h.metadata_offset, h.metadata_length,
        h.leaf_offset, h.leaf_length, h.data_offset, h.data_length, h.addressed_tiles,
        h.tile_entries, h.tile_contents, int(h.clustered), h.internal_compression,
        h.tile_compression, h.tile_type, h.min_zoom, h.max_zoom,
        round(h.min_lon * 1e7), round(h.min_lat * 1e7), round(h.max_lon * 1e7), round(h.max_lat * 1e7),
        h.center_zoom, round(h.center_lon * 1e7), round(h.center_lat * 1e7),
    )  # fmt: skip


# --- tile ids -----------------------------------------------------------------------------------


def zxy_to_id(z: int, x: int, y: int) -> int:
    """The tile id of z/x/y: the tiles of every lower zoom, then the Hilbert index within ``z``."""
    if not 0 <= z <= MAX_ZOOM:
        raise ValueError(f"zoom {z} out of range")
    n = 1 << z
    if not (0 <= x < n and 0 <= y < n):
        raise ValueError(f"tile {z}/{x}/{y} out of range")
    acc = ((1 << (2 * z)) - 1) // 3
    d = 0
    s = n >> 1
    while s > 0:
        rx = 1 if x & s else 0
        ry = 1 if y & s else 0
        d += s * s * ((3 * rx) ^ ry)
        if ry == 0:  # rotate the quadrant
            if rx == 1:
                x = s - 1 - (x & (s - 1))
                y = s - 1 - (y & (s - 1))
            x, y = y, x
        s >>= 1
    return acc + d


def id_to_zxy(tile_id: int) -> tuple[int, int, int]:
    z = 0
    acc = 0
    while True:
        count = 1 << (2 * z)
        if tile_id < acc + count:
            break
        acc += count
        z += 1
        if z > MAX_ZOOM:
            raise ValueError(f"tile id {tile_id} out of range")
    d = tile_id - acc
    x = y = 0
    s = 1
    n = 1 << z
    while s < n:
        rx = 1 & (d // 2)
        ry = 1 & (d ^ rx)
        if ry == 0:
            if rx == 1:
                x = s - 1 - x
                y = s - 1 - y
            x, y = y, x
        x += s * rx
        y += s * ry
        d //= 4
        s *= 2
    return z, x, y


def first_id(z: int) -> int:
    """The id of the first tile of zoom ``z`` (every tile of a lower zoom has a smaller id)."""
    return ((1 << (2 * z)) - 1) // 3


# --- directories --------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Entry:
    tile_id: int
    offset: int
    length: int
    run_length: int  # 0: a leaf directory


def _read_varint(buf: bytes, pos: int) -> tuple[int, int]:
    result = shift = 0
    while True:
        if pos >= len(buf):
            raise PMTilesError("directory ends inside a varint")
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result, pos
        shift += 7
        if shift > 63:
            raise PMTilesError("varint too long")


def _write_varint(out: bytearray, value: int) -> None:
    while value >= 0x80:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value)


def decompress(data: bytes, how: Compression) -> bytes:
    if how in (Compression.NONE, Compression.UNKNOWN):
        return data
    if how is Compression.GZIP:
        return gzip.decompress(data)
    raise PMTilesError(f"{how.name.lower()} compression is not supported")


def parse_directory(raw: bytes) -> list[Entry]:
    n, pos = _read_varint(raw, 0)
    if n > len(raw):  # every entry takes at least four bytes: a corrupt count
        raise PMTilesError("directory entry count exceeds its size")
    ids: list[int] = []
    last = 0
    for _ in range(n):
        delta, pos = _read_varint(raw, pos)
        last += delta
        ids.append(last)
    runs: list[int] = []
    for _ in range(n):
        v, pos = _read_varint(raw, pos)
        runs.append(v)
    lengths: list[int] = []
    for _ in range(n):
        v, pos = _read_varint(raw, pos)
        lengths.append(v)
    entries: list[Entry] = []
    for i in range(n):
        v, pos = _read_varint(raw, pos)
        offset = entries[i - 1].offset + entries[i - 1].length if v == 0 and i > 0 else v - 1
        entries.append(Entry(ids[i], offset, lengths[i], runs[i]))
    return entries


def serialize_directory(entries: list[Entry]) -> bytes:
    out = bytearray()
    _write_varint(out, len(entries))
    last = 0
    for e in entries:
        _write_varint(out, e.tile_id - last)
        last = e.tile_id
    for e in entries:
        _write_varint(out, e.run_length)
    for e in entries:
        _write_varint(out, e.length)
    for i, e in enumerate(entries):
        if i > 0 and e.offset == entries[i - 1].offset + entries[i - 1].length:
            _write_varint(out, 0)
        else:
            _write_varint(out, e.offset + 1)
    return bytes(out)


def find_entry(entries: list[Entry], tile_id: int) -> Entry | None:
    """The entry holding ``tile_id``: a tile entry whose run covers it, or the leaf it is in."""
    i = bisect.bisect_right(entries, tile_id, key=lambda e: e.tile_id) - 1
    if i < 0:
        return None
    e = entries[i]
    if e.run_length == 0:
        return e
    return e if tile_id < e.tile_id + e.run_length else None


# --- reading ------------------------------------------------------------------------------------

Reader = Callable[[int, int], bytes]
LEAF_CACHE = 256  # leaf directories kept per archive (a planet leaf is ~10-60 KB decoded)
MAX_DEPTH = 4  # root -> leaf -> leaf ...: the spec writes at most root + one level


class Archive:
    """One PMTiles archive read through ``read(offset, length)``. Thread-safe; leaf directories are
    cached (LRU, ``leaf_cache`` of them)."""

    def __init__(self, read: Reader, *, leaf_cache: int = LEAF_CACHE) -> None:
        self._read = read
        head = read(0, 16384)  # the header and, written by every known tool, the root directory
        self.header = parse_header(head)
        h = self.header
        if h.root_offset + h.root_length <= len(head):
            root = head[h.root_offset : h.root_offset + h.root_length]
        else:
            root = read(h.root_offset, h.root_length)
        self.root = parse_directory(decompress(root, h.internal_compression))
        self._leaves: OrderedDict[tuple[int, int], list[Entry]] = OrderedDict()
        self._leaf_cache = leaf_cache
        self._lock = threading.Lock()

    @classmethod
    def open_file(cls, path: Path, **kwargs: int) -> Archive:
        """An archive on disk; each read opens nothing new (one handle, positioned reads)."""
        fh = path.open("rb")
        archive = cls(lambda off, n: _pread(fh, off, n), **kwargs)
        archive._file = fh  # type: ignore[attr-defined]  # kept open with the archive
        return archive

    def metadata(self) -> dict[str, object]:
        h = self.header
        if not h.metadata_length:
            return {}
        raw = decompress(self._read(h.metadata_offset, h.metadata_length), h.internal_compression)
        try:
            found: object = json.loads(raw)
        except ValueError:
            return {}
        return (
            {str(k): v for k, v in cast(dict[object, object], found).items()}
            if isinstance(found, dict)
            else {}
        )

    def _leaf(self, offset: int, length: int) -> list[Entry]:
        key = (offset, length)
        with self._lock:
            hit = self._leaves.get(key)
            if hit is not None:
                self._leaves.move_to_end(key)
                return hit
        raw = self._read(self.header.leaf_offset + offset, length)
        entries = parse_directory(decompress(raw, self.header.internal_compression))
        with self._lock:
            self._leaves[key] = entries
            while len(self._leaves) > self._leaf_cache:
                self._leaves.popitem(last=False)
        return entries

    def locate(self, z: int, x: int, y: int) -> tuple[int, int] | None:
        """(offset, length) of z/x/y's bytes in the archive, or None if it holds no such tile."""
        h = self.header
        if not h.min_zoom <= z <= h.max_zoom:
            return None
        tile_id = zxy_to_id(z, x, y)
        entries = self.root
        for _ in range(MAX_DEPTH):
            e = find_entry(entries, tile_id)
            if e is None:
                return None
            if e.run_length > 0:
                return h.data_offset + e.offset, e.length
            entries = self._leaf(e.offset, e.length)
        raise PMTilesError("directories nest too deep")

    def tile(self, z: int, x: int, y: int) -> bytes | None:
        """The tile's bytes as stored (still ``tile_compression``-compressed), or None."""
        where = self.locate(z, x, y)
        return None if where is None else self._read(*where)

    def close(self) -> None:
        fh = getattr(self, "_file", None)
        if fh is not None:
            fh.close()


def _pread(fh: BinaryIO, offset: int, length: int) -> bytes:
    import os

    return os.pread(fh.fileno(), length, offset)


# --- writing ------------------------------------------------------------------------------------

ROOT_MAX = 16384 - HEADER_LEN  # the root directory fits in the first 16 KiB with the header
LEAF_SIZE = 4096  # entries per leaf when the root alone does not fit


def _directories(entries: list[Entry]) -> tuple[bytes, bytes]:
    """(root, leaves), gzip-compressed: one root if it fits, else a root of leaf pointers."""
    root = gzip.compress(serialize_directory(entries), mtime=0)
    if len(root) <= ROOT_MAX:
        return root, b""
    size = LEAF_SIZE
    while True:
        leaves = bytearray()
        pointers: list[Entry] = []
        for i in range(0, len(entries), size):
            chunk = gzip.compress(serialize_directory(entries[i : i + size]), mtime=0)
            pointers.append(Entry(entries[i].tile_id, len(leaves), len(chunk), 0))
            leaves += chunk
        root = gzip.compress(serialize_directory(pointers), mtime=0)
        if len(root) <= ROOT_MAX:
            return root, bytes(leaves)
        size *= 2


def write_archive(
    path: Path,
    tiles: Iterable[tuple[int, int, int, bytes]],
    *,
    tile_type: TileType,
    tile_compression: Compression = Compression.NONE,
    metadata: Mapping[str, object] | None = None,
    bounds: tuple[float, float, float, float] = (-180.0, -85.0511287798066, 180.0, 85.0511287798066),
) -> Header:
    """Write a clustered archive of ``tiles`` (z, x, y, bytes in ``tile_compression``). Identical
    tiles are stored once; tile bytes are staged in a temporary file beside ``path``, so memory holds
    only the directory."""
    staged = path.with_name(path.name + ".data")
    entries: list[Entry] = []
    seen: dict[bytes, tuple[int, int]] = {}
    offset = 0
    contents = 0
    try:
        with staged.open("wb") as data:
            for tile_id, blob in sorted(
                ((zxy_to_id(z, x, y), b) for z, x, y, b in tiles), key=lambda t: t[0]
            ):
                if entries and entries[-1].tile_id == tile_id:
                    raise ValueError(f"tile {id_to_zxy(tile_id)} given twice")
                key = blob if len(blob) <= 4096 else b""  # small tiles (ocean, empty) repeat most
                if key and key in seen:
                    off, length = seen[key]
                    last = entries[-1] if entries else None
                    if last and last.offset == off and last.tile_id + last.run_length == tile_id:
                        entries[-1] = Entry(last.tile_id, off, length, last.run_length + 1)
                        continue
                    entries.append(Entry(tile_id, off, length, 1))
                    continue
                data.write(blob)
                entries.append(Entry(tile_id, offset, len(blob), 1))
                if key:
                    seen[key] = (offset, len(blob))
                offset += len(blob)
                contents += 1
        if not entries:
            raise ValueError("an archive needs at least one tile")
        root, leaves = _directories(entries)
        meta = gzip.compress(json.dumps(dict(metadata or {})).encode(), mtime=0)
        zooms = [id_to_zxy(e.tile_id)[0] for e in (entries[0], entries[-1])]
        lon = (bounds[0] + bounds[2]) / 2
        lat = (bounds[1] + bounds[3]) / 2
        h = Header(
            root_offset=HEADER_LEN, root_length=len(root),
            metadata_offset=HEADER_LEN + len(root), metadata_length=len(meta),
            leaf_offset=HEADER_LEN + len(root) + len(meta), leaf_length=len(leaves),
            data_offset=HEADER_LEN + len(root) + len(meta) + len(leaves), data_length=offset,
            addressed_tiles=sum(e.run_length for e in entries), tile_entries=len(entries),
            tile_contents=contents, clustered=True, internal_compression=Compression.GZIP,
            tile_compression=tile_compression, tile_type=tile_type,
            min_zoom=zooms[0], max_zoom=zooms[1],
            min_lon=bounds[0], min_lat=bounds[1], max_lon=bounds[2], max_lat=bounds[3],
            center_zoom=zooms[0], center_lon=lon, center_lat=lat,
        )  # fmt: skip
        tmp = path.with_name(path.name + ".tmp")
        with tmp.open("wb") as out, staged.open("rb") as data:
            out.write(serialize_header(h))
            out.write(root)
            out.write(meta)
            out.write(leaves)
            while chunk := data.read(1 << 20):
                out.write(chunk)
        tmp.replace(path)
        return h
    finally:
        staged.unlink(missing_ok=True)
