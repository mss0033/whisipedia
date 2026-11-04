"""The Atlas's tiles (``/v1/maps``, 2026-10-18): tilesets from map sources' archives."""

from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

from wsp_api.errors import install_error_handlers
from wsp_api.maps import decode_elevation, encode_elevation, maps_router
from wsp_core.gazetteer import Place, write_gazetteer
from wsp_core.model import Artifact, Generation, GenerationStatus, SourceSpec, SourceStatus, Tier
from wsp_core.pmtiles import Archive, Compression, TileType, write_archive


def spec(sid: str, tileset: str, kind: str, priority: int = 0) -> SourceSpec:
    access: dict[str, Any] = {
        "adapter": "map-tiles",
        "partitioned": True,
        "tileset": tileset,
        "tile_kind": kind,
        "priority": priority,
        "attribution": f"© {sid}",
        "discovery": {"kind": "parts", "parts": []},
    }
    return SourceSpec(
        id=sid,
        name=sid,
        tier=Tier.T1,
        status=SourceStatus.CANDIDATE,
        license="x",
        legal_basis="open-license",
        domain=("maps",),
        adapter=access["adapter"],
        raw={"access": access},
    )


@dataclass
class FakeVault:
    root: Path
    held: dict[str, list[str]]  # source -> relpaths in one generation "g"

    def _gen(self, sid: str) -> Generation:
        arts = tuple(
            Artifact(sid, "g", PurePosixPath(rel), 1, "0" * 64, "0" * 64) for rel in self.held.get(sid, [])
        )
        return Generation(sid, "g", GenerationStatus.SEALED, arts, datetime(2026, 10, 18, tzinfo=UTC))

    def current_set(self, sid: str) -> list[Generation]:
        return [self._gen(sid)] if sid in self.held else []

    def current(self, sid: str) -> Generation | None:
        return self._gen(sid) if sid in self.held else None

    def generation_path(self, sid: str, gid: str) -> Path:
        return self.root / sid / gid


def terrain_tile(metres: float, size: int = 64) -> bytes:
    e = np.full((size, size), metres, dtype=np.float64)
    buf = io.BytesIO()
    Image.fromarray(encode_elevation(e, "terrarium"), "RGB").save(buf, "WEBP", lossless=True)
    return buf.getvalue()


def elevation(blob: bytes) -> np.ndarray[Any, np.dtype[np.float64]]:
    with Image.open(io.BytesIO(blob)) as img:
        return decode_elevation(np.asarray(img.convert("RGB"), dtype=np.float64), "terrarium")


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    def place(sid: str, name: str) -> Path:
        d = tmp_path / sid / "g" / "upstream"
        d.mkdir(parents=True, exist_ok=True)
        return d / name

    # World terrain z0-2 at 100 m; regional z3-4 at 250 m over one z3 tile (x=4, y=2: east of 0°, north).
    world = [(z, x, y, terrain_tile(100.0)) for z in range(3) for x in range(1 << z) for y in range(1 << z)]
    write_archive(place("terrain-world", "w.pmtiles"), world, tile_type=TileType.WEBP)
    regional = [(3, 4, 2, terrain_tile(250.0))] + [
        (4, x, y, terrain_tile(250.0)) for x in (8, 9) for y in (4, 5)
    ]
    write_archive(
        place("terrain-regional", "r.pmtiles"),
        regional,
        tile_type=TileType.WEBP,
        bounds=(0.0, 40.97, 45.0, 66.51),
    )
    import gzip

    vector = [(0, 0, 0, gzip.compress(b"mvt-0")), (1, 0, 0, gzip.compress(b"mvt-1"))]
    write_archive(
        place("basemap", "b.pmtiles"), vector, tile_type=TileType.MVT, tile_compression=Compression.GZIP
    )
    with zipfile.ZipFile(place("maps-assets", "assets.zip"), "w") as z:
        z.writestr("basemaps-assets-main/fonts/Noto Sans Regular/0-255.pbf", b"glyphs")
        z.writestr("basemaps-assets-main/sprites/v4/light.json", b"{}")
        z.writestr("basemaps-assets-main/sprites/v4/light.png", b"png")
    vault = FakeVault(
        tmp_path,
        {
            "terrain-world": ["upstream/w.pmtiles"],
            "terrain-regional": ["upstream/r.pmtiles"],
            "basemap": ["upstream/b.pmtiles"],
            "maps-assets": ["upstream/assets.zip"],
        },
    )
    specs = [
        spec("terrain-world", "terrain", "raster-dem"),
        spec("terrain-regional", "terrain", "raster-dem", priority=10),
        spec("basemap", "basemap", "vector"),
    ]
    gaz = tmp_path / "gazetteer" / "places.sqlite"
    write_gazetteer(
        gaz,
        [
            Place(
                "node/1", "Interlaken", "place=town", 46.6863, 7.8632, 60.0, 5700, "Q68968", "en:Interlaken"
            ),
            Place("node/2", "Jungfrau", "natural=peak", 46.5375, 7.9617, 25.0, None, "Q15283", "en:Jungfrau"),
        ],
    )
    lib = SimpleNamespace(
        s=SimpleNamespace(sources=specs), vault=vault, settings=SimpleNamespace(gazetteer=gaz)
    )
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(maps_router(lib))  # type: ignore[arg-type]
    return TestClient(app)


def test_tilesets_describe_what_is_held(client: TestClient) -> None:
    body = client.get("/v1/maps/tilesets").json()
    sets = {t["id"]: t for t in body["tilesets"]}
    assert set(sets) == {"basemap", "terrain"}
    terrain = sets["terrain"]
    assert (terrain["kind"], terrain["encoding"], terrain["archives"]) == ("raster-dem", "terrarium", 2)
    assert terrain["held_max_zoom"] == 4
    assert terrain["max_zoom"] == 16  # drawn deeper: cut from what is held
    assert terrain["tile_size"] == 64
    assert "© terrain-regional" in terrain["attribution"]
    assert sets["basemap"]["tiles"] == ["/v1/maps/tiles/basemap/{z}/{x}/{y}"]
    assert body["fonts"] == ["Noto Sans Regular"]
    assert body["glyphs"] == "/v1/maps/glyphs/{fontstack}/{range}.pbf"
    assert body["sprite"] == "/v1/maps/sprites/v4"


def test_the_regional_archive_wins_where_it_holds_the_tile(client: TestClient) -> None:
    r = client.get("/v1/maps/tiles/terrain/4/8/4")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/webp"
    assert np.allclose(elevation(r.content), 250.0, atol=0.01)


def test_a_tile_only_coarser_data_covers_is_cut_from_it_in_metres(client: TestClient) -> None:
    """Outside the regional archive, z5 comes from the world's z2 (three zooms up), still 100 m:
    interpolating Terrarium's RGB bytes instead would have broken values across a channel carry."""
    r = client.get("/v1/maps/tiles/terrain/5/3/30")
    assert r.status_code == 200
    e = elevation(r.content)
    assert e.shape == (64, 64)
    assert np.allclose(e, 100.0, atol=0.01)
    deep = client.get("/v1/maps/tiles/terrain/10/100/900")  # 8 zooms below any held tile
    assert deep.status_code == 204


def test_terrarium_round_trips_to_the_256th_of_a_metre() -> None:
    e = np.array([[-11034.0, -0.5, 0.0, 8848.86, 1234.567]], dtype=np.float64)
    back = decode_elevation(encode_elevation(e, "terrarium").astype(np.float64), "terrarium")
    assert np.allclose(back, e, atol=1 / 256)
    shallow = e[:, 1:]  # Mapbox RGB starts at -10,000 m
    back = decode_elevation(encode_elevation(shallow, "mapbox").astype(np.float64), "mapbox")
    assert np.allclose(back, shallow, atol=0.1)


def test_vector_tiles_stay_gzipped_and_a_missing_one_is_empty(client: TestClient) -> None:
    r = client.get("/v1/maps/tiles/basemap/1/0/0")
    assert r.status_code == 200
    assert r.content == b"mvt-1"  # the client undoes the Content-Encoding
    assert r.headers["content-type"] == "application/vnd.mapbox-vector-tile"
    assert client.get("/v1/maps/tiles/basemap/1/1/1").status_code == 204
    assert client.get("/v1/maps/tiles/nope/0/0/0").status_code == 404
    assert client.get("/v1/maps/tiles/basemap/1/2/0").status_code == 400


def test_fonts_and_sprites_come_from_the_assets_snapshot(client: TestClient) -> None:
    assert client.get("/v1/maps/glyphs/Noto Sans Regular/0-255.pbf").content == b"glyphs"
    # A font stack falls back to the first font held.
    assert client.get("/v1/maps/glyphs/Missing Font,Noto Sans Regular/0-255.pbf").content == b"glyphs"
    assert client.get("/v1/maps/glyphs/Noto Sans Regular/256-511.pbf").status_code == 404
    assert client.get("/v1/maps/sprites/v4/light.json").json() == {}
    assert client.get("/v1/maps/sprites/v4/../../x").status_code == 404


def test_places_and_nearby_come_from_the_gazetteer(client: TestClient) -> None:
    body = client.get("/v1/maps/places", params={"q": "interl"}).json()
    assert [p["name"] for p in body["places"]] == ["Interlaken"]
    assert body["places"][0]["wikipedia"] == "en:Interlaken"
    near = client.get("/v1/maps/nearby", params={"lat": 46.69, "lon": 7.86, "radius": 20000}).json()
    assert [p["name"] for p in near["places"]] == ["Interlaken", "Jungfrau"]
    assert near["places"][0]["distance_m"] < 1000
    assert client.get("/v1/maps/nearby", params={"lat": 95, "lon": 0}).status_code == 422


def test_an_archive_that_failed_to_open_is_tried_again(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """2026-11-04 trawl: one NAS refusal while an archive was first opened took it away until the
    API restarted, and its tiles were empty ones the browser kept for a day."""
    from wsp_api import maps

    now = [1000.0]
    monkeypatch.setattr(maps, "time", SimpleNamespace(monotonic=lambda: now[0]))
    real = Archive.open_file
    refusals = {"b.pmtiles": 1}

    def flaky(path: Path, **kwargs: int) -> Archive:
        if refusals.get(path.name):
            refusals[path.name] -= 1
            raise OSError(11, "Resource temporarily unavailable")
        return real(path, **kwargs)

    monkeypatch.setattr(Archive, "open_file", staticmethod(flaky))

    def ids() -> set[str]:
        return {t["id"] for t in client.get("/v1/maps/tilesets").json()["tilesets"]}

    assert ids() == {"terrain"}
    r = client.get("/v1/maps/tiles/basemap/1/0/0")
    assert r.status_code == 503  # not a 204 the browser keeps
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["retry-after"] == maps.RETRY_AFTER
    now[0] += maps.RETRY_FIRST + 1
    assert ids() == {"basemap", "terrain"}
    r = client.get("/v1/maps/tiles/basemap/1/0/0")
    assert (r.status_code, r.content) == (200, b"mvt-1")
    assert r.headers["cache-control"] == maps.CACHE_HEADER


def test_a_tile_read_that_failed_is_never_kept_by_the_browser(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = Archive.tile
    refusals = {(1, 0, 0), (4, 8, 4)}

    def flaky(self: Archive, z: int, x: int, y: int) -> bytes | None:
        if (z, x, y) in refusals:
            refusals.discard((z, x, y))
            raise OSError(103, "Software caused connection abort")
        return real(self, z, x, y)

    monkeypatch.setattr(Archive, "tile", flaky)
    assert client.get("/v1/maps/tiles/basemap/1/0/0").status_code == 503
    # The regional z4 tile failed: the one cut from its z3 is served, but only for this view.
    cut = client.get("/v1/maps/tiles/terrain/4/8/4")
    assert cut.status_code == 200
    assert cut.headers["cache-control"] == "no-store"
    again = client.get("/v1/maps/tiles/terrain/4/8/4")
    assert again.headers["cache-control"] == "public, max-age=86400"


def test_a_cut_made_while_a_nearer_archive_failed_is_never_remembered(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """2026-11-04 trawl: the server's cut cache is keyed by tile, not by the ancestor cut from, so
    a coarse stand-in cut while the regional archive refused a read was served for a day after."""
    real = Archive.tile
    refusals = {(4, 8, 4), (3, 4, 2)}  # the regional z4 and z3 ancestors of 5/16/8

    def flaky(self: Archive, z: int, x: int, y: int) -> bytes | None:
        if (z, x, y) in refusals:
            refusals.discard((z, x, y))
            raise OSError(103, "Software caused connection abort")
        return real(self, z, x, y)

    monkeypatch.setattr(Archive, "tile", flaky)
    coarse = client.get("/v1/maps/tiles/terrain/5/16/8")  # cut from the world z2 tile
    assert coarse.status_code == 200
    assert coarse.headers["cache-control"] == "no-store"
    assert np.allclose(elevation(coarse.content), 100.0, atol=1.0)
    fine = client.get("/v1/maps/tiles/terrain/5/16/8")  # the regional z4 answers now
    assert fine.headers["cache-control"] == "public, max-age=86400"
    assert np.allclose(elevation(fine.content), 250.0, atol=1.0)


def test_no_gazetteer_yet_is_a_503(tmp_path: Path) -> None:
    lib = SimpleNamespace(
        s=SimpleNamespace(sources=[]), vault=FakeVault(tmp_path, {}), settings=SimpleNamespace(gazetteer=None)
    )
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(maps_router(lib))  # type: ignore[arg-type]
    r = TestClient(app).get("/v1/maps/places", params={"q": "x"})
    assert r.status_code == 503
    assert TestClient(app).get("/v1/maps/tilesets").json()["tilesets"] == []
