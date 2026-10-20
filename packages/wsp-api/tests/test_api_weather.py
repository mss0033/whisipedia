"""/v1/weather (docs/components/weather.md): the formats on disk, and every answer saying how it is
available: in the library, a kept copy (with its age, out of date when newer runs should exist), or
needing a connection."""

from __future__ import annotations

import json
import sqlite3
import zlib
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from wsp_api import weatherdata as wd
from wsp_api.errors import install_error_handlers
from wsp_api.weather import weather_router
from wsp_core.model import Artifact, Generation, GenerationStatus

NOW = datetime(2026, 10, 20, 14, 0, tzinfo=UTC)


def test_values_survive_the_16_bit_code_and_the_png() -> None:
    t = wd.VARIABLES["t2m"]
    values = np.array([[180.0, 273.15, 330.123, np.nan]])
    n = wd.encode(values, t)
    back = wd.decode(n, t)
    np.testing.assert_allclose(back[0, :3], values[0, :3], atol=t.scale)
    assert np.isnan(back[0, 3])
    assert wd.encode(np.array([1e9]), t)[0] == wd.MISSING - 1  # clipped, never "missing"
    field = np.arange(wd.NJ * wd.NI, dtype=np.uint32).reshape(wd.NJ, wd.NI) % 65536
    assert np.array_equal(wd.png_field(wd.field_png(field.astype(np.uint16))), field)
    assert float(wd.decode(np.uint16(wd.MISSING), t)) != float(wd.decode(np.uint16(0), t))  # a scalar decodes


def test_grid_index_and_point() -> None:
    assert wd.grid_index(90, 0) == (0, 0)
    assert wd.grid_index(-90, 359.9) == (720, 0)
    assert wd.grid_index(51.5, -0.12) == (154, 0)  # -0.12° is nearer 0° than 359.75°
    assert wd.grid_index(51.5, -0.13) == (154, 1439)
    with pytest.raises(ValueError, match="not a place"):
        wd.grid_index(91, 0)
    assert wd.grid_point(154, 1439) == (51.5, -0.25)


def hot_run(root: Path, model: str, run: str, steps: tuple[int, ...], fill: dict[str, float]) -> Path:
    meta = wd.RunMeta(
        model, run, steps, tuple(fill), (NOW - timedelta(hours=1)).isoformat(), f"weather-{model}"
    )
    writer = wd.HotRunWriter(root / "hot", meta)
    for var, value in fill.items():
        for i, st in enumerate(steps):
            writer.put(var, i, wd.encode(np.full((wd.NJ, wd.NI), value + st), wd.VARIABLES[var]))
    return writer.finish()


@dataclass
class FakeVault:
    root: Path
    years: list[int]

    def current_set(self, sid: str) -> list[Generation]:
        assert sid == "era5-daily"
        gens: list[Generation] = []
        for y in self.years:
            gid = f"2026-10-20.y{y}"
            arts = tuple(
                Artifact(sid, gid, PurePosixPath(f"upstream/{n}"), 1, "0" * 64, "0" * 64)
                for n in (wd.era5_meta_name(y), *wd.era5_names(y, "t2m_mean"), *wd.era5_names(y, "tp_sum"))
            )
            gens.append(Generation(sid, gid, GenerationStatus.SEALED, arts, NOW))
        return gens

    def generation_path(self, sid: str, gid: str) -> Path:
        return self.root / sid / gid


def era5_year(root: Path, year: int, first: date, days: int) -> None:
    """``days`` days from ``first``: mean temperature 280 K + the year's last two digits / 10, rising
    0.01 K a day at London's grid point; 1 mm of precipitation a day."""
    out = root / "era5-daily" / f"2026-10-20.y{year}" / "upstream"
    t = np.full((days, wd.NJ, wd.NI), 280.0 + (year % 100) / 10, dtype=np.float32)
    t[:, 154, 0] += np.arange(days, dtype=np.float32) / 100
    wd.write_era5_var(out, year, first, "t2m_mean", wd.encode(t, wd.VARIABLES["t2m_mean"]))
    del t
    rain = np.full((days, wd.NJ, wd.NI), wd.encode(np.array(1.0), wd.VARIABLES["tp_sum"]), dtype=np.uint16)
    wd.write_era5_var(out, year, first, "tp_sum", rain)
    wd.write_era5_meta(out, year, first, days, ("t2m_mean", "tp_sum"), "x")


def stations_db(root: Path) -> None:
    db = sqlite3.connect(root / "stations.sqlite")
    db.executescript(
        "CREATE TABLE stations (id TEXT PRIMARY KEY, lat REAL, lon REAL, elevation REAL, name TEXT,"
        " state TEXT, country TEXT, first_year INTEGER, last_year INTEGER, elements TEXT);"
        "CREATE TABLE obs (id TEXT, element TEXT, year INTEGER, data BLOB, PRIMARY KEY (id, element, year));"
        "CREATE TABLE countries (code TEXT PRIMARY KEY, name TEXT); CREATE TABLE info (key TEXT, value TEXT);"
    )
    db.executemany(
        "INSERT INTO stations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            ("UK000003772", 51.478, -0.461, 25.3, "LONDON HEATHROW", "", "UK", 1948, 2025, "TMAX,PRCP"),
            ("FR000007150", 48.717, 2.383, 89.0, "PARIS ORLY", "", "FR", 1920, 2025, "TMAX"),
            ("US1NYNY0001", 40.7, -74.0, 10.0, "NEW YORK", "NY", "US", 2000, 2025, "PRCP"),
        ],
    )
    db.execute(
        "INSERT INTO countries VALUES ('UK', 'United Kingdom'), ('FR', 'France'), ('US', 'United States')"
    )
    db.execute("INSERT INTO info VALUES ('built_at', '2026-10-19T03:00:00+00:00')")
    tmax = np.full(366, -32768, dtype="<i2")
    tmax[184] = 301  # 4 July 2025... day 185 of a common year is 4 July
    db.execute("INSERT INTO obs VALUES ('UK000003772', 'TMAX', 2025, ?)", (zlib.compress(tmax.tobytes()),))
    db.commit()
    db.close()


def client(tmp_path: Path, *, years: list[int] | None = None, now: datetime = NOW) -> TestClient:
    lib = SimpleNamespace(vault=FakeVault(tmp_path, years or []), settings=SimpleNamespace(weather=tmp_path))
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(weather_router(lib, clock=lambda: now))  # type: ignore[arg-type]
    return TestClient(app, client=("127.0.0.1", 50000))


def test_status_says_what_is_held_and_how(tmp_path: Path) -> None:
    hot_run(tmp_path, "gfs", "2026102006", (0, 6), {"t2m": 280.0})
    (tmp_path / "state.json").write_text(
        json.dumps(
            {
                "feeds": {
                    "gfs": {"last_ok": "2026-10-20T13:30:00+00:00"},
                    "ifs": {
                        "last_error": "boom",
                        "last_error_at": "2026-10-20T13:31:00+00:00",
                        "last_ok": "2026-10-20T07:00:00+00:00",
                    },
                }
            }
        )
    )
    st = client(tmp_path).get("/v1/weather").json()
    gfs, ifs, aifs = st["forecast"]
    assert (gfs["id"], gfs["run"], gfs["run_time"]) == ("gfs", "2026102006", "2026-10-20T06:00:00+00:00")
    assert gfs["availability"]["kind"] == "kept"
    assert gfs["availability"]["label"] == "Kept copy"
    assert not gfs["availability"]["stale"]
    assert ifs["run"] is None
    assert "No run is held yet" in ifs["availability"]["note"]
    assert aifs["feed"]["last_ok"] is None
    assert st["past"]["availability"]["kind"] == "offline"
    assert st["past"]["years"] == []
    assert st["stations"]["stations"] == 0
    assert st["connection"]["online"] is False  # the newest answer is an error
    assert st["connection"]["last_error"] == "boom"
    assert st["refresh"]["kind"] == "live"
    assert {v["id"] for v in st["variables"]} >= {"t2m", "tp_sum"}


def test_an_old_run_is_marked_out_of_date_and_why(tmp_path: Path) -> None:
    hot_run(tmp_path, "gfs", "2026101800", (0,), {"t2m": 280.0})
    (tmp_path / "state.json").write_text(
        json.dumps(
            {
                "feeds": {
                    "gfs": {
                        "last_ok": "2026-10-18T05:00:00+00:00",
                        "last_error": "no route to host",
                        "last_error_at": "2026-10-20T13:00:00+00:00",
                    }
                }
            }
        )
    )
    gfs = client(tmp_path).get("/v1/weather").json()["forecast"][0]
    assert gfs["availability"]["stale"]
    assert "could not reach the provider since 2026-10-20T13:00:00+00:00" in gfs["availability"]["note"]


def test_point_forecast_and_fields(tmp_path: Path) -> None:
    hot_run(tmp_path, "gfs", "2026102006", (0, 6), {"t2m": 280.0, "tp": 1.0})
    hot_run(tmp_path, "aifs", "2026102000", (0, 6, 12), {"t2m": 270.0})
    c = client(tmp_path)
    pf = c.get("/v1/weather/forecast/point", params={"lat": 51.5, "lon": -0.12}).json()
    assert [m["model"] for m in pf["models"]] == ["gfs", "aifs"]
    gfs = pf["models"][0]
    assert gfs["times"] == ["2026-10-20T06:00:00+00:00", "2026-10-20T12:00:00+00:00"]
    assert gfs["values"]["t2m"] == [280.0, 286.0]
    assert gfs["values"]["tp"] == [1.0, 7.0]
    assert pf["availability"]["kind"] == "kept"
    r = c.get("/v1/weather/forecast/gfs/2026102006/t2m/6.png")
    assert r.status_code == 200
    assert r.headers["cache-control"].endswith("immutable")
    assert float(wd.decode(wd.png_field(r.content)[10, 10], wd.VARIABLES["t2m"])) == pytest.approx(
        286.0, abs=0.01
    )
    assert c.get("/v1/weather/forecast/gfs/2026102006/t2m/9.png").json()["error"]["code"] == "no_field"
    assert c.get("/v1/weather/forecast/ifs/2026102006/t2m/6.png").status_code == 404
    assert c.get("/v1/weather/forecast/gfs/2026102006/../6.png").status_code == 404


def test_no_forecast_is_a_404_not_an_error(tmp_path: Path) -> None:
    r = client(tmp_path).get("/v1/weather/forecast/point", params={"lat": 1, "lon": 2})
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "no_forecast"


def test_past_series_with_normals(tmp_path: Path) -> None:
    era5_year(tmp_path, 1991, date(1991, 1, 1), 3)
    era5_year(tmp_path, 1992, date(1992, 1, 1), 3)
    era5_year(tmp_path, 2025, date(2025, 12, 30), 2)
    era5_year(tmp_path, 2026, date(2026, 1, 1), 2)
    c = client(tmp_path, years=[1991, 1992, 2025, 2026])
    st = c.get("/v1/weather").json()["past"]
    assert st["years"] == [1991, 1992, 2025, 2026]
    assert (st["first_day"], st["last_day"]) == ("1991-01-01", "2026-01-02")
    ps = c.get(
        "/v1/weather/past/point",
        params={
            "lat": 51.5,
            "lon": 0.0,
            "from": "2025-12-30",
            "to": "2026-01-03",
            "var": ["t2m_mean", "tp_sum"],
        },
    ).json()
    assert ps["days"] == [
        "2025-12-30",
        "2025-12-31",
        "2026-01-01",
        "2026-01-02",
    ]  # "to" capped at the last day
    assert ps["values"]["t2m_mean"] == pytest.approx([282.5, 282.51, 282.6, 282.61], abs=0.01)
    assert ps["values"]["tp_sum"] == [1.0, 1.0, 1.0, 1.0]
    assert ps["normal_years"] == [1991, 1992]
    # 1 January's normal: the mean of 1991's 289.1 and 1992's 289.2; 30 and 31 December none held
    assert ps["normals"]["t2m_mean"] == pytest.approx([None, None, 289.15, 289.16], abs=0.01)
    assert ps["availability"]["kind"] == "offline"
    day = c.get("/v1/weather/past/t2m_mean/2026-01-02.png")
    assert day.status_code == 200
    assert c.get("/v1/weather/past/t2m_mean/2026-03-01.png").status_code == 404  # not held yet
    assert c.get("/v1/weather/past/point", params={"lat": 1, "lon": 2, "var": ["nope"]}).status_code == 400
    bad = {"lat": 1, "lon": 2, "from": "2026-01-02", "to": "2026-01-01"}
    assert c.get("/v1/weather/past/point", params=bad).status_code == 400


def test_nearest_stations_and_a_record(tmp_path: Path) -> None:
    stations_db(tmp_path)
    c = client(tmp_path)
    st = c.get("/v1/weather").json()["stations"]
    assert st["stations"] == 3
    assert st["availability"]["kind"] == "offline"
    near = c.get("/v1/weather/stations", params={"lat": 51.5, "lon": -0.12, "limit": 2}).json()["stations"]
    assert [s["name"] for s in near] == ["LONDON HEATHROW", "PARIS ORLY"]
    assert near[0]["country"] == "United Kingdom"
    assert near[0]["distance_km"] == pytest.approx(24.0, abs=1.0)
    rec = c.get("/v1/weather/stations/UK000003772", params={"from": "2025-07-03", "to": "2025-07-05"}).json()
    assert rec["days"] == ["2025-07-03", "2025-07-04", "2025-07-05"]
    assert rec["values"]["TMAX"] == [None, 30.1, None]
    assert rec["units"] == {"TMAX": "°C"}
    assert c.get("/v1/weather/stations/XX000000000").status_code == 404


def test_refresh_is_for_this_computer_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    started: list[list[str]] = []

    def run(argv: list[str], **_: Any) -> SimpleNamespace:
        started.append(argv)
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr("wsp_api.weather.subprocess.run", run)
    r = client(tmp_path).post("/v1/weather/refresh")
    assert r.status_code == 202
    assert r.json()["started"]
    assert r.json()["availability"]["kind"] == "live"
    assert started == [["systemctl", "--user", "start", "--no-block", "whisipedia-weather.service"]]
    lib = SimpleNamespace(vault=FakeVault(tmp_path, []), settings=SimpleNamespace(weather=tmp_path))
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(weather_router(lib))  # type: ignore[arg-type]
    far = TestClient(app, client=("203.0.113.9", 50000))
    assert far.post("/v1/weather/refresh").status_code == 403
    assert len(started) == 1


def test_without_a_weather_directory_everything_is_empty_not_broken(tmp_path: Path) -> None:
    lib = SimpleNamespace(vault=FakeVault(tmp_path, []), settings=SimpleNamespace(weather=None))
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(weather_router(lib))  # type: ignore[arg-type]
    c = TestClient(app)
    st = c.get("/v1/weather").json()
    assert [m["run"] for m in st["forecast"]] == [None, None, None]
    assert c.get("/v1/weather/stations", params={"lat": 0, "lon": 0}).json()["stations"] == []


def test_hot_runs_are_listed_newest_first_and_pruned(tmp_path: Path) -> None:
    for run in ("2026102000", "2026102006", "2026101918"):
        hot_run(tmp_path, "gfs", run, (0,), {"t2m": 280.0})
    (tmp_path / "hot" / "gfs" / "2026102012.partial").mkdir()
    assert [p.name for p in wd.hot_runs(tmp_path / "hot", "gfs")] == [
        "2026102006",
        "2026102000",
        "2026101918",
    ]
    assert wd.prune_hot(tmp_path / "hot", "gfs", 2) == ["2026101918"]
    assert sorted(p.name for p in (tmp_path / "hot" / "gfs").iterdir()) == ["2026102000", "2026102006"]
