"""``map-tiles`` adapter: PMTiles archives for the Atlas (``docs/components/maps.md``, 2026-10-18).

A map source is one part of a **tileset** (``access.tileset``: ``basemap``, ``terrain``, ``imagery``,
...): the OpenStreetMap basemap, Mapterhorn's world terrain, its regional high-resolution terrain.
The API serves every current archive of a tileset as one ``/v1/maps/tiles/<tileset>/{z}/{x}/{y}``
source (:mod:`wsp_api.maps`).

Archives run to hundreds of gigabytes, more than the local work disk holds, so map sources are
``partitioned`` (ADR-0010): ``wsp maps fetch --source S --partition P`` downloads one part straight
into ``<vault>/_incoming`` and ``wsp ingest`` seals it; the orchestrator never fetches them itself.
A part is

* a whole upstream archive, checked against its published MD5 (the Protomaps build, Mapterhorn's
  smaller regional files), or
* a **cut** of one (``pmtiles extract`` with a zoom range and/or a bounding box). PMTiles stores a
  clustered archive's tiles in tile-id order, lowest zoom first, so a zoom cap reads only the bytes
  it keeps: Mapterhorn's 264 GB ``6-14-26`` is 20 GB up to z15. A cut has no upstream checksum; it
  is checked by reading it back (:func:`check_cut`) and stored with our own SHA-256.

Discovery (``access.discovery.kind``):

``protomaps-builds``
    The newest build in ``build-metadata.protomaps.dev/builds.json`` (one part, ``planet``).
``pmtiles-list``
    A JSON list of archives with MD5, zooms and bounds (Mapterhorn's ``download_urls.json``);
    ``names`` keeps only those files, ``min_zoom`` only archives starting at that zoom or above; each
    archive is a part named after its file. ``cut: {max_zoom: N}`` caps every part (an archive
    already within the cap is taken whole, MD5-checked).
``parts``
    An explicit list ``[{partition, url, min_zoom?, max_zoom?, bbox?}]``, for splitting one archive
    too large to stage (Mapterhorn's 356 GB planet in zoom bands).
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from wsp_core.mapsources import ADAPTER, MapConfigError, MapSource, map_source
from wsp_core.model import (
    DocRecord,
    FetchPlan,
    FetchResult,
    Generation,
    ReleaseRef,
    RemoteFile,
    Verdict,
    VerdictStatus,
)
from wsp_core.partitions import check_partition
from wsp_core.pmtiles import Archive, PMTilesError
from wsp_core.ports import Ctx

PROTOMAPS_BUILDS = "https://build-metadata.protomaps.dev/builds.json"
PROTOMAPS_FILES = "https://build.protomaps.com/"


@dataclass(frozen=True, slots=True)
class Cut:
    """A sub-archive: zooms ``min_zoom..max_zoom`` (None: the archive's own) inside ``bbox``."""

    min_zoom: int | None = None
    max_zoom: int | None = None
    bbox: tuple[float, float, float, float] | None = None

    def args(self) -> list[str]:
        out: list[str] = []
        if self.min_zoom is not None:
            out.append(f"--minzoom={self.min_zoom}")
        if self.max_zoom is not None:
            out.append(f"--maxzoom={self.max_zoom}")
        if self.bbox is not None:
            out.append("--bbox=" + ",".join(f"{v:g}" for v in self.bbox))
        return out


@dataclass(frozen=True, slots=True)
class MapPart:
    """One partition of a map source: an upstream archive, whole or cut."""

    partition: str
    name: str  # the stored file name
    url: str
    size: int | None = None  # of the upstream archive (a cut is smaller)
    md5: str | None = None  # hex, of the upstream archive; only checked when it is taken whole
    cut: Cut | None = None
    version: str = ""  # upstream's own version (a build date), when it says
    bounds: tuple[float, float, float, float] | None = None
    zooms: tuple[int, int] | None = None


def _md5_hex(value: object) -> str | None:
    """A published MD5 as hex: Mapterhorn writes hex, Protomaps base64 (S3's Content-MD5)."""
    text = str(value or "").strip()
    if re.fullmatch(r"[0-9a-fA-F]{32}", text):
        return text.lower()
    try:
        raw = base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError):
        return None
    return raw.hex() if len(raw) == 16 else None


def _cut(raw: object) -> Cut | None:
    if not isinstance(raw, Mapping):
        return None
    m = cast(Mapping[str, Any], raw)
    bbox = m.get("bbox")
    box: tuple[float, float, float, float] | None = None
    if bbox is not None:
        vals = [float(v) for v in cast(Sequence[object], bbox)]  # type: ignore[arg-type]
        if len(vals) != 4 or not (-180 <= vals[0] < vals[2] <= 180 and -90 <= vals[1] < vals[3] <= 90):
            raise MapConfigError(f"bbox {bbox!r} must be min_lon,min_lat,max_lon,max_lat")
        box = (vals[0], vals[1], vals[2], vals[3])
    lo = m.get("min_zoom")
    hi = m.get("max_zoom")
    cut = Cut(int(lo) if lo is not None else None, int(hi) if hi is not None else None, box)
    return cut if cut != Cut() else None


def map_parts(spec_access: Mapping[str, Any], get_bytes: Callable[[str], bytes]) -> list[MapPart]:
    """Every part the source's discovery lists now (metadata only: one small JSON request)."""
    src = MapSource.of(spec_access)
    disc = src.discovery
    kind = str(disc["kind"])
    if kind == "protomaps-builds":
        builds = cast(
            list[Mapping[str, Any]], json.loads(get_bytes(str(disc.get("url") or PROTOMAPS_BUILDS)))
        )
        dated = [b for b in builds if re.fullmatch(r"\d{8}\.pmtiles", str(b.get("key", "")))]
        if not dated:
            return []
        b = max(dated, key=lambda x: str(x["key"]))
        key = str(b["key"])
        stamp = key[:8]
        return [
            MapPart(
                partition="planet",
                name=f"protomaps-{key}",
                url=str(disc.get("files") or PROTOMAPS_FILES) + key,
                size=int(b["size"]) if b.get("size") is not None else None,
                md5=_md5_hex(b.get("md5sum")),
                version=f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:]}",
            )
        ]
    if kind == "pmtiles-list":
        listing: object = json.loads(get_bytes(str(disc["url"])))
        if isinstance(listing, dict):
            listing = cast(dict[str, object], listing).get("items", [])
        items = cast(list[Mapping[str, Any]], listing)
        names = {str(n) for n in cast(Sequence[object], disc.get("names") or [])}
        floor = disc.get("min_zoom")
        cap = _cut(disc.get("cut"))
        out: list[MapPart] = []
        for item in items:
            name = str(item.get("name") or "")
            if not name.endswith(".pmtiles") or (names and name not in names):
                continue
            zooms = (int(item.get("min_zoom", 0)), int(item.get("max_zoom", 0)))
            if floor is not None and zooms[0] < int(floor):
                continue
            stem = name.removesuffix(".pmtiles").lower()
            top = cap.max_zoom if cap is not None and cap.max_zoom is not None else zooms[1]
            whole = cap is None or (
                cap.max_zoom is not None and zooms[1] <= cap.max_zoom and cap.bbox is None
            )
            bounds = (
                tuple(float(item[k]) for k in ("min_lon", "min_lat", "max_lon", "max_lat"))
                if "min_lon" in item
                else None
            )
            out.append(
                MapPart(
                    partition=check_partition(stem),
                    name=name if whole else f"{stem}-z{zooms[0]}-{top}.pmtiles",
                    url=str(item["url"]),
                    size=int(item["size"]) if item.get("size") is not None else None,
                    md5=_md5_hex(item.get("md5sum")),
                    cut=None if whole else cap,
                    bounds=cast(tuple[float, float, float, float] | None, bounds),
                    zooms=zooms,
                )
            )
        return out
    out = []
    for raw in cast(Sequence[Mapping[str, Any]], disc.get("parts") or []):
        partition = check_partition(str(raw.get("partition") or ""))
        url = str(raw.get("url") or "")
        if not url.startswith("https://"):
            raise MapConfigError(f"part {partition}: url must be https")
        cut = _cut(raw)
        base = url.rsplit("/", 1)[-1].removesuffix(".pmtiles")
        out.append(MapPart(partition=partition, name=f"{base}-{partition}.pmtiles", url=url, cut=cut))
    return out


_ENTRIES = re.compile(r"result tile entries (\d+)")


def expected_entries(log_text: str) -> int | None:
    """The tile-entry count ``pmtiles extract`` reports for its result."""
    found = _ENTRIES.findall(log_text)
    return int(found[-1]) if found else None


def check_cut(path: Path, cut: Cut, entries: int | None) -> str | None:
    """A problem with a cut archive, or None: it must open, stay within the cut's zooms, and hold the
    tile entries the extract reported."""
    try:
        archive = Archive.open_file(path)
    except (OSError, PMTilesError) as exc:
        return f"not a readable archive: {exc}"
    try:
        h = archive.header
        if cut.max_zoom is not None and h.max_zoom > cut.max_zoom:
            return f"max zoom {h.max_zoom} above the cut's {cut.max_zoom}"
        if cut.min_zoom is not None and h.min_zoom < cut.min_zoom:
            return f"min zoom {h.min_zoom} below the cut's {cut.min_zoom}"
        if entries is not None and h.tile_entries != entries:
            return f"{h.tile_entries} tile entries, the extract reported {entries}"
        if not h.clustered:
            return "not clustered"
    finally:
        archive.close()
    return None


class MapTilesAdapter:
    """``SourceAdapter`` for map archives. Map sources are partitioned and fetched by ``wsp maps
    fetch``; the orchestrator's own discovery finds nothing to do."""

    id = ADAPTER

    def discover(self, ctx: Ctx) -> Iterable[ReleaseRef]:
        return []

    def plan(self, ctx: Ctx, have: Generation | None, want: ReleaseRef) -> FetchPlan:
        return FetchPlan(want, ())

    def fetch(self, ctx: Ctx, plan: FetchPlan) -> Iterable[FetchResult]:
        return []

    def verify(self, ctx: Ctx, remote: RemoteFile, local: Path) -> Verdict:
        try:
            Archive.open_file(local).close()
        except (OSError, PMTilesError) as exc:
            return Verdict(VerdictStatus.MISMATCH, f"not a PMTiles archive: {exc}")
        return Verdict(VerdictStatus.OK, "PMTiles archive")

    def documents(self, ctx: Ctx, generation_dir: Path) -> Iterator[DocRecord]:
        return iter(())  # tiles are drawn, not searched (index: false)


__all__ = [
    "Cut",
    "MapConfigError",
    "MapPart",
    "MapSource",
    "MapTilesAdapter",
    "check_cut",
    "expected_entries",
    "map_parts",
    "map_source",
]
