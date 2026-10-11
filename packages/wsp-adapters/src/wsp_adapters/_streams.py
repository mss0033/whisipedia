"""Streaming helpers shared by the bulk-file adapters: decompression, listings, bitsets, politeness.

**bz2 decoding.** Several upstreams ship one huge single-stream ``.bz2`` (the Wikidata JSON dump,
CourtListener CSVs). Python's ``bz2`` decodes one stream on one core at about 70 MB/s of output.
:func:`open_bz2` picks the fastest decoder available, in this order:

1. ``lbzip2`` or ``pbzip2`` on ``PATH`` (external process, parallel);
2. ``indexed_bzip2`` (a wheel dependency): finds bz2 *block* boundaries at bit level and decodes
   blocks on ``workers`` threads, so it is parallel even for single-stream files. Measured on a
   Wikidata sample: 70 MB/s sequential, 250 MB/s with 4 threads, 410 MB/s with 8, 580 MB/s with 15;
3. sequential ``bz2``.
"""

from __future__ import annotations

import bz2
import gzip
import io
import re
import shutil
import subprocess
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager
from html import unescape
from pathlib import Path
from typing import IO, Any, cast
from urllib.parse import urljoin

import zstandard


@contextmanager
def open_bz2(path: Path, workers: int = 1) -> Generator[IO[bytes]]:
    """Binary reader of a ``.bz2`` file, decoded in parallel when ``workers > 1`` (see module doc)."""
    tool = shutil.which("lbzip2") or shutil.which("pbzip2")
    if workers > 1 and tool is not None:
        proc = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
            [tool, "-dc", f"-n{workers}" if tool.endswith("lbzip2") else f"-p{workers}", str(path)],
            stdout=subprocess.PIPE,
        )
        assert proc.stdout is not None
        try:
            yield proc.stdout
        finally:
            proc.stdout.close()
            if proc.wait() not in (0, -13):  # -13: SIGPIPE when the consumer stops early
                raise OSError(f"{tool} failed on {path.name} with status {proc.returncode}")
        return
    if workers > 1:
        import indexed_bzip2  # imported on use: only the parallel path needs it

        raw = cast(Any, indexed_bzip2).open(str(path), parallelization=workers)
        reader = io.BufferedReader(cast(io.RawIOBase, raw), buffer_size=4 * 1024 * 1024)
        try:
            yield reader
        finally:
            reader.close()
        return
    with bz2.open(path, "rb") as fh:
        yield fh


@contextmanager
def open_any(path: Path, workers: int = 1) -> Generator[IO[bytes]]:
    """Binary reader that undoes ``.gz``, ``.bz2`` or ``.zst`` compression by file name."""
    name = path.name
    if name.endswith(".bz2"):
        with open_bz2(path, workers) as fh:
            yield fh
    elif name.endswith(".gz"):
        with gzip.open(path, "rb") as fh:
            yield cast(IO[bytes], fh)
    elif name.endswith(".zst"):
        with path.open("rb") as raw, zstandard.ZstdDecompressor().stream_reader(raw) as zr:
            yield cast(IO[bytes], io.BufferedReader(cast(io.RawIOBase, zr), buffer_size=1024 * 1024))
    else:
        with path.open("rb") as fh:
            yield fh


def text_lines(fh: IO[bytes]) -> io.TextIOWrapper:
    """UTF-8 text view of a binary stream with universal newlines turned off (CSV needs that)."""
    return io.TextIOWrapper(fh, encoding="utf-8", errors="replace", newline="")


_HREF_RE = re.compile(r'href="([^"?#]+)"', re.IGNORECASE)


def listing_links(html: bytes, base: str) -> list[str]:
    """Absolute URLs of the file links in an Apache/nginx style directory listing."""
    out: list[str] = []
    for href in _HREF_RE.findall(html.decode("utf-8", "replace")):
        url = urljoin(base, unescape(href))
        if url.startswith(base) and url != base:
            out.append(url)
    return sorted(set(out))


_ROW_RE = re.compile(r'<a\s[^>]*href="([^"?#]+)"[^>]*>.*?</a>([^<\n]*)', re.IGNORECASE)


def listing_rows(html: bytes, base: str) -> dict[str, str]:
    """``{absolute URL: the row text after its link}`` of a directory listing (date and size as
    the server prints them, whitespace collapsed); used to tell when a listed file changed."""
    out: dict[str, str] = {}
    for href, rest in _ROW_RE.findall(html.decode("utf-8", "replace")):
        url = urljoin(base, unescape(href))
        if url.startswith(base) and url != base:
            out[url] = " ".join(rest.split())
    return out


class Bitset:
    """A growable set of non-negative integers, one bit each (PMIDs, docket ids: ~10 MB for 80M ids)."""

    def __init__(self) -> None:
        self._bits = bytearray()

    def add(self, n: int) -> None:
        byte = n >> 3
        if byte >= len(self._bits):
            self._bits.extend(b"\x00" * (byte + 1 - len(self._bits) + 1024 * 1024))
        self._bits[byte] |= 1 << (n & 7)

    def __contains__(self, n: int) -> bool:
        byte = n >> 3
        return byte < len(self._bits) and bool(self._bits[byte] & (1 << (n & 7)))


class Throttle:
    """At most one call per ``interval`` seconds (arXiv asks bulk harvesters for <= 1 request / 3 s)."""

    def __init__(
        self,
        interval: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.interval = interval
        self._clock = clock
        self._sleep = sleep
        self._last: float | None = None

    def wait(self) -> None:
        now = self._clock()
        if self._last is not None and now - self._last < self.interval:
            self._sleep(self.interval - (now - self._last))
        self._last = self._clock()
