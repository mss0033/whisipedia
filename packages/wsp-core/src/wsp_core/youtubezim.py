"""ZIMs made by openZIM's YouTube scraper (a channel's videos): pages are an app, not HTML.

Each page entry ``index/<slug>`` is a stub that only refreshes to the app
(``<meta http-equiv="refresh" content="0;URL='../index.html#/watch/<slug>?list=…'">``, or
``#/playlist/<slug>``); what the page says is in ``videos/<slug>.json`` (title, description, chapters,
thumbnail) or ``playlists/<slug>.json`` (its videos), and the channel's front page, ``index.html``,
is the app's empty ``<div id="app">`` with ``home_playlists.json`` beside it. Read as they are, every
page was blank on screen and its title alone in search (2026-10-16 field exam: "Enter site" on Quick
Guides for Medicine). The refiner and the API both turn a stub into a page with these helpers;
reading the archive is the caller's job.

The pages are text: what the video is about, its chapters and a thumbnail. The video itself is in the
archive (``videos/<id>/video.webm``) for the Screening Room, which does not play archives yet.
"""

from __future__ import annotations

import html
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any, cast

HOME = "home_playlists.json"
STUB_MAX_BYTES = 4096

_STUB_PATH = re.compile(r"index/[^/]+")
_ROUTE = re.compile(
    r"""http-equiv=["']?refresh["']?[^>]*?url\s*=\s*'?(?:\./)?\.\./index\.html#/(watch|playlist)/([^?'"#&]+)""",
    re.IGNORECASE,
)
_DURATION = re.compile(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?")
NOT_PLAYED = (
    "The video is in the library's copy of this channel; the Navigator does not play videos from "
    "archives yet."
)


def content_entry(path: str, page: str | bytes) -> str | None:
    """The entry holding what the stub ``page`` at entry ``path`` stands for, or ``None`` if it is not
    a YouTube-scraper stub (any other page of any other ZIM)."""
    if _STUB_PATH.fullmatch(path) is None or len(page) > STUB_MAX_BYTES:
        return None
    text = page if isinstance(page, str) else page.decode("utf-8", errors="replace")
    m = _ROUTE.search(text)
    if m is None:
        return None
    return f"{'videos' if m[1].lower() == 'watch' else 'playlists'}/{m[2]}.json"


def _load(raw: bytes) -> Mapping[str, Any] | None:
    try:
        data: object = json.loads(raw)
    except ValueError:
        return None
    return cast(Mapping[str, Any], data) if isinstance(data, Mapping) else None


def duration(iso: object) -> str:
    """``PT1H2M3S`` -> ``1:02:03``, ``PT9M7S`` -> ``9:07``; ``""`` if it is not one."""
    m = _DURATION.fullmatch(iso) if isinstance(iso, str) else None
    if m is None or not any(m.groups()):
        return ""
    h, mi, s = (int(x or 0) for x in m.groups())
    return f"{h}:{mi:02d}:{s:02d}" if h else f"{mi}:{s:02d}"


def _clock(seconds: object) -> str:
    n = int(seconds) if isinstance(seconds, (int, float)) else 0
    return f"{n // 3600}:{n // 60 % 60:02d}:{n % 60:02d}" if n >= 3600 else f"{n // 60}:{n % 60:02d}"


def _text(s: object) -> str:
    """Plain text as paragraphs, line breaks kept."""
    if not isinstance(s, str) or not s.strip():
        return ""
    paras = (p.strip() for p in s.replace("\r", "").split("\n\n"))
    return "".join(f"<p>{html.escape(p).replace(chr(10), '<br>')}</p>" for p in paras if p)


def _page(title: str, body: str) -> str:
    t = html.escape(title)
    return f"<html><head><title>{t}</title></head><body><h1>{t}</h1>{body}</body></html>"


def _list(videos: Sequence[object], up: str) -> str:
    """Links to videos (``slug``, ``title``, ``duration``, ``thumbnailPath``) from a page ``up`` deep."""
    items: list[str] = []
    for raw in videos:
        if not isinstance(raw, Mapping):
            continue
        v = cast(Mapping[str, Any], raw)
        slug, title = v.get("slug"), v.get("title")
        if not isinstance(slug, str) or not isinstance(title, str):
            continue
        thumb = v.get("thumbnailPath")
        img = f'<img src="{html.escape(up + thumb)}" alt="" width="160"> ' if isinstance(thumb, str) else ""
        took = duration(v.get("duration"))
        items.append(
            f'<li>{img}<a href="{html.escape(up + "index/" + slug)}">{html.escape(title)}</a>'
            + (f" ({took})" if took else "")
            + "</li>"
        )
    return f"<ul>{''.join(items)}</ul>" if items else ""


def page_html(content: bytes) -> str | None:
    """The page of a video or a playlist from its JSON (``content_entry``), links and images relative
    to the stub (``index/<slug>``). ``None`` if the JSON is neither."""
    data = _load(content)
    if data is None or not isinstance(data.get("title"), str):
        return None
    title = cast(str, data["title"])
    if isinstance(data.get("videos"), list):  # a playlist
        return _page(title, _text(data.get("description")) + _list(cast(list[object], data["videos"]), "../"))
    parts: list[str] = []
    thumb = data.get("thumbnailPath")
    if isinstance(thumb, str):
        parts.append(f'<p><img src="../{html.escape(thumb)}" alt="{html.escape(title)}"></p>')
    facts = [x for x in (str(data.get("publicationDate") or "")[:10], duration(data.get("duration"))) if x]
    if facts:
        parts.append(f"<p>{' · '.join(html.escape(x) for x in facts)}</p>")
    parts.append(f"<p><i>{html.escape(NOT_PLAYED)}</i></p>")
    parts.append(_text(data.get("description")))
    chapters = data.get("chapterList")
    named = [
        cast(Mapping[str, Any], c)
        for c in (cast(list[object], chapters) if isinstance(chapters, list) else [])
        if isinstance(c, Mapping)
        and isinstance(cast(Mapping[str, Any], c).get("title"), str)
        and not str(cast(Mapping[str, Any], c)["title"]).startswith("<Untitled")
    ]
    if named:
        rows = "".join(f"<li>{_clock(c.get('startTime'))} {html.escape(str(c['title']))}</li>" for c in named)
        parts.append(f"<h2>Chapters</h2><ol>{rows}</ol>")
    return _page(title, "".join(parts))


def home_html(home: bytes, channel: bytes | None) -> str | None:
    """The channel's front page (``index.html``): its playlists and their videos, from
    ``home_playlists.json`` (and the name and description from ``channel.json``)."""
    data = _load(home)
    playlists = data.get("playlists") if data is not None else None
    if not isinstance(playlists, list):
        return None
    about = _load(channel) if channel is not None else None
    title = str((about or {}).get("title") or "Videos")
    parts = [_text((about or {}).get("channelDescription") or (about or {}).get("description"))]
    for raw in cast(list[object], playlists):
        if not isinstance(raw, Mapping):
            continue
        p = cast(Mapping[str, Any], raw)
        name, slug = p.get("title"), p.get("slug")
        if not isinstance(name, str) or not isinstance(slug, str):
            continue
        videos = p.get("videos")
        parts.append(f'<h2><a href="index/{html.escape(slug)}">{html.escape(name)}</a></h2>')
        parts.append(_list(cast(list[object], videos) if isinstance(videos, list) else [], ""))
    return _page(title, "".join(parts))
