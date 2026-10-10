"""Rewrite original document HTML (ZIM) into the offline web, keeping everything else byte-for-byte.

* ``<img src>`` relative to the page → ``/v1/assets/<source>/<generation>/<path>`` (same origin, so the
  Navigator's ``default-src 'self'`` allows it). ``srcset`` is dropped: it would bypass the rewrite.
* ``<a href>`` relative to the page → ``wsp://<source>/<native id>[#fragment]``.
* Absolute and scheme-relative URLs are left alone. Clients show them as "not in library".

This is not a sanitizer: the HTML stays untrusted and clients must sanitize it (the Navigator does).
"""

from __future__ import annotations

import html
import posixpath
from html.parser import HTMLParser
from urllib.parse import quote, unquote, urlsplit

from wsp_api.addresses import address_of


def _resolve(base_native_id: str, href: str) -> tuple[str, str] | None:
    """``(path, fragment)`` of a relative reference inside the same container, or None."""
    parts = urlsplit(href)
    if parts.scheme or parts.netloc or href.startswith("//"):
        return None
    if not parts.path:
        return None
    path = unquote(parts.path)
    joined = (
        path.lstrip("/") if path.startswith("/") else posixpath.join(posixpath.dirname(base_native_id), path)
    )
    norm = posixpath.normpath(joined)
    if norm in (".", "") or norm.startswith("../") or norm == "..":
        return None
    return norm, parts.fragment


def _encode_native(native_id: str) -> str:
    # Same rule as the Navigator's encodePath: keep addresses readable, escape what breaks parsing.
    return "".join(quote(c, safe="") if c in "%?#" or c.isspace() else c for c in native_id)


class _Rewriter(HTMLParser):
    def __init__(self, source_id: str, generation_id: str, native_id: str) -> None:
        super().__init__(convert_charrefs=False)
        self._source, self._gen, self._base = source_id, generation_id, native_id
        self.out: list[str] = []

    def _attrs(self, tag: str, attrs: list[tuple[str, str | None]]) -> list[tuple[str, str | None]] | None:
        changed = False
        new: list[tuple[str, str | None]] = []
        for name, value in attrs:
            if tag == "img" and name == "srcset":
                changed = True
                continue
            if value is not None and ((tag == "img" and name == "src") or (tag == "a" and name == "href")):
                target = _resolve(self._base, value.strip())
                if target is not None:
                    path, fragment = target
                    if tag == "img":
                        value = f"/v1/assets/{self._source}/{self._gen}/{quote(path, safe='/')}"
                    else:
                        value = address_of(self._source, _encode_native(path)) + (
                            f"#{fragment}" if fragment else ""
                        )
                    changed = True
            new.append((name, value))
        return new if changed else None

    def _emit_tag(self, tag: str, attrs: list[tuple[str, str | None]], closing: bool) -> None:
        new = self._attrs(tag, attrs) if tag in ("a", "img") else None
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
        self.out.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
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


def rewrite_html(text: str, *, source_id: str, generation_id: str, native_id: str) -> str:
    parser = _Rewriter(source_id, generation_id, native_id)
    parser.feed(text)
    parser.close()
    return "".join(parser.out)
