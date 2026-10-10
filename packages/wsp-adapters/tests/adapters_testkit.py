"""Builders for tiny synthetic fixtures (ZIM files, multistream dumps) and test plumbing."""

from __future__ import annotations

import bz2
import hashlib
import json
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape, quoteattr

from libzim.writer import Creator, Hint, Item, StringProvider

from wsp_core.model import FetchResult, RemoteFile, SourceSpec, SourceStatus, Tier
from wsp_core.ports import Ctx, Fetcher

FIXTURES = Path(__file__).parent / "fixtures"
REPO_ROOT = Path(__file__).resolve().parents[3]
USER_AGENT = "Whisipedia-tests/0.1 (offline library; test suite)"


def make_spec(
    source_id: str, access: Mapping[str, Any], *, license_: str = "CC-BY-SA-4.0", **raw: Any
) -> SourceSpec:
    return SourceSpec(
        id=source_id,
        name=source_id,
        tier=Tier.T1,
        status=SourceStatus.CANDIDATE,
        license=license_,
        legal_basis="open-license",
        domain=("test",),
        adapter=str(access.get("adapter", "")) or None,
        raw={"id": source_id, "access": dict(access), **raw},
    )


def make_ctx(spec: SourceSpec, fetcher: Fetcher, workdir: Path) -> Ctx:
    return Ctx(source=spec, fetcher=fetcher, workdir=workdir, user_agent=USER_AGENT)


# --- ZIM -----------------------------------------------------------------------------------------


class _HtmlItem(Item):
    def __init__(self, path: str, title: str, content: str, mimetype: str = "text/html") -> None:
        super().__init__()
        self._path, self._title, self._content, self._mimetype = path, title, content, mimetype

    def get_path(self) -> str:
        return self._path

    def get_title(self) -> str:
        return self._title

    def get_mimetype(self) -> str:
        return self._mimetype

    def get_contentprovider(self) -> StringProvider:
        return StringProvider(self._content)

    def get_hints(self) -> dict[Hint, int]:
        return {Hint.FRONT_ARTICLE: self._mimetype.startswith("text/html")}


def build_zim(
    path: Path,
    *,
    name: str,
    articles: Sequence[tuple[str, str, str]],
    redirects: Sequence[tuple[str, str, str]] = (),
    assets: Sequence[tuple[str, str, str]] = (),
    date: str = "2026-08-01",
    language: str = "eng",
) -> Path:
    """Write a ZIM.

    ``articles``: (path, title, html); ``redirects``: (path, title, target); ``assets``: (path, mime, body).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with Creator(path).config_indexing(False, "eng").config_verbose(False) as creator:
        creator.set_mainpath(articles[0][0])
        for item_path, title, html in articles:
            creator.add_item(_HtmlItem(item_path, title, html))
        for item_path, mime, body in assets:
            creator.add_item(_HtmlItem(item_path, item_path, body, mime))
        for item_path, title, target in redirects:
            creator.add_redirection(item_path, title, target, {Hint.FRONT_ARTICLE: True})
        creator.add_metadata("Name", name)
        creator.add_metadata("Language", language)
        creator.add_metadata("Date", date)
        creator.add_metadata("Title", name)
    return path


# --- MediaWiki multistream dumps -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Page:
    page_id: int
    title: str
    text: str = ""
    ns: int = 0
    redirect: str | None = None
    rev_id: int = 1000
    timestamp: str = "2026-09-30T12:00:00Z"


def _page_xml(page: Page) -> str:
    redirect = f"    <redirect title={quoteattr(page.redirect)} />\n" if page.redirect else ""
    return (
        "  <page>\n"
        f"    <title>{escape(page.title)}</title>\n"
        f"    <ns>{page.ns}</ns>\n"
        f"    <id>{page.page_id}</id>\n"
        f"{redirect}"
        "    <revision>\n"
        f"      <id>{page.rev_id}</id>\n"
        f"      <timestamp>{page.timestamp}</timestamp>\n"
        "      <model>wikitext</model>\n"
        f'      <text bytes="{len(page.text)}" xml:space="preserve">{escape(page.text)}</text>\n'
        "    </revision>\n"
        "  </page>\n"
    )


def build_multistream(
    directory: Path, wiki: str, date: str, pages: Sequence[Page], *, per_stream: int = 2
) -> tuple[Path, Path]:
    """Write ``<wiki>-<date>-pages-articles-multistream.xml.bz2`` and its index, like Wikimedia does."""
    directory.mkdir(parents=True, exist_ok=True)
    header = (
        '<mediawiki xmlns="http://www.mediawiki.org/xml/export-0.11/" version="0.11" xml:lang="en">\n'
        "  <siteinfo>\n    <sitename>Test</sitename>\n  </siteinfo>\n"
    )
    blob = bytearray(bz2.compress(header.encode()))
    index_lines: list[str] = []
    for i in range(0, len(pages), per_stream):
        offset = len(blob)
        group = pages[i : i + per_stream]
        blob += bz2.compress("".join(_page_xml(p) for p in group).encode())
        index_lines += [f"{offset}:{p.page_id}:{p.title}" for p in group]
    blob += bz2.compress(b"</mediawiki>\n")
    xml_path = directory / f"{wiki}-{date}-pages-articles-multistream.xml.bz2"
    index_path = directory / f"{wiki}-{date}-pages-articles-multistream-index.txt.bz2"
    xml_path.write_bytes(bytes(blob))
    index_path.write_bytes(bz2.compress(("\n".join(index_lines) + "\n").encode()))
    return xml_path, index_path


def dumpstatus_for(files: Sequence[Path], wiki: str, date: str, *, status: str = "done") -> bytes:
    """A minimal ``dumpstatus.json`` describing ``files`` as the multistream job's output."""
    entries = {
        f.name: {
            "size": f.stat().st_size,
            "url": f"/{wiki}/{date}/{f.name}",
            "md5": hashlib.md5(f.read_bytes()).hexdigest(),
            "sha1": hashlib.sha1(f.read_bytes()).hexdigest(),
        }
        for f in files
    }
    job = {"status": status, "updated": "2026-10-03 10:00:00", "files": entries}
    return json.dumps({"jobs": {"articlesmultistreamdump": job}, "version": "0.8"}).encode()


def dumps_listing(dates: Sequence[str]) -> bytes:
    rows = "\n".join(f'<a href="{d}/">{d}/</a>   01-Oct-2026 10:00    -' for d in dates)
    return f"<html><body><pre>\n{rows}\n</pre></body></html>".encode()


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# --- live checks ---------------------------------------------------------------------------------


class UrllibFetcher:
    """Minimal metadata-only fetcher for the optional live smoke tests."""

    def __init__(self, user_agent: str = USER_AGENT) -> None:
        self.user_agent = user_agent

    def get_bytes(self, url: str, *, max_bytes: int = 64 * 1024 * 1024) -> bytes:
        if not url.startswith("https://"):
            raise ValueError(f"refusing non-HTTPS URL {url}")
        request = urllib.request.Request(url, headers={"User-Agent": self.user_agent})
        with urllib.request.urlopen(request, timeout=60) as response:
            data: bytes = response.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise ValueError(f"{url} exceeds {max_bytes} bytes")
        return data

    def download(self, remote: RemoteFile, dest: Path) -> FetchResult:
        raise NotImplementedError("live smoke tests never download corpus files")
