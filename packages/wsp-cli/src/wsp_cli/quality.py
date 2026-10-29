"""``wsp quality run``: the weekly search-quality check (PLAN §9.5, phase 3's exit criterion).

Runs the gold queries (``packages/wsp-oracle/eval/gold/queries.jsonl``) through the running library's
API, the way a reader's searches go, saves the report under ``<state>/quality/``, and compares it with
the previous one. With ``--alerts`` a drop of nDCG@10 by more than :data:`REGRESSION` opens the
``quality:regression`` warning (in the morning digest), measured against the best of the last
:data:`BASELINE_RUNS` runs; a run back within :data:`REGRESSION` of that score resolves it.
Started weekly by whisipedia-quality.timer.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO, cast

from wsp_cli import bootstrap

REGRESSION = 0.03
BASELINE_RUNS = 8  # the best of this many earlier runs is the baseline a drop is measured against
ALERT_KEY = "quality:regression"


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


def cmd_quality_run(args: argparse.Namespace, out: TextIO) -> int:
    from wsp_oracle.evaluation import DEFAULT_GOLD, api_searcher, evaluate, load_gold

    lay = bootstrap.layout(args.config_dir)
    cfg = bootstrap.load(lay)
    directory = bootstrap.paths(cfg).require_state() / "quality"
    directory.mkdir(parents=True, exist_ok=True)
    now = datetime.now(UTC)
    report = evaluate(
        "api", api_searcher(args.api), load_gold(Path(args.gold) if args.gold else DEFAULT_GOLD), warmup=0
    )
    name = now.strftime("%Y-%m-%dT%H%M%SZ") + ".json"
    saved = {
        "at": now.isoformat(),
        "ndcg10": report.ndcg10,
        "recall50": report.recall50,
        "mrr": report.mrr,
        "p50_ms": report.p50_ms,
        "p95_ms": report.p95_ms,
        "by_domain": dict(report.by_domain),
        "report": asdict(report),
    }
    (directory / name).write_text(json.dumps(saved, indent=1) + "\n", encoding="utf-8")
    out.write(report.summary() + "\n")
    drop = regression(report.ndcg10, previous_report(directory, name))
    if drop is not None:
        out.write(f"against the previous run: nDCG@10 {'-' if drop > 0 else '+'}{abs(drop):.3f}\n")
    fallen = regression(report.ndcg10, baseline(directory, name))
    if fallen is not None and fallen > 0:
        out.write(f"against the best of the last {BASELINE_RUNS} runs: nDCG@10 -{fallen:.3f}\n")
    if args.alerts and fallen is not None:
        from wsp_orchestrator.store import StateStore

        state_db = Path(str(bootstrap.section(cfg, "orchestrator").get("state_db") or ""))
        if state_db.is_file():
            store = StateStore(state_db, check_local=False)
            try:
                if fallen > REGRESSION:
                    store.open_alert(
                        ALERT_KEY,
                        "warning",
                        f"search quality fell: nDCG@10 {report.ndcg10:.3f}, {fallen:.3f} below the best of "
                        f"the last {BASELINE_RUNS} runs",
                        {"ndcg10": report.ndcg10, "drop": fallen, "report": name},
                        now,
                    )
                else:  # back within REGRESSION of the baseline: recovered
                    store.resolve_alert(ALERT_KEY, now)
            finally:
                store.close()
    return 0
