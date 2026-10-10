import errno
import hashlib
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
)
from wsp_vault.hashing import Digests, hash_bytes, hash_file, supported_extra

DATA = b"0123456789abcdef" * 4096


def no_sleep_policy(delays: list[float] | None = None, attempts: int = 3) -> RetryPolicy:
    return RetryPolicy(
        attempts=attempts, base_delay=0.5, sleep=(delays.append if delays is not None else lambda _: None)
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
