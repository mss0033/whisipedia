"""Map sources of the Atlas in the registry (``access.adapter: map-tiles``; ``docs/components/maps.md``).

Shared by the adapter that fetches their archives (:mod:`wsp_adapters.maptiles`) and the API that
serves them (:mod:`wsp_api.maps`).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, cast

ADAPTER = "map-tiles"
TILE_KINDS = frozenset({"vector", "raster", "raster-dem"})
DISCOVERY_KINDS = frozenset({"protomaps-builds", "pmtiles-list", "parts"})


class MapConfigError(ValueError):
    """The registry entry of a map source is malformed."""


@dataclass(frozen=True, slots=True)
class MapSource:
    tileset: str
    kind: str
    encoding: str  # raster-dem: "terrarium" or "mapbox"; else ""
    priority: int  # among a tileset's archives, higher is tried first (regional over world)
    attribution: str
    discovery: Mapping[str, Any]

    @classmethod
    def of(cls, spec_access: Mapping[str, Any]) -> MapSource:
        tileset = str(spec_access.get("tileset") or "")
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,31}", tileset):
            raise MapConfigError(f"access.tileset {tileset!r} must be a short lowercase name")
        kind = str(spec_access.get("tile_kind") or "")
        if kind not in TILE_KINDS:
            raise MapConfigError(f"access.tile_kind {kind!r} is not one of {sorted(TILE_KINDS)}")
        encoding = str(spec_access.get("encoding") or ("terrarium" if kind == "raster-dem" else ""))
        if kind == "raster-dem" and encoding not in ("terrarium", "mapbox"):
            raise MapConfigError(f"access.encoding {encoding!r}: terrain is terrarium or mapbox")
        disc = spec_access.get("discovery")
        if (
            not isinstance(disc, Mapping)
            or str(cast(Mapping[str, Any], disc).get("kind")) not in DISCOVERY_KINDS
        ):
            raise MapConfigError(f"access.discovery.kind must be one of {sorted(DISCOVERY_KINDS)}")
        return cls(
            tileset=tileset,
            kind=kind,
            encoding=encoding,
            priority=int(spec_access.get("priority") or 0),
            attribution=str(spec_access.get("attribution") or ""),
            discovery=cast(Mapping[str, Any], disc),
        )


def map_source(spec_access: Mapping[str, Any] | None) -> MapSource | None:
    """The map settings of a registry ``access`` block, or None if it is not a map source."""
    if not spec_access or spec_access.get("adapter") != ADAPTER:
        return None
    return MapSource.of(spec_access)
