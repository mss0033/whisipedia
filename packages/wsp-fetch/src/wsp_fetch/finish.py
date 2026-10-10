"""Atomic finish shared by all fetchers (PLAN §8.1 write path, ADR-0002).

``.partial`` is fsynced, its digests are checked against every upstream checksum, optionally
re-hashed from disk with the page cache dropped, and only then renamed onto ``dest``.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from wsp_core.model import Checksum, Verdict, VerdictStatus
from wsp_fetch.errors import ChecksumMismatchError, SizeMismatchError
from wsp_fetch.hashing import check_digests, hash_file


@dataclass(frozen=True, slots=True)
class FetchResult:
    """What a download produced. ``digests`` always holds ``sha256`` and ``blake3``."""

    path: Path
    size: int
    digests: Mapping[str, str]
    verified: tuple[Checksum, ...]  # upstream digests that matched
    bytes_downloaded: int = 0
    bytes_resumed: int = 0
    seconds: float = 0.0
    per_mirror_bytes: Mapping[str, int] = field(default_factory=dict[str, int])

    @property
    def sha256(self) -> str:
        return self.digests["sha256"]

    @property
    def blake3(self) -> str:
        return self.digests["blake3"]

    @property
    def verdict(self) -> Verdict:
        if not self.verified:
            return Verdict(VerdictStatus.UNVERIFIABLE, "upstream publishes no checksum")
        return Verdict(VerdictStatus.OK, ", ".join(c.algorithm for c in self.verified))


def fsync_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass  # some filesystems (SMB) reject directory fsync; the rename is still atomic
    finally:
        os.close(fd)


def drop_page_cache(path: Path) -> None:
    if not hasattr(os, "posix_fadvise"):
        return
    fd = os.open(path, os.O_RDONLY)
    try:
        os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
    finally:
        os.close(fd)


def finalize(
    url: str,
    partial: Path,
    dest: Path,
    *,
    expected_size: int | None,
    checksums: Sequence[Checksum],
    digests: Mapping[str, str],
    readback: bool,
) -> None:
    """Verify ``partial`` (already fsynced) and atomically rename it to ``dest``.

    On any mismatch the partial file is removed (its bytes are known bad) and a typed error raised.
    """
    actual = partial.stat().st_size
    try:
        if expected_size is not None and actual != expected_size:
            raise SizeMismatchError(f"{url}: expected {expected_size} bytes, got {actual}")
        check_digests(url, checksums, dict(digests))
        if readback:
            drop_page_cache(partial)
            again = hash_file(partial, digests.keys())
            for algo, value in digests.items():
                if again.get(algo) != value:
                    raise ChecksumMismatchError(url, f"{algo} (read-back)", value, again.get(algo, ""))
    except (SizeMismatchError, ChecksumMismatchError):
        partial.unlink(missing_ok=True)
        raise
    os.replace(partial, dest)
    fsync_dir(dest.parent)
