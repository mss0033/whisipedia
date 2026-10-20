"""``/v1/weather``: forecasts, past weather and station records (``docs/components/weather.md``).

Every answer says how it is available (:class:`Availability`): **offline** (sealed in the vault),
**kept** (a copy of a changing feed, with its age) or **live** (asked of the outside world now: only
``POST /v1/weather/refresh``). The forecasts are the puller's hot copy on the local disk
(``wsp weather pull``); ERA5 is the vault's ``era5-daily`` partitions; stations are the local
GHCN-Daily database (``wsp weather stations``). Reads run in their own pool, like the maps'.
"""

from __future__ import annotations

import json
import logging
import math
import sqlite3
import subprocess
import threading
import time
import zlib
from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Literal, cast

import numpy as np
from fastapi import APIRouter, Query, Request, Response
from fastapi import Path as PathParam
from pydantic import BaseModel, Field

from wsp_api import weatherdata as wd
from wsp_api.browse import is_loopback
from wsp_api.errors import ApiError, errors
from wsp_api.offload import Offload

if TYPE_CHECKING:
    from wsp_api.views import Library

log = logging.getLogger(__name__)

ERA5_SOURCE = "era5-daily"
RESCAN_SECONDS = 60.0
STALE_HOURS = 12  # a run older than this: a newer one should exist (runs every 6 h, out ~5 h later)
ONLINE_HOURS = 1.5  # a feed that answered this recently is "connected" (the timer runs every 30 min)
NORMAL_YEARS = (1991, 2020)  # the WMO climate normal period
PNG_CACHE_BYTES = 96 << 20
FOREVER = "public, max-age=31536000, immutable"  # a run's or a day's field never changes
MODELS: Mapping[str, tuple[str, str]] = {
    "gfs": ("GFS (NOAA)", "NOAA/NCEP Global Forecast System, public domain"),
    "ifs": ("ECMWF IFS", "ECMWF open data, CC BY 4.0"),
    "aifs": ("ECMWF AIFS (machine-learned)", "ECMWF open data, CC BY 4.0"),
}
ERA5_ATTRIBUTION = "Contains modified Copernicus Climate Change Service information (ERA5), ARCO-ERA5 copy"
GHCN_ATTRIBUTION = "NOAA NCEI Global Historical Climatology Network - Daily, public domain"
STATION_ELEMENTS: Mapping[str, tuple[str, str, float]] = {
    "TMAX": ("Daily high", "°C", 0.1),
    "TMIN": ("Daily low", "°C", 0.1),
    "TAVG": ("Daily mean", "°C", 0.1),
    "PRCP": ("Precipitation", "mm", 0.1),
    "SNOW": ("Snowfall", "mm", 1.0),
    "SNWD": ("Snow depth", "mm", 1.0),
}

Kind = Literal["offline", "kept", "live"]
LABELS: Mapping[str, str] = {"offline": "In the library", "kept": "Kept copy", "live": "Needs a connection"}


class Availability(BaseModel):
    """How a piece of data is available: the same three kinds everywhere (``docs/components/weather.md``)."""

    kind: Kind
    label: str
    as_of: str | None = Field(
        default=None, description="What the data describes up to (a run time, a last day)."
    )
    fetched_at: str | None = Field(default=None, description="When the library last copied it (kept data).")
    stale: bool = Field(default=False, description="Newer data should exist than this kept copy.")
    note: str = ""


def availability(kind: Kind, **kw: Any) -> Availability:
    return Availability(kind=kind, label=LABELS[kind], **kw)


class FeedState(BaseModel):
    last_attempt: str | None = None
    last_ok: str | None = None
    last_error: str | None = None
    last_error_at: str | None = None


class VariableInfo(BaseModel):
    id: str
    name: str
    unit: str
    offset: float
    scale: float
    kind: str


class ForecastModel(BaseModel):
    id: str
    name: str
    attribution: str
    run: str | None = Field(default=None, description="The newest run held, YYYYMMDDHH (UTC).")
    run_time: str | None = None
    steps: list[int] = []
    variables: list[str] = []
    availability: Availability
    feed: FeedState


class PastInfo(BaseModel):
    years: list[int]
    first_day: str | None
    last_day: str | None
    variables: list[str]
    attribution: str
    availability: Availability


class StationsInfo(BaseModel):
    stations: int
    built_at: str | None
    attribution: str
    availability: Availability


class Connection(BaseModel):
    online: bool | None = Field(default=None, description="None: never tried.")
    last_ok: str | None = None
    last_error: str | None = None
    last_error_at: str | None = None


class WeatherStatus(BaseModel):
    forecast: list[ForecastModel]
    past: PastInfo
    stations: StationsInfo
    connection: Connection
    variables: list[VariableInfo]
    field_png: str = Field(description="URL templates of map fields; see docs/components/weather.md §3.")
    refresh: Availability


class PointModel(BaseModel):
    model: str
    run: str
    times: list[str]
    values: dict[str, list[float | None]]


class PointForecast(BaseModel):
    lat: float
    lon: float
    grid_lat: float
    grid_lon: float
    models: list[PointModel]
    availability: Availability


class PastSeries(BaseModel):
    lat: float
    lon: float
    grid_lat: float
    grid_lon: float
    days: list[str]
    values: dict[str, list[float | None]]
    normals: dict[str, list[float | None]] = Field(
        default_factory=dict[str, list[float | None]],
        description="The 1991-2020 mean of each day of the year, when those years are held.",
    )
    normal_years: list[int] = []
    availability: Availability


class Station(BaseModel):
    id: str
    name: str
    country: str
    lat: float
    lon: float
    elevation: float | None
    first_year: int | None
    last_year: int | None
    elements: list[str]
    distance_km: float | None = None


class Stations(BaseModel):
    stations: list[Station]
    availability: Availability


class StationSeries(BaseModel):
    station: Station
    days: list[str]
    values: dict[str, list[float | None]]
    units: dict[str, str]
    availability: Availability


class Refresh(BaseModel):
    started: bool
    message: str
    availability: Availability


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _parse(ts: object) -> datetime | None:
    try:
        return datetime.fromisoformat(str(ts)) if ts else None
    except ValueError:
        return None


def _floats(a: np.ndarray[Any, Any], ndigits: int = 2) -> list[float | None]:
    return [None if not math.isfinite(float(x)) else round(float(x), ndigits) for x in a]


class _PngCache:
    def __init__(self, budget: int) -> None:
        self.budget = budget
        self.items: OrderedDict[tuple[str, ...], bytes] = OrderedDict()
        self.size = 0
        self.lock = threading.Lock()

    def get(self, key: tuple[str, ...], make: Callable[[], bytes]) -> bytes:
        with self.lock:
            hit = self.items.get(key)
            if hit is not None:
                self.items.move_to_end(key)
                return hit
        blob = make()
        with self.lock:
            if key not in self.items:
                self.items[key] = blob
                self.size += len(blob)
                while self.size > self.budget and self.items:
                    _, old = self.items.popitem(last=False)
                    self.size -= len(old)
        return blob


class WeatherLibrary:
    """What the library holds for weather: hot runs, ERA5 years, the station database."""

    def __init__(self, root: Path | None, vault: Callable[[], Any], clock: Callable[[], datetime]) -> None:
        self.root = root
        self._vault = vault
        self.clock = clock
        self.pngs = _PngCache(PNG_CACHE_BYTES)
        self._lock = threading.Lock()
        self._runs: dict[Path, wd.HotRun] = {}
        self._years: dict[int, wd.Era5Year] = {}
        self._years_at = 0.0
        self._db: sqlite3.Connection | None = None
        self._db_mtime = 0.0
        self._normals: OrderedDict[tuple[int, int, str], np.ndarray[Any, Any]] = OrderedDict()

    # forecasts

    @property
    def hot(self) -> Path | None:
        return None if self.root is None else self.root / "hot"

    def state(self) -> dict[str, Any]:
        if self.root is None:
            return {}
        try:
            return cast(dict[str, Any], json.loads((self.root / "state.json").read_text(encoding="utf-8")))
        except (OSError, ValueError):
            return {}

    def run(self, model: str, run: str | None = None) -> wd.HotRun | None:
        hot = self.hot
        if hot is None:
            return None
        paths = wd.hot_runs(hot, model)
        if run is not None:
            paths = [p for p in paths if p.name == run]
        if not paths:
            return None
        path = paths[0]
        with self._lock:
            for gone in [p for p in self._runs if not (p / wd.META).is_file()]:
                del self._runs[gone]  # pruned by the puller
            hr = self._runs.get(path)
            if hr is None:
                hr = wd.HotRun(path)
                self._runs[path] = hr
            return hr

    # ERA5

    def years(self) -> dict[int, wd.Era5Year]:
        with self._lock:
            if time.monotonic() - self._years_at < RESCAN_SECONDS and self._years_at:
                return self._years
        found: dict[int, wd.Era5Year] = {}
        vault: Any = None
        gens: list[Any] = []
        try:
            vault = self._vault()
            gens = list(vault.current_set(ERA5_SOURCE))
        except Exception as exc:  # no source yet, an unreachable NAS: no past data, not an error
            log.debug("era5: %s", exc)
        for gen in gens:
            part = str(gen.generation_id).rpartition(".")[2]
            if not (part.startswith("y") and part[1:].isdigit()):
                continue
            year = int(part[1:])
            root = Path(vault.generation_path(ERA5_SOURCE, gen.generation_id))
            metas = [
                root / Path(str(a.relpath))
                for a in gen.artifacts
                if str(a.relpath).endswith(wd.era5_meta_name(year))
            ]
            if not metas:
                continue
            old = self._years.get(year)
            if old is not None and old.dir == metas[0].parent:
                found[year] = old
                continue
            try:
                found[year] = wd.Era5Year(metas[0].parent, year)
            except (OSError, ValueError, KeyError) as exc:
                log.warning("era5 %s: %s", year, exc)
        with self._lock:
            for year, old in self._years.items():
                if found.get(year) is not old:
                    old.close()
            self._years = found
            self._years_at = time.monotonic()
        return found

    def normals(self, row: int, col: int, var: str) -> tuple[np.ndarray[Any, Any], list[int]]:
        """The 1991-2020 mean per day of the year (366 slots, 29 February its own) at a grid point."""
        years = self.years()
        held = [
            y for y in range(NORMAL_YEARS[0], NORMAL_YEARS[1] + 1) if y in years and var in years[y].variables
        ]
        key = (row, col, var)
        with self._lock:
            hit = self._normals.get(key)
        if hit is not None and len(held) == int(hit[-1]):
            return hit[:-1], held
        total = np.zeros(366)
        count = np.zeros(366)
        for y in held:
            ey = years[y]
            vals = wd.decode(ey.point(var, row, col), wd.VARIABLES[var])
            for i, v in enumerate(vals):
                if math.isfinite(float(v)):
                    d = ey.first + timedelta(days=i)
                    slot = _doy(d)
                    total[slot] += float(v)
                    count[slot] += 1
        with np.errstate(invalid="ignore", divide="ignore"):
            mean = np.where(count > 0, total / np.maximum(count, 1), np.nan)
        with self._lock:
            self._normals[key] = np.append(mean, len(held))
            while len(self._normals) > 512:
                self._normals.popitem(last=False)
        return mean, held

    # stations

    def db(self) -> sqlite3.Connection | None:
        if self.root is None:
            return None
        path = self.root / "stations.sqlite"
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return None
        with self._lock:
            if self._db is None or mtime != self._db_mtime:
                if self._db is not None:
                    self._db.close()
                self._db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)
                self._db_mtime = mtime
            return self._db

    def query(self, sql: str, args: Sequence[object] = ()) -> list[tuple[Any, ...]]:
        db = self.db()
        if db is None:
            return []
        with self._lock:
            return list(db.execute(sql, args).fetchall())

    def close(self) -> None:
        with self._lock:
            for y in self._years.values():
                y.close()
            if self._db is not None:
                self._db.close()
                self._db = None


def _doy(d: date) -> int:
    """A day's slot of 366: 1 January is 0, 29 February 59, 1 March always 60."""
    jan1 = date(d.year, 1, 1)
    n = (d - jan1).days
    leap = d.year % 4 == 0 and (d.year % 100 != 0 or d.year % 400 == 0)
    return n if leap or n < 59 else n + 1


def _km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 6371.0 * 2 * math.asin(min(1.0, math.sqrt(a)))


def _station(
    row: Sequence[Any], countries: Mapping[str, str], here: tuple[float, float] | None = None
) -> Station:
    sid, lat, lon, elev, name, _state, country, first, last, elements = row
    return Station(
        id=sid,
        name=name,
        country=countries.get(country, country),
        lat=lat,
        lon=lon,
        elevation=elev,
        first_year=first,
        last_year=last,
        elements=[e for e in str(elements or "").split(",") if e],
        distance_km=None if here is None else round(_km(here[0], here[1], lat, lon), 1),
    )


def weather_router(lib: Library, *, clock: Callable[[], datetime] | None = None) -> APIRouter:
    r = APIRouter(prefix="/v1/weather")
    pool = Offload(workers=4, queue=64, timeout=30.0, name="weather")
    settings = getattr(lib, "settings", None)
    root: Path | None = getattr(settings, "weather", None)
    now = clock or (lambda: datetime.now(UTC))
    w = WeatherLibrary(root, lambda: lib.vault, now)
    lib.weather = w  # type: ignore[attr-defined]  # tests and the System Monitor read it

    def countries() -> dict[str, str]:
        return {code: name for code, name in w.query("SELECT code, name FROM countries")}

    def status() -> WeatherStatus:
        state = w.state()
        feeds = cast(dict[str, dict[str, Any]], state.get("feeds", {}))
        t = now()
        models: list[ForecastModel] = []
        for mid, (name, attribution) in MODELS.items():
            feed = FeedState(**{k: v for k, v in feeds.get(mid, {}).items() if k in FeedState.model_fields})
            hr = w.run(mid)
            if hr is None:
                av = availability(
                    "kept",
                    note="No run is held yet: the first is fetched within half an hour of a connection.",
                )
                models.append(
                    ForecastModel(id=mid, name=name, attribution=attribution, availability=av, feed=feed)
                )
                continue
            run_time = datetime.strptime(hr.meta.run, "%Y%m%d%H").replace(tzinfo=UTC)
            stale = t - run_time > timedelta(hours=STALE_HOURS)
            note = ""
            if stale:
                err, ok = _parse(feed.last_error_at), _parse(feed.last_ok)
                note = (
                    "Newer runs exist, but the library could not reach the provider"
                    + (f" since {feed.last_error_at}" if err and (ok is None or err > ok) else "")
                    + "; this is the newest kept copy."
                    if err and (ok is None or err > ok)
                    else "Newer runs should exist; the next pull fetches them."
                )
            models.append(
                ForecastModel(
                    id=mid,
                    name=name,
                    attribution=attribution,
                    run=hr.meta.run,
                    run_time=_iso(run_time),
                    steps=list(hr.meta.steps),
                    variables=list(hr.meta.variables),
                    availability=availability(
                        "kept", as_of=_iso(run_time), fetched_at=hr.meta.fetched_at, stale=stale, note=note
                    ),
                    feed=feed,
                )
            )
        years = w.years()
        firsts = [y.first for y in years.values()]
        lasts = [y.last for y in years.values()]
        past_note = (
            f"{len(years)} of {t.year - 1940 + 1} years held; the rest are arriving, newest first."
            if years
            else "ERA5 is arriving, newest years first (wsp weather era5)."
        )
        past = PastInfo(
            years=sorted(years),
            first_day=min(firsts).isoformat() if firsts else None,
            last_day=max(lasts).isoformat() if lasts else None,
            variables=list(wd.ERA5_VARS),
            attribution=ERA5_ATTRIBUTION,
            availability=availability(
                "offline", as_of=max(lasts).isoformat() if lasts else None, note=past_note
            ),
        )
        n = w.query("SELECT count(*) FROM stations")
        built = w.query("SELECT value FROM info WHERE key = 'built_at'")
        stations = StationsInfo(
            stations=int(n[0][0]) if n else 0,
            built_at=str(built[0][0]) if built else None,
            attribution=GHCN_ATTRIBUTION,
            availability=availability(
                "offline",
                as_of=str(built[0][0])[:10] if built else None,
                note="" if n else "The station records are arriving (GHCN-Daily, then wsp weather stations).",
            ),
        )
        oks = [x for x in (_parse(f.get("last_ok")) for f in feeds.values()) if x]
        errs = [(x, f) for f in feeds.values() if (x := _parse(f.get("last_error_at")))]
        last_ok = max(oks) if oks else None
        last_err = max(errs, key=lambda e: e[0]) if errs else None
        online: bool | None = None
        if last_ok or last_err:
            online = bool(
                last_ok
                and t - last_ok <= timedelta(hours=ONLINE_HOURS)
                and (last_err is None or last_ok >= last_err[0])
            )
        conn = Connection(
            online=online,
            last_ok=_iso(last_ok) if last_ok else None,
            last_error=str(last_err[1].get("last_error")) if last_err else None,
            last_error_at=_iso(last_err[0]) if last_err else None,
        )
        return WeatherStatus(
            forecast=models,
            past=past,
            stations=stations,
            connection=conn,
            variables=[
                VariableInfo.model_validate(wd.VARIABLES[v].as_json())
                for v in (*wd.FORECAST_VARS, *wd.ERA5_VARS)
            ],
            field_png=(
                "/v1/weather/forecast/{model}/{run}/{var}/{step}.png | /v1/weather/past/{var}/{date}.png"
            ),
            refresh=availability("live", note="Asks the providers for newer runs now."),
        )

    @r.get("", tags=["weather"], response_model=WeatherStatus)
    async def weather_status() -> WeatherStatus:
        """What is held for weather, how each part is available, and the feeds' connection."""
        return await pool.run(status)

    @r.get("/forecast/point", tags=["weather"], response_model=PointForecast, responses=errors(400, 404))
    async def forecast_point(
        lat: Annotated[float, Query(ge=-90, le=90)], lon: Annotated[float, Query(ge=-180, le=360)]
    ) -> PointForecast:
        """Every model's newest run at the grid point nearest a place."""

        def build() -> PointForecast:
            row, col = wd.grid_index(lat, lon)
            out: list[PointModel] = []
            newest: datetime | None = None
            fetched: str | None = None
            for mid in MODELS:
                hr = w.run(mid)
                if hr is None:
                    continue
                t0 = datetime.strptime(hr.meta.run, "%Y%m%d%H").replace(tzinfo=UTC)
                if newest is None or t0 > newest:
                    newest, fetched = t0, hr.meta.fetched_at
                out.append(
                    PointModel(
                        model=mid,
                        run=hr.meta.run,
                        times=[_iso(t0 + timedelta(hours=s)) for s in hr.meta.steps],
                        values={
                            v: _floats(wd.decode(hr.point(v, row, col), wd.VARIABLES[v]))
                            for v in hr.meta.variables
                        },
                    )
                )
            if not out:
                raise ApiError(404, "no_forecast", "no forecast run is held yet (wsp weather pull)")
            glat, glon = wd.grid_point(row, col)
            return PointForecast(
                lat=lat,
                lon=lon,
                grid_lat=glat,
                grid_lon=glon,
                models=out,
                availability=availability(
                    "kept",
                    as_of=_iso(newest) if newest else None,
                    fetched_at=fetched,
                    stale=bool(newest and now() - newest > timedelta(hours=STALE_HOURS)),
                ),
            )

        return await pool.run(build)

    @r.get(
        "/forecast/{model}/{run}/{var}/{step}",
        tags=["weather"],
        responses=errors(404),
        response_class=Response,
    )
    async def forecast_field(
        model: Annotated[str, PathParam(pattern=r"^[a-z]{2,8}$")],
        run: Annotated[str, PathParam(pattern=r"^\d{10}$")],
        var: Annotated[str, PathParam(pattern=r"^[a-z0-9_]{2,12}$")],
        step: Annotated[str, PathParam(pattern=r"^\d{1,3}\.png$")],
    ) -> Response:
        """One variable of a run at one step, as an RG-encoded PNG (``docs/components/weather.md`` §3)."""
        hour = int(step.removesuffix(".png"))

        def build() -> bytes:
            hr = w.run(model, run)
            if hr is None or var not in hr.meta.variables or hour not in hr.meta.steps:
                raise ApiError(404, "no_field", f"no {var} at +{hour} h in {model} {run}")
            return w.pngs.get(("f", model, run, var, str(hour)), lambda: wd.field_png(hr.field(var, hour)))

        blob = await pool.run(build)
        return Response(blob, media_type="image/png", headers={"Cache-Control": FOREVER})

    @r.get("/past/point", tags=["weather"], response_model=PastSeries, responses=errors(400, 404))
    async def past_point(
        lat: Annotated[float, Query(ge=-90, le=90)],
        lon: Annotated[float, Query(ge=-180, le=360)],
        start: Annotated[date | None, Query(alias="from")] = None,
        end: Annotated[date | None, Query(alias="to")] = None,
        var: Annotated[list[str] | None, Query()] = None,
        normals: bool = True,
    ) -> PastSeries:
        """ERA5's daily values at the grid point nearest a place, from ``from`` to ``to`` (default: the
        last year held), with each day's 1991-2020 normal."""
        wanted = list(var or wd.ERA5_VARS)
        bad = [v for v in wanted if v not in wd.ERA5_VARS]
        if bad:
            raise ApiError(400, "bad_variable", f"not ERA5 daily variables: {bad}")

        def build() -> PastSeries:
            years = w.years()
            if not years:
                raise ApiError(404, "no_past", "no ERA5 year is held yet (wsp weather era5)")
            last = max(y.last for y in years.values())
            to = min(end or last, last)
            frm = start or (to - timedelta(days=364))
            if frm > to:
                raise ApiError(400, "bad_range", "from is after to")
            if (to - frm).days > 366 * 100:
                raise ApiError(400, "bad_range", "at most a hundred years at a time")
            row, col = wd.grid_index(lat, lon)
            n = (to - frm).days + 1
            series = {v: np.full(n, np.nan, dtype=np.float32) for v in wanted}
            for y in range(frm.year, to.year + 1):
                ey = years.get(y)
                if ey is None:
                    continue
                for v in wanted:
                    if v not in ey.variables:
                        continue
                    vals = wd.decode(ey.point(v, row, col), wd.VARIABLES[v])
                    lo = max(frm, ey.first)
                    hi = min(to, ey.last)
                    if lo > hi:
                        continue
                    a, b = (lo - ey.first).days, (hi - ey.first).days + 1
                    series[v][(lo - frm).days : (lo - frm).days + (b - a)] = vals[a:b]
            days = [frm + timedelta(days=i) for i in range(n)]
            norm: dict[str, list[float | None]] = {}
            held: list[int] = []
            if normals:
                for v in wanted:
                    mean, held = w.normals(row, col, v)
                    if held:
                        norm[v] = _floats(np.array([mean[_doy(d)] for d in days]))
            glat, glon = wd.grid_point(row, col)
            return PastSeries(
                lat=lat,
                lon=lon,
                grid_lat=glat,
                grid_lon=glon,
                days=[d.isoformat() for d in days],
                values={v: _floats(a) for v, a in series.items()},
                normals=norm,
                normal_years=held,
                availability=availability("offline", as_of=last.isoformat()),
            )

        return await pool.run(build, timeout=60.0)

    @r.get("/past/{var}/{day}", tags=["weather"], responses=errors(404), response_class=Response)
    async def past_field(
        var: Annotated[str, PathParam(pattern=r"^[a-z0-9_]{2,12}$")],
        day: Annotated[str, PathParam(pattern=r"^\d{4}-\d{2}-\d{2}\.png$")],
    ) -> Response:
        """One ERA5 daily field as an RG-encoded PNG, served as sealed."""
        try:
            d = date.fromisoformat(day.removesuffix(".png"))
        except ValueError:
            raise ApiError(404, "no_field", f"not a date: {day}") from None

        def read() -> bytes:
            ey = w.years().get(d.year)
            if ey is None or var not in ey.variables or not ey.holds(d):
                raise ApiError(404, "no_field", f"no {var} for {d} in the library")
            return w.pngs.get(("p", var, d.isoformat()), lambda: ey.day_png(var, d))

        blob = await pool.run(read)
        return Response(blob, media_type="image/png", headers={"Cache-Control": FOREVER})

    @r.get("/stations", tags=["weather"], response_model=Stations)
    async def stations_near(
        lat: Annotated[float, Query(ge=-90, le=90)],
        lon: Annotated[float, Query(ge=-180, le=360)],
        limit: Annotated[int, Query(ge=1, le=50)] = 8,
    ) -> Stations:
        """The GHCN-Daily stations nearest a place, nearest first."""

        def build() -> Stations:
            lon180 = ((lon + 180) % 360) - 180
            names = countries()
            for radius in (1.0, 3.0, 8.0, 25.0):
                rows = w.query(
                    "SELECT * FROM stations WHERE lat BETWEEN ? AND ?", (lat - radius, lat + radius)
                )
                near = [
                    _station(row, names, (lat, lon180))
                    for row in rows
                    if abs(((row[2] - lon180 + 180) % 360) - 180)
                    <= radius / max(math.cos(math.radians(lat)), 0.05)
                ]
                if len(near) >= limit or radius == 25.0:
                    near.sort(key=lambda s: s.distance_km or 0.0)
                    return Stations(stations=near[:limit], availability=availability("offline"))
            return Stations(stations=[], availability=availability("offline"))

        return await pool.run(build)

    @r.get(
        "/stations/{station_id}", tags=["weather"], response_model=StationSeries, responses=errors(404, 400)
    )
    async def station_series(
        station_id: Annotated[str, PathParam(pattern=r"^[A-Z0-9]{11}$")],
        start: Annotated[date | None, Query(alias="from")] = None,
        end: Annotated[date | None, Query(alias="to")] = None,
    ) -> StationSeries:
        """A station's daily records from ``from`` to ``to`` (default: its last year)."""

        def build() -> StationSeries:
            rows = w.query("SELECT * FROM stations WHERE id = ?", (station_id,))
            if not rows:
                raise ApiError(404, "no_station", f"no station {station_id} in the library")
            st = _station(rows[0], countries())
            last_year = st.last_year or date.today().year
            to = end or date(last_year, 12, 31)
            frm = start or date(to.year, 1, 1)
            if frm > to or (to - frm).days > 366 * 300:
                raise ApiError(400, "bad_range", "from must be before to, at most 300 years apart")
            n = (to - frm).days + 1
            values: dict[str, np.ndarray[Any, Any]] = {}
            for el, yr, blob in w.query(
                "SELECT element, year, data FROM obs WHERE id = ? AND year BETWEEN ? AND ?",
                (station_id, frm.year, to.year),
            ):
                if el not in STATION_ELEMENTS:
                    continue
                arr = np.frombuffer(zlib.decompress(blob), dtype="<i2")
                scale = STATION_ELEMENTS[el][2]
                out = values.setdefault(el, np.full(n, np.nan))
                jan1 = date(int(yr), 1, 1)
                for i in range(366):
                    raw = int(arr[i])
                    if raw == -32768:
                        continue
                    d = jan1 + timedelta(days=i)
                    if d.year != yr or not frm <= d <= to:
                        continue
                    out[(d - frm).days] = raw * scale
            return StationSeries(
                station=st,
                days=[(frm + timedelta(days=i)).isoformat() for i in range(n)],
                values={el: _floats(a, 1) for el, a in values.items()},
                units={el: STATION_ELEMENTS[el][1] for el in values},
                availability=availability("offline"),
            )

        return await pool.run(build, timeout=60.0)

    @r.post("/refresh", tags=["weather"], response_model=Refresh, status_code=202, responses=errors(403))
    async def refresh(request: Request) -> Refresh:
        """Start a pull now (loopback only): the one weather action that needs a connection."""
        if not is_loopback(request):
            raise ApiError(403, "local_only", "a refresh can be started from this computer only")

        def start() -> Refresh:
            try:
                done = subprocess.run(
                    ["systemctl", "--user", "start", "--no-block", "whisipedia-weather.service"],  # noqa: S607
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=20,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                return Refresh(
                    started=False,
                    message=f"could not start the pull: {exc}",
                    availability=availability("live"),
                )
            ok = done.returncode == 0
            message = (
                "asking the providers for newer runs" if ok else (done.stderr.strip() or "systemctl failed")
            )
            return Refresh(started=ok, message=message, availability=availability("live"))

        return await pool.run(start)

    return r


__all__ = ["Availability", "WeatherLibrary", "availability", "weather_router"]
