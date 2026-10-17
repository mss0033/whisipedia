"""ZIMs made by openZIM's freeCodeCamp scraper (fcc2zim: Rosetta Code, Project Euler, the courses).

Like the MindTouch archives (:mod:`wsp_core.mindtouch`) they are an app: each challenge page
``index/<course>/<block>/<id>.html`` only refreshes to ``index.html#<course>/<block>/<id>``, and the
challenge itself is ``content/curriculum/<course>/<block>/<id>.md``: front matter (``title``), then
sections ``# --description--``, ``# --instructions--``, ``# --hints--`` (tests), ``# --seed--`` and
``# --solutions--``. Read as stored, the 160 Rosetta Code tasks were their titles alone
(2026-10-17). The refiner and the API turn a stub into a page with these helpers, and
:func:`home_html` lists every challenge from ``content/curriculum/index.json`` for the app shell.
"""

from __future__ import annotations

import html
import json
import re
from collections.abc import Mapping
from typing import Any, cast
from urllib.parse import quote

INDEX = "content/curriculum/index.json"
STUB_MAX_BYTES = 4096
_STUB_PATH = re.compile(r"index/(.+)\.html")
_REFRESH = re.compile(
    r"""http-equiv=["']?refresh["']?[^>]*?url\s*=\s*'?(?:\./)?(?:\.\./)*index\.html#([^'"\s>]+)""", re.I
)
_SECTION = re.compile(r"^#\s+--([a-z-]+)--\s*$", re.MULTILINE)
_SHOWN = (
    ("description", ""),
    ("instructions", "Instructions"),
    ("hints", "Tests"),
    ("solutions", "A solution"),
)


def content_entry(path: str, page: str | bytes) -> str | None:
    """The Markdown entry of the challenge a stub at ``path`` stands for, or ``None``."""
    m = _STUB_PATH.fullmatch(path)
    if m is None or len(page) > STUB_MAX_BYTES:
        return None
    text = page if isinstance(page, str) else page.decode("utf-8", errors="replace")
    r = _REFRESH.search(text)
    if r is None or r[1] != m[1]:
        return None
    return f"content/curriculum/{m[1]}.md"


def _front_matter(md: str) -> tuple[dict[str, str], str]:
    if not md.startswith("---"):
        return {}, md
    end = md.find("\n---", 3)
    if end < 0:
        return {}, md
    meta: dict[str, str] = {}
    for line in md[3:end].splitlines():
        key, sep, value = line.partition(":")
        if sep:
            meta[key.strip()] = value.strip()
    return meta, md[end + 4 :]


def _sections(body: str) -> dict[str, str]:
    marks = list(_SECTION.finditer(body))
    return {
        m[1]: body[m.end() : (marks[i + 1].start() if i + 1 < len(marks) else len(body))]
        for i, m in enumerate(marks)
    }


def _inline(text: str) -> str:
    """Markdown inline marks on text that may already hold HTML tags (kept as they are)."""
    parts = re.split(r"(`[^`]+`)", text)
    out: list[str] = []
    for part in parts:
        if part.startswith("`") and part.endswith("`") and len(part) > 1:
            out.append(f"<code>{html.escape(part[1:-1])}</code>")
        else:
            part = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", part)
            out.append(re.sub(r"(?<![*\w])\*(?!\s)(.+?)(?<!\s)\*(?![*\w])", r"<em>\1</em>", part))
    return "".join(out)


def markdown_html(md: str) -> str:
    """The Markdown a challenge is written in, as HTML: fenced code, ``##`` headings, paragraphs."""
    out: list[str] = []
    para: list[str] = []
    lines = md.splitlines()
    i = 0

    def flush() -> None:
        if para:
            joined = " ".join(p.strip() for p in para)
            out.append(joined if joined.startswith("<") else f"<p>{_inline(joined)}</p>")
            para.clear()

    while i < len(lines):
        line = lines[i]
        if line.strip().startswith("```"):
            flush()
            code: list[str] = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code.append(lines[i])
                i += 1
            out.append(f"<pre><code>{html.escape(chr(10).join(code))}</code></pre>")
        elif m := re.match(r"^(#{2,6})\s+(.*)$", line):
            flush()
            label = m[2].strip().strip("-").replace("-", " ").strip()
            out.append(f"<h3>{html.escape(label.capitalize())}</h3>")
        elif not line.strip():
            flush()
        else:
            para.append(line)
        i += 1
    flush()
    return "".join(out)


def page_html(content: bytes) -> str | None:
    """A challenge's page: its title, description, instructions, tests and a solution."""
    md = content.decode("utf-8", errors="replace")
    meta, body = _front_matter(md)
    sections = _sections(body)
    if not sections:
        return None
    title = html.escape(meta.get("title", "").strip("'\""))
    parts = [f"<h1>{title}</h1>"] if title else []
    for key, heading in _SHOWN:
        text = sections.get(key, "").strip()
        if not text:
            continue
        if heading:
            parts.append(f"<h2>{heading}</h2>")
        parts.append(markdown_html(text))
    return f"<html><head><title>{title}</title></head><body>{''.join(parts)}</body></html>"


def home_html(name: str, index: bytes) -> str | None:
    """Every challenge of the archive, by course and block (``content/curriculum/index.json``),
    linked as the stubs are named (``index/<course>/<block>/<id>.html``, relative to the app shell)."""
    try:
        data: object = json.loads(index)
    except ValueError:
        return None
    if not isinstance(data, Mapping):
        return None
    parts = [f"<h1>{html.escape(name)}</h1>"]
    total = 0
    for course, blocks in cast(Mapping[str, Any], data).items():
        for block, challenges in (
            cast(Mapping[str, Any], blocks).items() if isinstance(blocks, Mapping) else ()
        ):
            rows: list[str] = []
            for c in cast(list[object], challenges) if isinstance(challenges, list) else []:
                if not isinstance(c, Mapping):
                    continue
                ch = cast(Mapping[str, Any], c)
                slug, title = str(ch.get("slug", "")), str(ch.get("title", ""))
                if slug and "/" not in slug:
                    href = f"index/{quote(course)}/{quote(str(block))}/{quote(slug)}.html"
                    rows.append(f'<li><a href="{href}">{html.escape(title or slug)}</a></li>')
            if rows:
                total += len(rows)
                label = str(block).replace("-", " ").capitalize()
                parts.append(f"<h2>{html.escape(label)}</h2><ul>{''.join(rows)}</ul>")
    if not total:
        return None
    parts.insert(1, f"<p>{total} challenge{'s' if total != 1 else ''}.</p>")
    return f"<html><head><title>{html.escape(name)}</title></head><body>{''.join(parts)}</body></html>"
