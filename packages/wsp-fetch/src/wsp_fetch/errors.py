"""Typed fetch errors. Every failure that leaves no verified file raises a ``FetchError`` subclass.

``FetchError``, ``ChecksumMismatchError`` and ``NotFoundError`` are the port-level types from
:mod:`wsp_core.errors` (ADR-0005); the rest refine ``FetchError`` for this implementation.
"""

from __future__ import annotations

from wsp_core.errors import ChecksumMismatchError, FetchError, NotFoundError

__all__ = [
    "Aria2Error",
    "ChecksumMismatchError",
    "ConflictingChecksumsError",
    "FetchError",
    "MirrorsExhaustedError",
    "NotFoundError",
    "ResponseTooLargeError",
    "SizeMismatchError",
    "UnsupportedChecksumError",
]


class SizeMismatchError(FetchError):
    """Upstream size (declared or advertised by a mirror) disagrees with the bytes received."""


class ConflictingChecksumsError(FetchError):
    """Two sources (e.g. ``RemoteFile`` and its Metalink) publish different digests for one algorithm."""


class UnsupportedChecksumError(FetchError):
    """An upstream digest uses an algorithm we cannot compute, so the file could not be verified."""


class MirrorsExhaustedError(FetchError):
    """Every mirror failed or was excluded for at least one piece."""


class ResponseTooLargeError(FetchError):
    """``get_bytes`` body exceeded ``max_bytes``."""


class Aria2Error(FetchError):
    """``aria2c`` exited with a non-zero status that does not map to a more specific error."""

    def __init__(self, returncode: int, message: str) -> None:
        super().__init__(f"aria2c exited with status {returncode}: {message}")
        self.returncode = returncode
