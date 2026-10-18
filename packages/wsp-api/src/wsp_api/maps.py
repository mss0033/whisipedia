"""The Atlas's tiles: ``/v1/maps/...`` (``docs/components/maps.md``).

A **tileset** (``basemap``, ``terrain``, ``imagery``, ...) is the current generations of every registry
source whose ``access.tileset`` names it (adapter ``map-tiles``): PMTiles archives in the vault, read
with :class:`wsp_core.pmtiles.Archive`. The browser sees each tileset as one source,
``/v1/maps/tiles/<tileset>/{z}/{x}/{y}``, however many archives hold it (Mapterhorn's terrain is a
world archive in six parts and 458 regional ones):

* the archives are tried in priority order (``access.priority``, regional before world), then
  highest zoom first, skipping those whose bounds miss the tile;
* a raster or terrain tile no archive holds at its zoom is **cut from its nearest held ancestor**
  (up to :data:`MAX_OVERZOOM` zooms up): imagery is cropped and resized; terrain is decoded to metres,
  interpolated and re-encoded losslessly, because interpolating Terrarium's RGB bytes would mix the
  carry between channels. The browser so never sees a hole where only coarser data exists. Such
  tiles are kept in a small cache;
* a vector tile no archive holds is an empty tile (204): the sea, or past the archive's edge.

Reads run in their own pool (``maps``), never in the document reads' ``nas`` slots. Archives are
opened lazily and re-listed every :data:`RESCAN_SECONDS` (a newly sealed part appears by itself).

Fonts and sprites come from the ``maps-assets`` source (protomaps/basemaps-assets, a git snapshot
zip): ``/v1/maps/glyphs/{fontstack}/{range}.pbf`` and ``/v1/maps/sprites/{name}``.
"""

from __future__ import annotations

import io
import logging
import math
import re
import threading
import time
import zipfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, cast

from fastapi import APIRouter, Query, Response
from fastapi import Path as PathParam
from pydantic import BaseModel, Field

from wsp_api.errors import ApiError, errors
from wsp_api.offload import Offload
from wsp_core.gazetteer import Gazetteer, Place
from wsp_core.mapsources import MapConfigError, map_source
from wsp_core.model import SourceSpec
from wsp_core.pmtiles import MEDIA_TYPES, Archive, Compression, PMTilesError, TileType

if TYPE_CHECKING:
    import numpy as np
    from numpy.typing import NDArray
    from PIL import Image

    from wsp_api.views import Library

log = logging.getLogger(__name__)

RESCAN_SECONDS = 60.0
MAX_OVERZOOM = 6  # a tile is cut from an ancestor at most this many zooms up (64x64 px of a 512 px tile)
DRAWN_MAX_ZOOM = {"vector": 15, "raster": 18, "raster-dem": 16}  # what the browser asks the API for
CUT_CACHE = 512  # tiles cut from ancestors, kept (terrain re-encoding costs ~30-60 ms)
ASSETS_SOURCE = "maps-assets"
CACHE_HEADER = "public, max-age=86400"


class Tileset(BaseModel):
    id: str = Field(description="`basemap`, `terrain`, `imagery`, ...")
    kind: str = Field(description="`vector`, `raster` or `raster-dem`.")
    encoding: str = Field(description="`terrarium` or `mapbox` for raster-dem; empty otherwise.")
    tile_type: str = Field(description="`mvt`, `webp`, `png`, `jpeg`.")
    tile_size: int = Field(description="Pixels per raster tile side (512 for Mapterhorn); 512 for vector.")
    min_zoom: int
    max_zoom: int = Field(description="Highest zoom the API serves; the browser overzooms beyond.")
    held_max_zoom: int = Field(description="Highest zoom any archive holds (somewhere).")
    bounds: list[float] = Field(description="min_lon, min_lat, max_lon, max_lat of everything held.")
    archives: int
    attribution: str
    tiles: list[str] = Field(description="URL template, `{z}/{x}/{y}`.")


class PlaceHit(BaseModel):
    name: str
    kind: str = Field(description="`place=city`, `natural=peak`, ...")
    lat: float
    lon: float
    population: int | None
    wikidata: str | None
    wikipedia: str | None = Field(description='OpenStreetMap\'s `wikipedia` tag, "en:Interlaken".')
    osm: str = Field(description="`node/123`")
    distance_m: float | None = Field(description="From the point asked about (nearby only).")


class Places(BaseModel):
    places: list[PlaceHit]


class Tilesets(BaseModel):
    tilesets: list[Tileset]
    glyphs: str | None = Field(description="Glyph URL template, or null when no fonts are held.")
    sprite: str | None = Field(description="Sprite base URL (MapLibre appends .json/.png), or null.")
    fonts: list[str] = Field(description="Font stacks the glyphs cover.")


@dataclass
class _Held:
    """One archive of a tileset."""

    path: Path
    priority: int
    source_id: str
    archive: Archive | None = None
    failed: bool = False

    def open(self) -> Archive | None:
        if self.archive is None and not self.failed:
            try:
                self.archive = Archive.open_file(self.path)
            except (OSError, PMTilesError) as exc:
                log.warning("map archive %s unreadable: %s", self.path, exc)
                self.failed = True
        return self.archive


@dataclass
class _Set:
    id: str
    kind: str
    encoding: str
    attribution: list[str] = field(default_factory=list[str])
    held: list[_Held] = field(default_factory=list[_Held])


def tile_bounds(z: int, x: int, y: int) -> tuple[float, float, float, float]:
    n = 1 << z

    def lat(yy: int) -> float:
        return math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * yy / n))))

    return (x / n * 360 - 180, lat(y + 1), (x + 1) / n * 360 - 180, lat(y))


def _overlaps(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


# --- terrain and imagery cut from an ancestor -----------------------------------------------------


def _decode(blob: bytes) -> Image.Image:
    from PIL import Image

    with Image.open(io.BytesIO(blob)) as img:
        return img.convert("RGB").copy()


def decode_elevation(rgb: NDArray[np.float64], encoding: str) -> NDArray[np.float64]:
    """Metres from Terrarium or Mapbox terrain RGB."""
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    if encoding == "mapbox":
        return -10000 + (r * 65536 + g * 256 + b) * 0.1
    return r * 256 + g + b / 256 - 32768


def encode_elevation(e: NDArray[np.float64], encoding: str) -> NDArray[np.uint8]:
    import numpy as np

    if encoding == "mapbox":
        v = np.clip(np.round((e + 10000) * 10), 0, 2**24 - 1).astype(np.int64)
        planes = [(v >> 16) & 255, (v >> 8) & 255, v & 255]
    else:
        f = np.clip(e + 32768, 0, 65535.996)
        whole = np.floor(f)
        planes = [whole // 256, whole % 256, np.floor((f - whole) * 256)]
    return np.stack(planes, axis=-1).astype(np.uint8)


def upsample(
    e: NDArray[np.float64], box: tuple[float, float, float, float], size: int
) -> NDArray[np.float64]:
    """Bilinear: ``e`` sampled at the centres of a ``size`` x ``size`` grid over ``box`` (pixels)."""
    import numpy as np

    span = box[2] - box[0]
    coords = (np.arange(size, dtype=np.float64) + 0.5) / size * span - 0.5
    ys = np.clip(box[1] + coords, 0, e.shape[0] - 1)
    xs = np.clip(box[0] + coords, 0, e.shape[1] - 1)
    y0 = np.floor(ys).astype(np.intp)
    x0 = np.floor(xs).astype(np.intp)
    y1 = np.minimum(y0 + 1, e.shape[0] - 1)
    x1 = np.minimum(x0 + 1, e.shape[1] - 1)
    fy = (ys - y0)[:, None]
    fx = (xs - x0)[None, :]
    top = e[np.ix_(y0, x0)] * (1 - fx) + e[np.ix_(y0, x1)] * fx
    bottom = e[np.ix_(y1, x0)] * (1 - fx) + e[np.ix_(y1, x1)] * fx
    return top * (1 - fy) + bottom * fy


def cut_from_ancestor(
    blob: bytes, dz: int, x: int, y: int, *, kind: str, encoding: str, tile_type: TileType
) -> bytes:
    """The tile ``dz`` zooms below an ancestor tile ``blob``, at position (x, y) among its 2**dz x
    2**dz descendants: the matching square of the ancestor, scaled up to full size. Terrain is
    interpolated in metres (interpolating its RGB bytes would mix the carry between channels)."""
    import numpy as np
    from PIL import Image

    img = _decode(blob)
    size = img.width
    n = 1 << dz
    span = size / n
    box = (x % n * span, y % n * span, (x % n + 1) * span, (y % n + 1) * span)
    out = io.BytesIO()
    if kind != "raster-dem":
        part = img.resize((size, size), Image.Resampling.BICUBIC, box=box)
        fmt = {TileType.PNG: "PNG", TileType.JPEG: "JPEG"}.get(tile_type, "WEBP")
        part.save(out, fmt, quality=85)
        return out.getvalue()
    elev = decode_elevation(np.asarray(img, dtype=np.float64), encoding)
    enc = encode_elevation(upsample(elev, box, size), encoding)
    Image.fromarray(enc, "RGB").save(out, "WEBP", lossless=True, method=1)
    return out.getvalue()


# --- the registry of tilesets ----------------------------------------------------------------------


class MapLibrary:
    """Tilesets built from the vault's current map generations; thread-safe."""

    def __init__(self, sources: Callable[[], Sequence[SourceSpec]], vault: Callable[[], Any]) -> None:
        self._sources = sources
        self._vault = vault
        self._lock = threading.Lock()
        self._sets: dict[str, _Set] = {}
        self._scanned = -math.inf
        self._cuts: dict[tuple[str, int, int, int], bytes] = {}
        self._assets: tuple[Path, zipfile.ZipFile] | None = None

    def sets(self) -> dict[str, _Set]:
        with self._lock:
            if time.monotonic() - self._scanned < RESCAN_SECONDS:
                return self._sets
            self._scanned = time.monotonic()
            old = {h.path: h for s in self._sets.values() for h in s.held}
        found = self._scan(old)
        with self._lock:
            self._sets = found
            for held in old.values():  # archives no longer current
                if all(held.path != h.path for s in found.values() for h in s.held) and held.archive:
                    held.archive.close()
            return found

    def _scan(self, old: Mapping[Path, _Held]) -> dict[str, _Set]:
        vault = self._vault()
        out: dict[str, _Set] = {}
        for spec in self._sources():
            access = cast(Mapping[str, Any], spec.raw.get("access") or {})
            try:
                ms = map_source(access)
            except MapConfigError as exc:
                log.warning("map source %s: %s", spec.id, exc)
                continue
            if ms is None:
                continue
            try:
                gens = vault.current_set(spec.id)
            except Exception as exc:  # a missing pointer, an unreachable NAS: the others still serve
                log.warning("map source %s: no current generations: %s", spec.id, exc)
                continue
            s = out.setdefault(ms.tileset, _Set(ms.tileset, ms.kind, ms.encoding))
            if ms.attribution and ms.attribution not in s.attribution:
                s.attribution.append(ms.attribution)
            for gen in gens:
                root = Path(vault.generation_path(spec.id, gen.generation_id))
                for art in gen.artifacts:
                    if art.relpath.suffix != ".pmtiles":
                        continue
                    path = root / Path(str(art.relpath))
                    s.held.append(old.get(path) or _Held(path, ms.priority, spec.id))
        for s in out.values():
            s.held.sort(key=lambda h: -h.priority)
        return out

    def describe(self, s: _Set) -> Tileset | None:
        archives = [a for a in (h.open() for h in s.held) if a is not None]
        if not archives:
            return None
        heads = [a.header for a in archives]
        held_max = max(h.max_zoom for h in heads)
        tile_type = heads[0].tile_type
        size = 512 if s.kind == "vector" else self._raster_size(archives[0])
        return Tileset(
            id=s.id,
            kind=s.kind,
            encoding=s.encoding,
            tile_type=tile_type.name.lower(),
            tile_size=size,
            min_zoom=min(h.min_zoom for h in heads),
            max_zoom=max(held_max, DRAWN_MAX_ZOOM.get(s.kind, held_max)) if s.kind != "vector" else held_max,
            held_max_zoom=held_max,
            bounds=[
                max(-180.0, min(h.min_lon for h in heads)),
                max(-85.0511, min(h.min_lat for h in heads)),
                min(180.0, max(h.max_lon for h in heads)),
                min(85.0511, max(h.max_lat for h in heads)),
            ],
            archives=len(archives),
            attribution=" · ".join(s.attribution),
            tiles=[f"/v1/maps/tiles/{s.id}/{{z}}/{{x}}/{{y}}"],
        )

    @staticmethod
    def _raster_size(archive: Archive) -> int:
        h = archive.header
        z = h.min_zoom
        n = 1 << z
        cx = min(n - 1, max(0, int((h.center_lon + 180) / 360 * n)))
        for x in (cx, 0):
            for y in range(n):
                blob = archive.tile(z, x, y) if z <= 3 else None
                if blob:
                    try:
                        return _decode(blob).width
                    except OSError:
                        return 256
        return 512 if h.tile_type is TileType.WEBP else 256

    def tile(self, tileset: str, z: int, x: int, y: int) -> tuple[bytes, TileType, Compression] | None:
        s = self.sets().get(tileset)
        if s is None:
            raise ApiError(404, "tileset_not_found", f"no tileset {tileset!r}")
        n = 1 << z
        if not (0 <= x < n and 0 <= y < n):
            raise ApiError(400, "tile_out_of_range", f"tile {z}/{x}/{y} does not exist")
        bounds = tile_bounds(z, x, y)
        for dz in range(0, (MAX_OVERZOOM if s.kind != "vector" else 0) + 1):
            if dz > z:
                break
            az, ax, ay = z - dz, x >> dz, y >> dz
            for held in s.held:
                archive = held.open()
                if archive is None:
                    continue
                h = archive.header
                if not (h.min_zoom <= az <= h.max_zoom) or not _overlaps(bounds, h.bounds):
                    continue
                try:
                    blob = archive.tile(az, ax, ay)
                except (OSError, PMTilesError) as exc:
                    log.warning("map archive %s: tile %d/%d/%d: %s", held.path, az, ax, ay, exc)
                    continue
                if not blob:
                    continue
                if dz == 0:
                    return blob, h.tile_type, h.tile_compression
                key = (tileset, z, x, y)
                with self._lock:
                    hit = self._cuts.get(key)
                if hit is None:
                    hit = cut_from_ancestor(
                        blob, dz, x, y, kind=s.kind, encoding=s.encoding, tile_type=h.tile_type
                    )
                    with self._lock:
                        if len(self._cuts) >= CUT_CACHE:
                            self._cuts.pop(next(iter(self._cuts)))
                        self._cuts[key] = hit
                return hit, h.tile_type, Compression.NONE
        return None

    # --- fonts and sprites ---

    def _asset_zip(self) -> zipfile.ZipFile | None:
        vault = self._vault()
        try:
            gen = vault.current(ASSETS_SOURCE)
        except Exception:
            return None
        if gen is None:
            return None
        art = next((a for a in gen.artifacts if a.relpath.suffix == ".zip"), None)
        if art is None:
            return None
        path = Path(vault.generation_path(ASSETS_SOURCE, gen.generation_id)) / Path(str(art.relpath))
        with self._lock:
            if self._assets is not None and self._assets[0] == path:
                return self._assets[1]
            try:
                z = zipfile.ZipFile(path)
            except (OSError, zipfile.BadZipFile) as exc:
                log.warning("map assets %s unreadable: %s", path, exc)
                return None
            if self._assets is not None:
                self._assets[1].close()
            self._assets = (path, z)
            return z

    def asset(self, inner: str) -> bytes | None:
        """``fonts/...`` or ``sprites/...`` from the assets snapshot (its top folder is skipped)."""
        z = self._asset_zip()
        if z is None:
            return None
        with self._lock:
            for name in (n for n in z.namelist() if n.partition("/")[2] == inner):
                return z.read(name)
        return None

    def fonts(self) -> list[str]:
        z = self._asset_zip()
        if z is None:
            return []
        found = {
            parts[2]
            for parts in (n.split("/") for n in z.namelist())
            if len(parts) == 4 and parts[1] == "fonts" and parts[3] == "0-255.pbf"
        }
        return sorted(found)

    def sprite_version(self) -> str | None:
        z = self._asset_zip()
        if z is None:
            return None
        versions = sorted(
            {
                p[2]
                for p in (n.split("/") for n in z.namelist())
                if len(p) >= 4 and p[1] == "sprites" and p[2].startswith("v")
            },
            key=lambda v: int(v[1:]) if v[1:].isdigit() else 0,
        )
        return versions[-1] if versions else None


_FONTSTACK = re.compile(r"^[A-Za-z0-9 ,_-]{1,200}$")
_RANGE = re.compile(r"^(\d{1,5})-(\d{1,5})\.pbf$")
_SPRITE = re.compile(r"^(v\d+)/([a-z0-9_-]{1,40})(@2x)?\.(json|png)$")


def maps_router(lib: Library) -> APIRouter:
    r = APIRouter(prefix="/v1/maps")
    pool = Offload(workers=6, queue=96, timeout=30.0, name="maps")
    maps = MapLibrary(lambda: lib.s.sources, lambda: lib.vault)
    lib.maps = maps  # type: ignore[attr-defined]  # the System Monitor and tests read it
    settings = getattr(lib, "settings", None)
    gazetteer_path: Path | None = getattr(settings, "gazetteer", None)
    gazetteers: list[Gazetteer] = []

    def gazetteer() -> Gazetteer:
        if gazetteer_path is None or not gazetteer_path.is_file():
            raise ApiError(503, "no_gazetteer", "the place index is not built yet (wsp maps gazetteer)")
        if not gazetteers or gazetteers[0].path != gazetteer_path:
            gazetteers[:] = [Gazetteer(gazetteer_path)]
        return gazetteers[0]

    def hit(p: Place) -> PlaceHit:
        dist = None if p.distance_m is None else round(p.distance_m, 1)
        return PlaceHit(
            name=p.name, kind=p.kind, lat=p.lat, lon=p.lon, population=p.population, wikidata=p.qid,
            wikipedia=p.wikipedia, osm=p.osm, distance_m=dist,
        )  # fmt: skip

    @r.get("/places", tags=["maps"], response_model=Places, responses=errors(503))
    async def places(
        q: Annotated[
            str, Query(min_length=1, max_length=200, description="A place name; the last word is a prefix.")
        ],
        limit: Annotated[int, Query(ge=1, le=50)] = 10,
    ) -> Places:
        """Named places (OpenStreetMap) matching ``q``, the most important first."""
        found = await pool.run(lambda: gazetteer().search(q, limit=limit))
        return Places(places=[hit(p) for p in found])

    @r.get("/nearby", tags=["maps"], response_model=Places, responses=errors(503))
    async def nearby(
        lat: Annotated[float, Query(ge=-90, le=90)],
        lon: Annotated[float, Query(ge=-180, le=180)],
        radius: Annotated[float, Query(gt=0, le=200_000, description="Metres.")] = 5000,
        limit: Annotated[int, Query(ge=1, le=50)] = 10,
    ) -> Places:
        """Named places within ``radius`` metres of a point, nearest first."""
        found = await pool.run(lambda: gazetteer().nearby(lat, lon, radius_m=radius, limit=limit))
        return Places(places=[hit(p) for p in found])

    @r.get("/tilesets", tags=["maps"], response_model=Tilesets)
    async def tilesets() -> Tilesets:
        """The tilesets the library holds now, with their tile URL templates."""

        def build() -> Tilesets:
            sets = [t for t in (maps.describe(s) for s in maps.sets().values()) if t is not None]
            fonts = maps.fonts()
            sprite = maps.sprite_version()
            return Tilesets(
                tilesets=sorted(sets, key=lambda t: t.id),
                glyphs="/v1/maps/glyphs/{fontstack}/{range}.pbf" if fonts else None,
                sprite=f"/v1/maps/sprites/{sprite}" if sprite else None,
                fonts=fonts,
            )

        return await pool.run(build)

    @r.get("/tiles/{tileset}/{z}/{x}/{y}", tags=["maps"], responses=errors(400, 404, 503))
    async def tile(
        tileset: Annotated[str, PathParam(pattern=r"^[a-z][a-z0-9-]{0,31}$")],
        z: Annotated[int, PathParam(ge=0, le=24)],
        x: Annotated[int, PathParam(ge=0)],
        y: Annotated[str, PathParam(pattern=r"^\d+(\.(mvt|pbf|webp|png|jpg))?$")],
    ) -> Response:
        """One tile. 204 for an empty vector tile; a raster or terrain tile only coarser data covers
        is cut from that data."""
        yy = int(y.partition(".")[0])
        found = await pool.run(lambda: maps.tile(tileset, z, x, yy))
        if found is None:
            return Response(status_code=204, headers={"Cache-Control": CACHE_HEADER})
        blob, tile_type, compression = found
        headers = {"Cache-Control": CACHE_HEADER}
        if compression is Compression.GZIP:
            headers["Content-Encoding"] = "gzip"
        return Response(
            blob, media_type=MEDIA_TYPES.get(tile_type, "application/octet-stream"), headers=headers
        )

    @r.get("/glyphs/{fontstack}/{range_}", tags=["maps"], responses=errors(404))
    async def glyphs(fontstack: str, range_: str) -> Response:
        """Glyphs of a font stack (MapLibre asks for "Noto Sans Regular" or "A,B"): the first font held."""
        if not _FONTSTACK.match(fontstack) or not _RANGE.match(range_):
            raise ApiError(404, "glyphs_not_found", "no such glyph range")

        def read() -> bytes | None:
            for font in (f.strip() for f in fontstack.split(",")):
                blob = maps.asset(f"fonts/{font}/{range_}")
                if blob is not None:
                    return blob
            return None

        blob = await pool.run(read)
        if blob is None:
            raise ApiError(404, "glyphs_not_found", f"no glyphs {fontstack}/{range_}")
        return Response(blob, media_type="application/x-protobuf", headers={"Cache-Control": CACHE_HEADER})

    @r.get("/sprites/{version}/{name}", tags=["maps"], responses=errors(404))
    async def sprites(version: str, name: str) -> Response:
        """A sprite sheet (``light.json``, ``dark@2x.png``, ...)."""
        m = _SPRITE.match(f"{version}/{name}")
        if m is None:
            raise ApiError(404, "sprite_not_found", "no such sprite")
        blob = await pool.run(lambda: maps.asset(f"sprites/{version}/{name}"))
        if blob is None:
            raise ApiError(404, "sprite_not_found", f"no sprite {version}/{name}")
        media = "application/json" if name.endswith(".json") else "image/png"
        return Response(blob, media_type=media, headers={"Cache-Control": CACHE_HEADER})

    return r
