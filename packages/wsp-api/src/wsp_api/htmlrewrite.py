"""Rewrite original document HTML (ZIM) into the offline web, keeping everything else byte-for-byte.

* Links (``<a href>``, ``<area href>``) relative to the page → ``wsp://<source>/<native id>[#fragment]``;
  a link to a file (``.epub``, ``.pdf``, ``.png``, ``.mid``: :data:`FILES`) → its asset URL, which the
  asset route serves as the image or as a download (the Gutenberg cover page's EPUB link, and 66 of
  133 links the trawler found dead by 2026-10-17 — sheet music, PDFs, pictures — were files the archive
  holds, which the page route never serves).
* Everything else a page loads by URL (``<img src>``, ``srcset`` candidates, ``<source>``, SVG
  ``<image href>``/``xlink:href``, ``<link href>``, ``<script src>``, media, ``<object data>``,
  frames) relative to the page → ``/v1/assets/<source>/<generation>/<path>``. The asset route serves
  images only, so a script or stylesheet becomes an inert 404 instead of a URL that the browser would
  resolve against the Navigator's own origin.
* Root-relative URLs (``/-/static/x.png``) resolve inside the page's book: in a bundle source the
  container root is ``<book>/``, not the source root (audit #15). Given ``find``, a root-relative URL
  that is not there is also tried under each of ``site_roots``: a crawled site (zimit) keeps its host
  in every path, and one published below a folder (the OPSEC Bible at ``privacydefence.org/opsec/
  bibleprivacy/``) writes ``/assets/logo.png`` for a file under that folder.
* ``<base>`` is dropped: links are already resolved here, and a base would re-target them.
* Absolute URLs of sites the library holds (``outside``: Wikipedia, xkcd, an RFC) → the library's
  copy; a relative path into another site the crawl did not keep → the archive that copies it. Other
  absolute and scheme-relative URLs are left alone; clients show them as "not in library".
* ``style`` attributes and ``<style>`` elements are dropped: the Navigator's CSP refuses them anyway,
  and Chrome reports every one as a violation while merely parsing the page (E2E-1).

This is not a sanitizer: the HTML stays untrusted and clients must sanitize it (the Navigator does).
"""

from __future__ import annotations

import html
import posixpath
import re
from collections.abc import Callable, Sequence
from html.parser import HTMLParser
from urllib.parse import quote, unquote, urlsplit

from wsp_api.addresses import address_of

VERSION = 8  # bump when the output changes: rendered pages are cached by it

# Links to these are files (shown by the browser, or saved), not pages. By name, so rewriting needs no
# lookup per link; a page with such a name (rare) is then offered as a file the asset route refuses.
FILES = (
    *(".epub", ".pdf", ".mobi", ".azw3", ".djvu", ".doc", ".docx", ".odt", ".rtf", ".txt", ".csv", ".tsv"),
    *(".xls", ".xlsx", ".ods", ".ppt", ".pptx", ".odp", ".json", ".xml", ".rdf", ".rss", ".atom", ".kml"),
    *(".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif", ".bmp", ".svg", ".ico", ".tif", ".tiff"),
    *(".mp3", ".ogg", ".oga", ".opus", ".wav", ".flac", ".m4a", ".mid", ".midi", ".webm", ".mp4", ".m4v"),
    *(".ogv", ".mkv", ".mov", ".avi", ".zip", ".gz", ".tgz", ".bz2", ".xz", ".7z", ".tar"),
    *(".ly", ".ily", ".abc", ".mscz", ".musicxml", ".mxl", ".ps", ".eps", ".tex", ".bib"),
)

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


_NONE_HOST = "//:None/"
_ABSOLUTE = re.compile(r"\s*https?://", re.IGNORECASE)
Outside = Callable[..., str | None]  # (url, *, asset: bool) -> the library's URL for it, or None


def _resolve(base_native_id: str, href: str, root: str = "") -> tuple[str, str] | None:
    """``(path, fragment)`` of a relative reference inside the same container, or None.

    ``root`` is the container root inside the source (``""``, or ``"<book>/"`` in a bundle): where a
    root-relative path (``/x``) starts.
    """
    if href.startswith(_NONE_HOST):
        # The Stack Exchange ZIMs (sotoki) write some related-question links as "//:None/../../x":
        # a relative path behind a host that never got filled in (2026-10-14 field exam).
        href = href[len(_NONE_HOST) :]
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
    def __init__(
        self,
        source_id: str,
        generation_id: str,
        native_id: str,
        root: str,
        site_roots: Sequence[str],
        find: Callable[[str], str | None] | None,
        outside: Outside | None = None,
    ) -> None:
        super().__init__(convert_charrefs=False)
        self._source, self._gen, self._base, self._root = source_id, generation_id, native_id, root
        self._site_roots = [r for r in dict.fromkeys(site_roots) if r != root]
        self._find = find
        self._outside = outside
        self._seen: dict[tuple[str, bool], str | None] = {}  # (outside url, asset) -> library url
        self._found: dict[str, tuple[str, str] | None] = {}  # root-relative href -> where it is
        self.out: list[str] = []
        self._in_style = False

    def _target(self, value: str) -> tuple[str, str] | None:
        href = value.strip()
        target = _resolve(self._base, href, self._root)
        if self._find is None or not self._site_roots or not href.startswith("/") or href.startswith("//"):
            return target
        if href not in self._found:
            self._found[href] = self._elsewhere(href, target)
        return self._found[href] or target

    def _elsewhere(self, href: str, target: tuple[str, str] | None) -> tuple[str, str] | None:
        """Where a root-relative ``href`` that is not at ``target`` is under a site root, else None."""
        assert self._find is not None
        if target is not None and self._find(target[0]) is not None:
            return None
        for site in self._site_roots:
            there = _resolve(self._base, href, site)
            if there is not None and (path := self._find(there[0])) is not None:
                return path, there[1]
        return None

    def _asset_url(self, path: str) -> str:
        return f"/v1/assets/{self._source}/{self._gen}/{quote(path, safe='/')}"

    def _library_url(self, url: str, *, asset: bool) -> str | None:
        """The library's copy of an outside URL (``outside``), or None."""
        if self._outside is None:
            return None
        key = (url, asset)
        if key not in self._seen:
            self._seen[key] = self._outside(url, asset=asset)
        return self._seen[key]

    def _foreign(self, path: str, *, asset: bool) -> str | None:
        """A path into another website that this crawl did not keep (``imgs.xkcd.com/comics/x.png`` in a
        statistics book): the archive that copies that website, if this one does not hold the file."""
        host = path.split("/", 1)[0]
        if self._root or self._find is None or "." not in host:
            return None
        there = self._library_url(f"https://{path}", asset=asset)
        if there is None or there.startswith((f"/v1/assets/{self._source}/", f"wsp://{self._source}/")):
            return None  # this archive copies that site itself (no lookup for its own pages)
        return None if self._find(path) is not None else there

    def _asset(self, value: str) -> str | None:
        if _ABSOLUTE.match(value):
            return self._library_url(value.strip(), asset=True)
        target = self._target(value)
        if target is None:
            return None
        return self._foreign(target[0], asset=True) or self._asset_url(target[0])

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
                    target = self._target(value)
                    if target is not None:
                        path, fragment = target
                        is_file = path.lower().endswith(FILES)
                        rewritten = self._foreign(path, asset=is_file) or (
                            self._asset_url(path)
                            if is_file
                            else address_of(self._source, path) + (f"#{fragment}" if fragment else "")
                        )
                    elif _ABSOLUTE.match(value):
                        is_file = urlsplit(value.strip()).path.lower().endswith(FILES)
                        rewritten = self._library_url(value.strip(), asset=is_file)
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


def rewrite_html(
    text: str,
    *,
    source_id: str,
    generation_id: str,
    native_id: str,
    root: str = "",
    site_roots: Sequence[str] = (),
    find: Callable[[str], str | None] | None = None,
    outside: Outside | None = None,
) -> str:
    """``native_id``: the entry the page was served from (relative URLs resolve against it); ``root``:
    its container root inside the source (``"<book>/"`` in a bundle, else ``""``). ``site_roots`` (each
    ending in ``/``) and ``find`` (the native id of the entry at a path, slash forms tried, or None):
    where else a root-relative URL may be. ``outside(url, asset=...)``: the library's copy of an outside
    URL (``wsp_api.outside``): a link to a site the library holds opens there, a picture from it loads."""
    parser = _Rewriter(source_id, generation_id, native_id, root, site_roots, find, outside)
    parser.feed(text)
    parser.close()
    return "".join(parser.out)
