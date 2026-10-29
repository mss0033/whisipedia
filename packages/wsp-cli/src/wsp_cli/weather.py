"""``wsp weather``: the data behind ``wsp://weather`` (``docs/components/weather.md``).

``pull``
    For each forecast model (GFS, ECMWF IFS, ECMWF AIFS), the newest run whose last wanted step is
    published, if it is newer than the one held: each field fetched with a range request from the
    provider's index (``.idx``/``.index``), decoded with ecCodes onto the library's grid
    (:mod:`wsp_api.weatherdata`) and written to the hot copy, ``<index.root>/weather/hot``. A run of
    an archived hour (``[weather] archive_runs``) is also copied, as the provider's GRIB bytes, into
    ``<vault>/_incoming/<source>/<day>.r<run>/`` with its ingest plan, and sealed by ``wsp ingest``
    (retried by later pulls while another ingest holds the lock). Every attempt is recorded in
    ``weather/state.json``: the connection report of ``GET /v1/weather``.
``era5 --year Y``
    The year's daily fields from ERA5's hourly ones (ARCO-ERA5), packed (``write_era5_year``) into
    ``_incoming/era5-daily/<cycle>.y<Y>/`` for ``wsp ingest``.
``stations``
    The GHCN-Daily station records, from the sealed tarball, into ``weather/stations.sqlite``.
``status``
    What the hot copy holds and the last attempts.

Exit codes: 0 done (or nothing newer), 3 another pull is running, 6 a feed failed (the others were
pulled), 2 refused (bad arguments or configuration).
"""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import subprocess
import sys
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from importlib import import_module
from pathlib import Path
from typing import Any, Protocol, TextIO, cast

import numpy as np

from wsp_api import weatherdata as wd
from wsp_cli import bootstrap, locks
from wsp_cli.nasretry import retrying
from wsp_core.errors import FetchError, NotFoundError

EXIT_OK, EXIT_REFUSED, EXIT_FAILED = 0, 2, 6
STATE_FILE = "state.json"
PLANS_SUBDIR = "targets.d"
LOOKBACK_HOURS = 36  # the runs a pull looks at: anything older is not worth fetching
COPY_CHUNK = 8 << 20


# --- configuration -----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class WeatherSettings:
    root: Path  # <index.root>/weather
    keep_runs: int = 2
    archive_runs: tuple[str, ...] = ("00",)
    workers: int = 6
    models: tuple[str, ...] = ("gfs", "ifs", "aifs")

    @property
    def hot(self) -> Path:
        return self.root / "hot"

    @classmethod
    def from_config(cls, cfg: Mapping[str, Any]) -> WeatherSettings:
        w = bootstrap.section(cfg, "weather")
        return cls(
            root=bootstrap.weather_dir(cfg) or bootstrap.paths(cfg).require_index() / "weather",
            keep_runs=max(1, int(w.get("keep_runs", 2))),
            archive_runs=tuple(f"{int(h):02d}" for h in cast(Sequence[Any], w.get("archive_runs", ["00"]))),
            workers=max(1, int(w.get("workers", 6))),
            models=tuple(str(m) for m in cast(Sequence[Any], w.get("models", ["gfs", "ifs", "aifs"]))),
        )


# --- the models --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Message:
    """One GRIB message to fetch: a variable at a step, ``length`` bytes from ``offset`` of ``url``."""

    var: str
    url: str
    offset: int
    length: int


class Getter(Protocol):
    def get_bytes(self, url: str, *, max_bytes: int = ...) -> bytes: ...
    def get_range(self, url: str, start: int, length: int) -> bytes: ...


class Model(Protocol):
    id: str
    source: str
    name: str
    variables: tuple[str, ...]

    def url(self, run: datetime, step: int) -> str: ...
    def steps(self, run: datetime) -> tuple[int, ...]: ...
    def ready(self, get: Getter, run: datetime) -> bool: ...
    def messages(self, get: Getter, run: datetime, step: int) -> list[Message]: ...


def run_id(run: datetime) -> str:
    return run.strftime("%Y%m%d%H")


def candidate_runs(now: datetime, every_hours: int = 6) -> list[datetime]:
    """Run times in the last :data:`LOOKBACK_HOURS`, newest first."""
    base = now.replace(minute=0, second=0, microsecond=0)
    base -= timedelta(hours=base.hour % every_hours)
    return [base - timedelta(hours=h) for h in range(0, LOOKBACK_HOURS + 1, every_hours)]


GFS_BASE = "https://noaa-gfs-bdp-pds.s3.amazonaws.com"
# (variable, idx name, level); the time description is checked separately (instantaneous or 0-N acc).
GFS_FIELDS = (
    ("t2m", "TMP", "2 m above ground"),
    ("d2m", "DPT", "2 m above ground"),
    ("u10", "UGRD", "10 m above ground"),
    ("v10", "VGRD", "10 m above ground"),
    ("gust", "GUST", "surface"),
    ("msl", "PRMSL", "mean sea level"),
    ("tp", "APCP", "surface"),
    ("tcc", "TCDC", "entire atmosphere"),
)


@dataclass(frozen=True, slots=True)
class IdxLine:
    offset: int
    end: int | None  # exclusive; None: to the end of the file
    name: str
    level: str
    when: str


def parse_idx(text: str) -> list[IdxLine]:
    """NCEP's ``.idx``: ``num:offset:d=YYYYMMDDHH:NAME:level:time:...``; each message ends where the
    next begins."""
    rows: list[tuple[int, str, str, str]] = []
    for line in text.splitlines():
        parts = line.split(":")
        if len(parts) >= 6 and parts[1].isdigit():
            rows.append((int(parts[1]), parts[3], parts[4], parts[5]))
    out: list[IdxLine] = []
    for i, (off, name, level, when) in enumerate(rows):
        end = rows[i + 1][0] if i + 1 < len(rows) else None
        out.append(IdxLine(off, end, name, level, when))
    return out


def gfs_pick(lines: Sequence[IdxLine], var: str, name: str, level: str, step: int) -> IdxLine | None:
    """The message of ``var`` at ``step``: precipitation accumulated from the start (``0-N hour acc``),
    everything else instantaneous (``N hour fcst``, ``anl`` at 0)."""
    wanted = {f"0-{step} hour acc fcst"} if var == "tp" else {f"{step} hour fcst", "anl"}
    for ln in lines:
        if ln.name == name and ln.level == level and ln.when in wanted and ln.end is not None:
            return ln
    return None


class Gfs:
    id: str = "gfs"
    source: str = "weather-gfs"
    name: str = "GFS (NOAA)"
    variables: tuple[str, ...] = wd.FORECAST_VARS

    def url(self, run: datetime, step: int) -> str:
        d, h = run.strftime("%Y%m%d"), run.strftime("%H")
        return f"{GFS_BASE}/gfs.{d}/{h}/atmos/gfs.t{h}z.pgrb2.0p25.f{step:03d}"

    def steps(self, run: datetime) -> tuple[int, ...]:
        return tuple(range(0, 241, 3))

    def ready(self, get: Getter, run: datetime) -> bool:
        try:
            get.get_bytes(self.url(run, self.steps(run)[-1]) + ".idx", max_bytes=1 << 20)
        except NotFoundError:
            return False
        return True

    def messages(self, get: Getter, run: datetime, step: int) -> list[Message]:
        url = self.url(run, step)
        lines = parse_idx(get.get_bytes(url + ".idx", max_bytes=1 << 20).decode("utf-8", "replace"))
        out: list[Message] = []
        for var, name, level in GFS_FIELDS:
            ln = gfs_pick(lines, var, name, level, step)
            if ln is not None and ln.end is not None:
                out.append(Message(var, url, ln.offset, ln.end - ln.offset))
        return out


ECMWF_BASE = "https://data.ecmwf.int/forecasts"
ECMWF_PARAMS = {
    "2t": "t2m",
    "2d": "d2m",
    "10u": "u10",
    "10v": "v10",
    "10fg": "gust",
    "msl": "msl",
    "tp": "tp",
    "tcc": "tcc",
}


class Ecmwf:
    def __init__(self, id_: str, product: str, source: str, name: str, variables: tuple[str, ...]) -> None:
        self.id = id_
        self.product = product  # "ifs" | "aifs-single"
        self.source = source
        self.name = name
        self.variables = variables

    def url(self, run: datetime, step: int) -> str:
        d, h = run.strftime("%Y%m%d"), run.strftime("%H")
        return f"{ECMWF_BASE}/{d}/{h}z/{self.product}/0p25/oper/{d}{h}0000-{step}h-oper-fc"

    def steps(self, run: datetime) -> tuple[int, ...]:
        if self.product == "ifs":
            short = tuple(range(0, 145, 3))
            return short + tuple(range(150, 241, 6)) if run.hour in (0, 12) else short
        return tuple(range(0, 241, 6))

    def ready(self, get: Getter, run: datetime) -> bool:
        try:
            get.get_bytes(self.url(run, self.steps(run)[-1]) + ".index", max_bytes=4 << 20)
        except NotFoundError:
            return False
        return True

    def messages(self, get: Getter, run: datetime, step: int) -> list[Message]:
        url = self.url(run, step)
        try:
            text = get.get_bytes(url + ".index", max_bytes=4 << 20).decode("utf-8", "replace")
        except NotFoundError:
            return []  # AIFS publishes no step 0 for some runs
        out: list[Message] = []
        seen: set[str] = set()
        for line in text.splitlines():
            try:
                row = cast(dict[str, Any], json.loads(line))
            except ValueError:
                continue
            var = ECMWF_PARAMS.get(str(row.get("param")))
            if var is None or var not in self.variables or row.get("levtype") != "sfc" or var in seen:
                continue
            seen.add(var)
            out.append(Message(var, url + ".grib2", int(row["_offset"]), int(row["_length"])))
        return out


def models() -> dict[str, Model]:
    no_gust = tuple(v for v in wd.FORECAST_VARS if v != "gust")
    out: dict[str, Model] = {
        "gfs": Gfs(),
        "ifs": Ecmwf("ifs", "ifs", "weather-ecmwf-ifs", "ECMWF IFS", wd.FORECAST_VARS),
        "aifs": Ecmwf("aifs", "aifs-single", "weather-ecmwf-aifs", "ECMWF AIFS (machine-learned)", no_gust),
    }
    return out


# --- decoding ----------------------------------------------------------------------------------------


@dataclass(slots=True)
class Decoded:
    values: np.ndarray[Any, np.dtype[np.float64]]  # [NJ, NI] on the library's grid, library units
    accumulated: bool  # precipitation accumulated from the run's start (to be differenced)


def decode_grib(var: str, message: bytes) -> Decoded:
    """One GRIB message onto the library's grid (north to south, 0°E first), in the variable's units."""
    ec: Any = import_module("eccodes")  # imported on use: only the puller decodes GRIB (untyped)

    h = ec.codes_new_from_message(message)
    try:
        ni, nj = int(ec.codes_get(h, "Ni")), int(ec.codes_get(h, "Nj"))
        if (ni, nj) != (wd.NI, wd.NJ):
            raise ValueError(f"{var}: grid {ni}x{nj}, expected {wd.NI}x{wd.NJ}")
        vals = np.asarray(ec.codes_get_values(h), dtype=np.float64).reshape(nj, ni)
        if int(ec.codes_get(h, "bitmapPresent")):
            vals[vals == float(ec.codes_get(h, "missingValue"))] = np.nan
        if int(ec.codes_get(h, "jScansPositively")):
            vals = vals[::-1]
        if int(ec.codes_get(h, "iScansNegatively")):
            vals = vals[:, ::-1]
        lon0 = float(ec.codes_get(h, "longitudeOfFirstGridPointInDegrees"))
        if int(ec.codes_get(h, "iScansNegatively")):
            lon0 = float(ec.codes_get(h, "longitudeOfLastGridPointInDegrees"))
        shift = round(lon0 / wd.STEP_DEG)
        if shift % wd.NI:
            vals = vals[:, (np.arange(wd.NI) - shift) % wd.NI]
        units = str(ec.codes_get(h, "units"))
        step_range = str(ec.codes_get(h, "stepRange"))
    finally:
        ec.codes_release(h)
    accumulated = False
    if var == "tp":
        if units == "m":
            vals = vals * 1000.0
        start, _, end = step_range.partition("-")
        accumulated = bool(end) and start == "0"
    elif var == "tcc" and units != "%":
        vals = vals * 100.0  # ECMWF writes a fraction, NCEP per cent
    return Decoded(vals, accumulated)


# --- one run -----------------------------------------------------------------------------------------


@dataclass(slots=True)
class StepResult:
    """One step, ready to write: 16-bit fields (2 MB a variable), precipitation accumulated from the
    start as float32 (it is differenced once every step is in), and the GRIB bytes when archived."""

    index: int
    step: int
    fields: dict[str, wd.U16] = field(default_factory=dict[str, wd.U16])
    accumulated: np.ndarray[Any, np.dtype[np.float32]] | None = None
    grib: bytes = b""
    nbytes: int = 0


STEPS_AHEAD_PER_WORKER = 2  # steps fetched ahead of the one being written: bounds the job's memory


def fetch_step(
    model: Model, get: Getter, run: datetime, index: int, step: int, keep_grib: bool
) -> StepResult:
    res = StepResult(index, step)
    raw = bytearray()
    for m in model.messages(get, run, step):
        data = get.get_range(m.url, m.offset, m.length)
        res.nbytes += len(data)
        if keep_grib:
            raw += data
        dec = decode_grib(m.var, data)
        if m.var == "tp" and dec.accumulated:
            res.accumulated = dec.values.astype(np.float32)
        else:
            res.fields[m.var] = wd.encode(dec.values, wd.VARIABLES[m.var])
    res.grib = bytes(raw)
    return res


def pull_run(
    model: Model,
    get: Getter,
    run: datetime,
    ws: WeatherSettings,
    *,
    keep_grib: bool,
    say: Callable[[str], None],
) -> Path:
    """Fetch, decode and write one run to the hot copy; its directory. At most
    :data:`STEPS_AHEAD_PER_WORKER` steps a worker are in memory at once (all 81 of a GFS run at once
    held 850 MB and swapped, 2026-10-20)."""
    steps = model.steps(run)
    meta = wd.RunMeta(
        model=model.id,
        run=run_id(run),
        steps=steps,
        variables=model.variables,
        fetched_at=datetime.now(UTC).isoformat(timespec="seconds"),
        source=model.source,
    )
    writer = wd.HotRunWriter(ws.hot, meta)
    acc_path = writer.dir / "tp-acc.f32"
    acc = np.memmap(acc_path, dtype=np.float32, mode="w+", shape=(len(steps), wd.NJ, wd.NI))
    acc[:] = np.nan
    accumulated = False
    grib_dir = writer.dir / "grib"
    if keep_grib:
        grib_dir.mkdir()
    started, nbytes, done = time.monotonic(), 0, 0
    ahead = max(1, ws.workers * STEPS_AHEAD_PER_WORKER)
    try:
        with ThreadPoolExecutor(ws.workers, thread_name_prefix=f"wx-{model.id}") as pool:
            pending: deque[Future[StepResult]] = deque()
            todo = iter(enumerate(steps))
            for i, st in itertools.islice(todo, ahead):
                pending.append(pool.submit(fetch_step, model, get, run, i, st, keep_grib))
            while pending:
                res = pending.popleft().result()
                nxt = next(todo, None)
                if nxt is not None:
                    pending.append(pool.submit(fetch_step, model, get, run, nxt[0], nxt[1], keep_grib))
                for var, n in res.fields.items():
                    writer.put(var, res.index, n)
                if res.accumulated is not None:
                    accumulated = True
                    acc[res.index] = res.accumulated
                if keep_grib and res.grib:
                    (grib_dir / f"{model.id}-{meta.run}-f{res.step:03d}.grib2").write_bytes(res.grib)
                nbytes += res.nbytes
                done += 1
                if done % 10 == 0:
                    writer.flush()  # written pages become clean, so the job's memory ceiling can drop them
                    acc.flush()
                if done % 20 == 0:
                    say(f"  {model.id} {meta.run}: {done}/{len(steps)} steps, {nbytes / 1e6:,.0f} MB")
        if accumulated:
            # Per-interval precipitation: each step's total less the previous step's (0 before any).
            tp = wd.VARIABLES["tp"]
            prev = np.zeros((wd.NJ, wd.NI), dtype=np.float64)
            for i in range(len(steps)):
                cur = np.asarray(acc[i], dtype=np.float64)
                if np.isnan(cur).all():
                    continue  # no total at this step (step 0)
                writer.put("tp", i, wd.encode(np.maximum(cur - prev, 0.0), tp))
                prev = cur
        del acc
        acc_path.unlink(missing_ok=True)
        path = writer.finish()
    except BaseException:
        writer.abandon()
        raise
    say(
        f"{model.id} {meta.run}: {len(steps)} steps, {nbytes / 1e6:,.0f} MB in "
        f"{time.monotonic() - started:,.0f} s"
    )
    return path


# --- state -------------------------------------------------------------------------------------------


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def load_state(root: Path) -> dict[str, Any]:
    path = root / STATE_FILE
    try:
        return cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return {"feeds": {}, "pending": []}


# --- archiving into the vault ------------------------------------------------------------------------


def archive_version(run: str) -> str:
    """``<YYYY-MM-DD>.r<YYYYMMDDHH>``: the run's day and the run as its partition."""
    return f"{run[:4]}-{run[4:6]}-{run[6:8]}.r{run}"


def copy_hashed(
    src: Path, dest: Path, pace: Callable[[int], None] | None, *, sleep: Callable[[float], None] = time.sleep
) -> str:
    """Copy ``src`` to ``dest`` (through ``dest.partial``) and return its SHA-256, started again when the
    NAS refuses it for a moment (:mod:`wsp_cli.nasretry`: an ERA5 year failed so on 2026-10-24)."""
    return retrying(f"copy {src.name}", lambda: _copy_hashed_once(src, dest, pace), sleep=sleep)


def _copy_hashed_once(src: Path, dest: Path, pace: Callable[[int], None] | None) -> str:
    tmp = dest.with_name(dest.name + ".partial")
    digest = hashlib.sha256()
    with src.open("rb") as fin, tmp.open("wb") as fout:
        while chunk := fin.read(COPY_CHUNK):
            digest.update(chunk)
            fout.write(chunk)
            if pace is not None:
                pace(len(chunk))
    tmp.replace(dest)
    return digest.hexdigest()


def stage_files(
    incoming: Path,
    source: str,
    version: str,
    files: Sequence[tuple[Path, str]],
    pace: Callable[[int], None] | None,
) -> Path:
    """Copy ``(file, url)`` pairs into ``_incoming/<source>/<version>/`` and write their ingest plan."""
    dest_dir = incoming / source / version
    dest_dir.mkdir(parents=True, exist_ok=True)
    targets: list[dict[str, object]] = []
    for path, url in files:
        dest = dest_dir / path.name
        sha = copy_hashed(path, dest, pace)
        targets.append(
            {
                "source_id": source,
                "version": version,
                "name": path.name,
                "uris": [url],
                "lane": "collections",
                "checksum": ["sha-256", sha],
                "size": dest.stat().st_size,
                "meta": {},
            }
        )
    plans = incoming / "_plans" / PLANS_SUBDIR
    plans.mkdir(parents=True, exist_ok=True)
    plan = plans / f"{source}-{version}.json"
    tmp = plan.with_name(plan.name + ".partial")
    tmp.write_text(json.dumps(targets, indent=1), encoding="utf-8")
    tmp.replace(plan)
    return dest_dir


INGEST_WAIT_S = 1200  # a pull waits this long for the lock, then leaves the seal to the next pull


def run_ingest(config_dir: Path | None, source: str, version: str, *, wait: float = 0) -> tuple[int, str]:
    """``wsp ingest --source S --version V`` in a child process: its exit code and last line. With
    ``wait``, it first waits up to that many seconds for its turn in the ingest lock's queue (``--wait``:
    first come, first served; exit 3 when the wait runs out). A raw flock(1) wrapper jumped that queue
    (2026-10-26 review)."""
    argv = [sys.executable, "-m", "wsp_cli"]
    if config_dir is not None:
        argv += ["--config-dir", str(config_dir)]
    argv += ["ingest", "--source", source, "--version", version]
    if wait > 0:
        argv += ["--wait", str(int(wait))]
    done = subprocess.run(argv, capture_output=True, text=True, check=False, timeout=6 * 3600 + wait)  # noqa: S603
    lines = (done.stdout + done.stderr).strip().splitlines()
    return done.returncode, lines[-1] if lines else ""


# --- commands ----------------------------------------------------------------------------------------


def _settings(args: argparse.Namespace) -> tuple[Mapping[str, Any], WeatherSettings]:
    lay = bootstrap.layout(args.config_dir)
    cfg = bootstrap.load(lay)
    return cfg, WeatherSettings.from_config(cfg)


def cmd_pull(args: argparse.Namespace, out: TextIO) -> int:
    cfg, ws = _settings(args)
    ws.root.mkdir(parents=True, exist_ok=True)
    chosen = list(args.model or ws.models)
    known = models()
    unknown = [m for m in chosen if m not in known]
    if unknown:
        print(f"error: unknown model(s) {unknown}; known: {sorted(known)}", file=sys.stderr)
        return EXIT_REFUSED

    def say(text: str) -> None:
        out.write(text + "\n")
        out.flush()

    try:
        with locks.exclusive(ws.root, "pull"):
            return _pull(cfg, ws, [known[m] for m in chosen], args, say)
    except locks.LockHeldError:
        say("another weather pull is running")
        return locks.EXIT_BUSY


def _pull(
    cfg: Mapping[str, Any],
    ws: WeatherSettings,
    chosen: Sequence[Model],
    args: argparse.Namespace,
    say: Callable[[str], None],
) -> int:
    state = load_state(ws.root)
    feeds = cast(dict[str, dict[str, Any]], state.setdefault("feeds", {}))
    pending = cast(list[dict[str, str]], state.setdefault("pending", []))
    fetcher = bootstrap.build_fetcher(cfg)
    get = cast(Getter, fetcher)
    failed = False
    now = datetime.now(UTC)
    for model in chosen:
        feed = feeds.setdefault(model.id, {})
        feed["last_attempt"] = now_iso()
        held = wd.hot_runs(ws.hot, model.id)
        newest_held = held[0].name if held else ""
        try:
            for run in candidate_runs(now):
                rid = run_id(run)
                if rid <= newest_held:
                    say(f"{model.id}: {newest_held} is the newest run published")
                    break
                if not model.ready(get, run):
                    continue
                keep = run.strftime("%H") in ws.archive_runs
                try:
                    pull_run(model, get, run, ws, keep_grib=keep, say=say)
                except NotFoundError as exc:
                    # Steps are published out of order: GFS had f240 up and not f219 (2026-10-21). An
                    # unfinished run is not a failure; the next pull takes it.
                    say(f"{model.id}: {rid} is not all published yet ({exc}); an older run instead")
                    continue
                feed["newest_run"] = rid
                if keep:
                    pending.append({"model": model.id, "source": model.source, "run": rid, "staged": ""})
                break
            feed["last_ok"] = now_iso()
            feed.pop("last_error", None)
        except (FetchError, OSError, ValueError, RuntimeError) as exc:
            failed = True
            feed["last_error"] = f"{type(exc).__name__}: {exc}"[:500]
            feed["last_error_at"] = now_iso()
            say(f"{model.id}: failed: {exc}")
        wd.atomic_json(ws.root / STATE_FILE, state)
        for gone in wd.prune_hot(ws.hot, model.id, ws.keep_runs):
            say(f"{model.id}: {gone} pruned from the hot copy")
    if not args.no_archive:
        _archive(cfg, ws, state, args, say)
    wd.atomic_json(ws.root / STATE_FILE, state)
    return EXIT_FAILED if failed else EXIT_OK


def _archive(
    cfg: Mapping[str, Any],
    ws: WeatherSettings,
    state: dict[str, Any],
    args: argparse.Namespace,
    say: Callable[[str], None],
) -> None:
    """Stage each archived run's GRIB into the vault's ``_incoming`` and ingest what is staged."""
    pending = cast(list[dict[str, str]], state.get("pending", []))
    if not pending:
        return
    vault = bootstrap.paths(cfg).require_vault()
    incoming = vault / "_incoming"
    pace = bootstrap.ingest_pace(cfg)
    known = models()
    left: list[dict[str, str]] = []
    queue_busy = False
    for item in pending:
        model = known.get(item["model"])
        version = archive_version(item["run"])
        if model is None:
            continue
        if not item.get("staged"):
            grib = ws.hot / model.id / item["run"] / "grib"
            files = sorted(grib.glob("*.grib2")) if grib.is_dir() else []
            if not files:
                say(f"{model.id} {item['run']}: no GRIB left to archive (pruned); dropped")
                continue
            run = datetime.strptime(item["run"], "%Y%m%d%H").replace(tzinfo=UTC)
            urls = {f"{model.id}-{item['run']}-f{s:03d}.grib2": model.url(run, s) for s in model.steps(run)}
            stage_files(incoming, model.source, version, [(f, urls.get(f.name, "")) for f in files], pace)
            item["staged"] = now_iso()
            say(f"{model.source}/{version}: {len(files)} GRIB files staged")
        if queue_busy:  # staged above, before the hot copy prunes it; sealed by a later pull
            left.append(item)
            continue
        code, last = run_ingest(args.config_dir, model.source, version, wait=INGEST_WAIT_S)
        if code == 0:
            say(f"{model.source}/{version}: sealed")
            continue
        say(f"{model.source}/{version}: ingest exit {code} ({last}); retried by the next pull")
        left.append(item)
        # The queue was still busy after a full wait: waiting again for each later run only stacked
        # 20-minute waits past the unit's 90-minute limit (2026-10-28, nine lane ingests queued).
        queue_busy = code == locks.EXIT_BUSY
    if queue_busy and len(left) > 1:
        say(f"ingest queue busy: {len(left)} run(s) staged, left for the next pull")
    state["pending"] = left


def cmd_status(args: argparse.Namespace, out: TextIO) -> int:
    _, ws = _settings(args)
    state = load_state(ws.root)
    for mid, model in models().items():
        runs = wd.hot_runs(ws.hot, mid)
        feed = cast(dict[str, Any], state.get("feeds", {}).get(mid, {}))
        out.write(
            f"{mid:5} {model.name:30} hot: {', '.join(p.name for p in runs) or '-':24}"
            f" ok {feed.get('last_ok', '-')}  error {feed.get('last_error', '-')}\n"
        )
    for item in cast(list[dict[str, str]], state.get("pending", [])):
        out.write(f"pending archive: {item['source']} {archive_version(item['run'])}\n")
    return EXIT_OK


def _usage(args: argparse.Namespace, out: TextIO) -> int:
    out.write("usage: wsp weather {pull,status,era5,stations} ...\n")
    return 2


def register(sub: Any, add: Any) -> None:
    """Add ``wsp weather ...`` to the CLI parser (called from :func:`wsp_cli.main.build_parser`)."""
    we = add("weather", _usage, "Forecasts, ERA5 and station records for wsp://weather.")
    we_sub = we.add_subparsers(dest="weather_command", required=True, metavar="COMMAND")
    s = add(
        "pull", cmd_pull, "Fetch each model's newest forecast run into the hot copy (and archive).", we_sub
    )
    s.add_argument("--model", action="append", help="gfs, ifs or aifs (repeatable; default all)")
    s.add_argument("--no-archive", action="store_true", help="do not stage or ingest archived runs")
    add("status", cmd_status, "What the hot copy holds and the feeds' last attempts.", we_sub)
    from wsp_cli import weatherpast

    weatherpast.register(add, we_sub)


__all__ = ["WeatherSettings", "candidate_runs", "decode_grib", "models", "parse_idx", "pull_run", "register"]
