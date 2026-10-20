"""``wsp weather era5`` and ``wsp weather stations``: the past behind ``wsp://weather``
(``docs/components/weather.md``).

ERA5 (``era5-daily``): ECMWF's reanalysis as the ARCO-ERA5 Zarr store on Google Cloud (public, no
account; one chunk per hour and variable, 721 x 1440 float32, Blosc-LZ4). A day's eight fields come from
24 hourly fields of temperature and precipitation and eight 3-hourly ones of dew point, pressure, wind
and cloud (88 chunks, about 165 MB). Days are kept in a local work directory as they are made (a rerun
continues), then a year (or the current year's first days) is packed and staged into
``_incoming/era5-daily/<cycle>.y<year>/``; ``wsp ingest`` seals it.

Stations (``ghcn-daily``): NOAA's GHCN-Daily, the sealed ``ghcnd_all.tar.gz`` read at the refinery
read rate into ``<index.root>/weather/stations.sqlite``: the station list, and per station, element and
year one zlib-compressed ``int16[366]`` (tenths of °C, tenths of mm, mm; day of year from 1 January;
``-32768`` missing or failing NOAA's quality checks).
"""

from __future__ import annotations

import argparse
import io
import json
import shutil
import sqlite3
import sys
import tarfile
import time
import zlib
from collections.abc import Callable, Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from importlib import import_module
from pathlib import Path
from typing import IO, Any, TextIO, cast

import numpy as np

from wsp_api import weatherdata as wd
from wsp_cli import bootstrap, locks
from wsp_cli.weather import EXIT_FAILED, EXIT_OK, EXIT_REFUSED, Getter, WeatherSettings, stage_files
from wsp_core.errors import FetchError, NotFoundError

ERA5_SOURCE = "era5-daily"
ARCO = "https://storage.googleapis.com/gcp-public-data-arco-era5/ar/full_37-1h-0p25deg-chunk-1.zarr-v3"
ARCO_NAMES = {
    "t2m": "2m_temperature",
    "d2m": "2m_dewpoint_temperature",
    "tp": "total_precipitation",
    "msl": "mean_sea_level_pressure",
    "u10": "10m_u_component_of_wind",
    "v10": "10m_v_component_of_wind",
    "tcc": "total_cloud_cover",
}
ARCO_EPOCH = datetime(1900, 1, 1, tzinfo=UTC)  # the store's time axis: hours since then, from index 0
ERA5_FIRST = date(1940, 1, 1)
SYNOPTIC = tuple(range(0, 24, 3))
END_LAG_DAYS = 14  # a missing hour this close to the store's stated end is "not yet published"


def hour_index(t: datetime) -> int:
    return int((t - ARCO_EPOCH).total_seconds() // 3600)


class Arco:
    """Hourly ERA5 fields from the ARCO store, checked against the layout this reader assumes."""

    def __init__(self, get: Getter) -> None:
        numcodecs: Any = import_module("numcodecs")  # imported on use: only the ERA5 builder reads Zarr

        self.get = get
        self.codec: Any = numcodecs.Blosc()
        for name in ARCO_NAMES.values():
            meta = json.loads(get.get_bytes(f"{ARCO}/{name}/.zarray", max_bytes=1 << 16))
            meta = cast(dict[str, Any], meta)
            layout = (
                meta.get("chunks"),
                meta.get("dtype"),
                cast(dict[str, Any], meta.get("compressor") or {}).get("id"),
            )
            if layout != ([1, wd.NJ, wd.NI], "<f4", "blosc") or meta.get("filters"):
                raise ValueError(f"ARCO-ERA5 {name}: unexpected layout {layout}; this reader needs updating")
        attrs = json.loads(get.get_bytes(f"{ARCO}/.zattrs", max_bytes=1 << 20))
        last = str(attrs.get("valid_time_stop_era5t") or attrs.get("valid_time_stop") or "")
        self.last_hour = datetime.fromisoformat(last[:10]).replace(tzinfo=UTC) + timedelta(hours=23)

    def last_day(self) -> date:
        """The last day whose precipitation (to 00 UTC the next day) is in the store."""
        return (self.last_hour - timedelta(hours=24)).date()

    def field(self, var: str, t: datetime) -> np.ndarray[Any, np.dtype[np.float32]]:
        raw = self.get.get_bytes(f"{ARCO}/{ARCO_NAMES[var]}/{hour_index(t)}.0.0", max_bytes=16 << 20)
        return np.frombuffer(self.codec.decode(raw), dtype="<f4").reshape(wd.NJ, wd.NI)


def daily_fields(arco: Arco, day: date, workers: int) -> dict[str, np.ndarray[Any, np.dtype[np.float64]]]:
    """The day's ERA5 daily fields in the library's units."""
    t0 = datetime(day.year, day.month, day.day, tzinfo=UTC)
    wanted = [("t2m", t0 + timedelta(hours=h)) for h in range(24)]
    wanted += [("tp", t0 + timedelta(hours=h)) for h in range(1, 25)]  # each hour's total ends at its time
    wanted += [(v, t0 + timedelta(hours=h)) for v in ("d2m", "msl", "u10", "v10", "tcc") for h in SYNOPTIC]

    def one(vt: tuple[str, datetime]) -> tuple[str, np.ndarray[Any, np.dtype[np.float32]]]:
        return vt[0], arco.field(vt[0], vt[1])

    with ThreadPoolExecutor(workers, thread_name_prefix="era5") as pool:
        got = list(pool.map(one, wanted))
    by: dict[str, list[np.ndarray[Any, np.dtype[np.float32]]]] = {}
    for var, f in got:
        by.setdefault(var, []).append(f)

    def stack(var: str) -> np.ndarray[Any, np.dtype[np.float64]]:
        return np.stack(by[var]).astype(np.float64)

    t2m = stack("t2m")
    u, v = stack("u10"), stack("v10")
    return {
        "t2m_mean": t2m.mean(axis=0),
        "t2m_min": t2m.min(axis=0),
        "t2m_max": t2m.max(axis=0),
        "d2m_mean": stack("d2m").mean(axis=0),
        "tp_sum": np.maximum(stack("tp").sum(axis=0) * 1000.0, 0.0),
        "msl_mean": stack("msl").mean(axis=0),
        "ws10_mean": np.sqrt(u * u + v * v).mean(axis=0),
        "tcc_mean": stack("tcc").mean(axis=0) * 100.0,
    }


def era5_sealed(vault: Path, domain: str, year: int, last: date) -> bool:
    """A sealed generation of the year's partition that already reaches ``last``."""
    base = vault / "library" / domain / ERA5_SOURCE
    if not base.is_dir():
        return False
    for gen in base.glob(f"*.y{year}"):
        meta = gen / "upstream" / wd.era5_meta_name(year)
        if not (gen / "MANIFEST.json").is_file():
            continue
        for candidate in (meta, gen / wd.era5_meta_name(year)):
            try:
                raw = json.loads(candidate.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            first = date.fromisoformat(str(raw["first"]))
            if first + timedelta(days=int(raw["days"]) - 1) >= last:
                return True
    return False


def cmd_era5(args: argparse.Namespace, out: TextIO) -> int:
    lay = bootstrap.layout(args.config_dir)
    cfg = bootstrap.load(lay)
    ws = WeatherSettings.from_config(cfg)
    year = int(args.year)
    if year < ERA5_FIRST.year or year > datetime.now(UTC).year:
        print(f"error: ERA5 covers {ERA5_FIRST.year} to this year, not {year}", file=sys.stderr)
        return EXIT_REFUSED
    specs = {s.id: s for s in bootstrap.load_sources(cfg, lay)}
    spec = specs.get(ERA5_SOURCE)
    if spec is None:
        print(f"error: no {ERA5_SOURCE} source in the registry", file=sys.stderr)
        return EXIT_REFUSED
    workers = max(1, int(bootstrap.section(cfg, "weather").get("era5_workers", 4)))
    fetcher = bootstrap.build_fetcher(cfg)
    try:
        arco = Arco(cast(Getter, fetcher))
    except (FetchError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_FAILED
    first = date(year, 1, 1)
    last = min(date(year, 12, 31), arco.last_day())
    if last < first:
        out.write(f"era5 {year}: nothing published yet\n")
        return EXIT_OK
    vault = bootstrap.paths(cfg).require_vault()
    if era5_sealed(vault, spec.vault_domain, year, last):
        out.write(f"era5 {year}: sealed to {last}; nothing new\n")
        return 7
    work = ws.root / "era5-work" / str(year)
    try:
        with locks.exclusive(ws.root, f"era5-{year}"):
            return _era5_year(arco, cfg, ws, year, first, last, work, workers, out, spec.vault_domain)
    except locks.LockHeldError:
        out.write(f"era5 {year}: another run is building it\n")
        return locks.EXIT_BUSY


def _era5_year(
    arco: Arco,
    cfg: Mapping[str, Any],
    ws: WeatherSettings,
    year: int,
    first: date,
    last: date,
    work: Path,
    workers: int,
    out: TextIO,
    domain: str,
) -> int:
    days = (last - first).days + 1
    stacks: dict[str, np.memmap[Any, np.dtype[np.uint16]]] = {}
    for var in wd.ERA5_VARS:
        (work).mkdir(parents=True, exist_ok=True)
        path = work / f"{var}.u16"
        full = (date(year, 12, 31) - first).days + 1
        mode = "r+" if path.is_file() and path.stat().st_size == full * wd.NJ * wd.NI * 2 else "w+"
        arr = np.memmap(path, dtype=np.uint16, mode=mode, shape=(full, wd.NJ, wd.NI))
        if mode == "w+":
            arr[:] = wd.MISSING
        stacks[var] = arr
    done_file = work / "days.json"
    try:
        made: set[str] = set(cast(list[str], json.loads(done_file.read_text(encoding="utf-8"))))
    except (OSError, ValueError):
        made = set[str]()
    started = time.monotonic()
    fresh = 0
    for i in range(days):
        day = first + timedelta(days=i)
        if day.isoformat() in made:
            continue
        try:
            fields = daily_fields(arco, day, workers)
        except NotFoundError as exc:
            if (last - day).days > END_LAG_DAYS:
                out.write(f"era5 {year}: {day} missing ({exc}); a rerun continues from here\n")
                _save_days(done_file, made)
                return EXIT_FAILED
            # The store's stated end runs ahead of some variables (2026-10-20: precipitation to
            # 10 October 00 UTC with the store "to 13 October"): the year is packed to the day before.
            out.write(f"era5 {year}: published to {day - timedelta(days=1)} ({exc})\n")
            days, last = i, day - timedelta(days=1)
            break
        except FetchError as exc:
            out.write(f"era5 {year}: {day} failed ({exc}); a rerun continues from here\n")
            _save_days(done_file, made)
            return EXIT_FAILED
        for var, values in fields.items():
            stacks[var][i] = wd.encode(values, wd.VARIABLES[var])
        made.add(day.isoformat())
        fresh += 1
        if fresh % 10 == 0:
            for arr in stacks.values():
                arr.flush()
            _save_days(done_file, made)
            rate = fresh / max(time.monotonic() - started, 1e-6) * 3600
            out.write(f"  era5 {year}: {len(made)}/{days} days ({rate:,.0f} a hour)\n")
            out.flush()
    for arr in stacks.values():
        arr.flush()
    _save_days(done_file, made)
    vault = bootstrap.paths(cfg).require_vault()
    if days <= 0 or era5_sealed(vault, domain, year, last):
        out.write(f"era5 {year}: sealed to {last}; nothing new\n")
        return 7
    pack = work / "pack"
    if pack.exists():
        shutil.rmtree(pack)
    files: list[Path] = []
    for var, arr in stacks.items():
        files += wd.write_era5_var(pack, year, first, var, arr[:days])
    files.append(
        wd.write_era5_meta(
            pack, year, first, days, wd.ERA5_VARS, datetime.now(UTC).isoformat(timespec="seconds")
        )
    )
    stacks.clear()
    version = f"{datetime.now(UTC):%Y-%m-%d}.y{year}"
    size = sum(f.stat().st_size for f in files)
    stage_files(
        vault / "_incoming", ERA5_SOURCE, version, [(f, ARCO) for f in files], bootstrap.ingest_pace(cfg)
    )
    shutil.rmtree(pack)
    if last == date(year, 12, 31):
        shutil.rmtree(work)  # a whole year is packed: its days are not needed again
    out.write(f"era5 {year}: {days} days to {last}, {size / 1e9:,.2f} GB staged as {ERA5_SOURCE}/{version}\n")
    out.write(f"next: wsp ingest --source {ERA5_SOURCE} --version {version}\n")
    return EXIT_OK


def _save_days(path: Path, made: set[str]) -> None:
    wd.atomic_json(path, sorted(made))


# --- stations ----------------------------------------------------------------------------------------

GHCN_SOURCE = "ghcn-daily"
ELEMENTS = ("TMAX", "TMIN", "TAVG", "PRCP", "SNOW", "SNWD")
STATION_MISSING = -32768

SCHEMA = """
CREATE TABLE stations (
  id TEXT PRIMARY KEY, lat REAL NOT NULL, lon REAL NOT NULL, elevation REAL, name TEXT NOT NULL,
  state TEXT, country TEXT, first_year INTEGER, last_year INTEGER, elements TEXT
);
CREATE TABLE obs (id TEXT NOT NULL, element TEXT NOT NULL, year INTEGER NOT NULL, data BLOB NOT NULL,
  PRIMARY KEY (id, element, year)) WITHOUT ROWID;
CREATE TABLE countries (code TEXT PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE info (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def parse_stations(text: str) -> Iterator[tuple[str, float, float, float | None, str, str]]:
    """``ghcnd-stations.txt``: ID 1-11, LAT 13-20, LON 22-30, ELEV 32-37, STATE 39-40, NAME 42-71."""
    for line in text.splitlines():
        if len(line) < 41:
            continue
        try:
            lat, lon = float(line[12:20]), float(line[21:30])
        except ValueError:
            continue
        elev = line[31:37].strip()
        yield (
            line[0:11],
            lat,
            lon,
            float(elev) if elev and float(elev) > -999 else None,
            line[41:71].strip(),
            line[38:40].strip(),
        )


def parse_dly(text: str) -> dict[tuple[str, int], np.ndarray[Any, np.dtype[np.int16]]]:
    """One station's ``.dly``: ``(element, year) -> int16[366]`` by day of year, quality-failed values
    left missing. Line: ID 1-11, YEAR 12-15, MONTH 16-17, ELEMENT 18-21, then 31 x (VALUE 5, M, Q, S)."""
    out: dict[tuple[str, int], np.ndarray[Any, np.dtype[np.int16]]] = {}
    for line in text.splitlines():
        element = line[17:21]
        if element not in ELEMENTS or len(line) < 269:
            continue
        year, month = int(line[11:15]), int(line[15:17])
        key = (element, year)
        arr = out.get(key)
        if arr is None:
            arr = np.full(366, STATION_MISSING, dtype=np.int16)
            out[key] = arr
        start = (date(year, month, 1) - date(year, 1, 1)).days
        days_in = ((date(year + month // 12, month % 12 + 1, 1)) - date(year, month, 1)).days
        for d in range(days_in):
            o = 21 + d * 8
            value = int(line[o : o + 5])
            if value != -9999 and line[o + 6] == " ":
                arr[start + d] = value
    return out


class PacedReader(io.RawIOBase):
    """A file read at most ``rate`` bytes a second (the NAS link is shared with the reader)."""

    def __init__(self, raw: IO[bytes], rate: float) -> None:
        self.raw = raw
        self.rate = rate
        self.t0 = time.monotonic()
        self.sent = 0

    def readable(self) -> bool:
        return True

    def readinto(self, b: Any) -> int:
        data = self.raw.read(len(b))
        n = len(data)
        b[:n] = data
        self.sent += n
        if self.rate > 0:
            ahead = self.sent / self.rate - (time.monotonic() - self.t0)
            if ahead > 0:
                time.sleep(ahead)
        return n


def build_stations(
    tarball: Path,
    stations_txt: str,
    countries_txt: str,
    target: Path,
    *,
    rate: float,
    say: Callable[[str], None],
) -> int:
    """Write ``target`` (via ``target.partial``) from the tarball; the number of stations with data."""
    tmp = target.with_name(target.name + ".partial")
    tmp.unlink(missing_ok=True)
    db = sqlite3.connect(tmp)
    try:
        db.executescript("PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF;" + SCHEMA)
        listed = {row[0]: row for row in parse_stations(stations_txt)}
        db.executemany(
            "INSERT OR REPLACE INTO countries VALUES (?, ?)",
            [(ln[:2], ln[3:].strip()) for ln in countries_txt.splitlines() if len(ln) > 3],
        )
        count = 0
        with (
            tarball.open("rb") as fh,
            tarfile.open(fileobj=io.BufferedReader(PacedReader(fh, rate)), mode="r|gz") as tar,
        ):
            for member in tar:
                if not member.isfile() or not member.name.endswith(".dly"):
                    continue
                sid = Path(member.name).stem
                f = tar.extractfile(member)
                if f is None or sid not in listed:
                    continue
                series = parse_dly(f.read().decode("ascii", "replace"))
                if not series:
                    continue
                db.executemany(
                    "INSERT OR REPLACE INTO obs VALUES (?, ?, ?, ?)",
                    [
                        (sid, el, yr, zlib.compress(arr.astype("<i2").tobytes(), 6))
                        for (el, yr), arr in series.items()
                    ],
                )
                years = [yr for _, yr in series]
                _, lat, lon, elev, name, state = listed[sid]
                db.execute(
                    "INSERT OR REPLACE INTO stations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        sid,
                        lat,
                        lon,
                        elev,
                        name,
                        state,
                        sid[:2],
                        min(years),
                        max(years),
                        ",".join(sorted({e for e, _ in series})),
                    ),
                )
                count += 1
                if count % 5000 == 0:
                    db.commit()
                    say(f"  stations: {count:,} (last {sid} {name})")
        db.execute("CREATE INDEX stations_lat ON stations (lat)")
        db.execute(
            "INSERT INTO info VALUES ('built_at', ?)", (datetime.now(UTC).isoformat(timespec="seconds"),)
        )
        db.execute("INSERT INTO info VALUES ('tarball', ?)", (tarball.name,))
        db.commit()
    finally:
        db.close()
    tmp.replace(target)
    return count


def _ghcn_files(cfg: Mapping[str, Any], lay: Any) -> tuple[Path, Path, Path] | None:
    specs = bootstrap.load_sources(cfg, lay)
    vault = bootstrap.build_vault(cfg, specs, bootstrap.load_signer(cfg, lay, create=False), ledger=False)
    gen = vault.current(GHCN_SOURCE)
    if gen is None:
        return None
    root = Path(vault.generation_path(GHCN_SOURCE, gen.generation_id))
    names = [root / Path(str(a.relpath)) for a in gen.artifacts]

    def one(*needles: str) -> Path | None:
        found = [p for p in names if all(n in p.name.lower() for n in needles)]
        return found[0] if found else None

    tar, st, co = one("all", ".gz"), one("stations"), one("countries")
    return (tar, st, co) if tar and st and co else None


def cmd_stations(args: argparse.Namespace, out: TextIO) -> int:
    lay = bootstrap.layout(args.config_dir)
    cfg = bootstrap.load(lay)
    ws = WeatherSettings.from_config(cfg)
    if args.tarball:
        tar, st, co = Path(args.tarball), Path(args.stations_txt), Path(args.countries_txt)
    else:
        found = _ghcn_files(cfg, lay)
        if found is None:
            print(f"error: no current {GHCN_SOURCE} generation with the tarball and lists", file=sys.stderr)
            return EXIT_REFUSED
        tar, st, co = found
    rate = float(bootstrap.section(cfg, "index").get("refinery_read_mb_per_sec", 30)) * 1e6
    ws.root.mkdir(parents=True, exist_ok=True)
    target = ws.root / "stations.sqlite"
    out.write(f"stations: reading {tar} at {rate / 1e6:g} MB/s into {target}\n")
    out.flush()
    t0 = time.monotonic()

    def say(text: str) -> None:
        out.write(text + "\n")
        out.flush()

    n = build_stations(
        tar, st.read_text("ascii", "replace"), co.read_text("ascii", "replace"), target, rate=rate, say=say
    )
    out.write(f"stations: {n:,} stations in {(time.monotonic() - t0) / 60:.1f} min -> {target}\n")
    return EXIT_OK


def register(add: Any, we_sub: Any) -> None:
    s = add("era5", cmd_era5, "Make a year of ERA5 daily fields and stage it for wsp ingest.", we_sub)
    s.add_argument("--year", required=True, type=int)
    s = add("stations", cmd_stations, "Build the GHCN-Daily station database from the vault.", we_sub)
    s.add_argument("--tarball", default=None, help="a ghcnd_all.tar.gz to read instead of the vault's")
    s.add_argument("--stations-txt", default=None)
    s.add_argument("--countries-txt", default=None)


__all__ = ["Arco", "build_stations", "daily_fields", "parse_dly", "parse_stations", "register"]
