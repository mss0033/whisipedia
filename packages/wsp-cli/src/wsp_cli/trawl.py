"""``wsp trawl report``: what the trawler (navigator/scripts/trawl.mjs) found, and alerts for it.

The trawler opens library pages nobody has opened yet, the way a reader would, and writes
``<state>/trawl/summary.json``. This command prints it and, with ``--alerts``, keeps one warning
open per source whose pages failed the check in the last week (``trawl:<source>``), resolving it
once a week passes without a failure there. The trawler runs it every hour.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, TextIO, cast

from wsp_cli import bootstrap

ALERT_PREFIX = "trawl:"
WINDOW = timedelta(days=7)
EXAMPLES = 3


@dataclass(frozen=True, slots=True)
class SourceFailures:
    source: str
    count: int
    examples: tuple[str, ...]  # "address: problem"


def summary_path(cfg: Mapping[str, Any]) -> Path:
    return bootstrap.paths(cfg).require_state() / "trawl" / "summary.json"


def recent_failures(
    summary: Mapping[str, Any], now: datetime, window: timedelta = WINDOW
) -> list[SourceFailures]:
    """Sources with a failed page in ``window`` before ``now``, most failures first."""
    since = now - window
    by_source: dict[str, list[str]] = {}
    for item in cast(Sequence[Mapping[str, Any]], summary.get("recent") or ()):
        try:
            ts = datetime.fromisoformat(str(item["ts"]).replace("Z", "+00:00"))
        except (KeyError, ValueError):
            continue
        if ts < since:
            continue
        problems = cast(Sequence[str], item.get("problems") or ("?",))
        by_source.setdefault(str(item.get("source", "?")), []).append(
            f"{item.get('address', '?')}: {problems[0]}"
        )
    out = [SourceFailures(s, len(v), tuple(v[-EXAMPLES:])) for s, v in by_source.items()]
    return sorted(out, key=lambda f: (-f.count, f.source))


def render(summary: Mapping[str, Any], failures: Sequence[SourceFailures], out: TextIO) -> None:
    pages = int(summary.get("pages", 0))
    failed = int(summary.get("failed", 0))
    out.write(
        f"trawler: {pages:,} page(s) checked, {failed:,} with problems; "
        f"last {summary.get('updated') or 'never'}\n"
    )
    defects = int(summary.get("with_defects", 0))
    if defects:
        out.write(f"pages that work but have a missing image or a link into nothing: {defects:,}\n")
    sources = cast(Mapping[str, Mapping[str, int]], summary.get("sources") or {})
    cards = sum(int(s.get("catalog_without_link", 0)) for s in sources.values())
    if cards:
        out.write(f"catalog cards without a direct link to their book: {cards:,}\n")
    for f in failures:
        out.write(f"\n{f.source}: {f.count} failed in the last {WINDOW.days} days\n")
        for e in f.examples:
            out.write(f"  {e}\n")


def sync_alerts(cfg: Mapping[str, Any], failures: Sequence[SourceFailures], now: datetime) -> tuple[int, int]:
    """Open ``trawl:<source>`` for each failing source, resolve the others. Returns (opened, resolved)."""
    from wsp_orchestrator.store import StateStore

    state_db = Path(str(bootstrap.section(cfg, "orchestrator").get("state_db") or ""))
    if not state_db.is_file():
        return 0, 0
    store = StateStore(state_db, check_local=False)
    opened = resolved = 0
    try:
        failing = {f.source for f in failures}
        for f in failures:
            message = (
                f"{f.count} page(s) of {f.source} failed the trawler's check in the last {WINDOW.days} days, "
                f"e.g. {f.examples[-1]}"
            )
            data: dict[str, object] = {"source": f.source, "count": f.count, "examples": list(f.examples)}
            opened += store.open_alert(ALERT_PREFIX + f.source, "warning", message, data, now)
        for row in store.open_alerts():
            key = str(row["key"])
            if key.startswith(ALERT_PREFIX) and key[len(ALERT_PREFIX) :] not in failing:
                resolved += store.resolve_alert(key, now)
    finally:
        store.close()
    return opened, resolved


def cmd_trawl_report(args: argparse.Namespace, out: TextIO) -> int:
    lay = bootstrap.layout(args.config_dir)
    cfg = bootstrap.load(lay)
    path = summary_path(cfg)
    if not path.is_file():
        out.write(f"trawler: nothing checked yet ({path} does not exist)\n")
        return 0
    summary = cast(Mapping[str, Any], json.loads(path.read_text(encoding="utf-8")))
    now = datetime.now(UTC)
    failures = recent_failures(summary, now)
    if not args.quiet:
        render(summary, failures, out)
    if args.alerts:
        opened, resolved = sync_alerts(cfg, failures, now)
        if not args.quiet or opened or resolved:
            out.write(f"alerts: {opened} opened, {resolved} resolved\n")
    return 0
