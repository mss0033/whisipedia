from __future__ import annotations

from collections.abc import Callable, Iterator

import pytest

from wsp_core.testing.fetcher_contract import RangeServer


@pytest.fixture
def servers() -> Iterator[Callable[[], RangeServer]]:
    """Factory for extra in-process mirrors (each on its own port = its own politeness host)."""
    started: list[RangeServer] = []

    def make() -> RangeServer:
        s = RangeServer().start()
        started.append(s)
        return s

    yield make
    for s in started:
        s.stop()
