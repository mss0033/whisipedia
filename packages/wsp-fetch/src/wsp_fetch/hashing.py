"""Streaming digests: SHA-256 + BLAKE3 + whatever upstream publishes, in one pass (PLAN §8.1).

Segments arrive out of order but SHA-256/MD5/SHA-1 cannot be combined from pieces, so the full-file
digests are computed by :class:`InOrderHasher`, a sliding in-order cursor fed with the piece buffers
the workers already hold in memory. Pieces that complete far ahead of the cursor are dropped from
memory (bounded window) and re-read from the page cache when the cursor reaches them. Each
algorithm runs on its own thread for large buffers: ``hashlib`` and ``blake3`` release the GIL,
so the wall time per buffer is the slowest algorithm, not the sum.
"""

from __future__ import annotations

import hashlib
import os
import threading
from collections.abc import Buffer, Callable, Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Protocol

from blake3 import blake3

from wsp_core.checksums import normalize_algorithm as normalize_algorithm  # re-export: one rule everywhere
from wsp_core.model import Checksum
from wsp_fetch.errors import ChecksumMismatchError, ConflictingChecksumsError, UnsupportedChecksumError

OWN_ALGORITHMS: tuple[str, ...] = ("sha256", "blake3")
"""Digests Whisipedia always records for its own manifests (PLAN §8.1)."""

_PARALLEL_MIN = 1 << 20


class Hasher(Protocol):
    def update(self, data: Buffer, /) -> object: ...

    def hexdigest(self) -> str: ...


def new_hasher(algorithm: str) -> Hasher:
    algo = normalize_algorithm(algorithm)
    if algo == "blake3":
        return blake3(max_threads=blake3.AUTO)
    try:
        return hashlib.new(algo, usedforsecurity=False)
    except ValueError as exc:
        raise UnsupportedChecksumError(f"cannot compute {algorithm!r} digests") from exc


def merge_checksums(*groups: Iterable[Checksum]) -> tuple[Checksum, ...]:
    """Union of digests with normalized algorithm names; raises if two sources disagree."""
    seen: dict[str, str] = {}
    for group in groups:
        for c in group:
            algo, value = normalize_algorithm(c.algorithm), c.hexdigest.strip().lower()
            if seen.setdefault(algo, value) != value:
                raise ConflictingChecksumsError(f"{algo}: {seen[algo]} != {value}")
    return tuple(Checksum(a, v) for a, v in seen.items())


class MultiHasher:
    """Feeds every buffer to several hashers, one thread per algorithm for large buffers."""

    def __init__(self, algorithms: Iterable[str]) -> None:
        names = list(dict.fromkeys([*OWN_ALGORITHMS, *(normalize_algorithm(a) for a in algorithms)]))
        self._hashers: dict[str, Hasher] = {n: new_hasher(n) for n in names}
        self._pool = ThreadPoolExecutor(max_workers=len(names), thread_name_prefix="wsp-hash")
        self.size = 0

    def update(self, data: Buffer) -> None:
        view = memoryview(data)
        self.size += view.nbytes
        if view.nbytes < _PARALLEL_MIN:
            for h in self._hashers.values():
                h.update(view)
            return
        futures = [self._pool.submit(h.update, view) for h in self._hashers.values()]
        for f in futures:
            f.result()

    def hexdigests(self) -> dict[str, str]:
        self._pool.shutdown(wait=True)
        return {n: h.hexdigest() for n, h in self._hashers.items()}

    def close(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)


def check_digests(url: str, expected: Sequence[Checksum], actual: dict[str, str]) -> None:
    """Raise :class:`ChecksumMismatchError` on the first upstream digest that differs."""
    for c in expected:
        algo = normalize_algorithm(c.algorithm)
        got = actual.get(algo)
        if got is None:
            raise UnsupportedChecksumError(f"{algo} was not computed for {url}")
        if got != c.hexdigest.strip().lower():
            raise ChecksumMismatchError(url, algo, c.hexdigest.lower(), got)


def hash_file(path: Path, algorithms: Iterable[str], *, block: int = 8 << 20) -> dict[str, str]:
    """Hash a file sequentially; returns ``{algorithm: hexdigest}`` including SHA-256 and BLAKE3."""
    hasher = MultiHasher(algorithms)
    buf = bytearray(block)
    view = memoryview(buf)
    try:
        with path.open("rb", buffering=0) as fh:
            if hasattr(os, "posix_fadvise"):
                os.posix_fadvise(fh.fileno(), 0, 0, os.POSIX_FADV_SEQUENTIAL)
            while n := fh.readinto(view):
                hasher.update(view[:n])
        return hasher.hexdigests()
    finally:
        hasher.close()


def pread_exact(fd: int, length: int, offset: int) -> bytes:
    chunks: list[bytes] = []
    while length > 0:
        chunk = os.pread(fd, length, offset)
        if not chunk:
            raise OSError(f"short read at offset {offset}")
        chunks.append(chunk)
        length -= len(chunk)
        offset += len(chunk)
    return b"".join(chunks)


class InOrderHasher:
    """Hashes pieces strictly in file order as they become available (sliding cursor).

    ``offer(i, data)`` hands over a verified piece; ``data=None`` means "it is on disk, read it".
    ``offer(i, None, verified=False)`` is for pieces left by a previous run: they are read once,
    checked with ``verify`` *before* entering the full-file digests, and reported via ``on_bad``
    if they fail so the scheduler can download them again.
    """

    def __init__(
        self,
        hasher: MultiHasher,
        fd: int,
        piece_span: Callable[[int], tuple[int, int]],
        n_pieces: int,
        *,
        window_bytes: int,
        verify: Callable[[int, bytes], bool],
        on_bad: Callable[[int], None],
    ) -> None:
        self.hasher = hasher
        self._fd = fd
        self._span = piece_span
        self._n = n_pieces
        self._window = window_bytes
        self._verify = verify
        self._on_bad = on_bad
        self._cond = threading.Condition()
        self._ready: dict[int, tuple[Buffer | None, bool]] = {}
        self._retained = 0
        self._aborted = False
        self.cursor = 0
        self.error: BaseException | None = None
        self.done = threading.Event()
        self._thread = threading.Thread(target=self._run, name="wsp-inorder-hash", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def offer(self, index: int, data: Buffer | None, *, verified: bool = True) -> None:
        with self._cond:
            if index < self.cursor or index in self._ready:
                return
            if data is not None:
                size = memoryview(data).nbytes
                if index != self.cursor and self._retained + size > self._window:
                    data = None  # beyond the window: re-read from page cache later
                else:
                    self._retained += size
            self._ready[index] = (data, verified)
            self._cond.notify_all()

    def abort(self) -> None:
        with self._cond:
            self._aborted = True
            self._cond.notify_all()

    def join(self, timeout: float | None = None) -> None:
        self._thread.join(timeout)

    def _run(self) -> None:
        try:
            while self.cursor < self._n:
                with self._cond:
                    while self.cursor not in self._ready and not self._aborted:
                        self._cond.wait()
                    if self._aborted:
                        return
                    data, verified = self._ready.pop(self.cursor)
                    if data is not None:
                        self._retained -= memoryview(data).nbytes
                if data is None:
                    offset, length = self._span(self.cursor)
                    data = pread_exact(self._fd, length, offset)
                if not verified and not self._verify(self.cursor, bytes(data)):
                    self._on_bad(self.cursor)
                    continue
                self.hasher.update(data)
                with self._cond:
                    self.cursor += 1
                    self._cond.notify_all()
        except BaseException as exc:
            self.error = exc
        finally:
            self.done.set()
