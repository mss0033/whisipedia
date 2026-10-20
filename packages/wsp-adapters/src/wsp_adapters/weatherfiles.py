"""``weather-data`` adapter: forecast runs and ERA5 years for ``wsp://weather``
(``docs/components/weather.md``).

Like the map sources, weather sources are partitioned (ADR-0010) and fetched by their own command
(``wsp weather pull`` stages each archived run, ``wsp weather era5`` each year, into
``<vault>/_incoming``); the orchestrator's discovery finds nothing to do. ``verify`` checks a file is
what its name says, beyond the SHA-256 the ingest plan carries:

* ``.grib2``: GRIB messages, the first starting ``GRIB`` and the file ending ``7777``;
* ``.zip`` / ``.npz``: a readable zip directory with members;
* ``.json``: JSON.

Nothing is searched: the API draws and charts these files (``index: false``).
"""

from __future__ import annotations

import json
import zipfile
from collections.abc import Iterable, Iterator
from pathlib import Path

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
from wsp_core.ports import Ctx

ADAPTER = "weather-data"


def check_file(path: Path) -> str | None:
    """What is wrong with a weather file, or None."""
    suffix = path.suffix.lower()
    try:
        if suffix == ".grib2":
            size = path.stat().st_size
            with path.open("rb") as fh:
                head = fh.read(4)
                fh.seek(max(0, size - 4))
                tail = fh.read(4)
            if head != b"GRIB" or tail != b"7777":
                return "not a complete GRIB file"
            return None
        if suffix in (".zip", ".npz"):
            with zipfile.ZipFile(path) as zf:
                if not zf.namelist():
                    return "an empty zip"
            return None
        if suffix == ".json":
            json.loads(path.read_text(encoding="utf-8"))
            return None
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        return f"unreadable: {exc}"
    return f"not a weather file ({suffix or 'no extension'})"


class WeatherDataAdapter:
    """``SourceAdapter`` for forecast runs and ERA5 years."""

    id = ADAPTER

    def discover(self, ctx: Ctx) -> Iterable[ReleaseRef]:
        return []

    def plan(self, ctx: Ctx, have: Generation | None, want: ReleaseRef) -> FetchPlan:
        return FetchPlan(want, ())

    def fetch(self, ctx: Ctx, plan: FetchPlan) -> Iterable[FetchResult]:
        return []

    def verify(self, ctx: Ctx, remote: RemoteFile, local: Path) -> Verdict:
        problem = check_file(local)
        if problem:
            return Verdict(VerdictStatus.MISMATCH, problem)
        return Verdict(VerdictStatus.OK, "weather data")

    def documents(self, ctx: Ctx, generation_dir: Path) -> Iterator[DocRecord]:
        return iter(())  # drawn and charted, not searched (index: false)


__all__ = ["ADAPTER", "WeatherDataAdapter", "check_file"]
