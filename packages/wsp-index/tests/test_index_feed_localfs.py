from __future__ import annotations

from pathlib import Path

import pytest

from wsp_core.ids import make_doc_id
from wsp_core.model import ChangeKind, ChangeRecord
from wsp_index import InvalidCursorError, RemoteFilesystemError, SqliteChangeFeed, ensure_local
from wsp_index.localfs import filesystem_type


def rec(n: int) -> ChangeRecord:
    return ChangeRecord(make_doc_id("s", str(n)), ChangeKind.ADDED, "g1", None, f"h{n}")


def test_feed_persists_and_cursors_are_monotonic(tmp_path: Path) -> None:
    feed = SqliteChangeFeed(tmp_path / "feed.sqlite")
    c1 = feed.publish([rec(1)])
    c2 = feed.publish([rec(2)])
    assert c1 < c2  # lexicographically monotonic as well
    feed.close()
    again = SqliteChangeFeed(tmp_path / "feed.sqlite")
    assert list(again.since(c1)[0]) == [rec(2)]
    assert again.head() == c2
    again.close()


def test_cursor_from_other_feed_or_future_rejected(tmp_path: Path) -> None:
    a = SqliteChangeFeed(tmp_path / "a.sqlite")
    b = SqliteChangeFeed(tmp_path / "b.sqlite")
    ca = a.publish([rec(1)])
    with pytest.raises(InvalidCursorError):
        b.since(ca)
    epoch = ca.split(".")[0]
    with pytest.raises(InvalidCursorError):
        a.since(f"{epoch}.{999:016x}")
    with pytest.raises(InvalidCursorError):
        a.since(f"{epoch}.zzzzzzzzzzzzzzzz")
    with pytest.raises(ValueError, match="positive"):
        a.since(None, limit=0)
    a.close()
    b.close()


def test_localfs_guard(tmp_path: Path) -> None:
    mounts = tmp_path / "mounts"
    mounts.write_text(
        "rootfs / ext4 rw 0 0\n"
        f"//nas/share {tmp_path / 'nas'} cifs rw,soft 0 0\n"
        f"/dev/x {tmp_path / 'with\\040space'} ext4 rw 0 0\n"
    )
    assert filesystem_type(tmp_path / "nas" / "deep", mounts) == "cifs"
    assert filesystem_type(tmp_path / "with space", mounts) == "ext4"
    assert filesystem_type(tmp_path / "local", mounts) == "ext4"
    with pytest.raises(RemoteFilesystemError):
        ensure_local(tmp_path / "nas" / "index", mounts_file=mounts)
    assert ensure_local(tmp_path / "local", mounts_file=mounts).is_dir()
    assert filesystem_type(tmp_path, tmp_path / "no-such-file") is None
