"""Persistent job queue in the local state store (PLAN §6.5).

At most one *active* (pending or running) job exists per ``(kind, key)``. A claimed job holds a lease;
after a crash the daemon returns running jobs to pending on startup (:meth:`JobQueue.recover`).
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, cast

from wsp_orchestrator.store import StateStore, parse_ts, ts


class JobKind(StrEnum):
    DISCOVER = "discover"
    FETCH_RELEASE = "fetch_release"
    SCRUB_SLICE = "scrub_slice"
    DRILL = "drill"
    HEALTH_PROBE = "health_probe"
    PRUNE = "prune"
    DISCOVERY_BOTS = "discovery_bots"


class JobStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    DEAD = "dead"  # gave up after retries
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class Job:
    id: int
    kind: JobKind
    key: str
    payload: Mapping[str, Any] = field(default_factory=dict[str, Any])
    status: JobStatus = JobStatus.PENDING
    run_after: datetime | None = None
    attempts: int = 0
    host: str | None = None
    last_error: str | None = None


def _job(r: sqlite3.Row) -> Job:
    return Job(
        id=int(r["id"]),
        kind=JobKind(r["kind"]),
        key=r["key"],
        payload=cast(dict[str, Any], json.loads(r["payload_json"])),
        status=JobStatus(r["status"]),
        run_after=parse_ts(r["run_after"]),
        attempts=int(r["attempts"]),
        host=r["host"],
        last_error=r["last_error"],
    )


class JobQueue:
    def __init__(self, store: StateStore) -> None:
        self.store = store

    def enqueue(
        self,
        kind: JobKind,
        key: str,
        *,
        now: datetime,
        payload: Mapping[str, Any] | None = None,
        run_after: datetime | None = None,
        host: str | None = None,
    ) -> Job | None:
        """Add a job. Returns ``None`` if an active job with the same kind and key already exists."""
        with self.store.tx() as c:
            cur = c.execute(
                "INSERT OR IGNORE INTO jobs(kind, key, payload_json, status, run_after, host, created_at,"
                " updated_at) VALUES (?, ?, ?, 'pending', ?, ?, ?, ?)",
                (
                    kind.value,
                    key,
                    json.dumps(dict(payload or {}), sort_keys=True),
                    ts(run_after or now),
                    host,
                    ts(now),
                    ts(now),
                ),
            )
            if cur.rowcount != 1:
                return None
            return _job(c.execute("SELECT * FROM jobs WHERE id=?", (cur.lastrowid,)).fetchone())

    def active(self, kind: JobKind, key: str) -> Job | None:
        rows = self.store.query(
            "SELECT * FROM jobs WHERE kind=? AND key=? AND status IN ('pending','running')", (kind.value, key)
        )
        return _job(rows[0]) if rows else None

    def get(self, job_id: int) -> Job:
        return _job(self.store.query("SELECT * FROM jobs WHERE id=?", (job_id,))[0])

    def jobs(self, *, status: JobStatus | None = None, kind: JobKind | None = None) -> list[Job]:
        sql, params = "SELECT * FROM jobs WHERE 1=1", list[object]()
        if status is not None:
            sql += " AND status=?"
            params.append(status.value)
        if kind is not None:
            sql += " AND kind=?"
            params.append(kind.value)
        return [_job(r) for r in self.store.query(sql + " ORDER BY id", params)]

    def claim(
        self,
        now: datetime,
        *,
        lease: timedelta,
        accept: Callable[[Job], bool] = lambda _job: True,
        limit: int = 1,
    ) -> list[Job]:
        """Atomically move up to ``limit`` due pending jobs accepted by ``accept`` to running."""
        claimed: list[Job] = []
        with self.store.tx() as c:
            rows = c.execute(
                "SELECT * FROM jobs WHERE status='pending' AND run_after<=? ORDER BY run_after, id",
                (ts(now),),
            ).fetchall()
            for r in rows:
                if len(claimed) >= limit:
                    break
                job = _job(r)
                if not accept(job):
                    continue
                c.execute(
                    "UPDATE jobs SET status='running', attempts=attempts+1, lease_until=?, updated_at=?"
                    " WHERE id=?",
                    (ts(now + lease), ts(now), job.id),
                )
                claimed.append(_job(c.execute("SELECT * FROM jobs WHERE id=?", (job.id,)).fetchone()))
        return claimed

    def complete(self, job: Job, now: datetime, *, mark_schedule: bool = True) -> None:
        with self.store.tx() as c:
            c.execute(
                "UPDATE jobs SET status='done', finished_at=?, updated_at=?, lease_until=NULL WHERE id=?",
                (ts(now), ts(now), job.id),
            )
            if mark_schedule:
                self.store.mark_schedule(c, job.kind.value, job.key, attempt=now, success=True)

    def fail(self, job: Job, now: datetime, error: str, *, retry_at: datetime | None) -> None:
        """Record a failure; requeue at ``retry_at`` or mark the job dead when it is ``None``."""
        with self.store.tx() as c:
            if retry_at is not None:
                c.execute(
                    "UPDATE jobs SET status='pending', run_after=?, last_error=?, lease_until=NULL,"
                    " updated_at=? WHERE id=?",
                    (ts(retry_at), error, ts(now), job.id),
                )
            else:
                c.execute(
                    "UPDATE jobs SET status='dead', last_error=?, lease_until=NULL, finished_at=?,"
                    " updated_at=? WHERE id=?",
                    (error, ts(now), ts(now), job.id),
                )
            self.store.mark_schedule(c, job.kind.value, job.key, attempt=now, success=False)

    def defer(self, job: Job, now: datetime, run_after: datetime, *, note: str | None = None) -> None:
        """Put a running job back to pending without counting an attempt (e.g. waiting on backoff)."""
        with self.store.tx() as c:
            c.execute(
                "UPDATE jobs SET status='pending', run_after=?, attempts=MAX(attempts-1, 0),"
                " lease_until=NULL, last_error=COALESCE(?, last_error), updated_at=? WHERE id=?",
                (ts(run_after), note, ts(now), job.id),
            )

    def cancel(self, job: Job, now: datetime, reason: str) -> None:
        with self.store.tx() as c:
            c.execute(
                "UPDATE jobs SET status='cancelled', last_error=?, finished_at=?, updated_at=?,"
                " lease_until=NULL WHERE id=?",
                (reason, ts(now), ts(now), job.id),
            )

    def extend_lease(self, job: Job, until: datetime) -> None:
        with self.store.tx() as c:
            c.execute("UPDATE jobs SET lease_until=? WHERE id=? AND status='running'", (ts(until), job.id))

    def recover(self, now: datetime, *, all_running: bool = False) -> int:
        """Return running jobs to pending: all of them at startup, or only those whose lease expired."""
        with self.store.tx() as c:
            if all_running:
                cur = c.execute(
                    "UPDATE jobs SET status='pending', attempts=MAX(attempts-1, 0), lease_until=NULL,"
                    " updated_at=? WHERE status='running'",
                    (ts(now),),
                )
            else:
                cur = c.execute(
                    "UPDATE jobs SET status='pending', lease_until=NULL, updated_at=?"
                    " WHERE status='running' AND lease_until<?",
                    (ts(now), ts(now)),
                )
            return cur.rowcount

    def depth(self) -> Mapping[tuple[str, str], int]:
        rows = self.store.query("SELECT kind, status, COUNT(*) AS n FROM jobs GROUP BY kind, status")
        return {(r["kind"], r["status"]): int(r["n"]) for r in rows}

    def next_run_after(self) -> datetime | None:
        rows = self.store.query("SELECT MIN(run_after) AS t FROM jobs WHERE status='pending'")
        return parse_ts(rows[0]["t"]) if rows else None


def kinds(values: Sequence[str]) -> tuple[JobKind, ...]:
    return tuple(JobKind(v) for v in values)
