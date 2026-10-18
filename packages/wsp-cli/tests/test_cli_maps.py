"""``wsp maps``: map parts, cut checks and the gazetteer (2026-10-18)."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path

import pytest

from wsp_adapters.maptiles import Cut, check_cut, expected_entries, map_parts
from wsp_cli.maps import _paced_fifo, read_gazetteer_places  # pyright: ignore[reportPrivateUsage]
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
    log = "extract.go:441: Region tiles 9, result tile entries 2\n"
    assert expected_entries(log) == 2
    assert check_cut(path, Cut(max_zoom=14), 2) is None
    assert "above the cut" in (check_cut(path, Cut(max_zoom=13), 2) or "")
    assert "reported 3" in (check_cut(path, Cut(max_zoom=14), 3) or "")
    (tmp_path / "junk.pmtiles").write_bytes(b"<html>")
    assert "not a readable archive" in (check_cut(tmp_path / "junk.pmtiles", Cut(max_zoom=14), None) or "")


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
