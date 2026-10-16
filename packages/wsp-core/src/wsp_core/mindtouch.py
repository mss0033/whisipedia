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
"""

from __future__ import annotations

import html
import json
import re
from collections.abc import Mapping
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
    return f"content/page_content_{m[1]}.json"


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


def page_html(stub: str | bytes, content: bytes, ids: Mapping[str, str]) -> str | None:
    """The page a stub stands for: its title and the body from ``content`` (the page's content JSON),
    links and images made relative to the stub. ``None`` if ``content`` has no body."""
    try:
        data: object = json.loads(content)
    except ValueError:
        return None
    body = cast(Mapping[str, Any], data).get("htmlBody") if isinstance(data, Mapping) else None
    if not isinstance(body, str):
        return None
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
