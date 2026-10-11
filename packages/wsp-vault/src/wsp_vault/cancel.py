"""Cooperative cancellation for long Warden work (scrub, parity) so the daemon stops promptly."""

from __future__ import annotations


class Interrupted(Exception):  # noqa: N818 - a control-flow signal, not an error
    """Raised inside long work when its owner asked it to stop (daemon shutdown)."""


def never() -> bool:
    """The default ``should_stop``/``should_pause``: never."""
    return False
