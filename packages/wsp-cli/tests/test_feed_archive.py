"""Change-feed archive (ADR-0011): old ranges sealed as Parquet, reads and cursors unchanged."""

from __future__ import annotations

from pathlib import Path

import pytest

from wsp_api.feed import ReadOnlyChangeFeed
from wsp_core.feedsql import ArchiveMissingError
from wsp_core.ids import DocId
from wsp_core.model import ChangeKind, ChangeRecord
from wsp_index.changefeed import SqliteChangeFeed
from wsp_index.feedarchive import ArchiveError, ChangeArchive

SOURCES = ("alpha", "beta", "gamma")


def _changes(n: int, start: int = 0) -> list[ChangeRecord]:
    out: list[ChangeRecord] = []
    for i in range(start, start + n):
        src = SOURCES[i % 3] if i % 7 else "alpha"  # alpha is the busiest
        kind = ChangeKind.REMOVED if i % 11 == 5 else ChangeKind.ADDED
        out.append(
            ChangeRecord(
                DocId(f"wsp:{src}/doc{i}"),
                kind,
                f"g{i // 10}",
                None if i % 2 else f"h{i - 1}",
                None if kind is ChangeKind.REMOVED else f"h{i}",
                DocId(f"wsp:{src}/old{i}") if i % 13 == 4 else None,
            )
        )
    return out


def _replay(feed: SqliteChangeFeed | ReadOnlyChangeFeed, page: int) -> tuple[list[ChangeRecord], list[str]]:
    records: list[ChangeRecord] = []
    cursors: list[str] = []
    cursor: str | None = None
    while True:
        batch, cursor = feed.since(cursor, limit=page)
        records += batch
        cursors.append(cursor)
        if len(batch) < page:
            return records, cursors


def _rows(feed: SqliteChangeFeed) -> int:
    return int(feed._db.execute("SELECT COUNT(*) FROM changes").fetchone()[0])  # pyright: ignore[reportPrivateUsage]


@pytest.fixture
def feed(tmp_path: Path) -> SqliteChangeFeed:
    f = SqliteChangeFeed(tmp_path / "changes.sqlite", archive=ChangeArchive(tmp_path / "nas" / "archive"))
    f.publish(_changes(25))
    return f


def test_archived_feed_reads_exactly_as_before(feed: SqliteChangeFeed, tmp_path: Path) -> None:
    before = {page: _replay(feed, page) for page in (1, 3, 7, 1000)}
    # tail stays exact up to the window (unfiltered) and up to keep_per_source (per source).
    tails = {(n, None): feed.tail(n) for n in (1, 5)}
    tails |= {(n, s): feed.tail(n, sources=s) for n in (1, 2) for s in (("beta",), ("alpha", "gamma"))}
    head = feed.head()

    report = feed.archive_old(keep_rows=5, keep_per_source=2, chunk_rows=4)
    assert report.ranges == 5  # 1-4 ... 17-20; 21-25 stay as the window
    assert report.archived_through == 20
    assert [p.name for _, _, p in feed.archive.ranges(feed._epoch)] == [  # pyright: ignore[reportOptionalMemberAccess, reportPrivateUsage]
        f"{a:016x}-{a + 3:016x}.parquet" for a in (1, 5, 9, 13, 17)
    ]
    # 5 window rows plus the newest 2 of each source that fall at or below the mark.
    assert 5 <= _rows(feed) <= 5 + 2 * len(SOURCES)

    for page, (records, cursors) in before.items():
        assert _replay(feed, page) == (records, cursors)
    for (n, s), expected in tails.items():
        assert feed.tail(n, sources=s) == expected
    assert feed.head() == head

    reader = ReadOnlyChangeFeed(tmp_path / "changes.sqlite", archive=feed.archive)
    for page, expected in before.items():
        assert _replay(reader, page) == expected
    with pytest.raises(ArchiveMissingError):
        ReadOnlyChangeFeed(tmp_path / "changes.sqlite").since(None)


def test_a_second_run_moves_on_and_drops_superseded_tail_rows(feed: SqliteChangeFeed) -> None:
    feed.archive_old(keep_rows=5, keep_per_source=2, chunk_rows=4)
    feed.publish(_changes(30, start=25))
    expected = _replay(feed, 6)
    report = feed.archive_old(keep_rows=5, keep_per_source=2, chunk_rows=4)
    assert report.archived_through == 48
    assert _rows(feed) <= 7 + 2 * len(SOURCES)  # 49-55, plus at most 2 per source
    assert _replay(feed, 6) == expected
    assert feed.since(expected[1][-1])[0] == []  # the last cursor still means "nothing newer"
    feed.compact()
    assert _replay(feed, 6) == expected


def test_a_range_sealed_before_a_crash_is_reused(feed: SqliteChangeFeed) -> None:
    expected = _replay(feed, 1000)
    epoch = feed._epoch  # pyright: ignore[reportPrivateUsage]
    archive = feed.archive
    assert archive is not None
    archive.write(epoch, 1, 4, feed._batches(1, 4))  # pyright: ignore[reportPrivateUsage]  # sealed, never committed
    report = feed.archive_old(keep_rows=5, keep_per_source=0, chunk_rows=4)
    assert report.ranges == 4  # 1-4 was already there
    assert report.archived_through == 20
    assert _replay(feed, 1000) == expected
    assert _rows(feed) == 5


def test_nothing_to_archive_and_bad_settings(feed: SqliteChangeFeed, tmp_path: Path) -> None:
    report = feed.archive_old(keep_rows=100, keep_per_source=0, chunk_rows=4)
    assert (report.ranges, report.archived_through, report.deleted) == (0, 0, 0)
    with pytest.raises(ValueError, match="keep_rows"):
        feed.archive_old(keep_rows=0, keep_per_source=0)
    with pytest.raises(RuntimeError, match="no change archive"):
        SqliteChangeFeed(tmp_path / "other.sqlite").archive_old(keep_rows=1, keep_per_source=0)


def test_archive_refuses_overlaps_and_bad_rows(tmp_path: Path) -> None:
    archive = ChangeArchive(tmp_path)
    row = ("x", "added", "g", None, "h", None)
    archive.write("e", 1, 3, [[(1, *row), (2, *row)], [(3, *row)]])
    with pytest.raises(ArchiveError, match="overlaps"):
        archive.write("e", 3, 5, [[(3, *row)]])
    with pytest.raises(ArchiveError, match="out of order"):
        archive.write("e", 4, 6, [[(5, *row), (4, *row)]])
    with pytest.raises(ArchiveError, match="out of order"):
        archive.write("e", 7, 8, [[(9, *row)]])
    assert [(a, b) for a, b, _ in archive.ranges("e")] == [(1, 3)]
    assert not list((tmp_path / "e").glob("*.partial"))
    assert [r[0] for r in archive.rows("e", 1, 3, 10)] == [2, 3]
    assert [r[0] for r in archive.rows("e", 0, 2, 10)] == [1, 2]
    assert [r[0] for r in archive.rows("e", 0, 3, 1)] == [1]
    assert archive.rows("other", 0, 3, 10) == []


def test_archived_rows_are_read_without_the_feed_lock(feed: SqliteChangeFeed) -> None:
    """A slow archive read (Parquet on the NAS) of an old cursor must not hold up ``tail`` (the start
    page) and every other reader (2026-10-13 review)."""
    feed.archive_old(keep_rows=5, keep_per_source=2, chunk_rows=4)
    archive = feed.archive
    assert archive is not None
    real = archive.rows
    seen: list[bool] = []

    def rows(epoch: str, after: int, through: int, limit: int) -> list[tuple[object, ...]]:
        seen.append(feed._lock.locked())  # pyright: ignore[reportPrivateUsage]
        return real(epoch, after, through, limit)

    archive.rows = rows  # type: ignore[method-assign]
    records, _ = feed.since(None, limit=3)
    assert len(records) == 3
    assert seen == [False]
