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
