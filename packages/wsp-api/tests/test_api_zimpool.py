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


def test_random_articles_and_warm_opens(pool: ZimProcessPool, zim: str) -> None:
    assert pool.warm(zim) == 51  # HTML pages: Home + 50, not the redirect or the image
    seen: set[str] = set()
    for _ in range(30):
        found = pool.random(zim)
        assert found is not None
        path, _title = found
        assert path.startswith("A/")  # HTML articles only: never the image, never the redirect
        assert path != "A/Old"
        seen.add(path)
    assert len(seen) > 5  # identical calls in flight are not coalesced into one pick


def test_requests_waiting_for_an_archive_to_open_hold_no_thread() -> None:
    """2026-10-16 field exam: four page requests for an archive still opening (a cold open over SMB
    takes minutes) held all four threads of its worker; every other archive of that worker timed out."""
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from types import SimpleNamespace

    from wsp_api.zimread import ArchiveCache
    from wsp_api.zimworker import _Server  # pyright: ignore[reportPrivateUsage]

    release = threading.Event()

    def opener(file: str) -> object:
        if file == "slow.zim":
            assert release.wait(10)
        if file == "broken.zim":
            raise OSError("unreadable")
        return SimpleNamespace(metadata_keys=[], article_count=7)

    replies: list[tuple[int, bool, object]] = []
    done = threading.Condition()

    class Out:
        def send(self, reply: tuple[int, bool, object]) -> None:
            with done:
                replies.append(reply)
                done.notify_all()

    pool = ThreadPoolExecutor(2)
    server = _Server(Out(), 4, False, pool.submit)  # type: ignore[arg-type]
    server.cache = ArchiveCache(4, opener)  # type: ignore[arg-type]
    try:
        for rid in range(1, 6):  # the first opens it; the other four wait without a thread
            pool.submit(server.handle, (rid, "count", ("slow.zim",)))
        pool.submit(server.handle, (10, "count", ("fast.zim",)))
        with done:
            assert done.wait_for(lambda: any(r[0] == 10 for r in replies), timeout=5)
        release.set()
        with done:
            assert done.wait_for(lambda: len(replies) == 6, timeout=5)
        assert sorted(r[0] for r in replies if r[1] and r[2] == 7) == [1, 2, 3, 4, 5, 10]
        for rid in (20, 21):
            pool.submit(server.handle, (rid, "count", ("broken.zim",)))
        with done:
            assert done.wait_for(lambda: len(replies) == 8, timeout=5)
        assert [r[1] for r in replies[6:]] == [False, False]
    finally:
        release.set()
        pool.shutdown(wait=True)


def test_a_worker_opens_an_archive_after_a_helper_process_has(
    zim: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """2026-10-19: libzim held the GIL for an 18 s cold open, freezing the worker's other reads. A
    short-lived process opens the archive first; this one waits for it without the GIL."""
    import subprocess

    from wsp_api import zimworker

    ran: list[list[str]] = []
    real_run = subprocess.run

    def run(cmd: list[str], **kw: object) -> subprocess.CompletedProcess[bytes]:
        ran.append(cmd)
        return real_run(cmd, **kw)  # type: ignore[call-overload,no-any-return]

    monkeypatch.setattr(subprocess, "run", run)
    archive = zimworker.open_prewarmed(zim)
    assert archive.entry_count > 0
    assert ran
    assert ran[0][-1] == zim
    assert real_run(ran[0], check=False).returncode == 0  # the helper itself opens it cleanly

    # A helper that cannot run leaves the open as it was; a missing archive still fails here.
    def cannot_run(*_: object, **__: object) -> None:
        raise OSError("no helper")

    monkeypatch.setattr(subprocess, "run", cannot_run)
    assert zimworker.open_prewarmed(zim).entry_count == archive.entry_count
    with pytest.raises(RuntimeError):
        zimworker.open_prewarmed(str(tmp_path / "missing.zim"))


def test_a_random_page_is_never_a_captured_bot_challenge(tmp_path: Path) -> None:
    """2026-10-19 audit: most random pages of the Cloudflare Learning Center were its crawler's captures
    of Cloudflare challenges (challenges.cloudflare.com/cdn-cgi/...), which open as Not Found."""
    from wsp_api.zimread import open_archive, random_article

    items = [("www.ex.org/learning/", "text/html", "<p>a real page</p>")]
    items += [
        (f"challenges.ex.org/cdn-cgi/challenge-platform/h/b/{i}", "text/html", "<p>challenge</p>")
        for i in range(30)
    ]
    items += [("www.google.com/recaptcha/api2/anchor?ar=1", "text/html", "<p></p>")]
    items += [("ghbtns.com/github-btn.html?user=fat&type=follow", "text/html", "<p></p>")]
    items += [("womenshistory.si.edu/media/oembed?url=https://x", "text/html", "<p></p>")]
    items += [(f"youtube.fuzzy.replayweb.page/embed/v{i}", "text/html", "<p></p>") for i in range(10)]
    archive = open_archive(str(write_zim(tmp_path / "c.zim", "c", items, [])))
    picks = {random_article(archive) for _ in range(20)}
    assert picks <= {("www.ex.org/learning/", "www.ex.org/learning/"), None}
