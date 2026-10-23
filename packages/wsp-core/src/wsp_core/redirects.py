"""Where a download may be redirected: a guard the caller sets, the fetcher checks on every hop.

2026-10-23: BODC's GEBCO link redirected to CEDA, whose robots.txt disallows every agent, and 7.5 GB
came from there; the collection's origin and robots checks saw only the URL it was asked for, and the
hops after the download, which had already happened. With :func:`redirect_guard` set around a
download, a fetcher (``wsp_fetch.http.HttpFetcher``) calls the check on each redirect target before
following it, in every thread of that download, and the check raises
:class:`wsp_core.errors.RedirectRefusedError` to refuse.
"""

from __future__ import annotations

import contextvars
from collections.abc import Callable, Generator
from contextlib import contextmanager

_GUARD: contextvars.ContextVar[Callable[[str], None] | None] = contextvars.ContextVar(
    "wsp_redirect_guard", default=None
)


@contextmanager
def redirect_guard(check: Callable[[str], None]) -> Generator[None]:
    """Downloads started in this block call ``check(url)`` on every redirect target first; a guard
    already set (an outer caller's) is checked too, before this one."""
    outer = _GUARD.get()

    def both(url: str) -> None:
        if outer is not None:
            outer(url)
        check(url)

    token = _GUARD.set(both)
    try:
        yield
    finally:
        _GUARD.reset(token)


@contextmanager
def unguarded() -> Generator[None]:
    """No guard in this block: a guard's own requests (a host's robots.txt) are not guarded by it."""
    token = _GUARD.set(None)
    try:
        yield
    finally:
        _GUARD.reset(token)


def current_guard() -> Callable[[str], None] | None:
    """The guard of the running download, if its caller set one."""
    return _GUARD.get()
