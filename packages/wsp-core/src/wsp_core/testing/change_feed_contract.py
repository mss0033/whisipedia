"""Contract suite for ``ChangeFeed`` (PLAN §11.1: ``/v1/changes?since=<cursor>``).

Subclass and provide a ``feed`` fixture returning an empty feed.

Semantics pinned here: cursors are opaque; ``since(None)`` starts at the beginning; following the
returned cursor never repeats or skips a change; ``publish`` returns a cursor after its own
changes; a cursor the feed did not issue raises ``ValueError``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from wsp_core.ids import make_doc_id
from wsp_core.model import ChangeKind, ChangeRecord
from wsp_core.ports import ChangeFeed


def change(n: int, kind: ChangeKind = ChangeKind.MODIFIED, gen: str = "2026-01-01") -> ChangeRecord:
    return ChangeRecord(
        doc_id=make_doc_id("feed-src", f"doc{n}"),
        kind=kind,
        generation_id=gen,
        old_hash=None if kind is ChangeKind.ADDED else f"old{n}",
        new_hash=None if kind is ChangeKind.REMOVED else f"new{n}",
    )


class ChangeFeedContract:
    @pytest.fixture
    def feed(self, tmp_path: Path, request: pytest.FixtureRequest) -> ChangeFeed:
        """Build an empty instance under ``tmp_path``; clean up via ``request.addfinalizer``."""
        raise NotImplementedError("subclass must provide a `feed` fixture")

    def test_satisfies_protocol(self, feed: ChangeFeed) -> None:
        assert isinstance(feed, ChangeFeed)

    def test_empty_feed(self, feed: ChangeFeed) -> None:
        changes, cursor = feed.since(None)
        assert list(changes) == []
        assert list(feed.since(cursor)[0]) == []

    def test_publish_then_read_in_order(self, feed: ChangeFeed) -> None:
        batch = [change(1, ChangeKind.ADDED), change(2), change(3, ChangeKind.REMOVED)]
        feed.publish(batch)
        changes, _ = feed.since(None)
        assert list(changes) == batch

    def test_round_trips_rename(self, feed: ChangeFeed) -> None:
        rec = ChangeRecord(
            doc_id=make_doc_id("feed-src", "new-name"),
            kind=ChangeKind.RENAMED,
            generation_id="2026-02-01",
            old_hash="h",
            new_hash="h",
            renamed_from=make_doc_id("feed-src", "old-name"),
        )
        feed.publish([rec])
        assert list(feed.since(None)[0]) == [rec]

    def test_cursor_from_publish_excludes_earlier(self, feed: ChangeFeed) -> None:
        c1 = feed.publish([change(1), change(2)])
        feed.publish([change(3)])
        changes, _ = feed.since(c1)
        assert list(changes) == [change(3)]

    def test_paging_never_repeats_or_skips(self, feed: ChangeFeed) -> None:
        batch = [change(i) for i in range(25)]
        feed.publish(batch[:10])
        feed.publish(batch[10:])
        seen: list[ChangeRecord] = []
        cursor: str | None = None
        for _ in range(10):
            page, cursor = feed.since(cursor, limit=7)
            assert len(page) <= 7
            if not page:
                break
            seen.extend(page)
        assert seen == batch

    def test_cursor_stays_valid_after_more_changes(self, feed: ChangeFeed) -> None:
        feed.publish([change(1)])
        _, cursor = feed.since(None)
        feed.publish([change(2)])
        assert list(feed.since(cursor)[0]) == [change(2)]
        _, head = feed.since(cursor)
        assert list(feed.since(head)[0]) == []

    def test_empty_publish_returns_usable_cursor(self, feed: ChangeFeed) -> None:
        feed.publish([change(1)])
        cursor = feed.publish([])
        assert list(feed.since(cursor)[0]) == []
        feed.publish([change(2)])
        assert list(feed.since(cursor)[0]) == [change(2)]

    @pytest.mark.parametrize("bad", ["garbage", "0", "x.y.z", "-1"])
    def test_foreign_cursor_rejected(self, feed: ChangeFeed, bad: str) -> None:
        feed.publish([change(1)])
        with pytest.raises(ValueError, match=r"."):
            feed.since(bad)
