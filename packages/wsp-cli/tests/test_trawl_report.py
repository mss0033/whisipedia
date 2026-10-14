import io
from datetime import UTC, datetime, timedelta

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
