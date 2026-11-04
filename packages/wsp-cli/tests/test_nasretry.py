"""A write the NAS refuses for a moment is tried again (2026-10-24: EAGAIN, a connection abort)."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from wsp_cli.nasretry import COPY_TRIES, COPY_WAIT_MAX_S, TRIES, copy_hashed, retrying


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


def _refusing_fsync(monkeypatch: pytest.MonkeyPatch, refusals: int) -> list[int]:
    """``os.fsync`` that the share refuses ``refusals`` times before it works."""
    import os

    real, left = os.fsync, [refusals]
    calls: list[int] = []

    def fsync(fd: int) -> None:
        calls.append(fd)
        if left[0] > 0:
            left[0] -= 1
            raise BlockingIOError(11, "Resource temporarily unavailable")
        real(fd)

    monkeypatch.setattr(os, "fsync", fsync)
    return calls


def test_a_long_copy_outlasts_a_bad_spell_on_the_share(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """2026-10-28 to 11-02: a 76 GB map part failed on five quick refusals in a row, three times."""
    src = tmp_path / "part.pmtiles"
    src.write_bytes(bytes(range(256)) * 4096)
    _refusing_fsync(monkeypatch, COPY_TRIES - 2)  # more than the old five tries
    pauses: list[float] = []
    sha = copy_hashed(src, tmp_path / "out.pmtiles", None, sleep=pauses.append, sync_every=1 << 20)
    assert (tmp_path / "out.pmtiles").read_bytes() == src.read_bytes()
    assert sha == hashlib.sha256(src.read_bytes()).hexdigest()
    assert pauses[:3] == [2.0, 4.0, 8.0]
    assert max(pauses) <= COPY_WAIT_MAX_S


def test_a_share_that_never_comes_back_fails_saying_where(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = tmp_path / "part.pmtiles"
    src.write_bytes(b"x" * 1000)
    _refusing_fsync(monkeypatch, 10**6)
    pauses: list[float] = []
    with pytest.raises(BlockingIOError, match="syncing the end at 0 of 1,000 bytes"):
        copy_hashed(src, tmp_path / "out.pmtiles", None, sleep=pauses.append)
    assert len(pauses) == COPY_TRIES - 1
    assert not (tmp_path / "out.pmtiles").exists()


def test_progress_between_refusals_renews_the_patience(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os

    src = tmp_path / "part.pmtiles"
    src.write_bytes(bytes(range(256)) * 4096 * 4)  # 4 MiB: four syncs of 1 MiB
    real, n = os.fsync, [0]

    def fsync(fd: int) -> None:  # every other sync is refused: each refusal follows progress
        n[0] += 1
        if n[0] % 2 == 0:
            raise ConnectionAbortedError(103, "Software caused connection abort")
        real(fd)

    monkeypatch.setattr(os, "fsync", fsync)
    pauses: list[float] = []
    copy_hashed(src, tmp_path / "out.pmtiles", None, sleep=pauses.append, sync_every=1 << 20)
    assert (tmp_path / "out.pmtiles").read_bytes() == src.read_bytes()
    assert set(pauses) == {2.0}  # never more than the first wait: each refusal came after new bytes
