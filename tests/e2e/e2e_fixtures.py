"""Tiny synthetic upstream files for the end-to-end test: a ZIM and a MediaWiki multistream dump."""

from __future__ import annotations

import bz2
import ipaddress
import socket
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

import pytest
from libzim.writer import Creator, Hint, Item, StringProvider  # pyright: ignore[reportMissingTypeStubs]


class _HtmlItem(Item):
    def __init__(self, path: str, title: str, content: str) -> None:
        super().__init__()
        self._path, self._title, self._content = path, title, content

    def get_path(self) -> str:
        return self._path

    def get_title(self) -> str:
        return self._title

    def get_mimetype(self) -> str:
        return "text/html"

    def get_contentprovider(self) -> StringProvider:
        return StringProvider(self._content)

    def get_hints(self) -> dict[Hint, int]:
        return {Hint.FRONT_ARTICLE: True}


def build_zim(
    path: Path,
    *,
    name: str,
    articles: Sequence[tuple[str, str, str]],
    redirects: Sequence[tuple[str, str, str]] = (),
    date: str = "2026-08-01",
) -> Path:
    """``articles``: (path, title, html); ``redirects``: (path, title, target path)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with Creator(path).config_indexing(False, "eng").config_verbose(False) as creator:
        creator.set_mainpath(articles[0][0])
        for item_path, title, html in articles:
            creator.add_item(_HtmlItem(item_path, title, html))
        for item_path, title, target in redirects:
            creator.add_redirection(item_path, title, target, {Hint.FRONT_ARTICLE: True})
        for key, value in (("Name", name), ("Language", "eng"), ("Date", date), ("Title", name)):
            creator.add_metadata(key, value)
    return path


def _page_xml(page_id: int, title: str, text: str, redirect: str | None) -> str:
    red = f'    <redirect title="{escape(redirect)}" />\n' if redirect else ""
    return (
        f"  <page>\n    <title>{escape(title)}</title>\n    <ns>0</ns>\n    <id>{page_id}</id>\n{red}"
        f"    <revision>\n      <id>{1000 + page_id}</id>\n"
        "      <timestamp>2026-09-30T12:00:00Z</timestamp>\n"
        f'      <model>wikitext</model>\n      <text xml:space="preserve">{escape(text)}</text>\n'
        "    </revision>\n  </page>\n"
    )


def build_multistream(
    directory: Path, wiki: str, date: str, pages: Sequence[tuple[str, str, str | None]]
) -> tuple[Path, Path]:
    """``pages``: (title, wikitext, redirect target). Two pages per bz2 stream, plus the offset index."""
    directory.mkdir(parents=True, exist_ok=True)
    header = '<mediawiki xmlns="http://www.mediawiki.org/xml/export-0.11/" version="0.11">\n'
    blob = bytearray(bz2.compress(header.encode()))
    index: list[str] = []
    for i in range(0, len(pages), 2):
        offset = len(blob)
        group = list(enumerate(pages[i : i + 2], start=i + 1))
        blob += bz2.compress("".join(_page_xml(n, t, x, r) for n, (t, x, r) in group).encode())
        index += [f"{offset}:{n}:{t}" for n, (t, _, _) in group]
    blob += bz2.compress(b"</mediawiki>\n")
    xml = directory / f"{wiki}-{date}-pages-articles-multistream.xml.bz2"
    idx = directory / f"{wiki}-{date}-pages-articles-multistream-index.txt.bz2"
    xml.write_bytes(bytes(blob))
    idx.write_bytes(bz2.compress(("\n".join(index) + "\n").encode()))
    return xml, idx


def _is_loopback(address: object) -> bool:
    if isinstance(address, str):  # AF_UNIX
        return True
    if isinstance(address, tuple) and address:
        host = str(address[0])  # pyright: ignore[reportUnknownArgumentType]
        if host == "localhost":
            return True
        try:
            return ipaddress.ip_address(host.split("%")[0]).is_loopback
        except ValueError:
            return False
    return False


def disable_network(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    """Refuse every non-loopback connection and name lookup; returns the list of attempts."""
    attempts: list[object] = []
    real_connect = socket.socket.connect
    real_getaddrinfo = socket.getaddrinfo

    def connect(self: socket.socket, address: Any) -> None:
        if not _is_loopback(address):
            attempts.append(address)
            raise OSError("network disabled in the e2e test")
        real_connect(self, address)

    def getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
        if host not in (None, "localhost") and not _is_loopback((host,)):
            attempts.append(("getaddrinfo", host))
            raise socket.gaierror("name resolution disabled in the e2e test")
        return real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    return attempts
