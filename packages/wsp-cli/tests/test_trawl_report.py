import io
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from wsp_cli.trawl import SourceFailures, recent_failures, render

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)


def _item(source: str, days_ago: float, problem: str = "empty") -> dict[str, object]:
    ts = (NOW - timedelta(days=days_ago)).isoformat().replace("+00:00", "Z")
    return {"ts": ts, "source": source, "address": f"wsp://{source}/p{days_ago}", "problems": [problem]}


def test_failures_of_the_last_week_are_grouped_by_source() -> None:
    summary = {
        "recent": [
            _item("a", 1),
            _item("b", 2, "image 404 /v1/assets/x"),
            _item("a", 3),
            _item("c", 9),  # older than a week
            {"ts": "not a time", "source": "d"},
        ]
    }
    got = recent_failures(summary, NOW)
    assert [(f.source, f.count) for f in got] == [("a", 2), ("b", 1)]
    assert got[1].examples == ("wsp://b/p2: image 404 /v1/assets/x",)


def test_the_report_prints_totals_cards_and_examples() -> None:
    summary = {
        "pages": 1200,
        "failed": 3,
        "updated": "2026-10-14T11:00:00Z",
        "sources": {"ol": {"checked": 10, "failed": 0, "catalog_without_link": 4}},
    }
    out = io.StringIO()
    render(summary, [SourceFailures("a", 2, ("wsp://a/x: empty",))], out)
    text = out.getvalue()
    assert "1,200 page(s) checked, 3 with problems" in text
    assert "catalog cards without a direct link to their book: 4" in text
    assert "a: 2 failed in the last 7 days\n  wsp://a/x: empty" in text


def test_the_monitor_reads_the_trawler_summary(tmp_path: Path) -> None:
    import json

    from wsp_cli.monitor import trawler_status

    path = tmp_path / "summary.json"
    path.write_text(
        json.dumps(
            {
                "pages": 50,
                "failed": 2,
                "updated": "2026-10-14T11:00:00Z",
                "sources": {"a": {"checked": 30, "catalog_without_link": 1}, "b": {"checked": 20}},
                "recent": [_item("a", 9, "old"), _item("b", 1, "broken-link: wsp://b/x")],
            }
        )
    )
    t = trawler_status(path, NOW)
    assert t is not None
    assert (t.pages, t.failed, t.failed_last_week, t.sources_checked, t.catalog_without_link) == (
        50,
        2,
        1,
        2,
        1,
    )
    assert [r.problem for r in t.recent] == ["broken-link: wsp://b/x", "old"]  # newest first
    path.write_text("{torn")
    assert trawler_status(path, NOW) is None


def test_index_merge_says_how_to_build_a_missing_tool(tmp_path: Path) -> None:
    import argparse
    import io

    from wsp_cli.ops import cmd_index_merge

    out = io.StringIO()
    args = argparse.Namespace(
        tool=str(tmp_path / "nope"), config_dir=None, target_docs=10, dry_run=True, wait_hours=0
    )
    assert cmd_index_merge(args, out) == 2
    assert "cargo build --release" in out.getvalue()


def test_the_unattended_streak_starts_after_the_newest_critical_alert_or_owner_action(tmp_path: Path) -> None:
    import json

    from wsp_cli.monitor import LibraryMonitor
    from wsp_orchestrator.store import StateStore

    db = tmp_path / "state.sqlite3"
    store = StateStore(db, check_local=False)
    store.open_alert("integrity:x", "critical", "bad mirror", {}, NOW - timedelta(days=10))
    store.resolve_alert("integrity:x", NOW - timedelta(days=9))
    store.set_meta(
        "last_owner_action", json.dumps({"at": (NOW - timedelta(days=4)).isoformat(), "what": "retry x/1"})
    )
    store.close()
    mon = LibraryMonitor(
        sources=[],
        vault=None,
        state_db=db,
        scrub_db=None,
        proposals=list,
        downloads_dir=None,
        disks={},
        background=False,
    )
    a = mon._autonomy(NOW, [], [])  # pyright: ignore[reportPrivateUsage]
    assert a is not None
    assert a.days == 4.0
    assert a.reset_by == "owner action: retry x/1"
    from wsp_api.schemas import AlertInfo

    open_now = [AlertInfo(key="k", severity="critical", message="m", raised_at=NOW)]
    assert mon._autonomy(NOW, open_now, []).days == 0.0  # pyright: ignore[reportPrivateUsage, reportOptionalMemberAccess]


def test_quality_compares_with_the_newest_older_report(tmp_path: Path) -> None:
    import json

    from wsp_cli.quality import previous_report, regression

    (tmp_path / "2026-10-07T031700Z.json").write_text(json.dumps({"ndcg10": 0.60}))
    (tmp_path / "2026-10-14T031700Z.json").write_text(json.dumps({"ndcg10": 0.55}))
    before = previous_report(tmp_path, "2026-10-21T031700Z.json")
    assert before == {"ndcg10": 0.55}
    assert regression(0.50, before) == pytest.approx(0.05)
    assert regression(0.58, before) == pytest.approx(-0.03)
    assert previous_report(tmp_path, "2026-10-01T000000Z.json") is None
    assert regression(0.5, None) is None
