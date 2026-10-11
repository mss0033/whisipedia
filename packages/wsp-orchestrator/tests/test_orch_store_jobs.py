from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from wsp_orchestrator.jobs import JobKind, JobQueue, JobStatus
from wsp_orchestrator.store import StateStore, UnsafeStoragePathError, assert_local_path

T0 = datetime(2026, 10, 15, tzinfo=UTC)
MOUNTS = """\
/dev/sdc / ext4 rw,relatime 0 0
//nas/share /mnt/nas cifs rw,soft 0 0
C:\\134 /mnt/c 9p rw 0 0
"""


def test_local_path_guard_refuses_network_and_vault(tmp_path: Path) -> None:
    assert_local_path(Path("/srv/wsp/state/db.sqlite3"), mounts_text=MOUNTS)
    with pytest.raises(UnsafeStoragePathError, match="cifs"):
        assert_local_path(Path("/mnt/nas/whisipedia/state.sqlite3"), mounts_text=MOUNTS)
    with pytest.raises(UnsafeStoragePathError, match="9p"):
        assert_local_path(Path("/mnt/c/state.sqlite3"), mounts_text=MOUNTS)
    with pytest.raises(UnsafeStoragePathError, match="local disk"):
        StateStore(tmp_path / "vault" / "state.sqlite3", forbidden_roots=[tmp_path / "vault"])


def test_store_uses_wal_and_persists(tmp_path: Path) -> None:
    db = tmp_path / "s.sqlite3"
    store = StateStore(db, check_local=False)
    assert store.query("PRAGMA journal_mode")[0][0] == "wal"
    store.set_meta("k", "v")
    store.close()
    again = StateStore(db, check_local=False)
    assert again.get_meta("k") == "v"
    again.close()


def test_alerts_deduplicate_until_resolved(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "s.sqlite3", check_local=False)
    assert store.open_alert("k", "warning", "m", {}, T0)
    assert not store.open_alert("k", "warning", "m", {}, T0)
    assert store.resolve_alert("k", T0)
    assert not store.resolve_alert("k", T0)
    assert store.open_alert("k", "warning", "again", {}, T0)


def test_queue_dedupes_active_jobs(tmp_path: Path) -> None:
    q = JobQueue(StateStore(tmp_path / "s.sqlite3", check_local=False))
    job = q.enqueue(JobKind.DISCOVER, "src", now=T0, payload={"source_id": "src"})
    assert job is not None
    assert q.enqueue(JobKind.DISCOVER, "src", now=T0) is None
    assert q.enqueue(JobKind.HEALTH_PROBE, "src", now=T0) is not None
    [claimed] = q.claim(T0, lease=timedelta(hours=1), accept=lambda j: j.kind is JobKind.DISCOVER)
    assert claimed.status is JobStatus.RUNNING
    assert claimed.attempts == 1
    q.complete(claimed, T0)
    assert q.enqueue(JobKind.DISCOVER, "src", now=T0) is not None, "finished jobs do not block new ones"
    assert q.store.last_success("discover", "src") == T0


def test_queue_respects_run_after_and_retry(tmp_path: Path) -> None:
    q = JobQueue(StateStore(tmp_path / "s.sqlite3", check_local=False))
    q.enqueue(JobKind.DRILL, "global", now=T0, run_after=T0 + timedelta(hours=1))
    assert q.claim(T0, lease=timedelta(hours=1)) == []
    [job] = q.claim(T0 + timedelta(hours=1), lease=timedelta(hours=1))
    q.fail(job, T0, "boom", retry_at=T0 + timedelta(hours=2))
    retried = q.get(job.id)
    assert retried.status is JobStatus.PENDING
    assert retried.last_error == "boom"
    assert q.store.schedule_marks("drill", "global") == (None, T0)
    [job] = q.claim(T0 + timedelta(hours=2), lease=timedelta(hours=1))
    q.fail(job, T0, "boom", retry_at=None)
    assert q.get(job.id).status is JobStatus.DEAD


def test_recover_returns_running_jobs_to_pending(tmp_path: Path) -> None:
    q = JobQueue(StateStore(tmp_path / "s.sqlite3", check_local=False))
    q.enqueue(JobKind.PRUNE, "a", now=T0)
    q.enqueue(JobKind.PRUNE, "b", now=T0)
    q.claim(T0, lease=timedelta(minutes=5), limit=2)
    assert q.recover(T0 + timedelta(minutes=1)) == 0  # leases still valid
    assert q.recover(T0 + timedelta(minutes=10)) == 2
    q.claim(T0, lease=timedelta(hours=5), limit=2)
    assert q.recover(T0, all_running=True) == 2
    # the crashed attempt stays counted (audit C1): a job that kills the daemon must run out of retries
    assert all(j.status is JobStatus.PENDING and j.attempts == 2 for j in q.jobs())
    assert q.next_run_after() == T0
    assert q.depth() == {("prune", "pending"): 2}
