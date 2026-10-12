from __future__ import annotations

import sqlite3
from pathlib import Path

from wsp_api.feed import ReadOnlyChangeFeed
from wsp_core.feedsql import SOURCE_EXPR
from wsp_core.ids import make_doc_id
from wsp_core.model import ChangeKind, ChangeRecord
from wsp_index import SqliteChangeFeed


def _feed(path: Path) -> None:
    feed = SqliteChangeFeed(path)
    rows = [
        ChangeRecord(make_doc_id(src, f"d{i}"), ChangeKind.ADDED, "g1", None, f"h{i}")
        for i in range(30)
        for src in ("big", "small" if i % 10 == 0 else "big", "mid")
    ]
    feed.publish(rows)
    feed.close()


def test_a_sites_recent_changes_come_from_the_source_index(tmp_path: Path) -> None:
    _feed(tmp_path / "changes.sqlite")
    ro = ReadOnlyChangeFeed(tmp_path / "changes.sqlite")
    small, _ = ro.tail(10, sources=["small"])
    assert [r.doc_id for r in small] == [make_doc_id("small", f"d{i}") for i in (20, 10, 0)]
    both, _ = ro.tail(4, sources=["small", "mid"])
    assert [r.doc_id for r in both] == [
        make_doc_id(s, f"d{i}") for s, i in (("mid", 29), ("mid", 28), ("mid", 27), ("mid", 26))
    ]
    everything, _ = ro.tail(2)
    assert len(everything) == 2
    db = sqlite3.connect(tmp_path / "changes.sqlite")
    plan = " ".join(
        str(r)
        for r in db.execute(
            f"EXPLAIN QUERY PLAN SELECT seq FROM changes WHERE {SOURCE_EXPR} = 'small' "
            "ORDER BY seq DESC LIMIT 10"
        )
    )
    assert "changes_by_source" in plan


def test_a_feed_without_the_index_falls_back_to_a_bounded_scan(tmp_path: Path) -> None:
    _feed(tmp_path / "changes.sqlite")
    db = sqlite3.connect(tmp_path / "changes.sqlite")
    db.execute("DROP INDEX changes_by_source")
    db.commit()
    db.close()
    ro = ReadOnlyChangeFeed(tmp_path / "changes.sqlite")
    small, _ = ro.tail(10, sources=["small"])
    assert [r.doc_id for r in small] == [make_doc_id("small", f"d{i}") for i in (20, 10, 0)]
