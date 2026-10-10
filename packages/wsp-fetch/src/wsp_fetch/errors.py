"""Typed fetch errors. Every failure that leaves no verified file raises a ``FetchError`` subclass."""

from __future__ import annotations


class FetchError(Exception):
    """Base class for all download failures."""


class ChecksumMismatchError(FetchError):
    """The assembled file does not match an upstream digest (PLAN §8.1). Nothing is renamed into place."""

    def __init__(self, url: str, algorithm: str, expected: str, actual: str) -> None:
        super().__init__(f"{algorithm} mismatch for {url}: expected {expected}, got {actual}")
        self.url = url
        self.algorithm = algorithm
        self.expected = expected
        self.actual = actual


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
