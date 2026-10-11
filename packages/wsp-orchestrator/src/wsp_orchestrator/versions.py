"""Release version and generation id ordering (audit 2026-10-11, M8).

Upstream versions are compared *naturally*: runs of digits as numbers, the rest as text, so
``pubmed26-u999 < pubmed26-u1000`` and ``2026-9 < 2026-10`` (plain string comparison gets both
wrong). Dates in ISO form (``2026-10-01``) order the same either way.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

_TOKEN = re.compile(r"(\d+)")


def version_key(version: str) -> tuple[tuple[int, int, str], ...]:
    """Sort key: digit runs compare as integers (then by length, so ``01`` != ``1``), text as text."""
    out: list[tuple[int, int, str]] = []
    for i, part in enumerate(_TOKEN.split(version)):
        if not part:
            continue
        if i % 2:  # a digit run
            out.append((1, int(part), part))
        else:
            out.append((0, 0, part.lower()))
    return tuple(out)


def newest(versions: Iterable[str]) -> str | None:
    """The newest version by :func:`version_key` (``None`` for none)."""
    return max(versions, key=version_key, default=None)


def is_newer(a: str, b: str) -> bool:
    """True if ``a`` is strictly newer than ``b``."""
    return version_key(a) > version_key(b)
