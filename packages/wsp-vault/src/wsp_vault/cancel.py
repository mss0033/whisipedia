"""Cooperative cancellation for long Warden work (scrub, parity) so the daemon stops promptly."""

from __future__ import annotations


class Interrupted(Exception):  # noqa: N818 - a control-flow signal, not an error
    """Raised inside long work when its owner asked it to stop (daemon shutdown)."""


class Paused(Interrupted):
    """Raised inside long work when the machine is busy (the owner is using the library): the work
    stops like on shutdown and nothing about the unfinished item is recorded."""


def never() -> bool:
    """The default ``should_stop``/``should_pause``: never."""
    return False
