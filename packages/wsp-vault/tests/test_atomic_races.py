"""Concurrency and fault injection for :mod:`wsp_vault.atomic` (vault-fetch audit #4, #6, #2, #9).

Real threads and real processes on a local temporary directory; the NAS is never touched.
"""

from __future__ import annotations

import errno
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from wsp_vault import atomic
from wsp_vault.atomic import (
    NoReplaceUnsupportedError,
    RetryPolicy,
    atomic_write_bytes,
    publish_noreplace,
    readback,
    rename_with_retry,
    temp_path,
)
from wsp_vault.hashing import hash_bytes

NO_SLEEP = RetryPolicy(attempts=5, base_delay=0.0, sleep=lambda _: None, jitter=0.0)
PAYLOADS = [bytes([65 + i]) * (50_000 * (i + 1)) for i in range(4)]


def _leftovers(directory: Path) -> list[str]:
    return sorted(p.name for p in directory.iterdir() if p.name.endswith(".partial"))


def test_temp_names_are_unique_and_exclusive(tmp_path: Path) -> None:
    dest = tmp_path / "CURRENT.txt"
    names = {temp_path(dest) for _ in range(1000)}
    assert len(names) == 1000
    assert all(n.name.startswith("CURRENT.txt.") and n.name.endswith(".partial") for n in names)
    taken = next(iter(names))
    taken.write_bytes(b"someone else's temporary")
    with pytest.raises(FileExistsError):
        atomic.create_exclusive(taken)
    assert taken.read_bytes() == b"someone else's temporary"


def test_concurrent_pointer_writers_threads_never_expose_torn_content(tmp_path: Path) -> None:
    """The auditor's torn.py: 4 writers + 1 reader on one pointer file (302 bad reads before)."""
    dest = tmp_path / "CURRENT.txt"
    atomic_write_bytes(dest, PAYLOADS[0], overwrite=True)
    valid = set(PAYLOADS)
    bad: list[int] = []
    errors: list[BaseException] = []
    stop = threading.Event()

    def writer(i: int) -> None:
        try:
            for _ in range(60):
                atomic_write_bytes(dest, PAYLOADS[i], overwrite=True, retry=NO_SLEEP)
        except BaseException as exc:
            errors.append(exc)

    def reader() -> None:
        while not stop.is_set():
            data = dest.read_bytes()
            if data not in valid:
                bad.append(len(data))

    r = threading.Thread(target=reader)
    r.start()
    writers = [threading.Thread(target=writer, args=(i,)) for i in range(4)]
    for t in writers:
        t.start()
    for t in writers:
        t.join()
    stop.set()
    r.join()
    assert errors == []
    assert bad == []
    assert dest.read_bytes() in valid
    assert _leftovers(tmp_path) == []


_WRITER = """
import sys
from pathlib import Path
from wsp_vault.atomic import RetryPolicy, atomic_write_bytes
dest, byte, n, size = Path(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4])
policy = RetryPolicy(attempts=5, base_delay=0.0, sleep=lambda _: None, jitter=0.0)
for _ in range(n):
    atomic_write_bytes(dest, bytes([byte]) * size, overwrite=True, retry=policy)
"""


def test_concurrent_pointer_writers_processes_never_expose_torn_content(tmp_path: Path) -> None:
    dest = tmp_path / "CURRENT.txt"
    sizes = [50_000 * (i + 1) for i in range(4)]
    valid = {bytes([65 + i]) * s for i, s in enumerate(sizes)}
    atomic_write_bytes(dest, next(iter(valid)), overwrite=True)
    procs = [
        subprocess.Popen([sys.executable, "-c", _WRITER, str(dest), str(65 + i), "40", str(s)])
        for i, s in enumerate(sizes)
    ]
    bad = 0
    while any(p.poll() is None for p in procs):
        if dest.read_bytes() not in valid:
            bad += 1
    assert [p.wait() for p in procs] == [0, 0, 0, 0]
    assert bad == 0
    assert dest.read_bytes() in valid
    assert _leftovers(tmp_path) == []


def test_no_replace_publish_has_exactly_one_winner(tmp_path: Path) -> None:
    dest = tmp_path / "MANIFEST.json"
    barrier = threading.Barrier(8)
    won: list[int] = []
    lost: list[int] = []

    def writer(i: int) -> None:
        barrier.wait()
        try:
            atomic_write_bytes(dest, f"writer {i}\n".encode() * 1000, retry=NO_SLEEP)
            won.append(i)
        except FileExistsError:
            lost.append(i)

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(won) == 1
    assert len(lost) == 7
    assert dest.read_bytes() == f"writer {won[0]}\n".encode() * 1000
    assert _leftovers(tmp_path) == []


def test_publish_falls_back_to_link_when_renameat2_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refused(src: Path, dest: Path) -> None:
        raise OSError(errno.EINVAL, "flag not supported by this filesystem")

    monkeypatch.setattr(atomic, "rename_noreplace", refused)
    src, dest = tmp_path / "a.partial", tmp_path / "a"
    src.write_bytes(b"new")
    publish_noreplace(src, dest)
    assert dest.read_bytes() == b"new"
    assert not src.exists()
    src.write_bytes(b"newer")
    with pytest.raises(FileExistsError):
        publish_noreplace(src, dest)
    assert dest.read_bytes() == b"new"


def test_publish_refuses_rather_than_check_then_act(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def no_rename(src: Path, dest: Path) -> None:
        raise OSError(errno.ENOSYS, "no renameat2")

    def no_link(src: Path, dest: Path) -> None:
        raise OSError(errno.EPERM, "hard links not supported")

    monkeypatch.setattr(atomic, "rename_noreplace", no_rename)
    monkeypatch.setattr(atomic, "link_noreplace", no_link)
    src = tmp_path / "x.partial"
    src.write_bytes(b"data")
    with pytest.raises(NoReplaceUnsupportedError, match="neither"):
        publish_noreplace(src, tmp_path / "x")
    assert not (tmp_path / "x").exists()
    assert src.exists()


def test_eio_mid_write_is_retried_with_a_fresh_temporary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A partial write that surfaces as EIO (soft mount): the attempt's temporary is removed and the
    next attempt starts from scratch."""
    real_write = os.write
    seen: list[str] = []
    calls = {"n": 0}

    def flaky(fd: int, data: bytes | memoryview) -> int:
        calls["n"] += 1
        if calls["n"] == 1:
            seen.append(os.readlink(f"/proc/self/fd/{fd}"))
            real_write(fd, bytes(data)[:100])
            raise OSError(errno.EIO, "soft mount timeout")
        return real_write(fd, data)

    monkeypatch.setattr("wsp_vault.atomic.os.write", flaky)
    dest = tmp_path / "f"
    data = os.urandom(300_000)
    assert atomic_write_bytes(dest, data, retry=NO_SLEEP) == hash_bytes(data)
    assert dest.read_bytes() == data
    assert not Path(seen[0]).exists()
    assert _leftovers(tmp_path) == []


def test_readback_retries_only_the_failed_chunk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "big"
    data = os.urandom(10 * 4096 + 123)
    path.write_bytes(data)
    real = os.preadv
    reads: list[int] = []
    failures = {"left": 3}

    def flaky(fd: int, buffers: list[memoryview], offset: int) -> int:
        reads.append(offset)
        if offset == 4 * 4096 and failures["left"]:
            failures["left"] -= 1
            raise OSError(errno.EHOSTDOWN, "server went away")
        return real(fd, buffers, offset)

    monkeypatch.setattr("wsp_vault.atomic.os.preadv", flaky)
    sleeps: list[float] = []
    policy = RetryPolicy(attempts=5, base_delay=1.0, sleep=sleeps.append, jitter=0.0)
    got = readback(path, retry=policy, bufsize=4096)
    assert got == hash_bytes(data)
    assert reads.count(0) == 1  # never restarted from the beginning
    assert reads.count(4 * 4096) == 4
    assert sleeps == [1.0, 2.0, 4.0]


def test_readback_gives_up_with_the_io_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "f"
    path.write_bytes(b"x" * 10_000)

    def dead(fd: int, buffers: list[memoryview], offset: int) -> int:
        raise OSError(errno.EIO, "mount is gone")

    monkeypatch.setattr("wsp_vault.atomic.os.preadv", dead)
    with pytest.raises(OSError, match="mount is gone"):
        readback(path, retry=RetryPolicy(attempts=3, base_delay=0.0, sleep=lambda _: None))
    with pytest.raises(FileNotFoundError):
        readback(tmp_path / "missing", retry=NO_SLEEP)


def test_rename_with_retry_waits_out_sharing_violations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src, dst = tmp_path / "d", tmp_path / "e"
    src.mkdir()
    real = os.rename
    left = {"n": 2}

    def busy(a: Path, b: Path) -> None:
        if left["n"]:
            left["n"] -= 1
            raise PermissionError(errno.EACCES, "sharing violation")
        real(a, b)

    monkeypatch.setattr("wsp_vault.atomic.os.rename", busy)
    sleeps: list[float] = []
    rename_with_retry(src, dst, sleep=sleeps.append)
    assert dst.is_dir()
    assert sleeps == [2.0, 4.0]

    def gone(a: Path, b: Path) -> None:
        raise FileNotFoundError(errno.ENOENT, "no such directory")

    monkeypatch.setattr("wsp_vault.atomic.os.rename", gone)
    with pytest.raises(FileNotFoundError):
        rename_with_retry(dst, src, sleep=sleeps.append)
    assert sleeps == [2.0, 4.0]
