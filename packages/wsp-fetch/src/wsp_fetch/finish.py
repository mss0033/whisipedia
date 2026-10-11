"""Atomic finish shared by all fetchers (PLAN §8.1 write path, ADR-0002).

``.partial`` is fsynced, its digests are checked against every upstream checksum, optionally
re-hashed from disk with the page cache dropped, and only then renamed onto ``dest``.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from wsp_core.model import Checksum, FetchResult
from wsp_fetch.errors import ChecksumMismatchError, SizeMismatchError
from wsp_fetch.hashing import check_digests, hash_file


@dataclass(frozen=True, slots=True)
class FetchReport(FetchResult):
    """The port's :class:`~wsp_core.model.FetchResult` plus every digest and transfer statistics."""

    digests: Mapping[str, str] = field(default_factory=dict[str, str])
    bytes_downloaded: int = 0
    bytes_resumed: int = 0
    seconds: float = 0.0
    per_mirror_bytes: Mapping[str, int] = field(default_factory=dict[str, int])
    # Where the bytes and digests came from (JSON-safe; see wsp_fetch.provenance, ADR-0009).
    provenance: Mapping[str, Any] = field(default_factory=dict[str, Any])
    # How the content was established (FetchConfig.unverified_policy): "verified" (an upstream
    # checksum or Metalink piece hashes matched), "corroborated" (no upstream digest; two full
    # downloads agreed) or "unverified" (no upstream digest; policy "allow").
    integrity: str = "verified"


def make_report(
    path: Path,
    size: int,
    digests: Mapping[str, str],
    verified: Sequence[Checksum],
    bytes_downloaded: int = 0,
    bytes_resumed: int = 0,
    seconds: float = 0.0,
    per_mirror_bytes: Mapping[str, int] | None = None,
    provenance: Mapping[str, Any] | None = None,
) -> FetchReport:
    """``digests`` must hold ``sha256`` and ``blake3`` (always computed, PLAN §8.1)."""
    return FetchReport(
        path=path,
        size=size,
        sha256=digests["sha256"],
        blake3=digests["blake3"],
        verified=tuple(verified),
        digests=dict(digests),
        bytes_downloaded=bytes_downloaded,
        bytes_resumed=bytes_resumed,
        seconds=seconds,
        per_mirror_bytes=dict(per_mirror_bytes or {}),
        provenance=dict(provenance or {}),
    )


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
