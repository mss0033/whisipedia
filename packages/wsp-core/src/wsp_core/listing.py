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
    # mwoffliner's resources: a page there describes a file and often holds only the wiki's menus.
    # Appropedia's 15, titled "null", led meaning-based search for "running fix" (2026-10-16).
    r"_assets_/.+",
    # A web archive's (zimit) copies of third-party frames: a robot check, a map, a player or a share
    # button the crawl could not capture. Each is a page with no text; the trawler found Cloudflare's
    # challenge frame and GitHub buttons listed as documents (2026-10-17 gap census).
    r"[^/]*\.fuzzy\.replayweb\.page/.*",
    r"(?:www\.)?google\.[a-z.]+/(?:recaptcha|maps(?:/d)?/embed)(?:[/?].*)?",
    r"challenges\.cloudflare\.com/.*",
    r"ghbtns\.com/.*",
    r"(?:embed\.spotify\.com|platform\.twitter\.com|player\.vimeo\.com|www\.facebook\.com/plugins)/.*",
    r"(?:www\.)?youtube(?:-nocookie)?\.com/embed/.*",
    r"docs\.google\.com/.+/pubchart.*",
    r"[^?]+/media/oembed\?.*",  # Drupal's embedded-video frame
)


# Web-archive ZIMs (warc2zim) keep a placeholder for each embedded player the crawl could not capture:
# ``youtube.fuzzy.replayweb.page/embed/<id>`` is a page that reads "YouTube" and holds no video. They
# are not content; feeds and listings leave them out and the reader says what they are (field exam
# 2026-10-14: the newest What's New item was one).
REPLAY_STUB = re.compile(
    r"(?:^|/)(?:"
    r"[^/]*\.fuzzy\.replayweb\.page/"  # warc2zim's placeholder for a player it could not capture
    r"|(?:www\.)?google\.[a-z.]+/maps(?:/d)?/embed"  # an embedded map frame (no map offline)
    r"|(?:www\.)?youtube(?:-nocookie)?\.com/embed/"
    r"|player\.vimeo\.com/video/"
    r")"
)


def is_replay_stub(native_id: str) -> bool:
    """A web archive's placeholder for an embedded player it did not capture."""
    return REPLAY_STUB.search(native_id) is not None


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


_DEFAULT = re.compile(skip_regex(DEFAULT_SKIP_PATHS))


def is_default_listing(native_id: str) -> bool:
    """A page the default rule leaves out (a bundle's native id is ``<book>/<entry path>``), for a
    consumer that does not read the registry: the embedder."""
    inner = native_id.partition("/")[2]
    return bool(_DEFAULT.fullmatch(native_id) or (inner and _DEFAULT.fullmatch(inner)))
