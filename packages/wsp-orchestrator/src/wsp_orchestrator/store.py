"""Persistent orchestrator state on LOCAL disk: SQLite in WAL mode (PLAN §6.5, ADR-0002).

The NAS is an SMB ``soft`` mount on which SQLite locking is unreliable, so :func:`assert_local_path`
refuses network and host-shared filesystems as well as anything under the vault root. Losing this
database costs only re-planning: the vault and the ledger remain the source of truth.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
import threading
from collections.abc import Callable, Generator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from wsp_core.model import ReleaseState
from wsp_orchestrator.clock import ensure_utc

SCHEMA_VERSION = 5  # 2: releases.fetched_json (v0.2); 3: upstream_offers, proposal ids;
# 4: origin_pins, provenance (source integrity, ADR-0009);
# 5: releases.row_version (optimistic concurrency), step token and crash accounting, state_since

# Columns added to ``releases`` by v5 (name, declaration); added idempotently on open.
_RELEASE_V5_COLUMNS: tuple[tuple[str, str], ...] = (
    ("row_version", "INTEGER NOT NULL DEFAULT 0"),
    ("step_token", "TEXT"),
    ("step_state", "TEXT"),
    ("step_started_at", "TEXT"),
    ("crash_count", "INTEGER NOT NULL DEFAULT 0"),
    ("crash_state", "TEXT"),
    ("state_since", "TEXT"),
)

# Filesystems on which SQLite WAL locking/shared memory is unsafe or unsupported.
UNSAFE_FS_TYPES = frozenset(
    {"cifs", "smb3", "smbfs", "nfs", "nfs4", "9p", "drvfs", "fuse.sshfs", "sshfs", "glusterfs", "ceph", "afs"}
)

_TS_FMT = "%Y-%m-%dT%H:%M:%S.%fZ"


class StoreError(RuntimeError):
    pass


class UnsafeStoragePathError(StoreError):
    pass


class ConcurrentUpdateError(StoreError):
    """The release row changed since it was read (another thread, the CLI, the owner)."""


def ts(when: datetime) -> str:
    """Fixed-width UTC timestamp so that SQL string comparison equals time comparison."""
    return ensure_utc(when).strftime(_TS_FMT)


def parse_ts(text: str | None) -> datetime | None:
    if text is None:
        return None
    return datetime.strptime(text, _TS_FMT).replace(tzinfo=UTC)


def _fs_type(path: Path, mounts_text: str) -> str | None:
    best: tuple[int, str] | None = None
    target = str(path)
    for line in mounts_text.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        mount_point = parts[1].replace("\\040", " ")
        prefix = mount_point.rstrip("/") + "/"
        if (target == mount_point or target.startswith(prefix) or mount_point == "/") and (
            best is None or len(mount_point) > best[0]
        ):
            best = (len(mount_point), parts[2])
    return best[1] if best else None


def assert_local_path(
    path: Path, *, forbidden_roots: Sequence[Path] = (), mounts_text: str | None = None
) -> None:
    """Raise :class:`UnsafeStoragePathError` if ``path`` is on a network FS or under a forbidden root."""
    resolved = path.expanduser().resolve()
    for root in forbidden_roots:
        root_r = root.expanduser().resolve()
        if resolved == root_r or root_r in resolved.parents:
            raise UnsafeStoragePathError(f"{resolved} is under {root_r}; state must live on local disk")
    if mounts_text is None:
        try:
            mounts_text = Path("/proc/mounts").read_text(encoding="utf-8")
        except OSError:
            return  # not Linux; nothing more we can check
    fs = _fs_type(resolved, mounts_text)
    if fs in UNSAFE_FS_TYPES:
        raise UnsafeStoragePathError(f"{resolved} is on a {fs} filesystem; SQLite WAL needs local disk")


@dataclass(frozen=True, slots=True)
class ReleaseRecord:
    """Persisted progress of one release through the state machine."""

    source_id: str
    version: str
    state: ReleaseState
    attempts: int
    ref: Mapping[str, Any]
    generation_id: str
    plan: Mapping[str, Any] | None = None
    local_files: tuple[str, ...] = ()
    fetched: Mapping[str, Any] = field(default_factory=dict[str, Any])  # file name -> FetchResult JSON
    doc_count: int | None = None
    size_bytes: int | None = None
    hold_reason: str | None = None
    approved: bool = False  # owner approved promotion despite a sanity hold
    # owner approved promoting it although a newer generation is current (``wsp approve --force``);
    # stored as ``approved = 2``
    force_older: bool = False
    last_error: str | None = None
    next_attempt_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    # Optimistic concurrency: every update is ``WHERE row_version=?`` and bumps it.
    row_version: int = 0
    # Crash accounting (audit C1): the step in progress, persisted before its side effects. A token
    # still set when the daemon starts means the process died inside that step.
    step_token: str | None = None
    step_state: ReleaseState | None = None
    step_started_at: datetime | None = None
    crash_count: int = 0  # crashes (or OOM kills of the step's unit) in ``crash_state``
    crash_state: ReleaseState | None = None
    state_since: datetime | None = None  # when ``state`` was entered (stuck-state alerts)

    @property
    def subject(self) -> str:
        return f"{self.source_id}/{self.version}"


@dataclass(frozen=True, slots=True)
class SourceHealth:
    source_id: str
    paused: bool = False
    pause_reasons: tuple[str, ...] = ()
    checksum_failures: int = 0
    observed_license: str | None = None
    observed_format: str | None = None
    last_cycle_ok: datetime | None = None
    last_new_release_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class GenerationStats:
    source_id: str
    generation_id: str
    doc_count: int | None
    size_bytes: int | None
    promoted_at: datetime


@dataclass(frozen=True, slots=True)
class OutboxEvent:
    id: int
    event_id: str
    kind: str
    subject: str
    data: Mapping[str, object] = field(default_factory=dict[str, object])


_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS releases (
  source_id TEXT NOT NULL, version TEXT NOT NULL, state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
  generation_id TEXT NOT NULL, ref_json TEXT NOT NULL, plan_json TEXT,
  local_files_json TEXT NOT NULL DEFAULT '[]', fetched_json TEXT NOT NULL DEFAULT '{}',
  doc_count INTEGER, size_bytes INTEGER, hold_reason TEXT, approved INTEGER NOT NULL DEFAULT 0,
  last_error TEXT, next_attempt_at TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY (source_id, version));
CREATE INDEX IF NOT EXISTS releases_state ON releases(state);
CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, key TEXT NOT NULL, payload_json TEXT NOT NULL,
  status TEXT NOT NULL, run_after TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, host TEXT,
  lease_until TEXT, last_error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, finished_at TEXT);
CREATE UNIQUE INDEX IF NOT EXISTS jobs_active ON jobs(kind, key) WHERE status IN ('pending', 'running');
CREATE INDEX IF NOT EXISTS jobs_due ON jobs(status, run_after);
CREATE TABLE IF NOT EXISTS schedule (
  kind TEXT NOT NULL, key TEXT NOT NULL, last_success TEXT, last_attempt TEXT, PRIMARY KEY (kind, key));
CREATE TABLE IF NOT EXISTS alerts (
  key TEXT PRIMARY KEY, severity TEXT NOT NULL, message TEXT NOT NULL, data_json TEXT NOT NULL,
  raised_at TEXT NOT NULL, resolved_at TEXT);
CREATE TABLE IF NOT EXISTS sources (
  source_id TEXT PRIMARY KEY, paused INTEGER NOT NULL DEFAULT 0,
  pause_reasons_json TEXT NOT NULL DEFAULT '[]',
  checksum_failures INTEGER NOT NULL DEFAULT 0, observed_license TEXT, observed_format TEXT,
  last_cycle_ok TEXT, last_new_release_at TEXT);
CREATE TABLE IF NOT EXISTS gen_stats (
  source_id TEXT NOT NULL, generation_id TEXT NOT NULL, doc_count INTEGER, size_bytes INTEGER,
  promoted_at TEXT NOT NULL, PRIMARY KEY (source_id, generation_id));
CREATE TABLE IF NOT EXISTS outbox (
  id INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL UNIQUE, kind TEXT NOT NULL,
  subject TEXT NOT NULL, data_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS upstream_offers (
  source_id TEXT PRIMARY KEY, offers_json TEXT NOT NULL, observed_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS proposals (
  id INTEGER PRIMARY KEY AUTOINCREMENT, candidate_key TEXT NOT NULL, decision TEXT NOT NULL,
  proposal_json TEXT NOT NULL, created_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open');
CREATE TABLE IF NOT EXISTS origin_pins (
  source_id TEXT NOT NULL, host TEXT NOT NULL, role TEXT NOT NULL, issuer_family TEXT, cert_sha256 TEXT,
  first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, PRIMARY KEY (source_id, host));
CREATE TABLE IF NOT EXISTS provenance (
  source_id TEXT NOT NULL, version TEXT NOT NULL, verdict TEXT NOT NULL, provenance_json TEXT NOT NULL,
  checked_at TEXT NOT NULL, PRIMARY KEY (source_id, version));
"""


def migrate_v5(c: sqlite3.Connection) -> None:
    """v4 -> v5, idempotent: add the missing ``releases`` columns, date ``state_since``.

    Runs on every open inside the schema transaction; a column that exists is left alone, so a
    database migrated by a crashed earlier open (or created at v5) is not touched twice.
    """
    have = {str(r[1]) for r in c.execute("PRAGMA table_info(releases)")}
    for name, decl in _RELEASE_V5_COLUMNS:
        if name not in have:
            c.execute(f"ALTER TABLE releases ADD COLUMN {name} {decl}")
    c.execute("UPDATE releases SET state_since=updated_at WHERE state_since IS NULL")


def _dumps(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _loads_obj(text: str | None) -> dict[str, Any] | None:
    return None if text is None else cast(dict[str, Any], json.loads(text))


class StateStore:
    """Thread-safe wrapper around one SQLite connection. All writes are short transactions."""

    def __init__(
        self,
        path: Path,
        *,
        forbidden_roots: Sequence[Path] = (),
        check_local: bool = True,
        mounts_text: str | None = None,
    ) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        if check_local:
            assert_local_path(path, forbidden_roots=forbidden_roots, mounts_text=mounts_text)
        self.path = path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False, timeout=30.0)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        with self.tx() as c:
            # executescript() would commit implicitly; run statements one by one inside the transaction.
            for stmt in filter(None, (s.strip() for s in _SCHEMA.split(";"))):
                c.execute(stmt)
            row = c.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            if row is None:
                c.execute("INSERT INTO meta(key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
            else:
                version = int(row["value"])
                if version > SCHEMA_VERSION or version < 1:
                    raise StoreError(f"state store schema {version} != supported {SCHEMA_VERSION}")
                if version == 1:  # v1 -> v2: add the fetch digests column
                    c.execute("ALTER TABLE releases ADD COLUMN fetched_json TEXT NOT NULL DEFAULT '{}'")
                # v2 -> v3 and v3 -> v4: new tables only (created above)
                c.execute("UPDATE meta SET value=? WHERE key='schema_version'", (str(SCHEMA_VERSION),))
            migrate_v5(c)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def tx(self) -> Generator[sqlite3.Connection]:
        """A write transaction (``BEGIN IMMEDIATE``); rolled back on any exception.

        A failed ``COMMIT`` (a deferred constraint, ``SQLITE_BUSY``, a full disk) leaves SQLite inside
        the transaction; it is rolled back too, so the next ``tx()`` does not fail with "cannot
        start a transaction within a transaction" (audit M3).
        """
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._rollback()
                raise
            try:
                self._conn.execute("COMMIT")
            except BaseException:
                self._rollback()
                raise

    def _rollback(self) -> None:
        if self._conn.in_transaction:
            # already rolled back by SQLite itself (e.g. after SQLITE_FULL): nothing left to undo
            with contextlib.suppress(sqlite3.Error):
                self._conn.execute("ROLLBACK")

    def query(self, sql: str, params: Sequence[object] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(sql, params).fetchall())

    # --- key/value -----------------------------------------------------------------------------

    def get_meta(self, key: str) -> str | None:
        rows = self.query("SELECT value FROM meta WHERE key=?", (key,))
        return str(rows[0]["value"]) if rows else None

    def set_meta(self, key: str, value: str, conn: sqlite3.Connection | None = None) -> None:
        sql = "INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value"
        if conn is not None:
            conn.execute(sql, (key, value))
            return
        with self.tx() as c:
            c.execute(sql, (key, value))

    def delete_meta(self, key: str) -> None:
        with self.tx() as c:
            c.execute("DELETE FROM meta WHERE key=?", (key,))

    # --- releases ------------------------------------------------------------------------------

    @staticmethod
    def _release_from_row(r: sqlite3.Row) -> ReleaseRecord:
        return ReleaseRecord(
            source_id=r["source_id"],
            version=r["version"],
            state=ReleaseState(r["state"]),
            attempts=int(r["attempts"]),
            ref=cast(dict[str, Any], json.loads(r["ref_json"])),
            generation_id=r["generation_id"],
            plan=_loads_obj(r["plan_json"]),
            local_files=tuple(cast(list[str], json.loads(r["local_files_json"]))),
            fetched=cast(dict[str, Any], json.loads(r["fetched_json"])),
            doc_count=r["doc_count"],
            size_bytes=r["size_bytes"],
            hold_reason=r["hold_reason"],
            approved=bool(r["approved"]),
            force_older=int(r["approved"]) >= 2,
            last_error=r["last_error"],
            next_attempt_at=parse_ts(r["next_attempt_at"]),
            created_at=parse_ts(r["created_at"]),
            updated_at=parse_ts(r["updated_at"]),
            row_version=int(r["row_version"]),
            step_token=r["step_token"],
            step_state=ReleaseState(r["step_state"]) if r["step_state"] else None,
            step_started_at=parse_ts(r["step_started_at"]),
            crash_count=int(r["crash_count"]),
            crash_state=ReleaseState(r["crash_state"]) if r["crash_state"] else None,
            state_since=parse_ts(r["state_since"]),
        )

    def get_release(self, source_id: str, version: str) -> ReleaseRecord | None:
        rows = self.query("SELECT * FROM releases WHERE source_id=? AND version=?", (source_id, version))
        return self._release_from_row(rows[0]) if rows else None

    def releases(
        self, *, source_id: str | None = None, states: Sequence[ReleaseState] | None = None
    ) -> list[ReleaseRecord]:
        sql, params = "SELECT * FROM releases WHERE 1=1", list[object]()
        if source_id is not None:
            sql += " AND source_id=?"
            params.append(source_id)
        if states:
            sql += f" AND state IN ({','.join('?' * len(states))})"
            params.extend(s.value for s in states)
        sql += " ORDER BY source_id, version"
        return [self._release_from_row(r) for r in self.query(sql, params)]

    def insert_release(self, rec: ReleaseRecord, now: datetime, conn: sqlite3.Connection) -> bool:
        """Insert if absent. Returns False if the release was already known."""
        cur = conn.execute(
            "INSERT OR IGNORE INTO releases(source_id, version, state, attempts, generation_id, ref_json,"
            " created_at, updated_at, state_since) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                rec.source_id,
                rec.version,
                rec.state.value,
                rec.attempts,
                rec.generation_id,
                _dumps(rec.ref),
                ts(now),
                ts(now),
                ts(now),
            ),
        )
        return cur.rowcount == 1

    def update_release(self, rec: ReleaseRecord, now: datetime, conn: sqlite3.Connection) -> None:
        """Write ``rec`` if the row is still at ``rec.row_version`` (and bump it).

        Raises :class:`ConcurrentUpdateError` when someone else updated the release since ``rec`` was
        read: a stale record never overwrites newer state (audit H4).
        """
        cur = conn.execute(
            "UPDATE releases SET state_since=CASE WHEN state=? THEN state_since ELSE ? END,"
            " state=?, attempts=?, plan_json=?, local_files_json=?, fetched_json=?,"
            " doc_count=?,"
            " size_bytes=?, hold_reason=?, approved=?, last_error=?, next_attempt_at=?, updated_at=?,"
            " step_token=?, step_state=?, step_started_at=?, crash_count=?, crash_state=?,"
            " row_version=row_version+1"
            " WHERE source_id=? AND version=? AND row_version=?",
            (
                rec.state.value,
                ts(now),
                rec.state.value,
                rec.attempts,
                _dumps(rec.plan) if rec.plan is not None else None,
                _dumps(list(rec.local_files)),
                _dumps(dict(rec.fetched)),
                rec.doc_count,
                rec.size_bytes,
                rec.hold_reason,
                2 if rec.approved and rec.force_older else int(rec.approved),
                rec.last_error,
                ts(rec.next_attempt_at) if rec.next_attempt_at else None,
                ts(now),
                rec.step_token,
                rec.step_state.value if rec.step_state else None,
                ts(rec.step_started_at) if rec.step_started_at else None,
                rec.crash_count,
                rec.crash_state.value if rec.crash_state else None,
                rec.source_id,
                rec.version,
                rec.row_version,
            ),
        )
        if cur.rowcount != 1:
            raise ConcurrentUpdateError(
                f"{rec.subject} changed since it was read (row_version {rec.row_version})"
            )

    # --- ledger outbox (exactly-once ledger events across crashes) ----------------------------

    def add_outbox(
        self, conn: sqlite3.Connection, event_id: str, kind: str, subject: str, data: Mapping[str, object]
    ) -> None:
        conn.execute(
            "INSERT INTO outbox(event_id, kind, subject, data_json) VALUES (?, ?, ?, ?)",
            (event_id, kind, subject, _dumps(data)),
        )

    def outbox(self) -> list[OutboxEvent]:
        return [
            OutboxEvent(
                id=int(r["id"]),
                event_id=r["event_id"],
                kind=r["kind"],
                subject=r["subject"],
                data=cast(dict[str, object], json.loads(r["data_json"])),
            )
            for r in self.query("SELECT * FROM outbox ORDER BY id")
        ]

    def remove_outbox(self, event_id: str, ledger_seq: int) -> None:
        with self.tx() as c:
            c.execute("DELETE FROM outbox WHERE event_id=?", (event_id,))
            # Never backwards: concurrent flushes may finish out of order (audit M4).
            c.execute(
                "INSERT INTO meta(key, value) VALUES ('ledger_watermark', ?) ON CONFLICT(key) DO UPDATE"
                " SET value=excluded.value"
                " WHERE CAST(excluded.value AS INTEGER) > CAST(meta.value AS INTEGER)",
                (str(ledger_seq),),
            )

    # --- schedule marks ------------------------------------------------------------------------

    def last_success(self, kind: str, key: str) -> datetime | None:
        return self.schedule_marks(kind, key)[0]

    def schedule_marks(self, kind: str, key: str) -> tuple[datetime | None, datetime | None]:
        """``(last_success, last_attempt)`` for a scheduled job."""
        rows = self.query(
            "SELECT last_success, last_attempt FROM schedule WHERE kind=? AND key=?", (kind, key)
        )
        if not rows:
            return None, None
        return parse_ts(rows[0]["last_success"]), parse_ts(rows[0]["last_attempt"])

    def mark_schedule(
        self, conn: sqlite3.Connection, kind: str, key: str, *, attempt: datetime, success: bool
    ) -> None:
        conn.execute(
            "INSERT INTO schedule(kind, key, last_success, last_attempt) VALUES (?, ?, ?, ?)"
            " ON CONFLICT(kind, key) DO UPDATE SET last_attempt=excluded.last_attempt,"
            " last_success=COALESCE(excluded.last_success, schedule.last_success)",
            (kind, key, ts(attempt) if success else None, ts(attempt)),
        )

    # --- alerts --------------------------------------------------------------------------------

    def alert_is_open(self, key: str) -> bool:
        rows = self.query("SELECT 1 FROM alerts WHERE key=? AND resolved_at IS NULL", (key,))
        return bool(rows)

    def open_alert(
        self, key: str, severity: str, message: str, data: Mapping[str, object], now: datetime
    ) -> bool:
        """Open an alert unless one with the same key is already open. Returns True if newly opened.

        Reads first: an alert that is already open costs no write transaction (audit H6).
        """
        if self.alert_is_open(key):
            return False
        with self.tx() as c:
            row = c.execute("SELECT resolved_at FROM alerts WHERE key=?", (key,)).fetchone()
            if row is not None and row["resolved_at"] is None:
                return False
            c.execute(
                "INSERT INTO alerts(key, severity, message, data_json, raised_at, resolved_at)"
                " VALUES (?, ?, ?, ?, ?, NULL) ON CONFLICT(key) DO UPDATE SET severity=excluded.severity,"
                " message=excluded.message, data_json=excluded.data_json, raised_at=excluded.raised_at,"
                " resolved_at=NULL",
                (key, severity, message, _dumps(data), ts(now)),
            )
            return True

    def resolve_alert(self, key: str, now: datetime) -> bool:
        if not self.alert_is_open(key):
            return False
        with self.tx() as c:
            cur = c.execute(
                "UPDATE alerts SET resolved_at=? WHERE key=? AND resolved_at IS NULL", (ts(now), key)
            )
            return cur.rowcount == 1

    def open_alerts(self) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM alerts WHERE resolved_at IS NULL ORDER BY key")

    # --- source health -------------------------------------------------------------------------

    def source_health(self, source_id: str) -> SourceHealth:
        rows = self.query("SELECT * FROM sources WHERE source_id=?", (source_id,))
        if not rows:
            return SourceHealth(source_id)
        r = rows[0]
        return SourceHealth(
            source_id=source_id,
            paused=bool(r["paused"]),
            pause_reasons=tuple(cast(list[str], json.loads(r["pause_reasons_json"]))),
            checksum_failures=int(r["checksum_failures"]),
            observed_license=r["observed_license"],
            observed_format=r["observed_format"],
            last_cycle_ok=parse_ts(r["last_cycle_ok"]),
            last_new_release_at=parse_ts(r["last_new_release_at"]),
        )

    def put_source_health(self, h: SourceHealth, conn: sqlite3.Connection | None = None) -> None:
        sql = (
            "INSERT INTO sources(source_id, paused, pause_reasons_json, checksum_failures, observed_license,"
            " observed_format, last_cycle_ok, last_new_release_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(source_id) DO UPDATE SET paused=excluded.paused,"
            " pause_reasons_json=excluded.pause_reasons_json, checksum_failures=excluded.checksum_failures,"
            " observed_license=excluded.observed_license, observed_format=excluded.observed_format,"
            " last_cycle_ok=excluded.last_cycle_ok, last_new_release_at=excluded.last_new_release_at"
        )
        params = (
            h.source_id,
            int(h.paused),
            _dumps(list(h.pause_reasons)),
            h.checksum_failures,
            h.observed_license,
            h.observed_format,
            ts(h.last_cycle_ok) if h.last_cycle_ok else None,
            ts(h.last_new_release_at) if h.last_new_release_at else None,
        )
        if conn is not None:
            conn.execute(sql, params)
            return
        with self.tx() as c:
            c.execute(sql, params)

    def modify_source_health(
        self,
        source_id: str,
        change: Callable[[SourceHealth], SourceHealth],
        conn: sqlite3.Connection | None = None,
    ) -> SourceHealth:
        """Read-modify-write the health row inside one transaction (no lost concurrent pause, H4).

        With ``conn`` the caller's transaction is used; the read happens inside it.
        """
        if conn is None:
            with self.tx() as c:
                return self.modify_source_health(source_id, change, c)
        new = change(self.source_health(source_id))
        self.put_source_health(new, conn)
        return new

    # --- generation stats (trend for promotion sanity checks) ---------------------------------

    def add_generation_stats(self, s: GenerationStats, conn: sqlite3.Connection) -> None:
        conn.execute(
            "INSERT OR REPLACE INTO gen_stats(source_id, generation_id, doc_count, size_bytes, promoted_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (s.source_id, s.generation_id, s.doc_count, s.size_bytes, ts(s.promoted_at)),
        )

    def generation_stats(self, source_id: str, *, limit: int = 12) -> list[GenerationStats]:
        """Most recent promoted generations, oldest first."""
        rows = self.query(
            "SELECT * FROM gen_stats WHERE source_id=? ORDER BY promoted_at DESC, generation_id DESC LIMIT ?",
            (source_id, limit),
        )
        out = [
            GenerationStats(
                source_id=r["source_id"],
                generation_id=r["generation_id"],
                doc_count=r["doc_count"],
                size_bytes=r["size_bytes"],
                promoted_at=cast(datetime, parse_ts(r["promoted_at"])),
            )
            for r in rows
        ]
        return out[::-1]

    # --- proposals -----------------------------------------------------------------------------

    def add_proposal(
        self, candidate_key: str, decision: str, proposal: Mapping[str, Any], now: datetime
    ) -> int:
        with self.tx() as c:
            cur = c.execute(
                "INSERT INTO proposals(candidate_key, decision, proposal_json, created_at)"
                " VALUES (?, ?, ?, ?)",
                (candidate_key, decision, _dumps(proposal), ts(now)),
            )
            return int(cast(int, cur.lastrowid))

    def proposals(self, *, status: str | None = "open") -> list[sqlite3.Row]:
        if status is None:
            return self.query("SELECT * FROM proposals ORDER BY id")
        return self.query("SELECT * FROM proposals WHERE status=? ORDER BY id", (status,))

    def proposal(self, proposal_id: int) -> sqlite3.Row | None:
        rows = self.query("SELECT * FROM proposals WHERE id=?", (proposal_id,))
        return rows[0] if rows else None

    def proposal_keys(self) -> set[str]:
        """Candidate keys ever proposed, whatever their status: a decided candidate is not re-raised."""
        return {str(r["candidate_key"]) for r in self.query("SELECT candidate_key FROM proposals")}

    def set_proposal_status(self, proposal_id: int, status: str) -> bool:
        with self.tx() as c:
            cur = c.execute("UPDATE proposals SET status=? WHERE id=?", (status, proposal_id))
            return cur.rowcount == 1

    # --- upstream offers (last-copy oracle, freshness) ----------------------------------------

    def put_offers(self, source_id: str, offers: Sequence[Mapping[str, object]], now: datetime) -> None:
        """What upstream served at ``now``: ``[{generation_id, version, published}]``."""
        with self.tx() as c:
            c.execute(
                "INSERT INTO upstream_offers(source_id, offers_json, observed_at) VALUES (?, ?, ?)"
                " ON CONFLICT(source_id) DO UPDATE SET offers_json=excluded.offers_json,"
                " observed_at=excluded.observed_at",
                (source_id, _dumps(list(offers)), ts(now)),
            )

    # --- source integrity (ADR-0009) ------------------------------------------------------------

    def origin_pins(self, source_id: str | None = None) -> list[sqlite3.Row]:
        if source_id is None:
            return self.query("SELECT * FROM origin_pins ORDER BY source_id, host")
        return self.query("SELECT * FROM origin_pins WHERE source_id=? ORDER BY host", (source_id,))

    def put_origin_pin(
        self,
        source_id: str,
        host: str,
        role: str,
        issuer_family: str | None,
        cert_sha256: str | None,
        first_seen: datetime,
        last_seen: datetime,
    ) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO origin_pins(source_id, host, role, issuer_family, cert_sha256, first_seen,"
                " last_seen) VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(source_id, host) DO UPDATE SET"
                " role=excluded.role, issuer_family=excluded.issuer_family,"
                " cert_sha256=excluded.cert_sha256, last_seen=excluded.last_seen",
                (source_id, host, role, issuer_family, cert_sha256, ts(first_seen), ts(last_seen)),
            )

    def put_provenance(
        self, source_id: str, version: str, verdict: str, provenance: Mapping[str, Any], now: datetime
    ) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO provenance(source_id, version, verdict, provenance_json, checked_at)"
                " VALUES (?, ?, ?, ?, ?) ON CONFLICT(source_id, version) DO UPDATE SET"
                " verdict=excluded.verdict, provenance_json=excluded.provenance_json,"
                " checked_at=excluded.checked_at",
                (source_id, version, verdict, _dumps(dict(provenance)), ts(now)),
            )

    def provenance(self, source_id: str, version: str) -> tuple[str, dict[str, Any]] | None:
        rows = self.query(
            "SELECT verdict, provenance_json FROM provenance WHERE source_id=? AND version=?",
            (source_id, version),
        )
        if not rows:
            return None
        return str(rows[0]["verdict"]), cast(dict[str, Any], json.loads(rows[0]["provenance_json"]))

    def offers(self, source_id: str) -> tuple[list[dict[str, Any]], datetime] | None:
        rows = self.query("SELECT * FROM upstream_offers WHERE source_id=?", (source_id,))
        if not rows:
            return None
        observed = parse_ts(rows[0]["observed_at"])
        assert observed is not None
        return cast(list[dict[str, Any]], json.loads(rows[0]["offers_json"])), observed
