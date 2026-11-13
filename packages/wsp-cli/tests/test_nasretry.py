"""A write the NAS refuses for a moment is tried again (2026-10-24: EAGAIN, a connection abort)."""

from __future__ import annotations

import errno
import hashlib
from pathlib import Path

import pytest

from wsp_cli.nasretry import COPY_TRIES, COPY_WAIT_MAX_S, TRIES, copy_hashed, is_blip, is_transient, retrying
from wsp_core import nasretry as core


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


@pytest.mark.parametrize("code", [errno.EHOSTDOWN, errno.EIO, errno.ESTALE, errno.EFAULT])
def test_host_down_and_eio_from_the_share_are_tried_again(code: int) -> None:
    """Trawl 2026-11-10: the CIFS mount reports a NAS that is away as a plain OSError ("[Errno 112]
    Host is down"), which the three exception classes never matched."""
    pauses: list[float] = []
    failures = [OSError(code, "the share is away")]

    def read() -> str:
        if failures:
            raise failures.pop(0)
        return "bytes"

    assert retrying("read", read, sleep=pauses.append) == "bytes"
    assert pauses == [2.0]


def test_an_outage_errno_gets_the_longer_bounded_wait() -> None:
    pauses: list[float] = []

    def away() -> None:
        raise OSError(errno.EHOSTDOWN, "Host is down")

    with pytest.raises(OSError, match="Host is down") as caught:
        retrying("read", away, sleep=pauses.append)
    assert caught.value.errno == errno.EHOSTDOWN
    assert pauses == [2.0, 4.0, 8.0, 16.0, 32.0, 60.0, 60.0]  # about three minutes, then it is raised

    pauses.clear()

    def busy() -> None:
        raise OSError(errno.EIO, "Input/output error")

    with pytest.raises(OSError, match="Input/output"):
        retrying("read", busy, sleep=pauses.append)
    assert pauses == [2.0, 4.0, 8.0, 16.0]  # not an outage: the usual five tries


@pytest.mark.parametrize("code", [errno.ENOENT, errno.ENOSPC, errno.EACCES, errno.EROFS])
def test_enoent_and_enospc_are_not_retried(code: int) -> None:
    calls: list[int] = []

    def real_error() -> None:
        calls.append(1)
        raise OSError(code, "a real error")

    with pytest.raises(OSError, match="a real error"):
        retrying("write", real_error, sleep=lambda _s: None)
    assert calls == [1]
    assert not is_transient(OSError(code, "x"))


def test_is_blip_does_not_retry_eio() -> None:
    """The one retry that repeats only an fsync (the end of a ranged download) keeps the narrow test:
    a second fsync after EIO succeeds with the bytes missing."""
    calls: list[int] = []

    def fsync() -> None:
        calls.append(1)
        raise OSError(errno.EIO, "Input/output error")

    with pytest.raises(OSError, match="Input/output"):
        retrying("fsync", fsync, sleep=lambda _s: None, transient=is_blip)
    assert calls == [1]
    assert is_blip(BlockingIOError(11, "again"))
    assert is_blip(ConnectionAbortedError(103, "abort"))
    assert not is_blip(OSError(errno.EHOSTDOWN, "Host is down"))


def test_copy_hashed_resumes_after_eio(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    src = tmp_path / "part.pmtiles"
    src.write_bytes(bytes(range(256)) * 4096 * 3)  # 3 MiB: three syncs of 1 MiB
    real, n = os.fsync, [0]

    def fsync(fd: int) -> None:
        n[0] += 1
        if n[0] == 2:
            raise OSError(errno.EIO, "Input/output error")  # plain OSError, as CIFS gives it
        real(fd)

    monkeypatch.setattr(os, "fsync", fsync)
    pauses: list[float] = []
    written: list[int] = []
    sha = copy_hashed(
        src, tmp_path / "out.pmtiles", written.append, sleep=pauses.append, sync_every=1 << 20, chunk=1 << 20
    )
    assert pauses == [2.0]
    assert sum(written) == 4 << 20  # 2 MiB, then on from the last synced byte (1 MiB): not 2 + 3
    assert (tmp_path / "out.pmtiles").read_bytes() == src.read_bytes()
    assert sha == hashlib.sha256(src.read_bytes()).hexdigest()


def test_one_errno_set_for_the_vault_the_cod_reader_and_the_retry() -> None:
    from wsp_adapters.doccollection import cod
    from wsp_vault import atomic

    assert atomic.TRANSIENT_ERRNOS is core.TRANSIENT_ERRNOS
    assert cod.TRANSIENT_ERRNOS is core.TRANSIENT_ERRNOS
    assert core.OUTAGE_ERRNOS < core.TRANSIENT_ERRNOS
    assert cod.transient(OSError(errno.EFAULT, "Bad address"))
    assert atomic.is_transient(OSError(errno.EHOSTDOWN, "Host is down"))
