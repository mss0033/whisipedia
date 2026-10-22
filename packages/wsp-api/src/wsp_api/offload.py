"""A bounded executor for work that may block on the NAS or a ZIM worker (audit 2026-10-11 #5).

FastAPI runs plain ``def`` routes on one shared thread pool (40 threads). When the NAS stalls, routes
that read it pile up there until nothing else (not even ``/v1/health``) gets a thread. Routes that may
block therefore run here instead: a few dedicated threads, a bounded number of admitted calls, and a
deadline per call. Over capacity a call is refused at once with ``503``; past its deadline the caller
gets ``503`` (the thread finishes in the background and keeps its slot until then, so a stuck NAS
sheds load instead of queueing it).

The threads are daemon threads, started on demand: a read stuck in the kernel on a dead SMB mount must
not keep the process from exiting (``concurrent.futures.ThreadPoolExecutor`` joins its threads at exit).
"""

from __future__ import annotations

import asyncio
import queue
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from concurrent.futures import Future, wait
from typing import Any

from wsp_api.errors import ApiError

RETRY_AFTER = "5"

_Job = tuple[Callable[[], Any], Future[Any]] | None


class DaemonPool:
    """Up to ``workers`` daemon threads running submitted calls in order."""

    def __init__(self, workers: int, name: str) -> None:
        self._max = max(1, workers)
        self._name = name
        self._jobs: queue.SimpleQueue[_Job] = queue.SimpleQueue()
        self._lock = threading.Lock()
        self._threads = 0
        self._idle = 0
        self._closed = False

    def submit(self, fn: Callable[[], Any]) -> Future[Any]:
        future: Future[Any] = Future()
        with self._lock:
            if self._closed:
                raise RuntimeError("pool closed")
            spawn = self._idle == 0 and self._threads < self._max
            if spawn:
                self._threads += 1
            else:
                self._idle -= 1 if self._idle else 0
        self._jobs.put((fn, future))
        if spawn:
            threading.Thread(target=self._work, name=f"{self._name}-{self._threads}", daemon=True).start()
        return future

    def _work(self) -> None:
        while True:
            job = self._jobs.get()
            if job is None:
                return
            fn, future = job
            if future.set_running_or_notify_cancel():
                try:
                    future.set_result(fn())
                except BaseException as exc:
                    future.set_exception(exc)
            with self._lock:
                self._idle += 1

    def close(self) -> None:
        with self._lock:
            self._closed = True
            n = self._threads
        for _ in range(n):
            self._jobs.put(None)


class Offload:
    def __init__(
        self, *, workers: int = 8, queue: int = 32, timeout: float = 20.0, name: str = "nas"
    ) -> None:
        self.timeout = timeout
        self.capacity = workers + queue
        self._slots = threading.BoundedSemaphore(self.capacity)
        self._pool = DaemonPool(workers, name)
        self._lock = threading.Lock()
        self.admitted = 0  # calls in the pool now (running or queued)
        self.shed = 0
        self.timed_out = 0

    def _release(self, _: Future[Any]) -> None:
        with self._lock:
            self.admitted -= 1
        self._slots.release()

    async def run[R](self, fn: Callable[[], R], *, timeout: float | None = None) -> R:
        if not self._slots.acquire(blocking=False):
            with self._lock:
                self.shed += 1
            raise ApiError(503, "busy", "the library is busy; try again shortly", retry_after=RETRY_AFTER)
        with self._lock:
            self.admitted += 1
        try:
            future: Future[R] = self._pool.submit(fn)
        except RuntimeError:  # closed
            self._release(Future())
            raise ApiError(
                503, "unavailable", "the server is shutting down", retry_after=RETRY_AFTER
            ) from None
        future.add_done_callback(self._release)
        try:
            return await asyncio.wait_for(
                asyncio.wrap_future(future), self.timeout if timeout is None else timeout
            )
        except TimeoutError:
            with self._lock:
                self.timed_out += 1
            raise ApiError(
                503, "timeout", "the library storage did not answer in time", retry_after=RETRY_AFTER
            ) from None

    def close(self) -> None:
        self._pool.close()


class LinkChecks:
    """Which of a page's links lead nowhere, remembered: an answer is kept ``ttl`` seconds and a link
    being looked up is not looked up twice, so a page asked again (the reader does, for links left
    unchecked) gets the lookups that finished meanwhile. Under a long ingest a page of Wikipedia links
    took six rounds, each starting the slow lookups again (2026-10-22). ``check`` returns ``True``
    (missing), ``False`` (there) or ``None`` (unknown just now: busy, not remembered)."""

    def __init__(self, pool: DaemonPool, *, ttl: float = 600.0, size: int = 20_000) -> None:
        self._pool = pool
        self._ttl = ttl
        self._size = size
        self._clock = time.monotonic
        self._known: OrderedDict[str, tuple[float, bool]] = OrderedDict()
        self._running: dict[str, Future[bool | None]] = {}
        self._lock = threading.Lock()

    def _run(self, address: str, check: Callable[[str], bool | None]) -> bool | None:
        try:
            result = check(address)
        finally:
            with self._lock:
                self._running.pop(address, None)
        if result is not None:
            with self._lock:
                self._known[address] = (self._clock(), result)
                self._known.move_to_end(address)
                while len(self._known) > self._size:
                    self._known.popitem(last=False)
        return result

    def missing(
        self, addresses: list[str], check: Callable[[str], bool | None], timeout: float
    ) -> tuple[list[str], list[str]]:
        """``(missing, unchecked)`` of ``addresses`` within ``timeout`` seconds."""
        answers: dict[str, bool] = {}
        waiting: dict[str, Future[bool | None]] = {}
        now = self._clock()
        with self._lock:
            for a in addresses:
                hit = self._known.get(a)
                if hit is not None and now - hit[0] < self._ttl:
                    answers[a] = hit[1]
                    continue
                running = self._running.get(a)
                if running is None:
                    running = self._pool.submit(lambda a=a: self._run(a, check))
                    self._running[a] = running
                waiting[a] = running
        if waiting:
            done, _ = wait(list(waiting.values()), timeout=timeout)
            for a, f in waiting.items():
                if f in done and f.exception() is None and (r := f.result()) is not None:
                    answers[a] = r
        return [a for a in addresses if answers.get(a)], [a for a in addresses if a not in answers]
