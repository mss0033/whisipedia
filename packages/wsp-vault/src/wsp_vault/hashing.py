"""Streaming digests (PLAN §8.1): SHA-256 and BLAKE3 computed in a single read pass.

Upstream checksums (md5, sha1, ...) can be computed in the same pass via ``extra`` so a file is
read once per verification no matter how many algorithms are involved.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import blake3

BUFSIZE = 8 * 1024 * 1024
OWN_ALGORITHMS = frozenset({"sha256", "blake3"})

ProgressFn = Callable[[int], None]


class _Readable(Protocol):
    def readinto(self, buffer: memoryview, /) -> int | None: ...


@dataclass(frozen=True, slots=True)
class Digests:
    """Size plus our two digests. ``extra`` holds optional upstream algorithms and is not compared."""

    size: int
    sha256: str
    blake3: str
    extra: Mapping[str, str] = field(default_factory=dict[str, str], compare=False)


class MultiHasher:
    def __init__(self, extra: Iterable[str] = ()) -> None:
        self.size = 0
        self._sha256 = hashlib.sha256()
        self._blake3 = blake3.blake3(max_threads=blake3.blake3.AUTO)
        names = sorted({a.lower() for a in extra} - OWN_ALGORITHMS)
        self._extra = {name: hashlib.new(name, usedforsecurity=False) for name in names}

    def update(self, chunk: memoryview | bytes) -> None:
        self.size += len(chunk)
        self._sha256.update(chunk)
        self._blake3.update(chunk)
        for h in self._extra.values():
            h.update(chunk)

    def digests(self) -> Digests:
        return Digests(
            size=self.size,
            sha256=self._sha256.hexdigest(),
            blake3=self._blake3.hexdigest(),
            extra={name: h.hexdigest() for name, h in self._extra.items()},
        )


def supported_extra(algorithms: Iterable[str]) -> frozenset[str]:
    """The subset of ``algorithms`` that hashlib can compute alongside our own digests."""
    return (
        frozenset(a.lower() for a in algorithms if a.lower() in hashlib.algorithms_available) - OWN_ALGORITHMS
    )


def hash_stream(
    stream: _Readable,
    *,
    extra: Iterable[str] = (),
    on_chunk: ProgressFn | None = None,
    bufsize: int = BUFSIZE,
) -> Digests:
    hasher = MultiHasher(extra)
    view = memoryview(bytearray(bufsize))
    while n := stream.readinto(view):
        hasher.update(view[:n])
        if on_chunk is not None:
            on_chunk(n)
    return hasher.digests()


def hash_file(
    path: Path, *, extra: Iterable[str] = (), on_chunk: ProgressFn | None = None, bufsize: int = BUFSIZE
) -> Digests:
    with path.open("rb", buffering=0) as f:
        return hash_stream(f, extra=extra, on_chunk=on_chunk, bufsize=bufsize)


def hash_bytes(data: bytes, *, extra: Iterable[str] = ()) -> Digests:
    hasher = MultiHasher(extra)
    hasher.update(data)
    return hasher.digests()
