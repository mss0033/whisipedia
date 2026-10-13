"""``ChangeFeed`` on local-disk SQLite (PLAN §11.1, ADR-0002).

Cursors are opaque strings ``"<epoch>.<seq as 16 hex digits>"``. The epoch is random per feed
database, so a cursor from a rebuilt or different feed is rejected instead of silently skipping
changes. Within one feed, cursors grow monotonically (also lexicographically).

Old changes can move to sealed Parquet ranges (:meth:`SqliteChangeFeed.archive_old`,
:mod:`wsp_index.feedarchive`, ADR-0011): the database then keeps the newest ``keep_rows`` changes, plus
the newest ``keep_per_source`` of every source so ``tail`` stays one local index range. Reads behind
the archive mark come from the archive; cursors do not change.
"""

from __future__ import annotations

import secrets
import sqlite3
import threading
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from wsp_core.errors import InvalidCursorError
from wsp_core.feedsql import (
    ARCHIVED_KEY,
    COLUMNS,
    SOURCE_EXPR,
    SOURCE_INDEX,
    archived_through,
    since_local,
    tail_rows,
    with_archive,
)
from wsp_core.ids import DocId
from wsp_core.model import ChangeKind, ChangeRecord
from wsp_index.feedarchive import ChangeArchive
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


__all__ = ["ArchiveReport", "InvalidCursorError", "SqliteChangeFeed"]

_DELETE_ARCHIVED = "DELETE FROM changes WHERE seq BETWEEN ? AND ? AND seq NOT IN (SELECT seq FROM keep_seq)"
ARCHIVE_CHUNK_ROWS = 10_000_000  # seq values per archived range (one Parquet file, ~0.5-1 GB)


@dataclass(frozen=True, slots=True)
class ArchiveReport:
    ranges: int  # sealed this run
    archived_through: int
    deleted: int  # rows removed from the database


class SqliteChangeFeed:
    """Append-only change log. Safe for one writer and many reader threads in one process."""

    def __init__(self, path: Path, *, archive: ChangeArchive | None = None) -> None:
        ensure_local(path.parent)
        self.path = path
        self.archive = archive
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

        With ``sources`` only changes of those sources are returned (one index range per source). On an
        archived feed a source's list is exact up to ``keep_per_source`` changes (:meth:`archive_old`).
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
            self._db.execute("BEGIN")  # the archive mark and the rows from one snapshot
            try:
                if after > 0:
                    top = self._db.execute("SELECT COALESCE(MAX(seq), 0) FROM changes").fetchone()
                    if after > max(int(top[0]), archived_through(self._db)):
                        raise InvalidCursorError(f"cursor is ahead of the feed: {cursor!r}")
                archived, local = since_local(self._db, after, limit)
            finally:
                self._db.execute("COMMIT")
        # The archive (Parquet, maybe on the NAS) is read without the lock: a stalled read of an old
        # cursor must not hold up ``tail`` (the start page) and every other reader.
        rows = with_archive(self._epoch, after, limit, self.archive, archived, local)
        out = [_record(r) for r in rows]
        last = int(cast(int, rows[-1][0])) if rows else after
        return out, self._cursor(last)

    def archive_old(
        self,
        *,
        keep_rows: int,
        keep_per_source: int,
        chunk_rows: int = ARCHIVE_CHUNK_ROWS,
        say: Callable[[str], object] = lambda _: None,
    ) -> ArchiveReport:
        """Seal every whole ``chunk_rows`` range of changes older than the newest ``keep_rows`` into the
        archive, and delete them here except the newest ``keep_per_source`` of each source.

        Each range is sealed (written, read back, renamed) before one transaction moves the archive mark
        and deletes its rows, so a crash leaves either the old state or a range the next run skips. Rows
        kept for ``tail`` by an earlier run are deleted once newer rows replace them. The file does not
        shrink by itself: :meth:`compact` returns the space.
        """
        if self.archive is None:
            raise RuntimeError("no change archive configured ([index] changes_archive)")
        if keep_rows < 1 or keep_per_source < 0 or chunk_rows < 1:
            # keep_rows >= 1: the newest change stays here, so head() and new seq values are unchanged.
            raise ValueError("keep_rows and chunk_rows must be >= 1, keep_per_source >= 0")
        archive = self.archive
        with self._lock:
            mark = archived_through(self._db)
            if archive.last(self._epoch) < mark:
                raise RuntimeError(f"the archive ends before {mark}, which the feed says is archived")
            head = int(self._db.execute("SELECT COALESCE(MAX(seq), 0) FROM changes").fetchone()[0])
            keep = self._newest_per_source(keep_per_source)
            self._db.execute("CREATE TEMP TABLE IF NOT EXISTS keep_seq (seq INTEGER PRIMARY KEY)")
            self._db.execute("DELETE FROM keep_seq")
            self._db.executemany("INSERT INTO keep_seq(seq) VALUES (?)", ((s,) for s in sorted(keep)))
            deleted = 0
            if mark > 0:  # rows kept for tail by an earlier run that newer ones have replaced
                deleted += self._delete_archived(1, mark)
            ranges = 0
            while True:
                first = mark + 1
                # A range sealed by a run that crashed before its commit is reused as it is.
                done = next((lst for f, lst, _ in archive.ranges(self._epoch) if f == first), None)
                if done is not None:
                    last = done
                else:
                    last = first + chunk_rows - 1
                    if last > head - keep_rows:
                        break
                    path = archive.write(self._epoch, first, last, self._batches(first, last))
                    say(f"changes: sealed {path.name}")
                    ranges += 1
                self._db.execute("BEGIN IMMEDIATE")
                try:
                    self._db.execute(
                        "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (ARCHIVED_KEY, str(last))
                    )
                    n = self._db.execute(
                        _DELETE_ARCHIVED,
                        (first, last),
                    ).rowcount
                    self._db.execute("COMMIT")
                except BaseException:
                    self._db.execute("ROLLBACK")
                    raise
                deleted += n
                mark = last
            return ArchiveReport(ranges=ranges, archived_through=mark, deleted=deleted)

    def compact(self) -> None:
        """``VACUUM``: give the space of archived rows back (reads and rewrites the whole file once)."""
        with self._lock:
            self._db.execute("VACUUM")
            self._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def _delete_archived(self, first: int, last: int) -> int:
        self._db.execute("BEGIN IMMEDIATE")
        try:
            n = self._db.execute(
                _DELETE_ARCHIVED,
                (first, last),
            ).rowcount
            self._db.execute("COMMIT")
        except BaseException:
            self._db.execute("ROLLBACK")
            raise
        return n

    def _newest_per_source(self, k: int) -> set[int]:
        """``seq`` of the newest ``k`` changes of every source (one index range each)."""
        out: set[int] = set()
        if k == 0:
            return out
        source = ""
        while True:  # skip-scan of the source index: one probe per distinct source
            row = self._db.execute(
                f"SELECT {SOURCE_EXPR} FROM changes WHERE {SOURCE_EXPR} > ? ORDER BY 1 LIMIT 1",  # noqa: S608 - constant
                (source,),
            ).fetchone()
            if row is None:
                return out
            source = str(row[0])
            out.update(
                int(r[0])
                for r in self._db.execute(
                    f"SELECT seq FROM changes WHERE {SOURCE_EXPR} = ? ORDER BY seq DESC LIMIT ?",  # noqa: S608 - constant
                    (source, k),
                )
            )

    def _batches(self, first: int, last: int, size: int = 500_000) -> Iterator[list[tuple[object, ...]]]:
        after = first - 1
        while after < last:
            rows = self._db.execute(
                f"SELECT {COLUMNS} FROM changes WHERE seq > ? AND seq <= ? ORDER BY seq LIMIT ?",  # noqa: S608 - constant
                (after, last, size),
            ).fetchall()
            if not rows:
                return
            yield rows
            after = int(rows[-1][0])


def _record(r: Sequence[object]) -> ChangeRecord:
    return ChangeRecord(
        doc_id=DocId(str(r[1])),
        kind=ChangeKind(str(r[2])),
        generation_id=str(r[3]),
        old_hash=None if r[4] is None else str(r[4]),
        new_hash=None if r[5] is None else str(r[5]),
        renamed_from=None if r[6] is None else DocId(str(r[6])),
    )
