"""wsp weather (docs/components/weather.md): GRIB onto the library's grid, a forecast run into the hot
copy, the providers' indexes, GHCN-Daily records and ERA5 daily fields."""

from __future__ import annotations

import io
import json
import tarfile
import zlib
from datetime import UTC, date, datetime
from importlib import import_module
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from wsp_api import weatherdata as wd
from wsp_cli import weather as w
from wsp_cli import weatherpast as wp
from wsp_core.errors import FetchError

ec: Any = import_module("eccodes")  # untyped

LAT = np.linspace(90, -90, wd.NJ)[:, None] * np.ones((1, wd.NI))
LON = np.ones((wd.NJ, 1)) * (np.arange(wd.NI) * 0.25)[None, :]


def grib(
    short: str,
    values: np.ndarray[Any, Any],
    *,
    lon0: float = 0.0,
    step: int = 6,
    start: int | None = None,
    south_first: bool = False,
) -> bytes:
    """A 0.25° GRIB2 message of ``values`` (``[NJ, NI]`` north to south from ``lon0``)."""
    h = ec.codes_grib_new_from_samples("regular_ll_sfc_grib2")
    for k, v in (
        ("Ni", wd.NI),
        ("Nj", wd.NJ),
        ("iDirectionIncrementInDegrees", 0.25),
        ("jDirectionIncrementInDegrees", 0.25),
    ):
        ec.codes_set(h, k, v)
    if south_first:
        ec.codes_set(h, "jScansPositively", 1)
        ec.codes_set(h, "latitudeOfFirstGridPointInDegrees", -90.0)
        ec.codes_set(h, "latitudeOfLastGridPointInDegrees", 90.0)
        values = values[::-1]
    else:
        ec.codes_set(h, "latitudeOfFirstGridPointInDegrees", 90.0)
        ec.codes_set(h, "latitudeOfLastGridPointInDegrees", -90.0)
    ec.codes_set(h, "longitudeOfFirstGridPointInDegrees", lon0)
    ec.codes_set(h, "longitudeOfLastGridPointInDegrees", (lon0 + 359.75) % 360)
    ec.codes_set(h, "shortName", short)
    if start is not None:
        ec.codes_set(h, "stepType", "accum")
        ec.codes_set(h, "startStep", start)
        ec.codes_set(h, "endStep", step)
    else:
        ec.codes_set(h, "step", step)
    ec.codes_set_values(h, np.asarray(values, dtype=np.float64).ravel())
    msg = ec.codes_get_message(h)
    ec.codes_release(h)
    return bytes(msg)


def test_a_field_lands_on_the_library_grid_whatever_its_scan_order() -> None:
    truth = 250.0 + LAT / 10 + LON / 100  # different everywhere: a misplaced row or column shows
    # ECMWF writes longitudes from 180°, some files run south to north; each comes back the same.
    shifted = np.roll(truth, -720, axis=1)  # column 0 is 180°E
    for msg in (grib("2t", truth), grib("2t", shifted, lon0=180.0), grib("2t", truth, south_first=True)):
        got = w.decode_grib("t2m", msg)
        assert not got.accumulated
        np.testing.assert_allclose(got.values, truth, atol=0.01)


def test_units_become_the_librarys_and_totals_are_marked_accumulated() -> None:
    ones = np.ones((wd.NJ, wd.NI))
    tp = w.decode_grib("tp", grib("tp", ones * 0.004, start=0, step=12))  # ECMWF: metres since the start
    assert tp.accumulated
    np.testing.assert_allclose(tp.values, 4.0, atol=1e-3)
    tcc = w.decode_grib("tcc", grib("tcc", ones * 0.5))  # ECMWF: a fraction
    np.testing.assert_allclose(tcc.values, 50.0, atol=0.01)


class FakeModel:
    id = "fake"
    source = "weather-fake"
    name = "Fake"
    variables: tuple[str, ...] = ("t2m", "tp", "tcc")

    def url(self, run: datetime, step: int) -> str:
        return f"https://example.invalid/{w.run_id(run)}/{step}"

    def steps(self, run: datetime) -> tuple[int, ...]:
        return (0, 6, 12)

    def ready(self, get: w.Getter, run: datetime) -> bool:
        return True

    def messages(self, get: w.Getter, run: datetime, step: int) -> list[w.Message]:
        return [w.Message(v, f"{self.url(run, step)}/{v}", 0, 0) for v in self.variables]


class FakeGet:
    """Accumulated precipitation 0, 2 and 5 mm (metres, ECMWF style); temperature rising 1 K a step."""

    def __init__(self) -> None:
        self.calls = 0

    def get_bytes(self, url: str, *, max_bytes: int = 0) -> bytes:
        raise AssertionError("not used")

    def get_range(self, url: str, start: int, length: int) -> bytes:
        self.calls += 1
        step, var = int(url.split("/")[-2]), url.split("/")[-1]
        ones = np.ones((wd.NJ, wd.NI))
        if var == "t2m":
            return grib("2t", ones * (280.0 + step / 6), step=step)
        if var == "tcc":
            return grib("tcc", ones * 0.25, step=step)
        if step == 0:
            return b""  # no total at step 0: the message is simply absent
        return grib("tp", ones * {6: 0.002, 12: 0.005}[step], start=0, step=step)


def test_a_run_is_written_whole_with_per_interval_precipitation(tmp_path: Path) -> None:
    ws = w.WeatherSettings(root=tmp_path, workers=2)
    get = FakeGet()
    model = FakeModel()
    orig = model.messages

    def messages(g: w.Getter, run: datetime, step: int) -> list[w.Message]:
        return [m for m in orig(g, run, step) if not (step == 0 and m.var == "tp")]

    model.messages = messages  # type: ignore[method-assign]
    run = datetime(2026, 10, 20, 0, tzinfo=UTC)
    path = w.pull_run(model, get, run, ws, keep_grib=True, say=lambda _: None)
    assert path == tmp_path / "hot" / "fake" / "2026102000"
    hr = wd.HotRun(path)
    assert hr.meta.steps == (0, 6, 12)
    row, col = wd.grid_index(51.5, -0.12)
    tp = wd.decode(hr.point("tp", row, col), wd.VARIABLES["tp"])
    assert np.isnan(tp[0])
    np.testing.assert_allclose(tp[1:], [2.0, 3.0], atol=0.01)  # 2 mm, then 5 - 2
    np.testing.assert_allclose(
        wd.decode(hr.point("t2m", row, col), wd.VARIABLES["t2m"]), [280, 281, 282], atol=0.01
    )
    np.testing.assert_allclose(wd.decode(hr.point("tcc", row, col), wd.VARIABLES["tcc"]), 25.0, atol=0.01)
    assert sorted(p.name for p in (path / "grib").iterdir()) == [
        "fake-2026102000-f000.grib2",
        "fake-2026102000-f006.grib2",
        "fake-2026102000-f012.grib2",
    ]
    assert not (path / "tp-acc.f32").exists()


def test_a_failed_run_leaves_nothing_behind(tmp_path: Path) -> None:
    class Broken(FakeGet):
        def get_range(self, url: str, start: int, length: int) -> bytes:
            raise FetchError("connection reset")

    with pytest.raises(FetchError):
        w.pull_run(
            FakeModel(),
            Broken(),
            datetime(2026, 10, 20, tzinfo=UTC),
            w.WeatherSettings(root=tmp_path),
            keep_grib=False,
            say=lambda _: None,
        )
    assert not list((tmp_path / "hot" / "fake").iterdir())


def test_ncep_index_lines_pick_instantaneous_fields_and_the_total_from_the_start() -> None:
    idx = "\n".join(
        [
            "1:0:d=2026102000:PRMSL:mean sea level:6 hour fcst:",
            "581:408342994:d=2026102000:TMP:2 m above ground:6 hour fcst:",
            "595:417090452:d=2026102000:PRATE:surface:0-6 hour ave fcst:",
            "596:417763914:d=2026102000:APCP:surface:3-6 hour acc fcst:",
            "597:418123563:d=2026102000:APCP:surface:0-6 hour acc fcst:",
            "636:435936682:d=2026102000:TCDC:entire atmosphere:6 hour fcst:",
            "637:436776908:d=2026102000:TCDC:entire atmosphere:0-6 hour ave fcst:",
        ]
    )
    lines = w.parse_idx(idx)
    tp = w.gfs_pick(lines, "tp", "APCP", "surface", 6)
    assert tp is not None
    assert (tp.offset, tp.end) == (418123563, 435936682)
    tcc = w.gfs_pick(lines, "tcc", "TCDC", "entire atmosphere", 6)
    assert tcc is not None
    assert tcc.offset == 435936682
    assert w.gfs_pick(lines, "tcc", "TCDC", "entire atmosphere", 9) is None
    last = w.parse_idx(idx)[-1]
    assert last.end is None
    assert w.gfs_pick(lines, "x", "TCDC", "entire atmosphere", 0) is None


def test_candidate_runs_are_every_six_hours_newest_first() -> None:
    runs = w.candidate_runs(datetime(2026, 10, 20, 14, 37, tzinfo=UTC))
    assert [w.run_id(r) for r in runs[:3]] == ["2026102012", "2026102006", "2026102000"]
    assert w.run_id(runs[-1]) == "2026101900"


def test_ecmwf_steps_and_index_parsing() -> None:
    ifs = w.models()["ifs"]
    assert ifs.steps(datetime(2026, 10, 20, 0, tzinfo=UTC))[-3:] == (228, 234, 240)
    assert ifs.steps(datetime(2026, 10, 20, 6, tzinfo=UTC))[-1] == 144

    class Get:
        def get_bytes(self, url: str, *, max_bytes: int = 0) -> bytes:
            rows = [
                {"param": "2t", "levtype": "sfc", "_offset": 10, "_length": 5},
                {"param": "2t", "levtype": "pl", "_offset": 99, "_length": 5},
                {"param": "tp", "levtype": "sfc", "_offset": 20, "_length": 7},
                {"param": "swh", "levtype": "sfc", "_offset": 30, "_length": 7},
            ]
            return "\n".join(json.dumps(r) for r in rows).encode()

        def get_range(self, url: str, start: int, length: int) -> bytes:
            return b""

    msgs = ifs.messages(Get(), datetime(2026, 10, 20, tzinfo=UTC), 24)
    assert [(m.var, m.offset, m.length) for m in msgs] == [("t2m", 10, 5), ("tp", 20, 7)]
    assert msgs[0].url.endswith("/20261020/00z/ifs/0p25/oper/20261020000000-24h-oper-fc.grib2")


def test_archive_versions_name_the_day_and_the_run() -> None:
    assert w.archive_version("2026102000") == "2026-10-20.r2026102000"


# --- stations -------------------------------------------------------------------------------------


def dly_line(sid: str, year: int, month: int, element: str, values: dict[int, tuple[int, str]]) -> str:
    out = f"{sid}{year:04d}{month:02d}{element}"
    for day in range(1, 32):
        v, q = values.get(day, (-9999, " "))
        out += f"{v:5d} {q} "
    return out


def test_dly_records_by_day_of_year_with_failed_quality_left_out() -> None:
    sid = "UK000003772"
    text = "\n".join(
        [
            dly_line(sid, 2024, 3, "TMAX", {1: (123, " "), 2: (999, "X")}),  # 2 March failed a check
            dly_line(sid, 2024, 1, "PRCP", {31: (52, " ")}),
            dly_line(sid, 2024, 1, "WT01", {1: (1, " ")}),  # weather types are not kept
        ]
    )
    got = wp.parse_dly(text)
    assert set(got) == {("TMAX", 2024), ("PRCP", 2024)}
    tmax = got[("TMAX", 2024)]
    assert tmax[60] == 123  # 1 March of a leap year is day 61
    assert tmax[61] == wp.STATION_MISSING
    assert got[("PRCP", 2024)][30] == 52


def test_station_database_from_a_tarball(tmp_path: Path) -> None:
    sid = "UK000003772"
    stations = f"{sid}  51.4780   -0.4610   25.3    LONDON HEATHROW                          \n"
    stations += "ZZ000000001  10.0000   10.0000 -999.9    NOWHERE (NO DATA)\n"
    tar_path = tmp_path / "ghcnd_all.tar.gz"
    with tarfile.open(tar_path, "w:gz") as tar:
        data = (dly_line(sid, 1990, 7, "TMAX", {4: (301, " ")}) + "\n").encode()
        info = tarfile.TarInfo(f"ghcnd_all/{sid}.dly")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    target = tmp_path / "stations.sqlite"
    n = wp.build_stations(
        tar_path, stations, "UK United Kingdom\nZZ Nowhere\n", target, rate=0, say=lambda _: None
    )
    assert n == 1
    import sqlite3

    db = sqlite3.connect(target)
    row = db.execute(
        "SELECT id, name, lat, lon, elevation, country, first_year, last_year, elements FROM stations"
    ).fetchone()
    assert row == (sid, "LONDON HEATHROW", 51.478, -0.461, 25.3, "UK", 1990, 1990, "TMAX")
    blob = db.execute(
        "SELECT data FROM obs WHERE id = ? AND element = 'TMAX' AND year = 1990", (sid,)
    ).fetchone()[0]
    arr = np.frombuffer(zlib.decompress(blob), dtype="<i2")
    assert arr[184] == 301  # 4 July 1990
    assert db.execute("SELECT name FROM countries WHERE code = 'UK'").fetchone()[0] == "United Kingdom"


# --- ERA5 -------------------------------------------------------------------------------------------


class FakeArco:
    """Hour h of a day: temperature 270 + h K; precipitation 0.001 m an hour; the rest constant."""

    def field(self, var: str, t: datetime) -> np.ndarray[Any, np.dtype[np.float32]]:
        base = {"d2m": 265.0, "msl": 101000.0, "u10": 3.0, "v10": 4.0, "tcc": 0.5, "tp": 0.001}
        value = 270.0 + t.hour if var == "t2m" else base[var]
        return np.full((wd.NJ, wd.NI), value, dtype=np.float32)


def test_a_days_fields_come_from_its_hours() -> None:
    got = wp.daily_fields(FakeArco(), date(2026, 10, 1), workers=4)  # type: ignore[arg-type]
    at = got["t2m_min"][100, 200], got["t2m_max"][100, 200], got["t2m_mean"][100, 200]
    assert at == (270.0, 293.0, 281.5)
    assert got["tp_sum"][0, 0] == pytest.approx(24.0)  # 24 hours of 1 mm
    assert got["ws10_mean"][0, 0] == pytest.approx(5.0)
    assert got["tcc_mean"][0, 0] == pytest.approx(50.0)
    assert got["msl_mean"][0, 0] == pytest.approx(101000.0)


def test_era5_hours_count_from_1900() -> None:
    assert wp.hour_index(datetime(2026, 10, 1, 12, tzinfo=UTC)) == 1111068  # the store's own index


def test_a_sealed_year_reaching_the_last_day_is_not_made_again(tmp_path: Path) -> None:
    gen = tmp_path / "library" / "weather" / "era5-daily" / "2026-10-20.y2025"
    (gen / "upstream").mkdir(parents=True)
    wd.write_era5_meta(gen / "upstream", 2025, date(2025, 1, 1), 365, ("t2m_mean",), "x")
    assert not wp.era5_sealed(tmp_path, "weather", 2025, date(2025, 12, 31))  # no MANIFEST: not sealed
    (gen / "MANIFEST.json").write_text("{}")
    assert wp.era5_sealed(tmp_path, "weather", 2025, date(2025, 12, 31))
    assert not wp.era5_sealed(tmp_path, "weather", 2026, date(2026, 1, 5))


def test_only_a_few_steps_are_fetched_ahead_of_the_one_being_written(tmp_path: Path) -> None:
    """2026-10-20: every step of a GFS run was in flight at once, 850 MB held and swapping."""
    import threading
    import time

    class Long(FakeModel):
        def steps(self, run: datetime) -> tuple[int, ...]:
            return tuple(range(0, 60, 6))

        def messages(self, get: w.Getter, run: datetime, step: int) -> list[w.Message]:
            return [w.Message("t2m", f"{self.url(run, step)}/t2m", 0, 0)]

    started: list[int] = []
    first_done = threading.Event()
    lock = threading.Lock()

    class Slow(FakeGet):
        def get_range(self, url: str, start: int, length: int) -> bytes:
            step = int(url.split("/")[-2])
            with lock:
                if not first_done.is_set():
                    started.append(step)
            if step == 0:
                time.sleep(0.5)
            data = super().get_range(url, start, length)
            if step == 0:
                first_done.set()
            return data

    ws = w.WeatherSettings(root=tmp_path, workers=2)
    w.pull_run(Long(), Slow(), datetime(2026, 10, 20, tzinfo=UTC), ws, keep_grib=False, say=lambda _: None)
    assert len(started) <= ws.workers * w.STEPS_AHEAD_PER_WORKER


def test_a_year_is_packed_to_the_last_day_whose_hours_are_published(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """2026-10-20: the store said "to 13 October" but its precipitation stopped at 10 October 00 UTC;
    the year was retried three times and left. It is now packed to the last whole day: 3 January's
    total needs 00 UTC on the 4th, so here the 2nd."""
    from types import SimpleNamespace

    from wsp_core.errors import NotFoundError

    class Partial(FakeArco):
        def field(self, var: str, t: datetime) -> np.ndarray[Any, np.dtype[np.float32]]:
            if var == "tp" and t >= datetime(2026, 1, 4, tzinfo=UTC):
                raise NotFoundError(f"https://example.invalid/tp/{t:%Y%m%d%H}")
            return super().field(var, t)

    vault = tmp_path / "vault"
    from wsp_cli import bootstrap

    def paths(cfg: object) -> SimpleNamespace:
        return SimpleNamespace(require_vault=lambda: vault)

    monkeypatch.setattr(bootstrap, "paths", paths)

    def no_pace(cfg: object) -> None:
        return None

    monkeypatch.setattr(bootstrap, "ingest_pace", no_pace)
    ws = w.WeatherSettings(root=tmp_path / "w")
    out = io.StringIO()
    args: tuple[Any, ...] = (
        Partial(),
        {},
        ws,
        2026,
        date(2026, 1, 1),
        date(2026, 1, 5),
        tmp_path / "w" / "work",
        2,
        out,
        "weather",
    )
    assert wp._era5_year(*args) == 0  # type: ignore[arg-type]
    assert "published to 2026-01-02" in out.getvalue()
    staged = next((vault / "_incoming" / "era5-daily").iterdir())
    meta = json.loads((staged / wd.era5_meta_name(2026)).read_text())
    assert (meta["first"], meta["days"]) == ("2026-01-01", 2)
    # Once that is sealed, the same store gives nothing new (no identical generation every run).
    gen = vault / "library" / "weather" / "era5-daily" / staged.name
    (gen / "upstream").mkdir(parents=True)
    (gen / "upstream" / wd.era5_meta_name(2026)).write_text(json.dumps(meta))
    (gen / "MANIFEST.json").write_text("{}")
    assert wp._era5_year(*args) == 7  # type: ignore[arg-type]
