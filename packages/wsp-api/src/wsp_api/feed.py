"""The change feed as the API reads it: read-only SQLite, one connection per thread (audit #13).

``wsp_index.SqliteChangeFeed`` is the writer's class: it opens the database read-write, may create a
new epoch (which invalidates every client cursor), and serializes all reads behind one lock while SSE
clients poll every second. The API only reads, so it opens ``mode=ro`` with ``query_only`` and gives
each thread its own connection. Cursors are byte-for-byte the writer's (``<epoch>.<seq as 16 hex>``).
TODO(index): a ``read_only`` mode in ``SqliteChangeFeed`` itself.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import cast

from wsp_core.errors import InvalidCursorError
from wsp_core.feedsql import ArchiveRows, archived_through, has_source_index, since_rows, tail_rows
from wsp_core.ids import DocId
from wsp_core.model import ChangeKind, ChangeRecord


class ReadOnlyChangeFeed:
    """``ChangeFeed`` reads (``since``, ``tail``, ``head``) over a feed database another process writes."""

    def __init__(
        self, path: Path, *, busy_timeout_ms: int = 2000, archive: ArchiveRows | None = None
    ) -> None:
        self.path = path
        self.archive = archive  # sealed old ranges (ADR-0011); read behind the feed's archive mark
        self._busy = busy_timeout_ms
        self._local = threading.local()
        self._all: list[sqlite3.Connection] = []
        self._lock = threading.Lock()
        self._epoch = self._epoch_of(self._db())

    def _db(self) -> sqlite3.Connection:
        db: sqlite3.Connection | None = getattr(self._local, "db", None)
        if db is None:
            uri = f"{self.path.resolve().as_uri()}?mode=ro"
            db = sqlite3.connect(uri, uri=True, check_same_thread=False, isolation_level=None)
            db.execute("PRAGMA query_only=1")
            db.execute(f"PRAGMA busy_timeout={int(self._busy)}")
            self._local.db = db
            with self._lock:
                self._all.append(db)
        return db

    @staticmethod
    def _epoch_of(db: sqlite3.Connection) -> str:
        row = db.execute("SELECT value FROM meta WHERE key='epoch'").fetchone()
        if row is None:
            raise InvalidCursorError("the change feed has no epoch yet")
        return str(row[0])

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
        row = self._db().execute("SELECT COALESCE(MAX(seq), 0) FROM changes").fetchone()
        return self._cursor(int(row[0]))

    def since(self, cursor: str | None, *, limit: int = 1000) -> tuple[Sequence[ChangeRecord], str]:
        if limit < 1:
            raise ValueError("limit must be positive")
        after = self._parse(cursor)
        db = self._db()
        db.execute("BEGIN")  # the archive mark and the rows from one snapshot
        try:
            if after > 0:
                top = int(db.execute("SELECT COALESCE(MAX(seq), 0) FROM changes").fetchone()[0])
                if after > max(top, archived_through(db)):
                    raise InvalidCursorError(f"cursor is ahead of the feed: {cursor!r}")
            rows = since_rows(db, self._epoch, after, limit, self.archive)
        finally:
            db.execute("COMMIT")
        last = int(cast(int, rows[-1][0])) if rows else after
        return [_record(r) for r in rows], self._cursor(last)

    def tail(self, limit: int, *, sources: Sequence[str] | None = None) -> tuple[Sequence[ChangeRecord], str]:
        """The newest ``limit`` changes, newest first, plus the head cursor (as the writer's ``tail``)."""
        db = self._db()
        top = int(db.execute("SELECT COALESCE(MAX(seq), 0) FROM changes").fetchone()[0])
        rows = tail_rows(db, limit, sources, indexed=bool(sources) and has_source_index(db))
        return [_record(r) for r in rows], self._cursor(top)

    def publish(self, changes: Iterable[ChangeRecord]) -> str:
        raise PermissionError("the API's change feed is read-only")

    def close(self) -> None:
        with self._lock:
            conns, self._all = self._all, []
        for db in conns:
            db.close()


def _record(r: Sequence[object]) -> ChangeRecord:
    return ChangeRecord(
        doc_id=DocId(str(r[1])),
        kind=ChangeKind(str(r[2])),
        generation_id=str(r[3]),
        old_hash=None if r[4] is None else str(r[4]),
        new_hash=None if r[5] is None else str(r[5]),
        renamed_from=None if r[6] is None else DocId(str(r[6])),
    )
