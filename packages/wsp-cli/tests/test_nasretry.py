"""A write the NAS refuses for a moment is tried again (2026-10-24: EAGAIN, a connection abort)."""

from __future__ import annotations

import pytest

from wsp_cli.nasretry import TRIES, retrying


def test_a_moment_of_trouble_on_the_share_is_tried_again_and_a_lasting_one_is_raised() -> None:
    pauses: list[float] = []
    failures = [ConnectionAbortedError(103, "Software caused connection abort"), BlockingIOError(11, "again")]

    def write() -> str:
        if failures:
            raise failures.pop(0)
        return "sha"

    assert retrying("copy", write, sleep=pauses.append) == "sha"
    assert pauses == [2.0, 4.0]

    def always() -> str:
        raise ConnectionResetError(104, "reset")

    pauses.clear()
    with pytest.raises(ConnectionResetError):
        retrying("copy", always, sleep=pauses.append)
    assert len(pauses) == TRIES - 1


def test_a_real_error_is_not_retried() -> None:
    calls: list[int] = []

    def missing() -> None:
        calls.append(1)
        raise FileNotFoundError(2, "gone")

    with pytest.raises(FileNotFoundError):
        retrying("copy", missing, sleep=lambda _s: None)
    assert calls == [1]
