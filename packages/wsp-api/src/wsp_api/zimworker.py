"""A ZIM worker process: ``python -m wsp_api.zimworker --fd-in R --fd-out W ...``.

Started by :class:`wsp_api.zimpool.ZimProcessPool`. It reads requests ``(id, op, args)`` from one pipe and
answers ``(id, ok, value)`` on the other, serving several requests at once from a few threads. Archives
stay open between requests (an LRU per worker). If libzim touches a page of a memory-mapped ZIM that
the NAS cannot deliver, the kernel sends ``SIGBUS`` and this process dies; the pool notices, fails the
requests in flight and starts a new worker. The process is marked non-dumpable first, so such a death
never writes a core dump (on this machine a crash dump is gigabytes on C:).

libzim holds the GIL for the whole of an archive's open: a cold open over SMB froze every other thread
of the worker, so every page of its other archives waited (2026-10-19: 18 s for an 8.6 GB archive, and
page views failed as busy all day, the background warmer opening one archive after another). Each
archive is first opened by a short-lived process (:func:`open_prewarmed`), which brings what libzim
reads into the page cache; the open here then takes a fraction of a second (0.05-0.2 s measured).
"""

from __future__ import annotations

import argparse
import ctypes
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import suppress
from multiprocessing.connection import Connection

from libzim.reader import Archive

from wsp_api.zimread import (
    ArchiveCache,
    Want,
    book_name_of,
    open_archive,
    page_count,
    random_article,
    read_entry,
    site_of,
)

_PR_SET_DUMPABLE = 4
PREOPEN_TIMEOUT = 900.0  # the largest archive (115 GB) took 99 s to open over SMB
# Run as `python -I -c PREOPEN file`: no core dump if the NAS fails a page (SIGBUS), any error ignored.
_PREOPEN = (
    "import resource, sys\n"
    "resource.setrlimit(resource.RLIMIT_CORE, (0, 0))\n"
    "from libzim.reader import Archive\n"
    "a = Archive(sys.argv[1])\n"
    "try:\n"
    "    a.main_entry.get_item()\n"
    "except Exception:\n"
    "    pass\n"
)


def open_prewarmed(file: str) -> Archive:
    """Open ``file`` after a short-lived process has opened it (see the module docstring): this thread
    waits for that process without the GIL, and the open here finds its pages cached. If the process
    fails, the open here is only as slow as it was before."""
    with suppress(OSError, subprocess.SubprocessError):
        subprocess.run(  # noqa: S603 - this interpreter, a fixed program, the archive path as argv
            [sys.executable, "-I", "-c", _PREOPEN, file],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=PREOPEN_TIMEOUT,
            check=False,
        )
    return open_archive(file)


def harden() -> None:
    """No core dumps (Linux only; elsewhere a no-op).

    Not ``PR_SET_PDEATHSIG``: it fires when the *thread* that started the worker exits, which in a
    thread pool is any time. The worker ends when its request pipe closes, which happens when the API
    process exits however it exits.
    """
    if not sys.platform.startswith("linux"):
        return
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        libc.prctl(_PR_SET_DUMPABLE, 0, 0, 0, 0)
    except (OSError, AttributeError):  # pragma: no cover - no libc prctl
        pass


def _inline(fn: Callable[..., object], *args: object) -> object:
    return fn(*args)


_ARCHIVE_OPS = frozenset({"read", "count", "random"})  # ops whose first argument is an archive to open


class _Server:
    def __init__(
        self,
        out: Connection,
        max_open: int,
        test_ops: bool,
        submit: Callable[..., object] | None = None,
        opener: Callable[[str], Archive] = open_archive,
    ) -> None:
        self.out = out
        self.cache = ArchiveCache(max_open, opener)
        self.test_ops = test_ops
        self.send_lock = threading.Lock()
        # Runs a handler on the worker's thread pool; None runs it inline (tests).
        self.submit: Callable[..., object] = submit or _inline

    def handle(self, msg: tuple[int, str, tuple[object, ...]]) -> None:
        rid, op, args = msg
        if op in _ARCHIVE_OPS and args:
            try:
                got = self.cache.get_or_wait(str(args[0]))
            except Exception as exc:
                self._reply(rid, exc)
                return
            if isinstance(got, Future):
                # Another thread is opening the archive: come back when it is open instead of holding
                # a thread meanwhile (the open itself fails every waiter with its error).
                got.add_done_callback(lambda f: self._resume(msg, f))
                return
        try:
            value = self.run(op, args)
        except Exception as exc:
            self._reply(rid, exc)
        else:
            self._send((rid, True, value))

    def _resume(self, msg: tuple[int, str, tuple[object, ...]], opened: Future[Archive]) -> None:
        exc = opened.exception()
        if exc is not None:
            self._reply(msg[0], exc)
        else:
            self.submit(self.handle, msg)

    def _reply(self, rid: int, exc: BaseException) -> None:
        self._send((rid, False, (type(exc).__name__, str(exc)[:500])))

    def _send(self, reply: tuple[int, bool, object]) -> None:
        # If the pool went away, the main loop ends on EOF.
        with self.send_lock, suppress(OSError, ValueError):
            self.out.send(reply)

    def run(self, op: str, args: Sequence[object]) -> object:
        if op == "read":
            file, path, want = (str(a) for a in args)
            return read_entry(self.cache.get(file), path, Want(want))
        if op == "book":
            file = str(args[0])
            return book_name_of(open_archive(file), file)  # not cached: bundles have hundreds of books
        if op == "site":
            file = str(args[0])
            found = site_of(self.cache.get(file), file)
            return None if found is None else list(found)
        if op == "count":  # opens the archive (kept open) and answers its article count
            return page_count(self.cache.get(str(args[0])))
        if op == "random":
            found = random_article(self.cache.get(str(args[0])))
            return None if found is None else list(found)
        if op == "ping":
            return os.getpid()
        if self.test_ops and op == "_sleep":
            time.sleep(float(str(args[0])))
            return None
        raise ValueError(f"unknown op {op!r}")


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="wsp_api.zimworker")
    p.add_argument("--fd-in", type=int, required=True)
    p.add_argument("--fd-out", type=int, required=True)
    p.add_argument("--max-open", type=int, default=16)
    p.add_argument("--threads", type=int, default=4)
    p.add_argument("--test-ops", action="store_true")
    args = p.parse_args(argv)
    harden()
    signal.signal(signal.SIGINT, signal.SIG_IGN)  # Ctrl-C goes to the API; the pool closes us
    inbox = Connection(args.fd_in, writable=False)
    pool = ThreadPoolExecutor(max(1, args.threads), thread_name_prefix="zim")
    server = _Server(
        Connection(args.fd_out, readable=False), args.max_open, args.test_ops, pool.submit, open_prewarmed
    )
    while True:
        try:
            msg = inbox.recv()
        except (EOFError, OSError):
            break
        if msg is None:
            break
        pool.submit(server.handle, msg)
    pool.shutdown(wait=False, cancel_futures=True)
    os._exit(0)  # do not wait for reads stuck on the NAS


if __name__ == "__main__":
    raise SystemExit(main())
