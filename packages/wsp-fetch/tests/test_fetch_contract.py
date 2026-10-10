from __future__ import annotations

from collections.abc import Iterator

import pytest

from wsp_core.ports import Fetcher
from wsp_core.testing.fetcher_contract import FetcherContract
from wsp_fetch import Aria2Fetcher, ChecksumMismatchError, FetchConfig, HttpFetcher, find_aria2c

FAST = FetchConfig(
    user_agent="wsp-test",
    piece_size=64 * 1024,
    min_piece_size=64 * 1024,
    backoff_base=0.01,
    backoff_max=0.05,
    max_attempts=3,
)


class TestHttpFetcherContract(FetcherContract):
    mismatch_error = ChecksumMismatchError

    @pytest.fixture
    def fetcher(self) -> Iterator[Fetcher]:
        with HttpFetcher(FAST) as f:
            yield f


@pytest.mark.skipif(find_aria2c() is None, reason="aria2c not installed")
class TestAria2FetcherContract(FetcherContract):
    mismatch_error = ChecksumMismatchError

    @pytest.fixture
    def fetcher(self) -> Iterator[Fetcher]:
        with Aria2Fetcher(FAST) as f:
            yield f
