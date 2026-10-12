"""Listing pages of ZIM archives: indexes of other pages, never content.

Stack Exchange archives hold one page per tag and per page of every list ("questions/tagged/first-aid",
"questions_page=10", "users_page=3"). The Kiwix refine leaves them out (``wsp_adapters.kiwix``) and the
search API drops any that an older refine kept, with the same rule. ``access.skip_paths`` in the
registry replaces the default per source.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence

DEFAULT_SKIP_PATHS: tuple[str, ...] = (
    r"(?:questions|users|tags|badges|unanswered)_page=\d+",
    r"questions/tagged/[^/]+",  # every question with a tag, page by page ("first-aid_page=3")
)


def skip_paths(access: Mapping[str, object]) -> tuple[str, ...]:
    """The source's listing-path patterns (full match against an entry path inside its book)."""
    raw = access.get("skip_paths")
    if raw is None:
        return DEFAULT_SKIP_PATHS
    if isinstance(raw, str):
        return (raw,)
    if isinstance(raw, Sequence):
        return tuple(str(x) for x in raw)  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
    raise ValueError(f"skip_paths must be a list of regular expressions, not {type(raw).__name__}")


def skip_regex(patterns: Sequence[str]) -> str:
    """One regex (full match) for ``patterns``; raises :class:`re.error` on a bad one."""
    for p in patterns:
        re.compile(p)
    return "|".join(f"(?:{p})" for p in patterns)
