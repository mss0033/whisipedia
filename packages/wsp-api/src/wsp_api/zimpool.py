"""ZIM reads in worker processes, so a ``SIGBUS`` from the NAS kills a worker instead of the API.

libzim memory-maps archives. On the soft-mounted CIFS share an I/O error during a page fault is a
``SIGBUS``, which terminates the process (audit 2026-10-11 #2; Python cannot catch it). The pool runs
:mod:`wsp_api.zimworker` processes and talks to them over pipes:

* **Affinity.** Requests for a file always go to the same worker, so each archive is opened once and
  stays open there (an LRU of ``max_open`` archives per worker).
* **Coalescing.** Identical requests in flight share one answer.
* **Timeouts and shedding.** A caller waits at most ``timeout`` seconds; a worker with ``max_pending``
  requests in flight refuses more. Both raise :class:`ZimUnavailable` (HTTP 503), never hang a thread.
* **Restart.** A worker that dies fails its requests in flight with :class:`ZimUnavailable` and is
  started again on the next request (with a short back-off after repeated deaths). A file that was
  being read during several deaths is refused for a while instead of crashing workers in a loop.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import threading
import time
import zlib
from collections import deque
from collections.abc import Callable
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeout
from contextlib import suppress
from dataclasses import dataclass
from multiprocessing.connection import Connection
from typing import cast

from wsp_api.zimread import EntryData, Want, ZimError, ZimUnavailable

log = logging.getLogger(__name__)

STRIKES = 3  # deaths with a file in flight before it is refused for a while
STRIKE_WINDOW = 600.0
RESTART_BURST = 5  # restarts within RESTART_WINDOW before backing off
RESTART_WINDOW = 60.0
RESTART_BACKOFF = 10.0

Key = tuple[str, tuple[str, ...]]


def _describe_exit(code: int | None) -> str:
    if code is None:
        return "still running"
    if code < 0:
        try:
            return f"killed by {signal.Signals(-code).name}"
        except ValueError:
            return f"killed by signal {-code}"
    return f"exited with status {code}"


@dataclass(slots=True)
class _Call:
    future: Future[object]
    file: str


class _Worker:
    """One worker process, its pipes, and the requests it has not answered yet."""

    def __init__(self, index: int, command: Callable[[int, int], list[str]]) -> None:
        self.index = index
        self._command = command
        self._lock = threading.Lock()
        self._proc: subprocess.Popen[bytes] | None = None
        self._out: Connection | None = None
        self._pending: dict[int, _Call] = {}
        self._next = 0
        self._restarts: deque[float] = deque(maxlen=RESTART_BURST)
        self.deaths = 0
        self.on_death: Callable[[list[str], str], None] = lambda _files, _why: None

    @property
    def pending(self) -> int:
        with self._lock:
            return len(self._pending)

    @property
    def pid(self) -> int | None:
        with self._lock:
            return None if self._proc is None else self._proc.pid

    def _start(self) -> None:
        """Start the process (lock held)."""
        now = time.monotonic()
        burst = len(self._restarts) == RESTART_BURST and now - self._restarts[0] < RESTART_WINDOW
        if burst and now - self._restarts[-1] < RESTART_BACKOFF:
            raise ZimUnavailable("ZIM worker keeps dying; backing off")
        to_child_r, to_child_w = os.pipe()
        to_parent_r, to_parent_w = os.pipe()
        try:
            proc = subprocess.Popen(  # noqa: S603 - our own interpreter and module
                self._command(to_child_r, to_parent_w),
                pass_fds=(to_child_r, to_parent_w),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                close_fds=True,
            )
        except OSError as exc:
            for fd in (to_child_r, to_child_w, to_parent_r, to_parent_w):
                os.close(fd)
            raise ZimUnavailable(f"cannot start a ZIM worker: {exc}") from exc
        os.close(to_child_r)
        os.close(to_parent_w)
        self._proc = proc
        self._out = Connection(to_child_w, readable=False)
        inbox = Connection(to_parent_r, writable=False)
        self._restarts.append(now)
        threading.Thread(
            target=self._receive, args=(proc, inbox), name=f"zim-worker-{self.index}", daemon=True
        ).start()

    def submit(self, op: str, args: tuple[str, ...], file: str) -> Future[object]:
        future: Future[object] = Future()
        with self._lock:
            if self._proc is None:
                self._start()
            assert self._out is not None
            rid = self._next
            self._next += 1
            self._pending[rid] = _Call(future, file)
            # If the worker is gone, its reader thread fails everything in flight, this one included.
            with suppress(OSError, ValueError):
                self._out.send((rid, op, args))
        return future

    def _receive(self, proc: subprocess.Popen[bytes], inbox: Connection) -> None:
        while True:
            try:
                rid, ok, value = cast(tuple[int, bool, object], inbox.recv())
            except (EOFError, OSError):
                break
            with self._lock:
                call = self._pending.pop(rid, None)
            if call is None:
                continue
            if ok:
                call.future.set_result(value)
            else:
                kind, message = cast(tuple[str, str], value)
                call.future.set_exception(ZimError(f"{kind}: {message}"))
        inbox.close()
        self._died(proc)

    def _died(self, proc: subprocess.Popen[bytes]) -> None:
        try:
            code = proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            code = proc.wait()
        why = _describe_exit(code)
        with self._lock:
            if self._proc is not proc:  # closed on purpose
                return
            self._proc = None
            if self._out is not None:
                self._out.close()
                self._out = None
            lost = list(self._pending.values())
            self._pending.clear()
            self.deaths += 1
        if lost or code != 0:
            log.warning(
                "ZIM worker %d (pid %d) %s with %d request(s) in flight", self.index, proc.pid, why, len(lost)
            )
        self.on_death([c.file for c in lost], why)
        for call in lost:
            call.future.set_exception(ZimUnavailable(f"ZIM worker {why}"))

    def close(self) -> None:
        with self._lock:
            proc, out = self._proc, self._out
            self._proc = self._out = None
            lost = list(self._pending.values())
            self._pending.clear()
        if out is not None:
            with suppress(OSError, ValueError):
                out.send(None)
            out.close()
        if proc is not None:
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        for call in lost:
            call.future.set_exception(ZimUnavailable("ZIM reader closed"))


class ZimProcessPool:
    """:class:`wsp_api.zimread.ZimReader` over worker processes (see the module docstring)."""

    def __init__(
        self,
        *,
        workers: int = 2,
        max_open: int = 16,
        threads: int = 4,
        timeout: float = 15.0,
        max_pending: int = 64,
        test_ops: bool = False,
    ) -> None:
        self.timeout = timeout
        self.max_pending = max_pending
        self._max_open = max_open
        self._threads = threads
        self._test_ops = test_ops
        self._workers = [_Worker(i, self._command) for i in range(max(1, workers))]
        for w in self._workers:
            w.on_death = self._strike
        self._inflight: dict[Key, Future[object]] = {}
        # Reentrant: a future that is already done runs its done-callback (which takes this lock) at once.
        self._lock = threading.RLock()
        self._strikes: dict[str, deque[float]] = {}
        self._closed = False

    def _command(self, fd_in: int, fd_out: int) -> list[str]:
        cmd = [
            sys.executable,
            "-m",
            "wsp_api.zimworker",
            f"--fd-in={fd_in}",
            f"--fd-out={fd_out}",
            f"--max-open={self._max_open}",
            f"--threads={self._threads}",
        ]
        return [*cmd, "--test-ops"] if self._test_ops else cmd

    # --- ZimReader ---

    def read(self, file: str, path: str, want: Want) -> EntryData | None:
        return cast(EntryData | None, self.call("read", (file, path, want.value), file))

    def book_name(self, file: str) -> str:
        return cast(str, self.call("book", (file,), file))

    def close(self) -> None:
        with self._lock:
            self._closed = True
        for w in self._workers:
            w.close()

    # --- plumbing ---

    def worker_for(self, file: str) -> int:
        return zlib.crc32(file.encode("utf-8", errors="surrogatepass")) % len(self._workers)

    def call(self, op: str, args: tuple[str, ...], file: str, *, timeout: float | None = None) -> object:
        """Run ``op`` in the worker that owns ``file``; identical calls in flight share one answer."""
        key: Key = (op, args)
        with self._lock:
            if self._closed:
                raise ZimUnavailable("ZIM reader closed")
            future = self._inflight.get(key)
            if future is None:
                self._check_strikes(file)
                worker = self._workers[self.worker_for(file)]
                if worker.pending >= self.max_pending:
                    raise ZimUnavailable("ZIM worker busy")
                future = worker.submit(op, args, file)
                self._inflight[key] = future
                future.add_done_callback(lambda f: self._forget(key, f))
        try:
            return future.result(timeout=self.timeout if timeout is None else timeout)
        except FutureTimeout as exc:
            raise ZimUnavailable(f"ZIM read timed out after {self.timeout:.0f} s") from exc

    def _forget(self, key: Key, future: Future[object]) -> None:
        with self._lock:
            if self._inflight.get(key) is future:
                del self._inflight[key]

    def _check_strikes(self, file: str) -> None:
        """Lock held. Refuse a file that was in flight during several recent worker deaths."""
        hits = self._strikes.get(file)
        if hits is None:
            return
        now = time.monotonic()
        while hits and now - hits[0] > STRIKE_WINDOW:
            hits.popleft()
        if not hits:
            del self._strikes[file]
        elif len(hits) >= STRIKES:
            raise ZimUnavailable("this ZIM crashed its reader repeatedly; refusing it for a while")

    def _strike(self, files: list[str], why: str) -> None:
        now = time.monotonic()
        with self._lock:
            for file in set(files):
                hits = self._strikes.setdefault(file, deque(maxlen=STRIKES))
                hits.append(now)
                if len(hits) >= STRIKES:
                    log.error(
                        "refusing %s for %.0f s: its reads %s %d times", file, STRIKE_WINDOW, why, STRIKES
                    )

    def stats(self) -> dict[str, object]:
        return {
            "workers": [{"pid": w.pid, "pending": w.pending, "deaths": w.deaths} for w in self._workers],
            "inflight": len(self._inflight),
        }
