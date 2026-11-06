"""``wsp quality run``: the weekly search-quality check (PLAN §9.5, phase 3's exit criterion).

Runs the gold queries (``packages/wsp-oracle/eval/gold/queries.jsonl``) through the running library's
API, the way a reader's searches go, saves the report under ``<state>/quality/``, and compares it with
the previous one. With ``--alerts`` a drop of nDCG@10 by more than :data:`REGRESSION` opens the
``quality:regression`` warning (in the morning digest), measured against the best of the last
:data:`BASELINE_RUNS` runs; a run back within :data:`REGRESSION` of that score resolves it.
Started weekly by whisipedia-quality.timer.

Its latencies are a reader's (the same API), so they are watched too: a median over
:data:`LATENCY_FACTOR` times the best of the last :data:`BASELINE_RUNS` quiet runs (and over
:data:`LATENCY_FLOOR_MS`) opens the ``quality:latency`` warning, and a quiet run back under it
resolves it. A run first waits, up to :data:`BUILD_WAIT_S`, for the index build to finish, and records
which of the library's heavy jobs (:data:`BUSY_LOCKS`) ran beside it (``contended``); a contended run
neither opens nor resolves the warning. 2026-11-05 deep dive: the median went from about 4 s to
16-39 s with no alert, and the 11-01 run, inside a catch-up index build, could not tell a slower search
from a busy NAS.
"""

from __future__ import annotations

import argparse
import json
import time
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO, cast

from wsp_cli import bootstrap
from wsp_cli.locks import HEAVY, INDEX, INGEST, held, lock_dir_for

REGRESSION = 0.03
BASELINE_RUNS = 8  # the best of this many earlier runs is the baseline a drop is measured against
ALERT_KEY = "quality:regression"
LATENCY_KEY = "quality:latency"
LATENCY_FACTOR = 2.0  # a median this many times the quiet baseline's is a regression...
LATENCY_FLOOR_MS = 2000.0  # ...when it is also over this (a 0.1 s search taking 0.3 s is no news)
BUILD_LOCKS = (INDEX, HEAVY)  # held by the nightly index build: waited for before the run
BUSY_LOCKS = (INDEX, HEAVY, INGEST)  # the jobs that compete with searches for the NAS and the disk
BUILD_WAIT_S = 6 * 3600.0
BUILD_POLL_S = 300.0


def previous_report(directory: Path, before: str) -> dict[str, Any] | None:
    """The newest saved report older than ``before`` (a file name), or None."""
    files = sorted(p for p in directory.glob("*.json") if p.name < before)
    if not files:
        return None
    try:
        return cast(dict[str, Any], json.loads(files[-1].read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return None


def baseline(directory: Path, before: str, runs: int = BASELINE_RUNS) -> dict[str, Any] | None:
    """The best of the last ``runs`` saved reports older than ``before`` (by nDCG@10), or None.

    Against the previous run alone, a drop resolved itself a week later with no recovery, and a slow
    slide of 0.02 a week never alerted (2026-10-26 review #46)."""
    best: dict[str, Any] | None = None
    for path in sorted(p for p in directory.glob("*.json") if p.name < before)[-runs:]:
        try:
            report = cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))
            score = float(report["ndcg10"])
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if best is None or score > float(best["ndcg10"]):
            best = report
    return best


def regression(now_score: float, before: dict[str, Any] | None) -> float | None:
    """How far nDCG@10 fell against the previous report (None without one)."""
    if before is None:
        return None
    try:
        return float(before["ndcg10"]) - now_score
    except (KeyError, TypeError, ValueError):
        return None


def latency_baseline(directory: Path, before: str, runs: int = BASELINE_RUNS) -> float | None:
    """The best (lowest) median latency, in ms, of the last ``runs`` saved reports older than ``before``
    that ran beside none of :data:`BUSY_LOCKS` (``"contended": []``), or None. Reports from before
    2026-11-05 did not record it and are not used: the 11-01 one ran inside an index build."""
    quiet: list[float] = []
    for path in sorted(p for p in directory.glob("*.json") if p.name < before):
        try:
            report = cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))
            p50 = float(report["p50_ms"])
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if report.get("contended") == [] and p50 > 0:
            quiet.append(p50)
    return min(quiet[-runs:]) if quiet else None


def latency_regressed(p50_ms: float, base_ms: float) -> bool:
    """Whether a median of ``p50_ms`` is a regression against the quiet baseline ``base_ms``."""
    return p50_ms > max(LATENCY_FACTOR * base_ms, LATENCY_FLOOR_MS)


def wait_for_build(
    lock_dir: Path,
    *,
    wait: float = BUILD_WAIT_S,
    poll: float = BUILD_POLL_S,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> float:
    """Wait, up to ``wait`` seconds, until the index build's locks are free; the seconds waited. The
    timer's hour is before the nightly build, but a catch-up build (``Persistent=true``) can fill the
    morning (2026-11-01: 02:13 to 11:22 UTC, the quality run inside it)."""
    start = monotonic()
    while held(lock_dir, BUILD_LOCKS) and monotonic() - start < wait:
        sleep(poll)
    return monotonic() - start


# All of a run's queries start within this: the unit's TimeoutStartSec is 12 h, and a slow library (6 x 180 s
# a query) used to be killed there with nothing saved. The wait for the index build counts.
RUN_BUDGET_S = 11 * 3600


def cmd_quality_run(args: argparse.Namespace, out: TextIO) -> int:
    from wsp_oracle.evaluation import DEFAULT_GOLD, api_searcher, evaluate, load_gold

    lay = bootstrap.layout(args.config_dir)
    cfg = bootstrap.load(lay)
    directory = bootstrap.paths(cfg).require_state() / "quality"
    directory.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    index_root = bootstrap.paths(cfg).index_root
    lock_dir = None if index_root is None else lock_dir_for(index_root)
    waited = 0.0
    busy: set[str] = set()
    if lock_dir is not None:
        waited = wait_for_build(lock_dir)
        if waited >= 1:
            out.write(f"waited {waited / 60:.0f} min for the index build to finish\n")
        busy = held(lock_dir, BUSY_LOCKS)
    now = datetime.now(UTC)
    report = evaluate(
        "api",
        api_searcher(args.api),
        load_gold(Path(args.gold) if args.gold else DEFAULT_GOLD),
        warmup=0,
        deadline=started + RUN_BUDGET_S,
    )
    if lock_dir is not None:
        busy |= held(lock_dir, BUSY_LOCKS)
    name = now.strftime("%Y-%m-%dT%H%M%SZ") + ".json"
    saved = {
        "at": now.isoformat(),
        "ndcg10": report.ndcg10,
        "recall50": report.recall50,
        "mrr": report.mrr,
        "p50_ms": report.p50_ms,
        "p95_ms": report.p95_ms,
        "by_domain": dict(report.by_domain),
        "skipped": report.skipped,
        # The heavy jobs that ran beside it (seen at its start or its end); None: unknown (no index.root).
        "contended": None if lock_dir is None else sorted(busy),
        "waited_s": round(waited),
        "report": asdict(report),
    }
    tmp = directory / (name + ".partial")
    tmp.write_text(json.dumps(saved, indent=1) + "\n", encoding="utf-8")
    tmp.replace(directory / name)
    out.write(report.summary() + "\n")
    drop = regression(report.ndcg10, previous_report(directory, name))
    if drop is not None:
        out.write(f"against the previous run: nDCG@10 {'-' if drop > 0 else '+'}{abs(drop):.3f}\n")
    fallen = regression(report.ndcg10, baseline(directory, name))
    if fallen is not None and fallen > 0:
        out.write(f"against the best of the last {BASELINE_RUNS} runs: nDCG@10 -{fallen:.3f}\n")
    quiet_ms = latency_baseline(directory, name)
    slow: bool | None = None  # None: this run says nothing about latency
    if busy:
        out.write(f"latency not judged: {', '.join(sorted(busy))} ran beside it\n")
    elif lock_dir is not None and quiet_ms is not None and report.p50_ms > 0:
        slow = latency_regressed(report.p50_ms, quiet_ms)
        out.write(f"median latency {report.p50_ms:.0f} ms; the best of the quiet runs {quiet_ms:.0f} ms\n")
    if args.alerts and (fallen is not None or slow is not None):
        from wsp_orchestrator.store import StateStore

        state_db = Path(str(bootstrap.section(cfg, "orchestrator").get("state_db") or ""))
        if state_db.is_file():
            store = StateStore(state_db, check_local=False)
            try:
                if fallen is not None and fallen > REGRESSION:
                    store.open_alert(
                        ALERT_KEY,
                        "warning",
                        f"search quality fell: nDCG@10 {report.ndcg10:.3f}, {fallen:.3f} below the best of "
                        f"the last {BASELINE_RUNS} runs",
                        {"ndcg10": report.ndcg10, "drop": fallen, "report": name},
                        now,
                    )
                elif fallen is not None:  # back within REGRESSION of the baseline: recovered
                    store.resolve_alert(ALERT_KEY, now)
                if slow and quiet_ms is not None:
                    store.open_alert(
                        LATENCY_KEY,
                        "warning",
                        f"search got slower: median {report.p50_ms / 1000:.1f} s against "
                        f"{quiet_ms / 1000:.1f} s, the best of the last {BASELINE_RUNS} runs with nothing "
                        "heavy beside them",
                        {"p50_ms": report.p50_ms, "baseline_ms": quiet_ms, "report": name},
                        now,
                    )
                elif slow is not None:
                    store.resolve_alert(LATENCY_KEY, now)
            finally:
                store.close()
    return 0
