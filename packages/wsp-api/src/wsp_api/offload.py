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
from collections.abc import Callable
from concurrent.futures import Future
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
