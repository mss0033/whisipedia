"""Weather data on disk (``docs/components/weather.md``): shared by the puller (:mod:`wsp_cli.weather`),
which writes it, and the API (:mod:`wsp_api.weather`), which reads it.

Every field is on one grid, ERA5's and the providers' 0.25° one: :data:`NJ` rows from 90°N to 90°S and
:data:`NI` columns from 0° to 359.75°E. A value is stored as ``n`` in 0..65534 (65535: missing) with
``value = offset + scale * n`` per variable (:data:`VARIABLES`), so a field is 2 MB however it is kept:

* a **hot run** (the newest forecast runs, local disk): ``<hot>/<model>/<run>/<var>.u16``, one
  C-ordered ``uint16`` array ``[steps, NJ, NI]`` per variable, read by memory map; ``meta.json`` last,
  so a directory without it is unfinished. A point's whole forecast is one strided read.
* an **ERA5 year** (vault partition ``era5-daily``/``<cycle>.y<year>``): per variable, the year's maps
  (``era5-<year>-<var>-days.zip``: one PNG per day, served as stored) and its point series
  (``era5-<year>-<var>-tiles.npz``: ``r<row>c<col>`` arrays ``[days, TILE, TILE]``), and
  ``era5-<year>.json`` (first day, number of days, variables).

A map field travels to the browser as an RGB PNG whose red and green bytes are ``n``'s high and low
bytes (:func:`field_png`): lossless, and the browser reads values under the cursor from it.
"""

from __future__ import annotations

import contextlib
import io
import json
import math
import os
import shutil
import threading
import zipfile
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any, cast

import numpy as np
from numpy.typing import NDArray

NI, NJ = 1440, 721  # columns (0..359.75°E), rows (90..-90°N)
STEP_DEG = 0.25
MISSING = 65535
TILE = 40  # ERA5 point-series tiles are TILE x TILE grid points
META = "meta.json"

U16 = NDArray[np.uint16]
F32 = NDArray[np.float32]


@dataclass(frozen=True, slots=True)
class Variable:
    id: str
    name: str
    unit: str  # of the stored value
    offset: float
    scale: float
    kind: str  # temperature | precipitation | pressure | speed | component | percent

    def as_json(self) -> dict[str, object]:
        return {
            "id": self.id,
            "name": self.name,
            "unit": self.unit,
            "offset": self.offset,
            "scale": self.scale,
            "kind": self.kind,
        }


def _v(id_: str, name: str, unit: str, offset: float, scale: float, kind: str) -> Variable:
    return Variable(id_, name, unit, offset, scale, kind)


# Scales: 0.003 K up to 370 K from -100 °C; 0.5 Pa from 850 hPa to 1177 hPa; 0.01 mm to 655 mm;
# wind 0.004 m/s for components (+-131 m/s), 0.003 m/s for speeds (to 196 m/s); cloud 0.002 %.
VARIABLES: Mapping[str, Variable] = {
    v.id: v
    for v in (
        # forecasts
        _v("t2m", "Temperature", "K", 173.15, 0.003, "temperature"),
        _v("d2m", "Dew point", "K", 173.15, 0.003, "temperature"),
        _v("u10", "Wind, eastward", "m/s", -131.0, 0.004, "component"),
        _v("v10", "Wind, northward", "m/s", -131.0, 0.004, "component"),
        _v("gust", "Wind gusts", "m/s", 0.0, 0.003, "speed"),
        _v("msl", "Pressure (sea level)", "Pa", 85000.0, 0.5, "pressure"),
        _v("tp", "Precipitation", "mm", 0.0, 0.01, "precipitation"),
        _v("tcc", "Cloud cover", "%", 0.0, 0.002, "percent"),
        # ERA5 daily
        _v("t2m_mean", "Temperature, daily mean", "K", 173.15, 0.003, "temperature"),
        _v("t2m_min", "Temperature, daily low", "K", 173.15, 0.003, "temperature"),
        _v("t2m_max", "Temperature, daily high", "K", 173.15, 0.003, "temperature"),
        _v("d2m_mean", "Dew point, daily mean", "K", 173.15, 0.003, "temperature"),
        _v("tp_sum", "Precipitation, daily total", "mm", 0.0, 0.01, "precipitation"),
        _v("msl_mean", "Pressure (sea level), daily mean", "Pa", 85000.0, 0.5, "pressure"),
        _v("ws10_mean", "Wind speed, daily mean", "m/s", 0.0, 0.003, "speed"),
        _v("tcc_mean", "Cloud cover, daily mean", "%", 0.0, 0.002, "percent"),
    )
}
FORECAST_VARS: tuple[str, ...] = ("t2m", "d2m", "u10", "v10", "gust", "msl", "tp", "tcc")
ERA5_VARS: tuple[str, ...] = (
    "t2m_mean",
    "t2m_min",
    "t2m_max",
    "d2m_mean",
    "tp_sum",
    "msl_mean",
    "ws10_mean",
    "tcc_mean",
)


def encode(values: NDArray[Any], var: Variable) -> U16:
    """Values (NaN: missing) as ``n``: rounded, clipped to 0..65534."""
    v = np.asarray(values, dtype=np.float64)
    n = np.clip(np.nan_to_num(np.rint((v - var.offset) / var.scale), nan=0.0), 0, MISSING - 1)
    return np.where(np.isnan(v), MISSING, n).astype(np.uint16)


def decode(n: NDArray[Any] | np.integer[Any], var: Variable) -> F32:
    """``n`` back to values; missing is NaN."""
    a = np.asarray(n, dtype=np.uint16)
    values = var.offset + var.scale * a.astype(np.float64)
    return np.where(a == MISSING, np.nan, values).astype(np.float32)


def field_png(n: U16) -> bytes:
    """A field ``[NJ, NI]`` as an RGB PNG: red = high byte, green = low byte, blue 0."""
    from PIL import Image

    rgb = np.zeros((*n.shape, 3), dtype=np.uint8)
    rgb[..., 0] = n >> 8
    rgb[..., 1] = n & 0xFF
    buf = io.BytesIO()
    Image.fromarray(rgb, "RGB").save(buf, "PNG", compress_level=6)
    return buf.getvalue()


def png_field(data: bytes) -> U16:
    """The inverse of :func:`field_png`."""
    from PIL import Image

    rgb = np.asarray(Image.open(io.BytesIO(data)).convert("RGB"))
    return ((rgb[..., 0].astype(np.uint16) << 8) | rgb[..., 1].astype(np.uint16)).astype(np.uint16)


def grid_index(lat: float, lon: float) -> tuple[int, int]:
    """The nearest grid point's ``(row, column)``."""
    if not (math.isfinite(lat) and math.isfinite(lon)) or not -90 <= lat <= 90:
        raise ValueError(f"not a place on Earth: {lat}, {lon}")
    row = round((90.0 - lat) / STEP_DEG)
    col = round((lon % 360.0) / STEP_DEG) % NI
    return min(max(row, 0), NJ - 1), col


def grid_point(row: int, col: int) -> tuple[float, float]:
    """``(lat, lon)`` of a grid point, longitude in -180..180."""
    lon = col * STEP_DEG
    return 90.0 - row * STEP_DEG, lon - 360.0 if lon >= 180.0 else lon


# --- hot forecast runs -------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RunMeta:
    model: str
    run: str  # "YYYYMMDDHH" (UTC)
    steps: tuple[int, ...]  # forecast hours
    variables: tuple[str, ...]
    fetched_at: str  # ISO 8601 UTC
    source: str  # registry source id

    def as_json(self) -> dict[str, object]:
        return {
            "model": self.model,
            "run": self.run,
            "steps": list(self.steps),
            "variables": list(self.variables),
            "fetched_at": self.fetched_at,
            "source": self.source,
        }

    @classmethod
    def from_json(cls, raw: Mapping[str, Any]) -> RunMeta:
        return cls(
            model=str(raw["model"]),
            run=str(raw["run"]),
            steps=tuple(int(s) for s in cast(Sequence[Any], raw["steps"])),
            variables=tuple(str(v) for v in cast(Sequence[Any], raw["variables"])),
            fetched_at=str(raw["fetched_at"]),
            source=str(raw.get("source") or ""),
        )


class HotRunWriter:
    """Writes one run: ``<hot>/<model>/<run>.partial/`` renamed to ``<run>/`` by :meth:`finish`."""

    def __init__(self, hot: Path, meta: RunMeta) -> None:
        self.meta = meta
        self.final = hot / meta.model / meta.run
        self.dir = self.final.with_name(meta.run + ".partial")
        if self.dir.exists():
            shutil.rmtree(self.dir)
        self.dir.mkdir(parents=True)
        shape = (len(meta.steps), NJ, NI)
        self._arrays: dict[str, np.memmap[Any, np.dtype[np.uint16]]] = {}
        for var in meta.variables:
            arr = np.memmap(self.dir / f"{var}.u16", dtype=np.uint16, mode="w+", shape=shape)
            arr[:] = MISSING
            self._arrays[var] = arr

    def put(self, var: str, step_index: int, field: U16) -> None:
        if field.shape != (NJ, NI):
            raise ValueError(f"{var} step {step_index}: field {field.shape}, expected {(NJ, NI)}")
        self._arrays[var][step_index] = field

    def flush(self) -> None:
        """Write what is put so far: dirty mapped pages count against a job's memory until written."""
        for arr in self._arrays.values():
            arr.flush()

    def finish(self) -> Path:
        self.flush()
        self._arrays.clear()
        (self.dir / META).write_text(json.dumps(self.meta.as_json(), indent=1), encoding="utf-8")
        if self.final.exists():
            shutil.rmtree(self.final)
        self.dir.rename(self.final)
        return self.final

    def abandon(self) -> None:
        self._arrays.clear()
        shutil.rmtree(self.dir, ignore_errors=True)


class HotRun:
    """A finished run, read by memory map (opened per variable on first use)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.meta = RunMeta.from_json(json.loads((path / META).read_text(encoding="utf-8")))
        self._maps: dict[str, np.memmap[Any, np.dtype[np.uint16]]] = {}
        self._lock = threading.Lock()

    def array(self, var: str) -> np.memmap[Any, np.dtype[np.uint16]]:
        if var not in self.meta.variables:
            raise KeyError(var)
        with self._lock:
            arr = self._maps.get(var)
            if arr is None:
                shape = (len(self.meta.steps), NJ, NI)
                arr = np.memmap(self.path / f"{var}.u16", dtype=np.uint16, mode="r", shape=shape)
                self._maps[var] = arr
            return arr

    def field(self, var: str, step: int) -> U16:
        return np.array(self.array(var)[self.meta.steps.index(step)])

    def point(self, var: str, row: int, col: int) -> U16:
        return np.array(self.array(var)[:, row, col])


def hot_runs(hot: Path, model: str) -> list[Path]:
    """A model's finished runs, newest first."""
    base = hot / model
    if not base.is_dir():
        return []
    runs = [p for p in base.iterdir() if p.is_dir() and len(p.name) == 10 and (p / META).is_file()]
    return sorted(runs, key=lambda p: p.name, reverse=True)


def prune_hot(hot: Path, model: str, keep: int) -> list[str]:
    """Delete all but the newest ``keep`` runs (and unfinished ones); the runs deleted."""
    gone: list[str] = []
    for p in hot_runs(hot, model)[keep:]:
        shutil.rmtree(p, ignore_errors=True)
        gone.append(p.name)
    base = hot / model
    if base.is_dir():
        for p in base.glob("*.partial"):
            shutil.rmtree(p, ignore_errors=True)
    return gone


# --- ERA5 years --------------------------------------------------------------------------------------


def era5_names(year: int, var: str) -> tuple[str, str]:
    return f"era5-{year}-{var}-days.zip", f"era5-{year}-{var}-tiles.npz"


def era5_meta_name(year: int) -> str:
    return f"era5-{year}.json"


def tiles() -> Iterator[tuple[str, slice, slice]]:
    """Every tile's key and its row and column slices."""
    for r0 in range(0, NJ, TILE):
        for c0 in range(0, NI, TILE):
            yield f"r{r0 // TILE:02}c{c0 // TILE:02}", slice(r0, min(r0 + TILE, NJ)), slice(c0, c0 + TILE)


def tile_of(row: int, col: int) -> tuple[str, int, int]:
    return f"r{row // TILE:02}c{col // TILE:02}", row % TILE, col % TILE


def write_era5_var(out_dir: Path, year: int, first: date, var: str, stack: NDArray[np.uint16]) -> list[Path]:
    """Pack one variable of a year (or of its first days): ``stack`` is ``[days, NJ, NI]`` from ``first``
    (a memory map is fine: it is read a day and a tile at a time)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    days_name, tiles_name = era5_names(year, var)
    tmp = out_dir / (days_name + ".partial")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED) as zf:
        for i in range(stack.shape[0]):
            day = np.asarray(stack[i], dtype=np.uint16)
            zf.writestr(f"{(first + timedelta(days=i)).isoformat()}.png", field_png(day))
    tmp.replace(out_dir / days_name)
    tmp = out_dir / (tiles_name + ".partial")
    with tmp.open("wb") as fh:
        members: dict[str, Any] = {key: np.ascontiguousarray(stack[:, rs, cs]) for key, rs, cs in tiles()}
        np.savez_compressed(fh, **members)
    tmp.replace(out_dir / tiles_name)
    return [out_dir / days_name, out_dir / tiles_name]


def write_era5_meta(
    out_dir: Path, year: int, first: date, days: int, variables: Sequence[str], made_at: str
) -> Path:
    """``era5-<year>.json``, written after every variable's files."""
    meta = {
        "year": year,
        "first": first.isoformat(),
        "days": days,
        "variables": sorted(variables),
        "made_at": made_at,
    }
    path = out_dir / era5_meta_name(year)
    path.write_text(json.dumps(meta, indent=1), encoding="utf-8")
    return path


class Era5Year:
    """One sealed year: maps from its zips, points from its tile archives (members read on demand)."""

    def __init__(self, directory: Path, year: int) -> None:
        self.dir = directory
        self.year = year
        raw = json.loads((directory / era5_meta_name(year)).read_text(encoding="utf-8"))
        self.first = date.fromisoformat(str(raw["first"]))
        self.days = int(raw["days"])
        self.variables = tuple(str(v) for v in raw["variables"])
        self._lock = threading.Lock()
        self._zips: dict[str, zipfile.ZipFile] = {}

    @property
    def last(self) -> date:
        return self.first + timedelta(days=self.days - 1)

    def holds(self, day: date) -> bool:
        return self.first <= day <= self.last

    def day_png(self, var: str, day: date) -> bytes:
        if var not in self.variables or not self.holds(day):
            raise KeyError(f"{var} {day}")
        with self._lock:
            zf = self._zips.get(var)
            if zf is None:
                zf = zipfile.ZipFile(self.dir / era5_names(self.year, var)[0])
                self._zips[var] = zf
            return zf.read(f"{day.isoformat()}.png")

    def point(self, var: str, row: int, col: int) -> U16:
        if var not in self.variables:
            raise KeyError(var)
        key, r, c = tile_of(row, col)
        with np.load(self.dir / era5_names(self.year, var)[1]) as npz:
            return np.array(npz[key][:, r, c])

    def close(self) -> None:
        with self._lock:
            for zf in self._zips.values():
                with contextlib.suppress(Exception):
                    zf.close()
            self._zips.clear()


def atomic_json(path: Path, value: object) -> None:
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(value, indent=1, sort_keys=True), encoding="utf-8")
    tmp.replace(path)
