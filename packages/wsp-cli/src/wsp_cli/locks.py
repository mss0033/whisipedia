"""Single-writer locks for operator commands (ADR-0002: lock files live on local disk, never the NAS).

The vault ledger and the index each assume one writer. Two concurrent ``wsp ingest`` runs, for example,
would interleave ledger appends and race on staging renames.

Lock names shared by every process (CLI, orchestrator, timer, ``tools/wsp-bg``):

* ``INDEX`` — the one writer of the Refinery, the Tantivy index, the suggest trie and the change feed;
* ``HEAVY`` — at most one memory-heavy job on the machine at a time (refine, index, rank, spell,
  embed), so their budgets never add up inside ``whisipedia-background.slice``;
* ``PROFILE`` — one content profile at a time. A profile holds little memory but, paced to a share of
  the NAS link (``[integrity] profile_busy_share``), runs for most of a day: under ``HEAVY`` it kept the
  nightly index build (which exits busy at once) and every refine waiting that long (2026-10-13);
* ``INGEST`` — the one writer of vault staging and promotion from ``_incoming``;
* ``SCRUB`` — the one scrub (and repair ladder) over the scrub state DB;
* ``DRILL`` — one chaos drill at a time.

A job that needs both takes ``HEAVY`` first, then ``INDEX`` (one global order, no deadlock). The
orchestrator's heavy steps take them *inside* their child unit (``wsp_cli.wiring``), so a child that
outlives a crashed daemon still holds them and a restarted daemon defers instead of running a second
writer. ``tools/wsp-bg --heavy`` takes ``HEAVY`` for ad-hoc jobs.
"""

from __future__ import annotations

import contextlib
import fcntl
import math
import os
import signal
import threading
import time
from collections.abc import Callable, Generator
from pathlib import Path

from wsp_orchestrator.errors import ResourceBusyError

INDEX = "index"
HEAVY = "heavy"
INGEST = "ingest"
PROFILE = "profile"  # one content profile at a time (hours long, paced: never under HEAVY)
SCRUB = "scrub"  # the one scrub/repair writer of the scrub state DB (orchestrator slices, wsp scrub)
DRILL = "drill"  # one chaos drill at a time (orchestrator, wsp drill)


class LockHeldError(ResourceBusyError):
    """Another process holds the lock.

    A :class:`~wsp_orchestrator.errors.ResourceBusyError`: in the orchestrator a busy lock defers
    the step (no attempt counted, audit H1); the CLI exits with :data:`EXIT_BUSY`.
    """


EXIT_BUSY = 3  # CLI exit code when a lock is held by another process


def lock_dir_for(index_root: Path) -> Path:
    """The machine's lock directory: local disk, next to the index root (ADR-0002)."""
    return index_root.parent / "locks"


INHERITED_ENV = "WSP_INHERITED_LOCKS"  # "name:fd[,name:fd]": locks a parent holds on our behalf


def _holds_inherited(name: str, path: Path) -> bool:
    """True if ``$WSP_INHERITED_LOCKS`` names an open descriptor of ``path`` that holds its lock."""
    for item in os.environ.get(INHERITED_ENV, "").split(","):
        lock, _, raw = item.strip().partition(":")
        if lock != name or not raw.isdigit():
            continue
        fd = int(raw)
        try:
            mine, there = os.fstat(fd), os.stat(path)
            if (mine.st_dev, mine.st_ino) != (there.st_dev, there.st_ino):
                return False
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # a no-op on the description that holds it
        except OSError:  # not open, not this file, or another process holds the lock
            return False
        return True
    return False


class _QueueTimeoutError(Exception):
    pass


MAX_TIMED_WAIT = 365 * 86400.0  # longer is "forever" (setitimer overflows on huge values)


def _block_for(fd: int, path: Path, name: str, wait: float) -> None:
    """Block in ``flock`` for at most ``wait`` seconds (a real-time timer interrupts it)."""
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return
    except BlockingIOError:
        pass

    if not math.isfinite(wait) or wait > MAX_TIMED_WAIT:  # no timer holds that: block outright
        fcntl.flock(fd, fcntl.LOCK_EX)  # (`--wait inf` crashed with OverflowError: review #37)
        return

    def expire(_signum: int, _frame: object) -> None:
        raise _QueueTimeoutError

    previous = signal.signal(signal.SIGALRM, expire)
    signal.setitimer(signal.ITIMER_REAL, max(wait, 0.001))
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
    except _QueueTimeoutError:
        raise LockHeldError(_held_message(path, name)) from None
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def _held_message(path: Path, name: str) -> str:
    holder = path.read_text(encoding="utf-8").strip() or "unknown"
    return f"another '{name}' is running (pid {holder}); lock: {path}"


def _alive(ticket: Path) -> bool:
    """A ticket is alive while its waiter holds its lock (the kernel drops it when the waiter dies)."""
    try:
        fd = os.open(ticket, os.O_RDONLY)
    except OSError:
        return False  # gone: its waiter took the lock or gave up
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    finally:
        os.close(fd)
    with contextlib.suppress(OSError):
        ticket.unlink()  # its waiter died
    return False


_RECHECK_S = 1.0  # how often the front waiter, blocked in flock, looks for a priority ticket


def _queue_for(fd: int, path: Path, name: str, wait: float, poll: float, *, priority: bool = False) -> None:
    """Take the lock in turn: first come, first served among queued waiters.

    A blocked ``flock`` is not a queue: the kernel wakes every waiter and whichever runs first takes
    the lock. The OpenAlex works ingest lost to a dozen lane ingests for 8 hours (2026-10-25), by
    giving up after 30 minutes and coming back last; a long wait keeps a waiter's place only if
    places are kept in order. Each waiter holds a
    ticket, ``<name>.queue/<time>-<pid>``, locked while it waits, and takes its turn when no older
    living ticket is left; then it blocks in ``flock`` for the lock itself. A ticket is locked before
    it appears in the queue (renamed in), so no one takes a new waiter's ticket for a dead one's.
    """
    queue = path.with_name(f"{name}.queue")
    queue.mkdir(exist_ok=True)
    # A priority ticket ("0" prefix: before every normal one, which starts with the time's own "1";
    # first come among themselves) is for a short, time-bound ingest: weather forecasts waited 20
    # minutes behind six one-hour lane slices, lost their turn every pull, and none was sealed for
    # three days (2026-10-29).
    mine = queue / f"{'0' if priority else ''}{time.time_ns():020d}-{os.getpid()}"
    draft = queue / f".{mine.name}"
    tfd = os.open(draft, os.O_RDWR | os.O_CREAT, 0o644)
    deadline = time.monotonic() + wait
    try:
        fcntl.flock(tfd, fcntl.LOCK_EX)
        os.rename(draft, mine)
        while True:
            while any(
                t.name < mine.name and _alive(t) for t in queue.iterdir() if not t.name.startswith(".")
            ):
                if time.monotonic() >= deadline:
                    raise LockHeldError(_held_message(path, name))
                time.sleep(min(poll, max(0.0, deadline - time.monotonic())))
            # Blocked in slices, so a priority ticket that comes while the front waiter is blocked
            # still goes before it; a blocked flock still takes the lock the moment it is released.
            try:
                remaining = deadline - time.monotonic()
                main = threading.current_thread() is threading.main_thread()  # a timer is the main thread's
                _block_for(fd, path, name, min(remaining, _RECHECK_S) if main else remaining)
                return
            except LockHeldError:
                if time.monotonic() >= deadline:
                    raise
    finally:
        for ticket in (mine, draft):
            with contextlib.suppress(OSError):
                ticket.unlink()
        os.close(tfd)


@contextlib.contextmanager
def exclusive(
    lock_dir: Path,
    name: str,
    *,
    wait: float | None = None,
    poll: float = 5.0,
    cancel: Callable[[], bool] | None = None,
    queue: bool = False,
    priority: bool = False,
) -> Generator[Path]:
    """Hold ``<lock_dir>/<name>.lock`` for the duration.

    ``wait=None`` (default) raises :class:`LockHeldError` at once if another process holds it;
    ``wait=seconds`` keeps trying every ``poll`` seconds for that long (``float("inf")``: forever)
    and stops early when ``cancel()`` turns true. ``flock`` locks are released by the kernel when the
    process exits, so a crash never leaves a stale lock behind. The file records the holder's pid.

    ``queue=True`` (with ``wait``, main thread only) waits in the lock's queue instead, in turn
    (:func:`_queue_for`), then in a blocking ``flock`` bounded by a timer. A process blocked in
    ``flock`` takes the lock the moment it is released, before a newcomer's first try; polling lost to
    the maps lane, whose next ingest took the lock within a second of each release, for hours
    (2026-10-24, as ERA5 did on 2026-10-20).

    A lock already held by a parent (``tools/wsp-bg`` holding ``HEAVY`` across a whole rebuild) is
    passed down as an open descriptor named in ``$WSP_INHERITED_LOCKS`` (``heavy:9``); it is used as
    is, never released here (audit 2026-10-12, M2). It is trusted only if that descriptor is this very
    lock file and holds the lock: ``flock`` on the inherited open file description succeeds only then.
    """
    lock_dir.mkdir(parents=True, exist_ok=True)
    path = lock_dir / f"{name}.lock"
    if _holds_inherited(name, path):
        yield path
        return
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        deadline = None if wait is None else time.monotonic() + wait
        if queue and wait is not None:
            _queue_for(fd, path, name, wait, min(poll, 2.0), priority=priority)
        while not queue or wait is None:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                done = deadline is None or time.monotonic() >= deadline or (cancel is not None and cancel())
                if done:
                    raise LockHeldError(_held_message(path, name)) from None
                time.sleep(poll)
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode())
        yield path
    finally:
        os.close(fd)
