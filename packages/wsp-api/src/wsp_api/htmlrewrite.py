"""Rewrite original document HTML (ZIM) into the offline web, keeping everything else byte-for-byte.

* Links (``<a href>``, ``<area href>``) relative to the page → ``wsp://<source>/<native id>[#fragment]``.
* Everything else a page loads by URL (``<img src>``, ``srcset`` candidates, ``<source>``, SVG
  ``<image href>``/``xlink:href``, ``<link href>``, ``<script src>``, media, ``<object data>``,
  frames) relative to the page → ``/v1/assets/<source>/<generation>/<path>``. The asset route serves
  images only, so a script or stylesheet becomes an inert 404 instead of a URL that the browser would
  resolve against the Navigator's own origin.
* Root-relative URLs (``/-/static/x.png``) resolve inside the page's book: in a bundle source the
  container root is ``<book>/``, not the source root (audit #15).
* ``<base>`` is dropped: links are already resolved here, and a base would re-target them.
* Absolute and scheme-relative URLs are left alone. Clients show them as "not in library".
* ``style`` attributes and ``<style>`` elements are dropped: the Navigator's CSP refuses them anyway,
  and Chrome reports every one as a violation while merely parsing the page (E2E-1).

This is not a sanitizer: the HTML stays untrusted and clients must sanitize it (the Navigator does).
"""

from __future__ import annotations

import html
import posixpath
import re
from html.parser import HTMLParser
from urllib.parse import quote, unquote, urlsplit

from wsp_api.addresses import address_of

VERSION = 3  # bump when the output changes: rendered pages are cached by it

_LINKS = frozenset({("a", "href"), ("area", "href")})
_ASSETS = frozenset(
    {
        ("img", "src"),
        ("source", "src"),
        ("image", "href"),
        ("image", "xlink:href"),
        ("link", "href"),
        ("script", "src"),
        ("video", "src"),
        ("video", "poster"),
        ("audio", "src"),
        ("track", "src"),
        ("embed", "src"),
        ("iframe", "src"),
        ("frame", "src"),
        ("object", "data"),
        ("input", "src"),
    }
)
_SRCSET = frozenset({("img", "srcset"), ("source", "srcset")})
_TAGS = frozenset(tag for tag, _ in _LINKS | _ASSETS | _SRCSET)
_CANDIDATE = re.compile(r"\s*([^\s,][^\s]*?)(\s+[^,]*)?\s*(?:,|$)")


def _resolve(base_native_id: str, href: str, root: str = "") -> tuple[str, str] | None:
    """``(path, fragment)`` of a relative reference inside the same container, or None.

    ``root`` is the container root inside the source (``""``, or ``"<book>/"`` in a bundle): where a
    root-relative path (``/x``) starts.
    """
    parts = urlsplit(href)
    if parts.scheme or parts.netloc or href.startswith("//"):
        return None
    if not parts.path:
        return None
    path = unquote(parts.path)
    joined = (
        root + path.lstrip("/")
        if path.startswith("/")
        else posixpath.join(posixpath.dirname(base_native_id), path)
    )
    norm = posixpath.normpath(joined)
    if norm in (".", "") or norm.startswith("../") or norm == "..":
        return None
    return norm, parts.fragment


class _Rewriter(HTMLParser):
    def __init__(self, source_id: str, generation_id: str, native_id: str, root: str) -> None:
        super().__init__(convert_charrefs=False)
        self._source, self._gen, self._base, self._root = source_id, generation_id, native_id, root
        self.out: list[str] = []
        self._in_style = False

    def _asset(self, value: str) -> str | None:
        target = _resolve(self._base, value.strip(), self._root)
        if target is None:
            return None
        return f"/v1/assets/{self._source}/{self._gen}/{quote(target[0], safe='/')}"

    def _srcset(self, value: str) -> str:
        out: list[str] = []
        for m in _CANDIDATE.finditer(value):
            url, descriptor = m.group(1), (m.group(2) or "").strip()
            if not url:
                continue
            new = self._asset(url) or url
            out.append(f"{new} {descriptor}" if descriptor else new)
        return ", ".join(out)

    def _attrs(self, tag: str, attrs: list[tuple[str, str | None]]) -> list[tuple[str, str | None]] | None:
        changed = False
        new: list[tuple[str, str | None]] = []
        for name, value in attrs:
            if value is not None:
                key = (tag, name)
                rewritten: str | None = None
                if key in _LINKS:
                    target = _resolve(self._base, value.strip(), self._root)
                    if target is not None:
                        path, fragment = target
                        rewritten = address_of(self._source, path) + (f"#{fragment}" if fragment else "")
                elif key in _ASSETS:
                    rewritten = self._asset(value)
                elif key in _SRCSET:
                    rewritten = self._srcset(value)
                if rewritten is not None and rewritten != value:
                    value, changed = rewritten, True
            new.append((name, value))
        return new if changed else None

    def _emit_tag(self, tag: str, attrs: list[tuple[str, str | None]], closing: bool) -> None:
        if tag == "style":
            self._in_style = not closing
            return
        if tag == "base":
            return
        new = self._attrs(tag, attrs) if tag in _TAGS else None
        if any(name == "style" for name, _ in new or attrs):
            new = [(name, value) for name, value in new or attrs if name != "style"]
        if new is None:
            self.out.append(self.get_starttag_text() or "")
            return
        rendered = "".join(f" {k}" if v is None else f' {k}="{html.escape(v, quote=True)}"' for k, v in new)
        self.out.append(f"<{tag}{rendered}{' /' if closing else ''}>")

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._emit_tag(tag, attrs, False)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._emit_tag(tag, attrs, True)

    def handle_endtag(self, tag: str) -> None:
        if tag == "style":
            self._in_style = False
            return
        if tag == "base":
            return
        self.out.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        if not self._in_style:
            self.out.append(data)

    def handle_entityref(self, name: str) -> None:
        self.out.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        self.out.append(f"&#{name};")

    def handle_comment(self, data: str) -> None:
        self.out.append(f"<!--{data}-->")

    def handle_decl(self, decl: str) -> None:
        self.out.append(f"<!{decl}>")

    def handle_pi(self, data: str) -> None:
        self.out.append(f"<?{data}>")

    def unknown_decl(self, data: str) -> None:
        self.out.append(f"<![{data}]>")


def rewrite_html(text: str, *, source_id: str, generation_id: str, native_id: str, root: str = "") -> str:
    """``native_id``: the entry the page was served from (relative URLs resolve against it); ``root``:
    its container root inside the source (``"<book>/"`` in a bundle, else ``""``)."""
    parser = _Rewriter(source_id, generation_id, native_id, root)
    parser.feed(text)
    parser.close()
    return "".join(parser.out)
