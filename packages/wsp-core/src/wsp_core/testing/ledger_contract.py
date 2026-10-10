"""Contract suite for :class:`wsp_core.ports.Ledger` (PLAN §8.3).

Subclass and implement :meth:`LedgerContract.make_ledger`. Implementations that persist to disk
should also implement :meth:`LedgerContract.tamper` (alter one stored event's data in place) and
:meth:`LedgerContract.reopen` (a fresh instance over the same storage).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from wsp_core.ports import Ledger


class LedgerContract:
    def make_ledger(self, directory: Path) -> Ledger:
        raise NotImplementedError

    def reopen(self, directory: Path) -> Ledger:
        pytest.skip("implementation has no persistent storage")

    def tamper(self, directory: Path) -> None:
        pytest.skip("implementation does not support tamper simulation")

    @pytest.fixture
    def ledger_dir(self, tmp_path: Path) -> Path:
        return tmp_path / "ledger"

    @pytest.fixture
    def ledger(self, ledger_dir: Path) -> Ledger:
        return self.make_ledger(ledger_dir)

    def test_empty_ledger_verifies(self, ledger: Ledger) -> None:
        assert list(ledger.events()) == []
        assert ledger.verify()

    def test_append_chains_events(self, ledger: Ledger) -> None:
        a = ledger.append("release.state", "src/2026-10-01", {"state": "fetched"})
        b = ledger.append("artifact.verified", "src/2026-10-01", {"path": "upstream/x", "ok": True})
        assert (a.seq, b.seq) == (1, 2)
        assert b.prev_hash == a.hash
        assert a.hash != b.hash
        assert len(a.hash) >= 32
        assert ledger.verify()

    def test_events_round_trip_and_filter(self, ledger: Ledger) -> None:
        for i in range(5):
            ledger.append("test.event", f"subject-{i}", {"i": i, "nested": {"k": [1, "two"]}})
        events = list(ledger.events())
        assert [e.seq for e in events] == [1, 2, 3, 4, 5]
        assert events[2].subject == "subject-2"
        assert events[2].data == {"i": 2, "nested": {"k": [1, "two"]}}
        assert [e.seq for e in ledger.events(since_seq=3)] == [4, 5]

    def test_reopen_continues_chain(self, ledger: Ledger, ledger_dir: Path) -> None:
        first = ledger.append("test.event", "s", {"n": 1})
        again = self.reopen(ledger_dir)
        second = again.append("test.event", "s", {"n": 2})
        assert second.seq == first.seq + 1
        assert second.prev_hash == first.hash
        assert again.verify()

    def test_tampering_is_detected(self, ledger: Ledger, ledger_dir: Path) -> None:
        for i in range(3):
            ledger.append("test.event", "s", {"n": i})
        assert ledger.verify()
        self.tamper(ledger_dir)
        assert not self.reopen(ledger_dir).verify()

    def test_tail_returns_newest_in_order(self, ledger: Ledger) -> None:
        assert list(ledger.tail(5)) == []
        for i in range(6):
            ledger.append("test.event", "s", {"n": i})
        assert [e.data["n"] for e in ledger.tail(3)] == [3, 4, 5]
        assert [e.seq for e in ledger.tail(100)] == [1, 2, 3, 4, 5, 6]
        assert list(ledger.tail(0)) == []

    def test_idempotency_key_appends_once(self, ledger: Ledger) -> None:
        first = ledger.append("test.event", "s", {"n": 1}, idempotency_key="k1")
        again = ledger.append("test.event", "s", {"n": 1}, idempotency_key="k1")
        other = ledger.append("test.event", "s", {"n": 2}, idempotency_key="k2")
        assert again == first
        assert first.data["idempotency_key"] == "k1"
        assert [e.seq for e in ledger.events()] == [first.seq, other.seq]
        assert ledger.verify()

    def test_idempotency_survives_reopen(self, ledger: Ledger, ledger_dir: Path) -> None:
        first = ledger.append("test.event", "s", {"n": 1}, idempotency_key="k1")
        again = self.reopen(ledger_dir)
        assert again.append("test.event", "s", {"n": 1}, idempotency_key="k1").seq == first.seq
        assert len(list(again.events())) == 1
