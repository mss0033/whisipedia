"""Typed errors that cross port boundaries (port revision v0.2, ADR-0005).

Implementations may raise subclasses with more detail; callers catch these base types so that a
replacement implementation never changes error handling upstream.
"""

from __future__ import annotations


class FetchError(Exception):
    """A download or metadata request failed and left no verified file behind (``Fetcher`` port)."""


class RedirectRefusedError(FetchError):
    """A redirect leads somewhere the caller's guard (:func:`wsp_core.redirects.redirect_guard`)
    refuses: another origin, or a host whose robots.txt disallows us. Raised before the redirect is
    followed, so nothing of the target is downloaded; not retried."""


class NotFoundError(FetchError):
    """Upstream answered 404 / 410: the resource does not exist (not a transient failure)."""

    def __init__(self, url: str, detail: str = "") -> None:
        super().__init__(f"not found: {url}" + (f" ({detail})" if detail else ""))
        self.url = url


class ChecksumMismatchError(FetchError):
    """Downloaded bytes do not match an upstream digest (PLAN §8.1). Nothing was renamed into place."""

    def __init__(self, url: str, algorithm: str, expected: str, actual: str) -> None:
        super().__init__(f"{algorithm} mismatch for {url}: expected {expected}, got {actual}")
        self.url = url
        self.algorithm = algorithm
        self.expected = expected
        self.actual = actual


class InvalidCursorError(ValueError):
    """A change-feed cursor this feed did not issue (foreign, malformed, or ahead of the feed)."""
