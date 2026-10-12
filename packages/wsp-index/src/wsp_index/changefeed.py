"""``ChangeFeed`` on local-disk SQLite (PLAN §11.1, ADR-0002).

Cursors are opaque strings ``"<epoch>.<seq as 16 hex digits>"``. The epoch is random per feed
database, so a cursor from a rebuilt or different feed is rejected instead of silently skipping
changes. Within one feed, cursors grow monotonically (also lexicographically).
"""

from __future__ import annotations

import secrets
import sqlite3
import threading
from collections.abc import Iterable, Sequence
from pathlib import Path

from wsp_core.errors import InvalidCursorError
from wsp_core.feedsql import SOURCE_INDEX, tail_rows
from wsp_core.ids import DocId
from wsp_core.model import ChangeKind, ChangeRecord
from wsp_index.localfs import ensure_local

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS changes (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    generation_id TEXT NOT NULL,
    old_hash TEXT,
    new_hash TEXT,
    renamed_from TEXT
);
"""


__all__ = ["InvalidCursorError", "SqliteChangeFeed"]


class SqliteChangeFeed:
    """Append-only change log. Safe for one writer and many reader threads in one process."""

    def __init__(self, path: Path) -> None:
        ensure_local(path.parent)
        self.path = path
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.executescript(_SCHEMA)
        self._db.execute(SOURCE_INDEX)  # built once on an older feed (a minute for 100M rows)
        row = self._db.execute("SELECT value FROM meta WHERE key='epoch'").fetchone()
        if row is None:
            epoch = secrets.token_hex(4)
            self._db.execute("INSERT INTO meta(key, value) VALUES ('epoch', ?)", (epoch,))
        else:
            epoch = str(row[0])
        self._epoch = epoch

    def close(self) -> None:
        """Idempotent."""
        with self._lock:
            self._db.close()

    def _cursor(self, seq: int) -> str:
        return f"{self._epoch}.{seq:016x}"

    def _parse(self, cursor: str | None) -> int:
        if cursor is None or cursor == "":
            return 0
        epoch, sep, seq_hex = cursor.partition(".")
        if not sep or epoch != self._epoch or len(seq_hex) != 16:
            raise InvalidCursorError(f"cursor does not belong to this feed: {cursor!r}")
        try:
            return int(seq_hex, 16)
        except ValueError as exc:
            raise InvalidCursorError(f"malformed cursor: {cursor!r}") from exc

    def head(self) -> str:
        """Cursor after the newest change (``since(head())`` returns nothing until new changes)."""
        with self._lock:
            row = self._db.execute("SELECT COALESCE(MAX(seq), 0) FROM changes").fetchone()
        return self._cursor(int(row[0]))

    def publish(self, changes: Iterable[ChangeRecord], *, checkpoint: tuple[str, str] | None = None) -> str:
        """Append ``changes``; with ``checkpoint=(key, value)`` also store that progress marker in the
        same transaction (a resumable writer then never loses or repeats a record across a crash)."""
        rows = [
            (c.doc_id, c.kind.value, c.generation_id, c.old_hash, c.new_hash, c.renamed_from) for c in changes
        ]
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                self._db.executemany(
                    "INSERT INTO changes(doc_id, kind, generation_id, old_hash, new_hash, renamed_from) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    rows,
                )
                if checkpoint is not None:
                    self._db.execute(
                        "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                        (f"checkpoint:{checkpoint[0]}", checkpoint[1]),
                    )
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            row = self._db.execute("SELECT COALESCE(MAX(seq), 0) FROM changes").fetchone()
        return self._cursor(int(row[0]))

    def checkpoint(self, key: str) -> str | None:
        """The progress marker last stored with :meth:`publish` under ``key``."""
        with self._lock:
            row = self._db.execute("SELECT value FROM meta WHERE key=?", (f"checkpoint:{key}",)).fetchone()
        return None if row is None else str(row[0])

    def tail(self, limit: int, *, sources: Sequence[str] | None = None) -> tuple[Sequence[ChangeRecord], str]:
        """The newest ``limit`` changes, newest first, plus the head cursor (no full replay).

        With ``sources`` only changes of those sources are returned (one index range per source).
        """
        with self._lock:
            top = int(self._db.execute("SELECT COALESCE(MAX(seq), 0) FROM changes").fetchone()[0])
            rows = tail_rows(self._db, limit, sources, indexed=True)
        return [_record(r) for r in rows], self._cursor(top)

    def since(self, cursor: str | None, *, limit: int = 1000) -> tuple[Sequence[ChangeRecord], str]:
        if limit < 1:
            raise ValueError("limit must be positive")
        after = self._parse(cursor)
        with self._lock:
            if after > 0:
                top = self._db.execute("SELECT COALESCE(MAX(seq), 0) FROM changes").fetchone()
                if after > int(top[0]):
                    raise InvalidCursorError(f"cursor is ahead of the feed: {cursor!r}")
            rows = self._db.execute(
                "SELECT seq, doc_id, kind, generation_id, old_hash, new_hash, renamed_from "
                "FROM changes WHERE seq > ? ORDER BY seq LIMIT ?",
                (after, limit),
            ).fetchall()
        out = [_record(r) for r in rows]
        last = int(rows[-1][0]) if rows else after
        return out, self._cursor(last)


def _record(r: Sequence[object]) -> ChangeRecord:
    return ChangeRecord(
        doc_id=DocId(str(r[1])),
        kind=ChangeKind(str(r[2])),
        generation_id=str(r[3]),
        old_hash=None if r[4] is None else str(r[4]),
        new_hash=None if r[5] is None else str(r[5]),
        renamed_from=None if r[6] is None else DocId(str(r[6])),
    )
