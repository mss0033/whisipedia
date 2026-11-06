"""Listing pages of ZIM archives: indexes of other pages, never content.

Stack Exchange archives hold one page per tag and per page of every list ("questions/tagged/first-aid",
"questions_page=10", "users_page=3"); iFixit's hold a profile page per user ("User/1554889/einstein").
The Kiwix refine leaves them out (``wsp_adapters.kiwix``) and the search API drops any that an older
refine kept, with the same rule. ``access.skip_paths`` in the registry replaces the default per source.
A crawler's capture of a bot check (:func:`is_bot_wall`) is found by its text and left out the same way.
"""

from __future__ import annotations

import html as htmllib
import re
from collections.abc import Mapping, Sequence

DEFAULT_SKIP_PATHS: tuple[str, ...] = (
    r"(?:questions|users|tags|badges|unanswered)_page=\d+",
    r"questions/tagged/[^/]+",  # every question with a tag, page by page ("first-aid_page=3")
    # iFixit's user profiles: a name, a handle, a reputation and a join date. Famous names are popular
    # user names: "Einstein" led with one, and all ten hits for "Albert Einstien" were profiles, each
    # once per language edition (2026-11-05 deep dive). MediaWiki's "User:Name" pages do not match.
    r"User/\d+/[^/]+",
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


# A crawler's capture of a bot check in place of the page it asked for: the FAS Military Medicine
# archive holds the Special Forces Medical Handbook's part 1 as an AWS WAF page ("JavaScript is
# disabled. In order to continue, we need to verify that you're not a robot."), and Random landed on
# it every other visit (2026-11-05 deep dive). It sits at the page's own address, so no path rule
# finds it; its text does. The text is in <noscript>, which a page's readable text leaves out, so the
# page itself is read. Only a small page with a short text counts: an article about CAPTCHAs is long.
BOT_WALL_MAX_BYTES = 64 * 1024  # the FAS capture is 5 KB with the replay scripts warc2zim adds
BOT_WALL_MAX_TEXT = 1500
BOT_WALL = re.compile(
    r"verify (?:that )?you(?:'re| are) (?:not a robot|(?:a )?human)"  # AWS WAF, Cloudflare Turnstile
    r"|checking (?:if the site connection is secure|your browser before accessing)"  # Cloudflare
    r"|needs to review the security of your connection before proceeding"  # Cloudflare
    r"|enable javascript and cookies to continue"  # Cloudflare
    r"|attention required! \| cloudflare"
    r"|please complete the security check to access"  # Cloudflare (older)
    r"|press (?:&|and) hold to confirm you are a human"  # PerimeterX
    r"|please enable js and disable any ad blocker",  # DataDome
    re.IGNORECASE,
)
# Words every check above holds, looked for in the raw bytes first: most small pages are not read further.
_BOT_WALL_HINT = re.compile(
    rb"robot|human|connection is secure|your browser|security|cookies to continue|ad blocker", re.IGNORECASE
)
_CODE = re.compile(rb"<(script|style|template)\b[^>]*>.*?</\1\s*>", re.IGNORECASE | re.DOTALL)
_TAG = re.compile(rb"<[^>]*>")


def is_bot_wall(text: str) -> bool:
    """Whether ``text`` (a page's words, its ``<noscript>`` and ``<title>`` included) is only a bot
    check or challenge: short, and saying so."""
    if len(text) > 8 * BOT_WALL_MAX_TEXT:  # not joined: a long text is no check
        return False
    t = " ".join(text.replace("\u2019", "'").split())
    return len(t) <= BOT_WALL_MAX_TEXT and BOT_WALL.search(t) is not None


def is_bot_wall_page(page: bytes) -> bool:
    """A web page a crawler captured that is only a bot check (:func:`is_bot_wall`), not the page it
    asked for: not content; the Kiwix refine leaves it out and Random draws again. A page over
    :data:`BOT_WALL_MAX_BYTES` is not read."""
    if len(page) > BOT_WALL_MAX_BYTES or _BOT_WALL_HINT.search(page) is None:
        return False
    words = _TAG.sub(b" ", _CODE.sub(b" ", page)).decode("utf-8", "replace")
    return is_bot_wall(htmllib.unescape(words))


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
