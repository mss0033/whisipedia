import sqlite3
from contextlib import closing
from pathlib import Path

from wsp_cli.indexer import _has_rows  # pyright: ignore[reportPrivateUsage]


def _table(path: Path, rows: int) -> Path:
    with closing(sqlite3.connect(path)) as db:
        db.execute("CREATE TABLE redirects (target TEXT NOT NULL, alias TEXT NOT NULL)")
        db.executemany("INSERT INTO redirects VALUES (?, ?)", [("t", f"a{i}") for i in range(rows)])
        db.commit()
    return path


def test_only_a_redirect_table_with_rows_marks_the_trie_stale(tmp_path: Path) -> None:
    """refine() creates the table for every generation; an empty one must not force a rebuild."""
    assert not _has_rows(tmp_path / "missing.sqlite")
    assert not _has_rows(_table(tmp_path / "empty.sqlite", 0))
    assert _has_rows(_table(tmp_path / "some.sqlite", 2))
    (tmp_path / "junk.sqlite").write_bytes(b"not a database at all" * 100)
    assert _has_rows(tmp_path / "junk.sqlite")  # unreadable: assume redirects
