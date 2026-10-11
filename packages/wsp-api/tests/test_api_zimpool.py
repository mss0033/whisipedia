"""ZIM reads in worker processes (audit 2026-10-11 #2): a dying worker costs its requests, not the API.

Workers are killed with SIGKILL here (what a SIGBUS does to them, without a crash dump).
"""

import os
import signal
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest

from wsp_api.testing.zims import write_zim
from wsp_api.zimpool import STRIKES, ZimProcessPool
from wsp_api.zimread import Want, ZimError, ZimUnavailable


@pytest.fixture
def zim(tmp_path: Path) -> str:
    items = [("A/Home", "text/html", "<p>home</p>"), ("I/x.png", "image/png", "png")]
    items += [(f"A/P{i}", "text/html", f"<p>{i}</p>") for i in range(50)]
    return str(write_zim(tmp_path / "w.zim", "w", items, [("A/Old", "A/Home")]))


@pytest.fixture
def pool() -> Iterator[ZimProcessPool]:
    p = ZimProcessPool(workers=2, max_open=4, timeout=10, test_ops=True)
    yield p
    p.close()


def _worker_pid(pool: ZimProcessPool, file: str) -> int:
    pid = cast(Any, pool.stats())["workers"][pool.worker_for(file)]["pid"]
    assert isinstance(pid, int)
    return pid


def test_reads_entries_redirects_and_the_main_page(pool: ZimProcessPool, zim: str) -> None:
    home = pool.read(zim, "A/Old", Want.HTML)
    assert home is not None
    assert (home.path, home.content) == ("A/Home", b"<p>home</p>")
    assert pool.read(zim, "", Want.NONE) == home.__class__("A/Home", "text/html", None)
    img = pool.read(zim, "I/x.png", Want.HTML)
    assert img is not None
    assert img.content is None  # not HTML: not shipped back
    assert pool.read(zim, "nope", Want.ANY) is None
    assert pool.book_name(zim) == "w"
    assert os.getpid() != _worker_pid(pool, zim)


def test_many_threads_read_concurrently(pool: ZimProcessPool, zim: str) -> None:
    got: dict[int, bytes | None] = {}
    errors: list[BaseException] = []

    def read(i: int) -> None:
        try:
            data = pool.read(zim, f"A/P{i % 50}", Want.HTML)
            got[i] = None if data is None else data.content
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=read, args=(i,)) for i in range(200)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert errors == []
    assert all(got[i] == f"<p>{i % 50}</p>".encode() for i in range(200))


def test_a_killed_worker_fails_its_requests_and_is_restarted(pool: ZimProcessPool, zim: str) -> None:
    assert pool.read(zim, "A/Home", Want.NONE) is not None
    first = _worker_pid(pool, zim)
    failed: list[BaseException] = []
    started = threading.Event()

    def slow() -> None:
        started.set()
        try:
            pool.call("_sleep", ("30",), zim)
        except BaseException as exc:
            failed.append(exc)

    t = threading.Thread(target=slow)
    t.start()
    started.wait()
    time.sleep(0.3)
    os.kill(first, signal.SIGKILL)  # as a SIGBUS would, minus the core dump
    t.join(10)
    assert len(failed) == 1
    assert isinstance(failed[0], ZimUnavailable)
    assert "SIGKILL" in str(failed[0])
    again = pool.read(zim, "A/Home", Want.HTML)  # a new worker, same answer
    assert again is not None
    assert again.content == b"<p>home</p>"
    assert _worker_pid(pool, zim) != first
    assert cast(Any, pool.stats())["workers"][pool.worker_for(zim)]["deaths"] == 1


def test_identical_requests_in_flight_share_one_call(pool: ZimProcessPool, zim: str) -> None:
    results: list[object] = []

    def call() -> None:
        results.append(pool.call("_sleep", ("0.5",), zim))

    threads = [threading.Thread(target=call) for _ in range(10)]
    t0 = time.monotonic()
    for t in threads:
        t.start()
    time.sleep(0.1)
    assert cast(Any, pool.stats())["inflight"] == 1
    for t in threads:
        t.join(10)
    assert results == [None] * 10
    assert time.monotonic() - t0 < 3


def test_timeouts_and_load_shedding(zim: str) -> None:
    pool = ZimProcessPool(workers=1, timeout=0.3, max_pending=2, test_ops=True)
    try:
        t0 = time.monotonic()
        with pytest.raises(ZimUnavailable, match="timed out"):
            pool.call("_sleep", ("5",), zim)
        assert time.monotonic() - t0 < 2
        with pytest.raises(ZimUnavailable):  # two calls still pending in the worker: refused at once
            pool.call("_sleep", ("6",), zim)
        with pytest.raises(ZimUnavailable, match="busy"):
            pool.call("_sleep", ("7",), zim)
    finally:
        pool.close()


def test_errors_inside_the_worker_are_reported_not_fatal(
    pool: ZimProcessPool, tmp_path: Path, zim: str
) -> None:
    missing = str(tmp_path / "missing.zim")
    with pytest.raises(ZimError) as err:
        pool.read(missing, "A/x", Want.HTML)
    assert not isinstance(err.value, ZimUnavailable)
    with pytest.raises(ZimError):
        pool.call("bogus-op", (), zim)
    assert pool.read(zim, "A/Home", Want.NONE) is not None  # the worker survived both


def test_a_file_that_keeps_killing_workers_is_refused(zim: str) -> None:
    pool = ZimProcessPool(workers=1, timeout=10, test_ops=True)
    try:
        for _ in range(STRIKES):
            pool.read(zim, "A/Home", Want.NONE)
            pid = _worker_pid(pool, zim)
            done = threading.Event()

            def slow(done: threading.Event = done) -> None:
                try:
                    pool.call("_sleep", ("30",), zim)
                except ZimUnavailable:
                    done.set()

            threading.Thread(target=slow).start()
            time.sleep(0.3)
            os.kill(pid, signal.SIGKILL)
            assert done.wait(10)
        with pytest.raises(ZimUnavailable, match="repeatedly"):
            pool.read(zim, "A/Home", Want.NONE)
    finally:
        pool.close()


def test_closed_pool_refuses_and_stops_its_workers(zim: str) -> None:
    pool = ZimProcessPool(workers=1)
    pool.read(zim, "A/Home", Want.NONE)
    pid = _worker_pid(pool, zim)
    pool.close()
    with pytest.raises(ZimUnavailable):
        pool.read(zim, "A/Home", Want.NONE)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)
    pytest.fail("worker still running after close()")


@pytest.mark.skipif(not Path("/proc/self/environ").exists(), reason="Linux /proc only")
def test_workers_never_write_core_dumps(pool: ZimProcessPool, zim: str) -> None:
    """A non-dumpable process's /proc files belong to root: its death by SIGBUS dumps nothing."""
    pool.read(zim, "A/Home", Want.NONE)
    pid = _worker_pid(pool, zim)
    assert os.stat(f"/proc/{pid}/environ").st_uid == 0
    assert os.stat(f"/proc/{os.getpid()}/environ").st_uid == os.getuid()
