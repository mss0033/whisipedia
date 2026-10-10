"""Atomic, verified writes for a ``soft`` SMB mount (ADR-0002, PLAN §8.1).

Every write follows the same path: write ``<name>.partial``, flush and fsync, drop the page cache,
read the bytes back and re-hash them, and only then rename onto the final name. A ``soft`` mount
surfaces server timeouts as ``EIO``/``ETIMEDOUT``; those are retried with exponential backoff, and
each retry starts the ``.partial`` from scratch.

Renames never replace an existing file unless the caller asks for it (pointer files such as
``CURRENT.txt``), so a copy can never silently destroy the only good version of something.
"""

from __future__ import annotations

import errno
import os
import time
from collections.abc import Callable, Iterable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from wsp_vault.hashing import BUFSIZE, Digests, MultiHasher, ProgressFn, hash_bytes, hash_stream

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


class IntegrityError(Exception):
    """Bytes do not match the digest they are supposed to have."""


class ReadbackError(IntegrityError):
    """What was read back differs from what was written. Treated as transient and retried."""


def is_transient(exc: BaseException) -> bool:
    if isinstance(exc, ReadbackError | TimeoutError):
        return True
    return isinstance(exc, OSError) and exc.errno in TRANSIENT_ERRNOS


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Exponential backoff for transient I/O failures."""

    attempts: int = 5
    base_delay: float = 1.0
    max_delay: float = 30.0
    sleep: Callable[[float], None] = time.sleep

    def run[T](self, op: Callable[[], T]) -> T:
        attempt = 1
        while True:
            try:
                return op()
            except (OSError, ReadbackError) as exc:
                if attempt >= self.attempts or not is_transient(exc):
                    raise
                self.sleep(min(self.max_delay, self.base_delay * 2 ** (attempt - 1)))
                attempt += 1


DEFAULT_RETRY = RetryPolicy()


def partial_path(dest: Path) -> Path:
    return dest.with_name(dest.name + PARTIAL_SUFFIX)


def fsync_dir(path: Path) -> None:
    """Best effort: CIFS and some platforms refuse to fsync a directory."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        with suppress(OSError):
            os.fsync(fd)
    finally:
        os.close(fd)


def _drop_cache(fd: int) -> None:
    with suppress(OSError, AttributeError):
        os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)


def readback(path: Path, *, extra: Iterable[str] = (), on_chunk: ProgressFn | None = None) -> Digests:
    """Hash a file after asking the kernel to drop cached pages, so the bytes come from storage."""
    with path.open("rb", buffering=0) as f:
        _drop_cache(f.fileno())
        return hash_stream(f, extra=extra, on_chunk=on_chunk)


def _commit(partial: Path, dest: Path, written: Digests, *, overwrite: bool) -> None:
    back = readback(partial)
    if back != written:
        raise ReadbackError(f"{partial}: read back {back.sha256} after writing {written.sha256}")
    if not overwrite and dest.exists():
        raise FileExistsError(errno.EEXIST, "refusing to replace existing file", str(dest))
    os.replace(partial, dest)
    fsync_dir(dest.parent)


def _cleanup_on_error[T](partial: Path, op: Callable[[], T]) -> T:
    try:
        return op()
    except BaseException:
        with suppress(OSError):
            partial.unlink()
        raise


def atomic_write_bytes(
    dest: Path, data: bytes, *, overwrite: bool = False, retry: RetryPolicy = DEFAULT_RETRY
) -> Digests:
    partial = partial_path(dest)
    written = hash_bytes(data)

    def attempt() -> Digests:
        with partial.open("wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        _commit(partial, dest, written, overwrite=overwrite)
        return written

    return retry.run(lambda: _cleanup_on_error(partial, attempt))


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
    """Copy ``src`` to ``dest`` hashing in the same pass; verify, read back, then rename.

    ``expected`` (size, sha256, blake3) and ``validate`` are checked against the source bytes before
    anything is renamed into place; a failure there raises :class:`IntegrityError` without retrying.
    """
    partial = partial_path(dest)
    algorithms = tuple(extra)

    def attempt() -> Digests:
        hasher = MultiHasher(algorithms)
        view = memoryview(bytearray(BUFSIZE))
        with src.open("rb", buffering=0) as fin, partial.open("wb") as fout:
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
        _commit(partial, dest, written, overwrite=overwrite)
        return written

    return retry.run(lambda: _cleanup_on_error(partial, attempt))
