"""Contract suite for ``GapLog`` ("not in library" reports, PLAN §5.3, §10.2).

Subclass, provide a ``gap_log`` fixture and implement :meth:`GapLogContract.read_back`, which returns
the recorded gaps in order (for the gap miner the log must be readable back without loss).
"""

from __future__ import annotations

import threading
from collections.abc import Sequence
from datetime import UTC, datetime

import pytest

from wsp_core.model import Gap
from wsp_core.ports import GapLog


class GapLogContract:
    @pytest.fixture
    def gap_log(self) -> GapLog:
        raise NotImplementedError("subclass must provide a `gap_log` fixture")

    def read_back(self, gap_log: GapLog) -> Sequence[Gap]:
        raise NotImplementedError

    def test_satisfies_protocol(self, gap_log: GapLog) -> None:
        assert isinstance(gap_log, GapLog)

    def test_records_in_order_without_loss(self, gap_log: GapLog) -> None:
        at = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
        gaps = [
            Gap(at, "https://en.wikipedia.org/wiki/Nonexistent", "wsp:wikipedia-en/Alan_Turing"),
            Gap(at, "wsp://rfc/99999"),
            Gap(at, "https://example.org/ünïcode?q=1#frag", None),
        ]
        for g in gaps:
            gap_log.record(g)
        assert list(self.read_back(gap_log)) == gaps

    def test_concurrent_records_are_not_torn(self, gap_log: GapLog) -> None:
        at = datetime(2026, 10, 10, tzinfo=UTC)

        def report(i: int) -> None:
            gap_log.record(Gap(at, f"wsp://x/{i}"))

        threads = [threading.Thread(target=report, args=(i,)) for i in range(50)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert sorted(g.href for g in self.read_back(gap_log)) == sorted(f"wsp://x/{i}" for i in range(50))
