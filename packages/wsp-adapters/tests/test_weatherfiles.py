"""weather-data adapter: a sealed forecast run or ERA5 year is what its name says
(docs/components/weather.md)."""

from __future__ import annotations

import zipfile
from pathlib import Path

from wsp_adapters.weatherfiles import check_file


def test_files_are_checked_by_kind(tmp_path: Path) -> None:
    good = tmp_path / "gfs-2026102000-f000.grib2"
    good.write_bytes(b"GRIB" + b"\0" * 20 + b"7777")
    cut = tmp_path / "cut.grib2"
    cut.write_bytes(b"GRIB" + b"\0" * 20)  # a download that stopped mid-message
    days = tmp_path / "era5-2025-t2m_mean-days.zip"
    with zipfile.ZipFile(days, "w") as zf:
        zf.writestr("2025-01-01.png", b"x")
    empty = tmp_path / "empty.npz"
    with zipfile.ZipFile(empty, "w"):
        pass
    meta = tmp_path / "era5-2025.json"
    meta.write_text('{"year": 2025}')
    broken = tmp_path / "broken.json"
    broken.write_text("{")
    other = tmp_path / "notes.txt"
    other.write_text("hello")
    assert check_file(good) is None
    assert check_file(days) is None
    assert check_file(meta) is None
    assert check_file(cut) == "not a complete GRIB file"
    assert check_file(empty) == "an empty zip"
    assert (check_file(broken) or "").startswith("unreadable")
    assert check_file(other) == "not a weather file (.txt)"
