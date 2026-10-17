"""ZIMs made by openZIM's MindTouch scraper (the LibreTexts libraries): pages are an app, not HTML.

Each page entry ``index/page_<id>`` is a stub that only refreshes to the app
(``<meta http-equiv="refresh" content="0;URL='../index.html#/<page path>'">``); the page's body is the
``htmlBody`` of ``content/page_content_<id>.json``, and ``content/shared.json`` lists every page
(``id``, ``title``, ``path``). Read as they are, all LibreTexts pages were their title alone: nothing
to search and an empty page on screen (2026-10-16 field exam). The refiner and the API both turn a
stub into the page it stands for with these helpers; reading the archive is the caller's job.

In the body, links to other pages are app routes (``#/<page path>``) and images are relative to the
app (``content/...``): they are rewritten relative to the stub (``page_<id>``, ``../content/...``),
so the usual link rewriting of a ZIM page applies unchanged.

A page may include another page's body through a content-reuse widget (``<div
class="mt-contentreuse-widget" data-page="<path>">``) that the app fills in; read as stored, 12,021 of
the LibreTexts Mathematics library's 38,184 pages lacked those parts and 546 were empty (2026-10-17).
:func:`expand_reuse` puts the included page's body inside the widget (:class:`Pages` finds it; a book
renamed since, "Book:_Calculus_(OpenStax)" now "Calculus_(OpenStax)", by its chapter and page).
"""

from __future__ import annotations

import html
import json
import re
from collections.abc import Callable, Mapping
from typing import Any, cast
from urllib.parse import unquote

SHARED = "content/shared.json"
STUB_MAX_BYTES = 4096

_STUB_PATH = re.compile(r"index/page_(\d+)")
_APP_REFRESH = re.compile(
    r"""http-equiv=["']?refresh["']?[^>]*?url\s*=\s*'?(?:\./)?\.\./index\.html#/""", re.IGNORECASE
)
_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_URL_ATTR = re.compile(r"""(\s(?:href|src)\s*=\s*)(["'])(.*?)\2""", re.IGNORECASE | re.DOTALL)


def content_entry(path: str, page: str | bytes) -> str | None:
    """The entry holding the body of the stub ``page`` at entry ``path``, or ``None`` if it is not a
    MindTouch stub (any other page of any other ZIM)."""
    m = _STUB_PATH.fullmatch(path)
    if m is None or len(page) > STUB_MAX_BYTES:
        return None
    text = page if isinstance(page, str) else page.decode("utf-8", errors="replace")
    if _APP_REFRESH.search(text) is None:
        return None
    return content_path(m[1])


def page_ids(shared: bytes) -> dict[str, str]:
    """``{page path: page id}`` from ``content/shared.json`` (empty if it is unreadable)."""
    try:
        data: object = json.loads(shared)
    except ValueError:
        return {}
    pages = cast(Mapping[str, Any], data).get("pages") if isinstance(data, Mapping) else None
    out: dict[str, str] = {}
    for raw in cast(list[object], pages) if isinstance(pages, list) else []:
        if isinstance(raw, Mapping):
            page = cast(Mapping[str, Any], raw)
            if isinstance(page.get("path"), str) and page.get("id") is not None:
                out[page["path"]] = str(page["id"])
    return out


_WIDGET = re.compile(r"""<div\b[^>]*\bclass=["']mt-contentreuse-widget["'][^>]*>""", re.IGNORECASE)
_DATA_PAGE = re.compile(r"""\bdata-page=["']([^"']+)["']""", re.IGNORECASE)
_OLD_BOOK = re.compile(r"(^|/)(?:Book|Map|Text|Textbook):_")
REUSE_DEPTH = 2  # a reused page may itself reuse one more level


class Pages:
    """The page list of a MindTouch archive: an id by path, found the ways a reuse widget needs."""

    def __init__(self, ids: Mapping[str, str]) -> None:
        self.ids = dict(ids)
        self._decoded = {unquote(path): pid for path, pid in ids.items()}
        tails: dict[str, list[str]] = {}
        for path, pid in self._decoded.items():
            tails.setdefault(_tail(path), []).append(pid)
        self._tails = {t: pids[0] for t, pids in tails.items() if len(pids) == 1}

    def find(self, path: str) -> str | None:
        """The id of the page at ``path``: as written, without a book's old "Book:_" prefix, or by its
        last two path parts when exactly one page has them."""
        path = unquote(html.unescape(path))
        renamed = _OLD_BOOK.sub(r"\1", path)
        return (
            self._decoded.get(path)
            or self._decoded.get(renamed)
            or self._tails.get(_tail(path))
            or self._tails.get(_tail(renamed))
        )


def _tail(path: str) -> str:
    return "/".join(path.rstrip("/").split("/")[-2:])


def html_body(content: bytes) -> str | None:
    """``htmlBody`` of a page's content JSON, or ``None``."""
    try:
        data: object = json.loads(content)
    except ValueError:
        return None
    body = cast(Mapping[str, Any], data).get("htmlBody") if isinstance(data, Mapping) else None
    return body if isinstance(body, str) else None


def expand_reuse(
    body: str,
    pages: Pages,
    load: Callable[[str], bytes | None],
    *,
    depth: int = REUSE_DEPTH,
    seen: frozenset[str] = frozenset(),
) -> str:
    """``body`` with each content-reuse widget holding the page it names (``load``: a page id's
    content JSON, or ``None``). A widget whose page is not in the archive stays empty."""
    if depth <= 0 or "mt-contentreuse-widget" not in body:
        return body

    def fill(m: re.Match[str]) -> str:
        named = _DATA_PAGE.search(m[0])
        pid = pages.find(named[1]) if named else None
        if pid is None or pid in seen:
            return m[0]
        raw = load(pid)
        included = html_body(raw) if raw is not None else None
        if not included:
            return m[0]
        return m[0] + expand_reuse(included, pages, load, depth=depth - 1, seen=seen | {pid})

    return _WIDGET.sub(fill, body)


def content_path(page_id: str) -> str:
    return f"content/page_content_{page_id}.json"


APP_SHELL = "index.html"  # the app itself: an empty <div id="app"> without its script


def root_page(shared: bytes) -> str | None:
    """Entry of the library's home page (``index/page_<id>`` of ``rootPagePath``), or ``None``: the
    archive's main entry is the app shell, empty without the app ("Enter site" on every LibreTexts
    library opened a blank page, 2026-10-16 field exam)."""
    try:
        data: object = json.loads(shared)
    except ValueError:
        return None
    root = cast(Mapping[str, Any], data).get("rootPagePath") if isinstance(data, Mapping) else None
    if not isinstance(root, str):
        return None
    pid = page_ids(shared).get(root)
    return None if pid is None else f"index/page_{pid}"


def page_html(
    stub: str | bytes,
    content: bytes,
    ids: Mapping[str, str],
    *,
    pages: Pages | None = None,
    load: Callable[[str], bytes | None] | None = None,
) -> str | None:
    """The page a stub stands for: its title and the body from ``content`` (the page's content JSON),
    reused pages included (``pages`` and ``load``, :func:`expand_reuse`), links and images made
    relative to the stub. ``None`` if ``content`` has no body."""
    body = html_body(content)
    if body is None:
        return None
    if pages is not None and load is not None:
        body = expand_reuse(body, pages, load)
    text = stub if isinstance(stub, str) else stub.decode("utf-8", errors="replace")
    m = _TITLE.search(text)
    title = html.escape(html.unescape(m[1].strip())) if m else ""

    def relative(m: re.Match[str]) -> str:
        url = m[3]
        if url.startswith("#/"):
            path, hash_, fragment = url[2:].partition("#")
            pid = ids.get(unquote(html.unescape(path)))
            if pid is None:
                return m[0]
            url = f"page_{pid}{hash_}{fragment}"
        elif url.startswith("content/"):
            url = f"../{url}"
        else:
            return m[0]
        return f"{m[1]}{m[2]}{url}{m[2]}"

    heading = f"<h1>{title}</h1>" if title else ""
    body = _URL_ATTR.sub(relative, body)
    return f"<html><head><title>{title}</title></head><body>{heading}{body}</body></html>"
