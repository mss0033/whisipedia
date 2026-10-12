"""Atomic, verified writes and resilient reads for a ``soft`` SMB mount (ADR-0002, PLAN §8.1).

**Writes.** Every write follows the same path: create a temporary file with a name unique to this
attempt (``<name>.<random>.partial``, opened ``O_CREAT | O_EXCL`` so two writers can never share or
truncate each other's temporary), write, flush and fsync, drop the page cache, read the bytes back
and re-hash them, and only then publish the temporary under the final name:

* ``overwrite=False`` (data, manifests, keys) publishes with ``renameat2(RENAME_NOREPLACE)``, or
  ``link()`` + ``unlink()`` where the kernel or share refuses the flag. Both fail with ``EEXIST``
  if the destination appeared in the meantime, so "never replace" is enforced by the filesystem and
  not by an ``exists()`` check followed by a rename (vault-fetch audit #6). If the share supports
  neither, :class:`NoReplaceUnsupportedError` is raised: there is no silent check-then-act fallback.
* ``overwrite=True`` (pointer files such as ``CURRENT.txt``) publishes with ``rename``: concurrent
  writers each publish a complete file and the last one wins; a reader never sees torn or mixed
  content (audit #4). Callers that need "last writer wins" to mean something hold the vault lock.

A ``soft`` mount surfaces server timeouts as ``EIO``/``ETIMEDOUT``; those are retried with jittered
exponential backoff, and each retry starts a fresh temporary file.

**Reads.** :func:`readback` hashes a file in chunks with ``pread`` and retries a chunk that fails with
a transient error at the same offset (re-opening the file), so a 250 GB file is not re-read from the
start after a 30-second outage (audit #2, #9). Pages are dropped behind the cursor
(``POSIX_FADV_DONTNEED``) so the bytes hashed come from storage and a scrub does not fill the page
cache. A chunk that keeps failing raises the ``OSError``: callers classify that as *unreadable*,
never as damaged content.

**Directory fsync on CIFS** is accepted by the kernel and does nothing (measured: 4 µs, audit #25).
:func:`fsync_dir` is still called because it matters on local filesystems; on the NAS a rename is
durable when the server acknowledges it.
"""

from __future__ import annotations

import ctypes
import errno
import os
import random
import secrets
import time
from collections.abc import Callable, Iterable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from wsp_vault.hashing import BUFSIZE, Digests, MultiHasher, ProgressFn, hash_bytes

PARTIAL_SUFFIX = ".partial"

TRANSIENT_ERRNOS = frozenset(
    {
        errno.EIO,
        errno.ETIMEDOUT,
        errno.EAGAIN,
        errno.ESTALE,
        errno.ECONNRESET,
        errno.ECONNABORTED,
        errno.EHOSTDOWN,
        errno.EHOSTUNREACH,
        errno.ENETDOWN,
        errno.ENETUNREACH,
        errno.ENETRESET,
    }
)

# A share or kernel that cannot do an atomic no-replace publish answers with one of these.
_NOREPLACE_UNSUPPORTED = frozenset({errno.EINVAL, errno.ENOSYS, errno.EOPNOTSUPP, errno.ENOTSUP, errno.EPERM})
_AT_FDCWD = -100
_RENAME_NOREPLACE = 1


class IntegrityError(Exception):
    """Bytes do not match the digest they are supposed to have."""


class ReadbackError(IntegrityError):
    """What was read back differs from what was written. Treated as transient and retried."""


class NoReplaceUnsupportedError(OSError):
    """The filesystem supports neither ``RENAME_NOREPLACE`` nor hard links, so a write that must never
    replace an existing file cannot be made atomic. Nothing was published."""


def is_transient(exc: BaseException) -> bool:
    if isinstance(exc, ReadbackError | TimeoutError):
        return True
    return isinstance(exc, OSError) and exc.errno in TRANSIENT_ERRNOS


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Exponential backoff for transient I/O failures.

    ``jitter`` is the random fraction taken off each delay (0.25: a delay of 4 s becomes 3-4 s), so
    processes that failed together on a NAS outage do not retry in lockstep.
    """

    attempts: int = 5
    base_delay: float = 1.0
    max_delay: float = 30.0
    sleep: Callable[[float], None] = time.sleep
    jitter: float = 0.25

    def delay(self, attempt: int) -> float:
        """The pause after failed attempt number ``attempt`` (1-based)."""
        full = min(self.max_delay, self.base_delay * 2 ** (attempt - 1))
        return full * (1.0 - self.jitter * random.random())  # noqa: S311 - jitter, not crypto

    def run[T](self, op: Callable[[], T]) -> T:
        attempt = 1
        while True:
            try:
                return op()
            except (OSError, ReadbackError) as exc:
                if attempt >= self.attempts or not is_transient(exc):
                    raise
                self.sleep(self.delay(attempt))
                attempt += 1


DEFAULT_RETRY = RetryPolicy()
# Reads ride out a soft-mount outage of about two minutes per chunk (1+2+4+8+16+32+60 s) before a
# file is reported unreadable.
DEFAULT_READ_RETRY = RetryPolicy(attempts=8, base_delay=1.0, max_delay=60.0)


def partial_path(dest: Path) -> Path:
    """The *legacy* fixed temporary name (``<name>.partial``). New writes never use it; it is kept so
    leftovers from older versions are recognised. See :func:`temp_path`."""
    return dest.with_name(dest.name + PARTIAL_SUFFIX)


def temp_path(dest: Path, tag: str = "") -> Path:
    """A fresh temporary name next to ``dest``: ``<name>.<random>[.<tag>].partial``.

    Names ending in ``.partial`` are reserved (:func:`wsp_vault.manifest.check_relpath`), so a
    temporary can never collide with real content.
    """
    middle = f"{secrets.token_hex(6)}.{tag}" if tag else secrets.token_hex(6)
    return dest.with_name(f"{dest.name}.{middle}{PARTIAL_SUFFIX}")


def create_exclusive(path: Path) -> int:
    """``open(O_CREAT | O_EXCL)``: fails if anything (another writer's temporary) already has the name."""
    return os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o644)


def fsync_dir(path: Path) -> None:
    """Best effort: CIFS accepts a directory fsync and does nothing; some platforms refuse it."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        with suppress(OSError):
            os.fsync(fd)
    finally:
        os.close(fd)


def drop_cache(fd: int, offset: int = 0, length: int = 0) -> None:
    with suppress(OSError, AttributeError):
        os.posix_fadvise(fd, offset, length, os.POSIX_FADV_DONTNEED)


# --- reads -------------------------------------------------------------------------------------


def readback(
    path: Path,
    *,
    extra: Iterable[str] = (),
    on_chunk: ProgressFn | None = None,
    retry: RetryPolicy = DEFAULT_READ_RETRY,
    bufsize: int = BUFSIZE,
) -> Digests:
    """Hash ``path`` from storage (page cache dropped), retrying each chunk at its own offset.

    A transient error re-opens the file and re-reads only the failed chunk; the digests are updated
    with a chunk only after it was read completely, so a retry can never hash bytes twice. Raises the
    last ``OSError`` when a chunk still fails after ``retry.attempts`` tries (the caller reports the
    file *unreadable*), and ``FileNotFoundError`` at once.
    """
    hasher = MultiHasher(extra)
    buf = bytearray(bufsize)
    view = memoryview(buf)
    offset = 0
    fd: int | None = None
    attempt = 1
    try:
        while True:
            try:
                if fd is None:
                    fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
                    drop_cache(fd)
                n = os.preadv(fd, [view], offset)
            except OSError as exc:
                if fd is not None:
                    with suppress(OSError):
                        os.close(fd)
                    fd = None
                if isinstance(exc, FileNotFoundError) or attempt >= retry.attempts or not is_transient(exc):
                    raise
                retry.sleep(retry.delay(attempt))
                attempt += 1
                continue
            if n == 0:
                break
            hasher.update(view[:n])
            drop_cache(fd, offset, n)
            offset += n
            attempt = 1  # the budget is per chunk: progress resets it
            if on_chunk is not None:
                on_chunk(n)
    finally:
        if fd is not None:
            os.close(fd)
    return hasher.digests()


# --- publishing --------------------------------------------------------------------------------


def _libc_renameat2() -> Callable[[bytes, bytes], int] | None:
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        fn = libc.renameat2
    except (OSError, AttributeError):
        return None
    fn.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    fn.restype = ctypes.c_int

    def call(src: bytes, dst: bytes) -> int:
        return int(fn(_AT_FDCWD, src, _AT_FDCWD, dst, _RENAME_NOREPLACE))

    return call


_renameat2 = _libc_renameat2()


def rename_noreplace(src: Path, dest: Path) -> None:
    """``renameat2(RENAME_NOREPLACE)``. Raises ``FileExistsError`` if ``dest`` exists, ``OSError``
    with an "unsupported" errno (``EINVAL``, ``ENOSYS``, ...) where the call or flag is unavailable."""
    if _renameat2 is None:
        raise OSError(errno.ENOSYS, "renameat2 is not available", str(dest))
    if _renameat2(os.fsencode(src), os.fsencode(dest)) != 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err), str(src), None, str(dest))


def link_noreplace(src: Path, dest: Path) -> None:
    """``link(src, dest)`` then ``unlink(src)``: fails with ``FileExistsError`` if ``dest`` exists."""
    os.link(src, dest)
    os.unlink(src)


def _finish_interrupted_link(src: Path, dest: Path) -> bool:
    """If ``src`` and ``dest`` are one file (hard links), drop ``src`` and report the move as done."""
    try:
        if not os.path.samefile(src, dest):
            return False
    except FileNotFoundError:
        return False
    os.unlink(src)
    return True


def publish_noreplace(src: Path, dest: Path) -> None:
    """Move ``src`` to ``dest`` atomically, never replacing an existing ``dest``.

    Tries ``renameat2(RENAME_NOREPLACE)``, then ``link`` + ``unlink``. Raises ``FileExistsError`` if
    ``dest`` exists, :class:`NoReplaceUnsupportedError` if the filesystem supports neither.

    Resumable: if ``dest`` exists and *is* ``src`` (the same inode: a crash came between the
    fallback's ``link`` and ``unlink``), the move is finished by unlinking ``src`` (audit 2026-10-12,
    M1). Every caller re-running after such a crash completes instead of failing forever.
    """
    try:
        rename_noreplace(src, dest)
        return
    except FileExistsError:
        if _finish_interrupted_link(src, dest):
            return
        raise
    except OSError as exc:
        if exc.errno not in _NOREPLACE_UNSUPPORTED:
            raise
    try:
        link_noreplace(src, dest)
    except FileExistsError:
        if _finish_interrupted_link(src, dest):
            return
        raise
    except OSError as exc:
        if exc.errno in _NOREPLACE_UNSUPPORTED | {errno.EMLINK}:
            raise NoReplaceUnsupportedError(
                exc.errno,
                f"{dest.parent} supports neither renameat2(RENAME_NOREPLACE) nor hard links; a "
                "never-replace write cannot be made atomic here (mount the share with hard-link "
                "support, or use a filesystem that has it)",
                str(dest),
            ) from exc
        raise


def rename_with_retry(
    src: Path,
    dst: Path,
    *,
    attempts: int = 6,
    first_delay: float = 2.0,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """``os.rename`` that waits out transient sharing locks (about two minutes in total).

    On SMB a directory cannot be renamed while any client holds a file inside it open (a reader, an
    indexer or a virus scanner on the NAS); the server answers with a sharing violation that arrives
    as ``EACCES``/``EPERM`` or ``EBUSY``. Transient I/O errors are retried too. ``dst`` must not exist
    (directory renames onto an existing empty directory would succeed silently on Linux): the
    caller chooses a fresh name.
    """
    delay = first_delay
    for attempt in range(attempts):
        try:
            os.rename(src, dst)
            return
        except OSError as exc:
            retryable = isinstance(exc, PermissionError) or exc.errno == errno.EBUSY or is_transient(exc)
            if not retryable or attempt == attempts - 1:
                raise
            sleep(delay)
            delay *= 2


def _commit(tmp: Path, dest: Path, written: Digests, *, overwrite: bool) -> None:
    back = readback(tmp, retry=RetryPolicy(attempts=1))  # the caller's policy retries the attempt
    if back != written:
        raise ReadbackError(f"{tmp}: read back {back.sha256} after writing {written.sha256}")
    if overwrite:
        os.replace(tmp, dest)
    else:
        publish_noreplace(tmp, dest)
    fsync_dir(dest.parent)


def _attempt_in_temp[T](dest: Path, op: Callable[[Path], T]) -> T:
    """Run ``op`` with a fresh temporary name; remove that temporary (ours alone) if it fails."""
    tmp = temp_path(dest)
    try:
        return op(tmp)
    except BaseException:
        with suppress(OSError):
            tmp.unlink()
        raise


def atomic_write_bytes(
    dest: Path, data: bytes, *, overwrite: bool = False, retry: RetryPolicy = DEFAULT_RETRY
) -> Digests:
    """Write ``data`` to ``dest`` via a unique temporary, verified by read-back. See the module doc."""
    written = hash_bytes(data)

    def attempt(tmp: Path) -> Digests:
        fd = create_exclusive(tmp)
        try:
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view) :]
            os.fsync(fd)
        finally:
            os.close(fd)
        _commit(tmp, dest, written, overwrite=overwrite)
        return written

    return retry.run(lambda: _attempt_in_temp(dest, attempt))


def atomic_copy(
    src: Path,
    dest: Path,
    *,
    expected: Digests | None = None,
    validate: Callable[[Digests], None] | None = None,
    extra: Iterable[str] = (),
    overwrite: bool = False,
    retry: RetryPolicy = DEFAULT_RETRY,
    on_chunk: ProgressFn | None = None,
) -> Digests:
    """Copy ``src`` to ``dest`` hashing in the same pass; verify, read back, then publish.

    ``expected`` (size, sha256, blake3) and ``validate`` are checked against the source bytes before
    anything is published; a failure there raises :class:`IntegrityError` without retrying. Returns
    the digests of the bytes written (including ``extra`` algorithms).
    """
    algorithms = tuple(extra)

    def attempt(tmp: Path) -> Digests:
        hasher = MultiHasher(algorithms)
        view = memoryview(bytearray(BUFSIZE))
        with src.open("rb", buffering=0) as fin, os.fdopen(create_exclusive(tmp), "wb") as fout:
            while n := fin.readinto(view):
                chunk = view[:n]
                hasher.update(chunk)
                fout.write(chunk)
                if on_chunk is not None:
                    on_chunk(n)
            fout.flush()
            os.fsync(fout.fileno())
        written = hasher.digests()
        if expected is not None and written != expected:
            raise IntegrityError(f"{src}: content {written.sha256} does not match expected {expected.sha256}")
        if validate is not None:
            validate(written)
        _commit(tmp, dest, written, overwrite=overwrite)
        return written

    return retry.run(lambda: _attempt_in_temp(dest, attempt))
