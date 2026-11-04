import errno
import hashlib
import os
from collections.abc import Sequence
from pathlib import Path

import blake3
import pytest

from wsp_vault import atomic
from wsp_vault.atomic import (
    IntegrityError,
    ReadbackError,
    RetryPolicy,
    atomic_copy,
    atomic_write_bytes,
    is_transient,
    partial_path,
    readback,
)
from wsp_vault.hashing import Digests, hash_bytes, hash_file, supported_extra

DATA = b"0123456789abcdef" * 4096


def no_sleep_policy(delays: list[float] | None = None, attempts: int = 3) -> RetryPolicy:
    return RetryPolicy(
        attempts=attempts,
        base_delay=0.5,
        sleep=(delays.append if delays is not None else lambda _: None),
        jitter=0.0,
    )


def test_hash_file_matches_reference_and_extras(tmp_path: Path) -> None:
    path = tmp_path / "f"
    path.write_bytes(DATA)
    seen: list[int] = []
    d = hash_file(path, extra=["MD5", "sha1", "sha256"], on_chunk=seen.append, bufsize=1000)
    assert d.size == len(DATA) == sum(seen)
    assert d.sha256 == hashlib.sha256(DATA).hexdigest()
    assert d.blake3 == blake3.blake3(DATA).hexdigest()
    assert d.extra == {"md5": hashlib.md5(DATA).hexdigest(), "sha1": hashlib.sha1(DATA).hexdigest()}
    assert d == hash_bytes(DATA)


def test_supported_extra_drops_own_and_unknown() -> None:
    assert supported_extra(["SHA256", "md5", "blake3", "crc-made-up"]) == {"md5"}


def test_atomic_copy_verifies_and_leaves_no_partial(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.write_bytes(DATA)
    dest = tmp_path / "out" / "dest"
    dest.parent.mkdir()
    d = atomic_copy(src, dest, expected=hash_bytes(DATA))
    assert dest.read_bytes() == DATA
    assert d == hash_bytes(DATA)
    assert not partial_path(dest).exists()


def test_atomic_copy_rejects_unexpected_content_without_retry(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.write_bytes(DATA)
    dest = tmp_path / "dest"
    delays: list[float] = []
    with pytest.raises(IntegrityError):
        atomic_copy(src, dest, expected=Digests(len(DATA), "0" * 64, "0" * 64), retry=no_sleep_policy(delays))
    assert not dest.exists()
    assert not partial_path(dest).exists()
    assert delays == []


def test_atomic_copy_never_replaces_existing_file(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.write_bytes(DATA)
    dest = tmp_path / "dest"
    dest.write_bytes(b"precious")
    with pytest.raises(FileExistsError):
        atomic_copy(src, dest, retry=no_sleep_policy())
    assert dest.read_bytes() == b"precious"


def test_transient_readback_failure_is_retried(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real = atomic.readback
    calls = {"n": 0}

    def flaky(path: Path, **kw: object) -> Digests:
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError(errno.EIO, "simulated soft-mount timeout")
        if calls["n"] == 2:
            return Digests(0, "bad", "bad")
        return real(path)

    monkeypatch.setattr(atomic, "readback", flaky)
    delays: list[float] = []
    dest = tmp_path / "dest"
    atomic_write_bytes(dest, DATA, retry=no_sleep_policy(delays, attempts=5))
    assert dest.read_bytes() == DATA
    assert delays == [0.5, 1.0]


def test_retry_gives_up_and_propagates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def always_bad(path: Path, **kw: object) -> Digests:
        return Digests(0, "bad", "bad")

    monkeypatch.setattr(atomic, "readback", always_bad)
    dest = tmp_path / "dest"
    with pytest.raises(ReadbackError):
        atomic_write_bytes(dest, DATA, retry=no_sleep_policy(attempts=2))
    assert not dest.exists()
    assert not partial_path(dest).exists()


def test_non_transient_errors_are_not_retried() -> None:
    delays: list[float] = []

    def op() -> None:
        raise PermissionError(errno.EACCES, "denied")

    with pytest.raises(PermissionError):
        no_sleep_policy(delays).run(op)
    assert delays == []
    assert is_transient(TimeoutError())
    assert not is_transient(FileNotFoundError(errno.ENOENT, "x"))


def test_overwrite_for_pointer_files(tmp_path: Path) -> None:
    dest = tmp_path / "CURRENT.txt"
    atomic_write_bytes(dest, b"a\n")
    atomic_write_bytes(dest, b"b\n", overwrite=True)
    assert dest.read_bytes() == b"b\n"


def test_a_bad_address_from_the_network_mount_is_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CIFS answered a read with EFAULT under load (2026-10-13); the chunk is read again like an EIO."""
    assert is_transient(OSError(errno.EFAULT, "Bad address"))
    path = tmp_path / "f"
    path.write_bytes(b"x" * 100_000)
    real = os.preadv
    calls = {"n": 0}

    def flaky(fd: int, buffers: Sequence[bytearray | memoryview], offset: int) -> int:
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError(errno.EFAULT, "Bad address")
        return real(fd, buffers, offset)

    monkeypatch.setattr(os, "preadv", flaky)
    digests = readback(path, retry=RetryPolicy(attempts=3, sleep=lambda _s: None), bufsize=32_768)
    assert digests.sha256 == hashlib.sha256(b"x" * 100_000).hexdigest()
    assert calls["n"] >= 4  # the failed chunk was read again


def _count_temporaries(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    made: list[Path] = []
    real = atomic.temp_path

    def counting(dest: Path, tag: str = "") -> Path:
        made.append(real(dest, tag))
        return made[-1]

    monkeypatch.setattr(atomic, "temp_path", counting)
    return made


def test_atomic_copy_goes_on_from_the_last_synced_byte(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One refusal late in a 200 GB copy used to delete the temporary and start over from byte 0 with
    about 15 s of patience (2026-11-04 trawl). Now the copy goes on from the last sync, in the same
    temporary, with the hash state of that byte."""
    data = os.urandom(10 * 4096 + 123)
    src, dest = tmp_path / "src", tmp_path / "dest"
    src.write_bytes(data)
    made = _count_temporaries(monkeypatch)
    real_pwrite, real_fsync = os.pwrite, os.fsync
    writes: list[int] = []
    refuse = {"write": 1, "end": 1, "sync": 0}

    def flaky_pwrite(fd: int, chunk: bytes | memoryview, offset: int) -> int:
        writes.append(offset)
        if offset == 5 * 4096 and refuse["write"]:
            refuse["write"] -= 1
            real_pwrite(fd, bytes(chunk)[:100], offset)  # a torn write past the synced byte
            raise OSError(errno.ECONNABORTED, "Software caused connection abort")
        if offset == 10 * 4096 and refuse["end"]:
            refuse["end"], refuse["sync"] = 0, 1  # the end's fsync is refused once
        return real_pwrite(fd, chunk, offset)

    def flaky_fsync(fd: int) -> None:
        if refuse["sync"] and os.fstat(fd).st_size == len(data):
            refuse["sync"] = 0
            raise OSError(errno.EAGAIN, "Resource temporarily unavailable")
        real_fsync(fd)

    monkeypatch.setattr("wsp_vault.atomic.os.pwrite", flaky_pwrite)
    monkeypatch.setattr("wsp_vault.atomic.os.fsync", flaky_fsync)
    delays: list[float] = []
    got = atomic_copy(
        src, dest, extra=["md5"], retry=no_sleep_policy(delays), bufsize=4096, sync_every=2 * 4096
    )
    assert got == hash_bytes(data)
    assert got.extra == {"md5": hashlib.md5(data).hexdigest()}
    assert dest.read_bytes() == data
    assert len(made) == 1  # one temporary: never started over
    assert writes.count(0) == 1  # never rewritten from the start
    assert writes.count(4 * 4096) == 2  # resumed at the sync before the refused write
    assert writes.count(8 * 4096) == 1
    assert writes.count(10 * 4096) == 2  # and at the last sync before the refused end
    assert delays == [2.0, 2.0]  # progress in between gave the second refusal the whole patience
    assert sorted(p.name for p in tmp_path.iterdir()) == ["dest", "src"]


def test_atomic_copy_retries_a_read_back_chunk_without_rewriting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The read-back retried nothing (attempts=1): one EAGAIN in it threw the whole write away
    (2026-11-04 trawl). Now the failed chunk is read again at its own offset."""
    data = os.urandom(5 * 4096)
    src, dest = tmp_path / "src", tmp_path / "dest"
    src.write_bytes(data)
    made = _count_temporaries(monkeypatch)
    real = os.preadv
    refuse = {"n": 1}

    def flaky(fd: int, buffers: Sequence[bytearray | memoryview], offset: int) -> int:
        if refuse["n"] and offset and os.readlink(f"/proc/self/fd/{fd}").endswith(".partial"):
            refuse["n"] -= 1
            raise OSError(errno.EAGAIN, "Resource temporarily unavailable")
        return real(fd, buffers, offset)

    monkeypatch.setattr("wsp_vault.atomic.os.preadv", flaky)
    delays: list[float] = []
    assert atomic_copy(src, dest, retry=no_sleep_policy(delays), bufsize=4096) == hash_bytes(data)
    assert dest.read_bytes() == data
    assert len(made) == 1
    assert refuse["n"] == 0
    assert len(delays) == 1


def test_atomic_copy_gives_up_after_its_patience_without_starting_over(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A share that keeps refusing with no progress: COPY_TRIES refusals in a row (about 13 minutes),
    then the error; the caller's policy does not multiply that by starting the copy over."""
    src, dest = tmp_path / "src", tmp_path / "dest"
    src.write_bytes(DATA)
    made = _count_temporaries(monkeypatch)

    def refused(fd: int, chunk: bytes | memoryview, offset: int) -> int:
        raise OSError(errno.ECONNABORTED, "Software caused connection abort")

    monkeypatch.setattr("wsp_vault.atomic.os.pwrite", refused)
    delays: list[float] = []
    with pytest.raises(ConnectionAbortedError, match="0 bytes synced"):
        atomic_copy(src, dest, retry=no_sleep_policy(delays, attempts=5))
    assert delays == [2.0, 4.0, 8.0, 16.0, 32.0, 64.0, 128.0, 256.0, 300.0]
    assert len(made) == 1
    assert sorted(p.name for p in tmp_path.iterdir()) == ["src"]


def test_atomic_copy_still_starts_over_when_the_read_back_disagrees(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bytes on the share that differ from what was written are not resumed: a fresh temporary."""
    src, dest = tmp_path / "src", tmp_path / "dest"
    src.write_bytes(DATA)
    made = _count_temporaries(monkeypatch)
    real = atomic.readback
    calls = {"n": 0}

    def once_bad(path: Path, **kw: object) -> Digests:
        calls["n"] += 1
        return Digests(0, "bad", "bad") if calls["n"] == 1 else real(path)

    monkeypatch.setattr(atomic, "readback", once_bad)
    assert atomic_copy(src, dest, expected=hash_bytes(DATA), retry=no_sleep_policy()) == hash_bytes(DATA)
    assert dest.read_bytes() == DATA
    assert len(made) == 2
    assert not made[0].exists()
