"""Ledger appends under storage faults (vault-fetch audit #3, #17)."""

from __future__ import annotations

import errno
import os
from datetime import UTC, date
from pathlib import Path

import pytest
from vaultkit import Clock

from wsp_vault.atomic import RetryPolicy
from wsp_vault.ledger import JsonlLedger, LedgerDirtyError, LedgerError
from wsp_vault.signing import Ed25519Signer

SIGNER = Ed25519Signer.generate()
NO_SLEEP = RetryPolicy(attempts=3, base_delay=0.0, sleep=lambda _: None, jitter=0.0)


class Faults:
    """``os.write`` that writes a prefix then raises EIO once; ``os.truncate`` that fails while armed."""

    def __init__(
        self, monkeypatch: pytest.MonkeyPatch, *, write_prefix: int | None, truncate_fails: bool
    ) -> None:
        self.real_write, self.real_truncate = os.write, os.truncate
        self.write_prefix = write_prefix
        self.truncate_fails = truncate_fails
        self.armed = True
        monkeypatch.setattr("wsp_vault.ledger.os.write", self.write)
        monkeypatch.setattr("wsp_vault.ledger.os.truncate", self.truncate)

    def write(self, fd: int, data: bytes | memoryview) -> int:
        if self.armed and self.write_prefix is not None:
            self.armed = False
            n = len(data) if self.write_prefix < 0 else self.write_prefix
            self.real_write(fd, bytes(data)[:n])  # the server applied part (or all) of the request ...
            raise OSError(errno.EIO, "soft mount: server not responding")  # ... the client saw EIO
        return self.real_write(fd, data)

    def truncate(self, path: str | Path, length: int) -> None:
        if self.truncate_fails:
            raise OSError(errno.EIO, "server still not responding")
        self.real_truncate(path, length)


def test_failed_append_with_failed_truncate_marks_dirty_and_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The auditor's ledgerdemo: before, the retry appended after the fragment and a fresh writer
    died in json.loads forever after."""
    ledger = JsonlLedger(tmp_path, SIGNER, retry=NO_SLEEP)
    ledger.append("a", "x", {"n": 1})
    faults = Faults(monkeypatch, write_prefix=40, truncate_fails=True)
    with pytest.raises(LedgerDirtyError):
        ledger.append("b", "x", {"n": 2})
    path = ledger.day_files()[-1][1]
    assert not path.read_bytes().endswith(b"\n")  # the fragment is still there, nothing after it
    with pytest.raises(LedgerDirtyError):  # still unreachable: nothing is appended after the fragment
        ledger.append("c", "x", {"n": 3})
    assert path.read_bytes().count(b"\n") == 1
    faults.truncate_fails = False  # the share is back
    event = ledger.append("c", "x", {"n": 3})
    kinds = [e.kind for e in ledger.events()]
    assert kinds == ["a", "ledger.recovered", "c"]
    assert event.seq == 3
    assert ledger.verify()
    assert path.with_name(path.name + ".torn").stat().st_size == 41  # fragment + newline, kept
    fresh = JsonlLedger(tmp_path, SIGNER)
    fresh.append("d", "x", {})
    assert fresh.verify()


def test_a_fresh_process_recovers_the_fragment_too(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ledger = JsonlLedger(tmp_path, SIGNER, retry=NO_SLEEP)
    ledger.append("a", "x", {})
    Faults(monkeypatch, write_prefix=25, truncate_fails=True)
    with pytest.raises(LedgerDirtyError):
        ledger.append("b", "x", {})
    monkeypatch.undo()
    other = JsonlLedger(tmp_path, SIGNER)  # e.g. the daemon restarted, or `wsp ingest` runs
    other.append("c", "x", {})
    assert [e.kind for e in other.events()] == ["a", "ledger.recovered", "c"]
    assert other.verify()


def test_append_that_actually_landed_is_kept(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """EIO after the server wrote the whole line: the event is in the chain; recovery cuts nothing."""
    ledger = JsonlLedger(tmp_path, SIGNER, retry=NO_SLEEP)
    ledger.append("a", "x", {})
    faults = Faults(monkeypatch, write_prefix=-1, truncate_fails=True)
    with pytest.raises(LedgerDirtyError):
        ledger.append("b", "x", {})
    faults.truncate_fails = False
    ledger.append("c", "x", {})
    assert [e.kind for e in ledger.events()] == ["a", "b", "c"]
    assert ledger.verify()


def test_truncate_that_silently_does_nothing_is_caught(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = JsonlLedger(tmp_path, SIGNER, retry=NO_SLEEP)
    ledger.append("a", "x", {})
    Faults(monkeypatch, write_prefix=10, truncate_fails=False)

    def lost_on_the_wire(path: str | Path, length: int) -> None:
        return None

    monkeypatch.setattr("wsp_vault.ledger.os.truncate", lost_on_the_wire)
    with pytest.raises(LedgerDirtyError, match="truncation left"):
        ledger.append("b", "x", {})


def test_transient_failure_with_working_truncate_is_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = JsonlLedger(tmp_path, SIGNER, retry=NO_SLEEP)
    ledger.append("a", "x", {})
    Faults(monkeypatch, write_prefix=30, truncate_fails=False)
    ledger.append("b", "x", {})
    assert [e.kind for e in ledger.events()] == ["a", "b"]
    assert ledger.verify()


def test_readback_drops_the_page_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    advice: list[int] = []
    real = os.posix_fadvise

    def record(fd: int, offset: int, length: int, how: int) -> None:
        advice.append(how)
        real(fd, offset, length, how)

    monkeypatch.setattr("wsp_vault.ledger.os.posix_fadvise", record)
    JsonlLedger(tmp_path, SIGNER).append("a", "x", {})
    assert os.POSIX_FADV_DONTNEED in advice


def test_readers_ignore_a_partial_last_line(tmp_path: Path) -> None:
    ledger = JsonlLedger(tmp_path, SIGNER)
    ledger.append("a", "x", {})
    ledger.append("b", "x", {})
    path = ledger.day_files()[-1][1]
    with path.open("ab") as f:
        f.write(b'{"at": "in progress')
    reader = JsonlLedger(tmp_path, SIGNER)
    assert [e.kind for e in reader.events()] == ["a", "b"]
    assert [e.kind for e in reader.tail(5)] == ["a", "b"]
    assert reader.verify()


def test_garbage_line_needs_explicit_recovery(tmp_path: Path) -> None:
    ledger = JsonlLedger(tmp_path, SIGNER)
    ledger.append("a", "x", {})
    path = ledger.day_files()[-1][1]
    with path.open("ab") as f:
        f.write(b"\x00" * 64 + b"\n")
    fresh = JsonlLedger(tmp_path, SIGNER)
    with pytest.raises(LedgerError, match="recover"):
        fresh.append("b", "x", {})
    event = fresh.recover("owner checked the damage")
    assert event is not None
    assert event.data["cut_bytes"] == 65
    fresh.append("b", "x", {})
    assert fresh.verify()
    assert fresh.recover() is None  # nothing left to cut


def test_quarantine_is_refused_for_a_day_that_later_days_chain_from(tmp_path: Path, clock: Clock) -> None:
    ledger = JsonlLedger(tmp_path, SIGNER, clock=clock)
    first = ledger.append("a", "x", {})
    clock.advance(days=1)
    ledger.append("b", "x", {})
    with pytest.raises(LedgerError, match="not the newest"):
        ledger.quarantine_day(first.at.astimezone(UTC).date(), "test")
    assert ledger.verify()
    newest: date = clock().date()
    ledger.quarantine_day(newest, "newest may be set aside")
    assert ledger.verify()


def test_idempotency_index_is_extended_incrementally(
    tmp_path: Path, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock = tmp_path / "locks" / "ledger.lock"
    a = JsonlLedger(tmp_path / "l", SIGNER, clock=clock, lock_path=lock)
    b = JsonlLedger(tmp_path / "l", SIGNER, clock=clock, lock_path=lock)
    for day in range(3):
        a.append("old", "x", {"day": day}, idempotency_key=f"old-{day}")
        clock.advance(days=1)
    first = a.append("k", "x", {}, idempotency_key="k1")  # index built (one full scan)
    from_b = b.append("k", "x", {}, idempotency_key="k2")  # written by another writer
    scanned: list[str] = []
    import wsp_vault.ledger as ledger_mod

    real = ledger_mod._day_events  # pyright: ignore[reportPrivateUsage]

    def counting(path: Path) -> list[object]:
        scanned.append(path.name)
        return real(path)  # type: ignore[return-value]

    monkeypatch.setattr("wsp_vault.ledger._day_events", counting)
    assert a.append("k", "x", {}, idempotency_key="k2") == from_b  # seen without a full rescan
    assert a.append("k", "x", {}, idempotency_key="k1") == first
    assert len(set(scanned)) == 1  # only the newest day file was read
