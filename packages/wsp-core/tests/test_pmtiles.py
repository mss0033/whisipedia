"""PMTiles v3 reading and writing (the Atlas's tilesets, 2026-10-18)."""

from __future__ import annotations

import random
from pathlib import Path

import pytest

from wsp_core.pmtiles import (
    Archive,
    Compression,
    Entry,
    PMTilesError,
    TileType,
    first_id,
    id_to_zxy,
    parse_directory,
    serialize_directory,
    write_archive,
    zxy_to_id,
)


def test_tile_ids_follow_the_spec() -> None:
    # The spec's and go-pmtiles' test vectors.
    for z, x, y, tile_id in [
        (0, 0, 0, 0),
        (1, 0, 0, 1),
        (1, 0, 1, 2),
        (1, 1, 1, 3),
        (1, 1, 0, 4),
        (2, 0, 0, 5),
    ]:
        assert zxy_to_id(z, x, y) == tile_id
        assert id_to_zxy(tile_id) == (z, x, y)
    assert zxy_to_id(12, 3423, 1763) == 19078479
    rng = random.Random(7)
    for _ in range(2000):
        z = rng.randint(0, 24)
        x, y = rng.randrange(1 << z), rng.randrange(1 << z)
        assert id_to_zxy(zxy_to_id(z, x, y)) == (z, x, y)
    assert first_id(13) == zxy_to_id(13, 0, 0)
    with pytest.raises(ValueError, match="out of range"):
        zxy_to_id(2, 4, 0)


def test_a_directory_round_trips_with_contiguous_offsets() -> None:
    entries = [Entry(0, 0, 10, 1), Entry(1, 10, 5, 3), Entry(9, 100, 7, 1), Entry(40, 0, 50, 0)]
    assert parse_directory(serialize_directory(entries)) == entries


def _tiles(zooms: range) -> list[tuple[int, int, int, bytes]]:
    out: list[tuple[int, int, int, bytes]] = []
    for z in zooms:
        for x in range(1 << z):
            for y in range(1 << z):
                out.append((z, x, y, f"{z}/{x}/{y}".encode() * 3))
    return out


def test_an_archive_reads_back_what_was_written(tmp_path: Path) -> None:
    path = tmp_path / "t.pmtiles"
    tiles = _tiles(range(0, 5))
    tiles.append((6, 10, 20, b"\x00" * 10))
    tiles.append((6, 11, 20, b"\x00" * 10))  # the same bytes: stored once, one run
    h = write_archive(path, tiles, tile_type=TileType.PNG, metadata={"attribution": "test"})
    assert (h.min_zoom, h.max_zoom) == (0, 6)
    assert h.tile_contents == len(tiles) - 1
    a = Archive.open_file(path)
    try:
        for z, x, y, blob in tiles:
            assert a.tile(z, x, y) == blob
        assert a.tile(5, 0, 0) is None  # a zoom not written
        assert a.tile(6, 12, 20) is None
        assert a.metadata() == {"attribution": "test"}
        assert a.header.tile_type is TileType.PNG
    finally:
        a.close()


def test_a_large_archive_uses_leaf_directories(tmp_path: Path) -> None:
    """The planet's root directory points at ~2,900 leaves (the Protomaps build of 2026-10-18)."""
    path = tmp_path / "big.pmtiles"
    rng = random.Random(3)
    tiles = [(9, x, y, rng.randbytes(rng.randint(20, 40))) for x in range(256) for y in range(256)]
    h = write_archive(path, tiles, tile_type=TileType.MVT, tile_compression=Compression.GZIP)
    assert h.leaf_length > 0
    a = Archive.open_file(path, leaf_cache=4)
    try:
        for z, x, y, blob in rng.sample(tiles, 300):
            assert a.tile(z, x, y) == blob
    finally:
        a.close()


def test_not_an_archive_is_refused(tmp_path: Path) -> None:
    junk = b"PK\x03\x04" + b"\x00" * 200
    with pytest.raises(PMTilesError):
        Archive(lambda off, n: junk[off : off + n])
    v2 = b"PMTiles\x02" + b"\x00" * 200
    with pytest.raises(PMTilesError, match="version 2"):
        Archive(lambda off, n: v2[off : off + n])
