import subprocess
import sys
from pathlib import Path

import pytest

from wsp_cli.locks import LockHeldError, exclusive


def test_second_holder_is_refused_and_lock_frees_on_exit(tmp_path: Path) -> None:
    code = (
        "import sys, time\n"
        "from pathlib import Path\n"
        "from wsp_cli.locks import exclusive\n"
        "with exclusive(Path(sys.argv[1]), 'ingest'):\n"
        "    print('held', flush=True)\n"
        "    time.sleep(30)\n"
    )
    proc = subprocess.Popen([sys.executable, "-c", code, str(tmp_path)], stdout=subprocess.PIPE, text=True)
    try:
        assert proc.stdout is not None
        assert proc.stdout.readline().strip() == "held"
        with pytest.raises(LockHeldError, match=f"pid {proc.pid}"), exclusive(tmp_path, "ingest"):
            pass
    finally:
        proc.kill()
        proc.wait()
    with exclusive(tmp_path, "ingest") as path:
        assert path.read_text() != ""


def test_reentry_after_release(tmp_path: Path) -> None:
    with exclusive(tmp_path, "index"):
        pass
    with exclusive(tmp_path, "index"):
        pass


def test_a_queued_wait_takes_the_lock_when_it_is_released_and_gives_up_after_its_time(tmp_path: Path) -> None:
    """``wsp ingest --wait``: a lane waits in the lock's queue instead of polling (2026-10-24)."""
    import subprocess
    import sys
    import time

    import pytest

    from wsp_cli.locks import LockHeldError, exclusive

    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import fcntl,os,sys,time;fd=os.open(sys.argv[1],os.O_RDWR|os.O_CREAT);"
            "fcntl.flock(fd,fcntl.LOCK_EX);print('held',flush=True);time.sleep(float(sys.argv[2]))",
            str(tmp_path / "ingest.lock"),
            "1.0",
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert holder.stdout is not None
    assert holder.stdout.readline().strip() == "held"
    with pytest.raises(LockHeldError), exclusive(tmp_path, "ingest", wait=0.2, queue=True):
        pass
    t0 = time.monotonic()
    with exclusive(tmp_path, "ingest", wait=10, queue=True):
        waited = time.monotonic() - t0
    holder.wait(5)
    assert 0.3 < waited < 3  # taken when the holder let go, not after a poll interval or the timeout
    import signal

    assert signal.getsignal(signal.SIGALRM) in (signal.SIG_DFL, signal.default_int_handler, None)


_WAITER = (
    "import sys,time;sys.path[:0]=sys.argv[4:];from pathlib import Path;from wsp_cli.locks import exclusive\n"
    "with exclusive(Path(sys.argv[1]),'ingest',wait=20,queue=True,poll=0.05):\n"
    " open(sys.argv[2],'a').write(sys.argv[3]+'\\n');time.sleep(0.3)"
)


def test_queued_waiters_take_the_lock_in_the_order_they_came(tmp_path: Path) -> None:
    # 2026-10-25: a blocked flock is not a queue; the OpenAlex works ingest lost to a dozen lane
    # ingests for 8 hours.
    import subprocess
    import sys
    import time

    from wsp_cli.locks import exclusive

    order = tmp_path / "order"
    with exclusive(tmp_path, "ingest"):
        waiters: list[subprocess.Popen[bytes]] = []
        for name in ("first", "second", "third"):
            waiters.append(
                subprocess.Popen([sys.executable, "-c", _WAITER, str(tmp_path), str(order), name, *sys.path])
            )
            time.sleep(0.5)  # each waiting before the next comes
    for w in waiters:
        assert w.wait(30) == 0
    assert order.read_text().split() == ["first", "second", "third"]
    assert not list((tmp_path / "ingest.queue").glob("*"))  # every ticket removed


def test_a_dead_waiters_ticket_does_not_hold_up_the_queue(tmp_path: Path) -> None:
    import time

    from wsp_cli.locks import exclusive

    queue = tmp_path / "ingest.queue"
    queue.mkdir()
    (queue / f"{1:020d}-999999").write_text("")  # older than any, and nobody holds it
    t0 = time.monotonic()
    with exclusive(tmp_path, "ingest", wait=10, queue=True):
        assert time.monotonic() - t0 < 1
    assert list(queue.iterdir()) == []


def test_an_endless_wait_takes_the_lock_without_a_timer(tmp_path: Path) -> None:
    """2026-10-26 review #37: `wsp ingest --wait inf` crashed with OverflowError in setitimer."""
    import threading
    import time

    from wsp_cli.locks import exclusive

    got: list[str] = []
    with exclusive(tmp_path, "ingest"):
        t = threading.Thread(target=lambda: _take(tmp_path, got))
        t.start()
        time.sleep(0.3)
        assert got == []  # waiting in the queue, not crashed
    t.join(timeout=10)
    assert got == ["took it"]


def _take(lock_dir: Path, got: list[str]) -> None:
    from wsp_cli.locks import exclusive

    with exclusive(lock_dir, "ingest", wait=float("inf"), queue=True, poll=0.05):
        got.append("took it")
