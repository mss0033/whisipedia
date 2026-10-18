"""``wsp maps``: map parts, cut checks and the gazetteer (2026-10-18)."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path

import pytest

from wsp_adapters.mapcut import Window, cut_archive
from wsp_adapters.maptiles import Cut, check_cut, map_parts
from wsp_cli.maps import _paced_fifo, read_gazetteer_places, reusable_cut  # pyright: ignore[reportPrivateUsage]
from wsp_core.gazetteer import Gazetteer, fts_query, place_rank, write_gazetteer
from wsp_core.mapsources import MapConfigError
from wsp_core.pmtiles import TileType, write_archive


def _item(
    name: str, md5: str, size: int, bounds: tuple[float, float, float, float], zooms: tuple[int, int]
) -> dict[str, object]:
    return {
        "name": name,
        "url": f"https://d.example/{name}",
        "md5sum": md5 * 32,
        "size": size,
        **dict(zip(("min_lon", "min_lat", "max_lon", "max_lat"), bounds, strict=True)),
        "min_zoom": zooms[0],
        "max_zoom": zooms[1],
    }


MAPTERHORN = {
    "version": 1,
    "items": [
        _item("planet.pmtiles", "a", 355, (-180, -85, 180, 85), (0, 12)),
        _item("6-14-26.pmtiles", "b", 264, (-101.25, 27.06, -95.6, 31.95), (13, 17)),
        _item("6-49-34.pmtiles", "c", 1, (95, -20, 101, -15), (13, 14)),
    ],
}


def access(discovery: Mapping[str, object], **extra: object) -> dict[str, object]:
    return {
        "adapter": "map-tiles",
        "tileset": "terrain",
        "tile_kind": "raster-dem",
        "discovery": discovery,
        **extra,
    }


def test_regional_archives_are_cut_at_the_zoom_cap_or_taken_whole() -> None:
    disc = {
        "kind": "pmtiles-list",
        "url": "https://d.example/list.json",
        "min_zoom": 13,
        "cut": {"max_zoom": 16},
    }
    parts = map_parts(access(disc), lambda url: json.dumps(MAPTERHORN).encode())
    assert [p.partition for p in parts] == ["6-14-26", "6-49-34"]  # the planet starts below z13
    cut, whole = parts
    assert cut.name == "6-14-26-z13-16.pmtiles"
    assert cut.cut == Cut(max_zoom=16)
    assert whole.name == "6-49-34.pmtiles"
    assert whole.cut is None  # already within the cap: MD5-checked whole
    assert whole.md5 == "c" * 32


def test_the_newest_protomaps_build_with_its_base64_md5() -> None:
    builds = [
        {"key": "20261017.pmtiles", "size": 1, "md5sum": "B12V1GnzC/+vVhBxePZHmg=="},
        {"key": "20261018.pmtiles", "size": 2, "md5sum": "1WAkMHEk9HsBNmYKkB6WDQ=="},
        {"key": "test.pmtiles", "size": 3},
    ]
    disc = {"kind": "protomaps-builds"}
    acc = access(disc, tileset="basemap", tile_kind="vector")
    (part,) = map_parts(acc, lambda url: json.dumps(builds).encode())
    assert (part.partition, part.name, part.version) == ("planet", "protomaps-20261018.pmtiles", "2026-10-18")
    assert part.url == "https://build.protomaps.com/20261018.pmtiles"
    assert part.md5 == "d56024307124f47b0136660a901e960d"  # hex of the base64 MD5


def test_explicit_parts_and_bad_entries() -> None:
    one = {"partition": "z12-e0", "url": "https://d.example/planet.pmtiles", "min_zoom": 12, "max_zoom": 12}
    disc = {"kind": "parts", "parts": [{**one, "bbox": [0, -85, 90, 85]}]}
    (part,) = map_parts(access(disc), lambda url: b"")
    assert part.cut is not None
    assert part.cut.args() == ["--minzoom=12", "--maxzoom=12", "--bbox=0,-85,90,85"]
    with pytest.raises(MapConfigError, match="tile_kind"):
        map_parts(access(disc, tile_kind="mesh"), lambda url: b"")
    bad = {"kind": "parts", "parts": [{"partition": "x", "url": "https://d.example/p", "bbox": [9, 0, 1, 1]}]}
    with pytest.raises(MapConfigError, match="bbox"):
        map_parts(access(bad), lambda url: b"")


def test_a_cut_is_checked_against_its_zooms_and_the_reported_entries(tmp_path: Path) -> None:
    path = tmp_path / "cut.pmtiles"
    write_archive(path, [(13, 0, 0, b"a"), (14, 0, 0, b"b")], tile_type=TileType.WEBP)
    assert check_cut(path, Cut(max_zoom=14), 2) is None
    assert "above the cut" in (check_cut(path, Cut(max_zoom=13), 2) or "")
    assert "reported 3" in (check_cut(path, Cut(max_zoom=14), 3) or "")
    (tmp_path / "junk.pmtiles").write_bytes(b"<html>")
    assert "not a readable archive" in (check_cut(tmp_path / "junk.pmtiles", Cut(max_zoom=14), None) or "")


def test_a_finished_cut_is_reused_and_a_half_built_or_foreign_one_is_not(tmp_path: Path) -> None:
    """A lane stopped while copying a 60 GB cut into _incoming must not cut it again (2026-10-18)."""
    local = tmp_path / "planet-z11.pmtiles"
    write_archive(local, [(11, 0, 0, b"a")], tile_type=TileType.WEBP, bounds=(-180, -85, -90, 85))
    assert reusable_cut(local, Cut(min_zoom=11, max_zoom=11, bbox=(-180, -85.0511, -90, 85.0511)))
    assert not reusable_cut(local, Cut(min_zoom=12, max_zoom=12))  # another part's zooms
    assert not reusable_cut(local, Cut(max_zoom=11, bbox=(0, -85, 90, 85)))  # another window
    (tmp_path / "planet-z11.pmtiles.building").write_bytes(b"")
    assert not reusable_cut(local, Cut(max_zoom=11))  # a cut in progress
    assert not reusable_cut(tmp_path / "none.pmtiles", Cut(max_zoom=11))


PLANET = [
    (
        1,
        (7.8632, 46.6863),
        {
            "name": "Interlaken",
            "place": "town",
            "population": "5,700",
            "wikipedia": "en:Interlaken",
            "wikidata": "Q68968",
            "name:ja": "インターラーケン",
        },
    ),
    (2, (7.9617, 46.5375), {"name": "Jungfrau", "natural": "peak", "wikidata": "Q15283", "ele": "4158"}),
    (3, (7.85, 46.69), {"name": "Bench", "amenity": "bench"}),
    (4, (-73.88, 42.62), {"name": "Interlaken", "place": "village"}),
    (5, (8.54, 47.37), {"name": "Zürich", "place": "city", "population": "421878", "name:en": "Zurich"}),
]  # fmt: skip  (id, (lon, lat), tags)


def _planet(path: Path) -> None:
    import osmium
    import osmium.osm.mutable

    with osmium.SimpleWriter(str(path)) as w:
        for node_id, where, tags in PLANET:
            w.add_node(osmium.osm.mutable.Node(id=node_id, location=where, tags=tags))


def test_the_gazetteer_finds_places_by_name_prefix_and_nearby(tmp_path: Path) -> None:
    pbf = tmp_path / "planet.osm.pbf"
    _planet(pbf)
    # Read the way the command does: through a paced FIFO.
    fifo = tmp_path / "fifo.osm.pbf"
    os.mkfifo(fifo)
    pump = _paced_fifo(pbf, fifo, 50.0)
    places = list(read_gazetteer_places(str(fifo)))
    assert pump.wait(timeout=10) == 0
    assert {p.osm for p in places} == {"node/1", "node/2", "node/4", "node/5"}  # the bench is neither
    db = tmp_path / "places.sqlite"
    assert write_gazetteer(db, places) == 4
    gz = Gazetteer(db)
    found = gz.search("interl")
    assert [p.osm for p in found] == ["node/1", "node/4"]  # the town of 5,700 before the village
    assert found[0].wikipedia == "en:Interlaken"
    assert gz.search("zurich")[0].name == "Zürich"  # diacritics folded
    assert gz.search("インターラーケン")[0].osm == "node/1"  # an alias
    assert gz.search("") == []
    near = gz.nearby(46.69, 7.86, radius_m=20_000)
    assert [p.osm for p in near] == ["node/1", "node/2"]
    assert near[0].distance_m is not None
    assert near[0].distance_m < 1000
    assert place_rank("place=city", 421_878, False) > place_rank("place=town", 5_700, True)
    assert fts_query('new "yo') == '"new" "yo"*'


def _source(tmp_path: Path) -> Path:
    import random

    rng = random.Random(5)
    tiles = [
        (z, x, y, rng.randbytes(rng.randint(30, 300)))
        for z in range(0, 8)
        for x in range(1 << z)
        for y in range(1 << z)
    ]
    tiles += [(8, x, 0, b"sea") for x in range(256)]  # one stored tile, run-length encoded
    src = tmp_path / "src.pmtiles"
    write_archive(src, tiles, tile_type=TileType.WEBP, metadata={"attribution": "test"})
    return src


def test_a_cut_keeps_exactly_the_window_and_its_bytes(tmp_path: Path) -> None:
    from wsp_core.pmtiles import Archive

    src = _source(tmp_path)
    data = src.read_bytes()
    reads: list[int] = []

    def read(off: int, n: int) -> bytes:
        reads.append(n)
        return data[off : off + n]

    window = Window(min_zoom=6, max_zoom=8, bbox=(0.0, 0.0, 90.0, 85.0))
    result = cut_archive(read, window, tmp_path / "cut.pmtiles", tmp_path / "work")
    a, s = Archive.open_file(tmp_path / "cut.pmtiles"), Archive.open_file(src)
    try:
        assert (a.header.min_zoom, a.header.max_zoom) == (6, 8)
        assert a.metadata() == {"attribution": "test"}
        assert result.entries == a.header.tile_entries
        for z in (6, 7, 8):
            n = 1 << z
            for x in range(n):
                for y in range(n):
                    west, east = x / n * 360 - 180, (x + 1) / n * 360 - 180
                    north_half = y < n // 2  # tiles wholly south of the equator are outside
                    want = s.tile(z, x, y) if (east > 0 and west < 90 and north_half) else None
                    assert a.tile(z, x, y) == want, (z, x, y)
        assert a.tile(5, 16, 10) is None  # below the window
    finally:
        a.close()
        s.close()
    assert check_cut(tmp_path / "cut.pmtiles", Cut(min_zoom=6, max_zoom=8), result.entries) is None
    assert not (tmp_path / "work" / "cut.pmtiles.building").exists()  # built in place, then renamed


def test_a_cut_that_fails_halfway_resumes_where_it_stopped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import wsp_adapters.mapcut as mapcut

    monkeypatch.setattr(mapcut, "CHUNK_BYTES", 2048)
    monkeypatch.setattr(mapcut, "GAP", 0)
    src = _source(tmp_path)
    data = src.read_bytes()
    calls = {"n": 0, "fail_after": 12}

    def flaky(off: int, n: int) -> bytes:
        calls["n"] += 1
        if calls["fail_after"] and calls["n"] > calls["fail_after"]:
            raise OSError("stream reset")
        return data[off : off + n]

    window = Window(min_zoom=0, max_zoom=7)
    with pytest.raises(OSError, match="stream reset"):
        cut_archive(flaky, window, tmp_path / "cut.pmtiles", tmp_path / "work", threads=1)
    state = json.loads((tmp_path / "work" / "cut.pmtiles.state.json").read_text())
    assert state["done"]  # some chunks landed and were recorded
    fetched_before = len(state["done"])
    calls.update(n=0, fail_after=0)
    seen: list[int] = []

    def counting(off: int, n: int) -> bytes:
        seen.append(off)
        return data[off : off + n]

    result = cut_archive(counting, window, tmp_path / "cut.pmtiles", tmp_path / "work", threads=1)
    plan_chunks = result.chunks
    # The rerun read the directories again but only the chunks not yet done.
    assert len(seen) < plan_chunks + 10
    assert plan_chunks - fetched_before <= len(seen)
    from wsp_core.pmtiles import Archive

    a, s = Archive.open_file(tmp_path / "cut.pmtiles"), Archive.open_file(src)
    try:
        for z in range(0, 8):
            for x in range(1 << z):
                for y in range(1 << z):
                    assert a.tile(z, x, y) == s.tile(z, x, y)
    finally:
        a.close()
        s.close()
