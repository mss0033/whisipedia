"""Discoverers for ``doc-collection`` sources: what an issuer lists, as :class:`Candidate` records.

Every discoverer reads **metadata only** (index pages, JSON APIs, OAI-PMH, S3 listings) through
the injected ``Fetcher``, wrapped in a :class:`Throttle` (one request per host per interval, a
longer interval where ``robots.txt`` asks for one). Kinds (``access.discovery[].kind``):

``index-page``
    HTML pages (``urls``). Document links match ``documents`` (regex on the absolute URL, default
    PDFs). ``follow`` (regex) names intermediate pages to visit (``depth``, ``max_pages``); with
    ``filter_follow: true`` a link is filtered on its text *before* it is visited, so a denied
    series is never opened. ``rows`` reads table rows (``columns``: field -> cell index) instead of
    bare links; ``detail_documents`` picks the document link on a row's detail page.
``api``
    A JSON API by ``flavor``: ``nga-msi``, ``govinfo`` (needs a key), ``congress-crs`` (needs a
    key), ``ntrs``, ``osti``, ``usgs-pubs``, ``govuk-search``, ``dspace7``, ``tnm``.
``oai-pmh``
    ``ListRecords`` with ``oai_dc``; rights from ``dc:rights``.
``archive-org``
    The archive.org scrape API (``query``); one item per document, provenance kept.
``s3-listing``
    A public bucket listing (``bucket_url``, ``prefix``, ``key_pattern``; ``newest: N`` for the N keys
    that sort last); ETag = MD5 when single-part.
``git``
    A GitHub repository (size from the GitHub API); fetched as a ``git bundle`` of a mirror clone.
``list`` / ``document``
    Fixed items (``items``) or a single ``url``.

Discoverers yield candidates lazily and report the upstream total when the API states one
(:attr:`DiscoverEnv.totals`), so a dry run can sample a few pages of a million-document collection
and still give honest totals. Landing pages that need a second request to find the document bytes
carry ``Candidate.resolve`` and are resolved only for documents the metadata filter allowed.
"""

from __future__ import annotations

import contextlib
import csv
import gzip
import hashlib
import io
import json
import logging
import os
import re
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote, urlencode, urljoin, urlsplit
from xml.etree import ElementTree as ET

from selectolax.lexbor import LexborHTMLParser, LexborNode

from wsp_adapters.doccollection.model import (
    Candidate,
    display_title,
    is_fragment,
    is_generic_title,
    key_stem_from_url,
    make_key,
    pub_key,
    title_from_url,
)
from wsp_adapters.doccollection.policy import DocFacts, Rules, Verdict, classify_metadata
from wsp_core.errors import FetchError, NotFoundError
from wsp_core.model import FetchResult, RemoteFile, RemoteHead
from wsp_core.ports import Fetcher, SupportsHead

log = logging.getLogger(__name__)

Json = dict[str, Any]


class DiscoveryError(RuntimeError):
    pass


class NoDocuments(Exception):  # noqa: N818 - an answer, not a failure
    """A resolver's positive answer that a listed item has no document to fetch (a news page without
    attachments, a repository item without a PDF). An empty list from a resolver is a *failure* (the
    page changed, the item is dark): it is retried, never cached, and never read as a withdrawal
    (2026-10-12 audit, CA-10)."""


# --- politeness ----------------------------------------------------------------------------------


class Throttle:
    """A ``Fetcher`` wrapper: at most one request per host every ``interval`` seconds.

    ``per_host`` overrides the interval (e.g. ``www.cia.gov: 10`` from its ``Crawl-delay``). Also
    counts requests per host, so a dry run can report what it cost upstream.
    """

    def __init__(
        self,
        inner: Fetcher,
        *,
        interval: float = 1.0,
        per_host: Mapping[str, float] | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.inner = inner
        self.interval = interval
        self.per_host = {k.lower(): v for k, v in (per_host or {}).items()}
        self.clock = clock
        self.sleep = sleep
        self._last: dict[str, float] = {}  # host -> start time of its latest (reserved) request
        self.requests: dict[str, int] = {}
        self._lock = threading.Lock()

    def _wait(self, url: str) -> None:
        """Reserve this host's next start slot, then sleep until it. Thread-safe: concurrent callers
        get consecutive slots, so requests may overlap but never *start* closer than the interval."""
        host = urlsplit(url).netloc.lower()
        gap = max(self.interval, self.per_host.get(host, 0.0))
        with self._lock:
            now = self.clock()
            last = self._last.get(host)
            start = now if last is None else max(now, last + gap)
            self._last[host] = start
            self.requests[host] = self.requests.get(host, 0) + 1
        if start > now:
            self.sleep(start - now)

    def _charge(self, url: str, extra: int) -> None:
        """Count ``extra`` more requests a call made to ``url``'s host (a probe, redirect hops, retries)
        and push the host's next start back by as many intervals, so the budget is per HTTP request,
        not per call (2026-10-12 audit, CA-21)."""
        if extra <= 0:
            return
        host = urlsplit(url).netloc.lower()
        gap = max(self.interval, self.per_host.get(host, 0.0))
        with self._lock:
            self._last[host] = max(self._last.get(host, self.clock()), self.clock()) + extra * gap
            self.requests[host] = self.requests.get(host, 0) + extra

    def get_bytes(self, url: str, *, max_bytes: int = 64 * 1024 * 1024) -> bytes:
        self._wait(url)
        try:
            return self.inner.get_bytes(url, max_bytes=max_bytes)
        except FetchError as exc:
            self._charge(url, _attempts(exc) - 1)
            raise

    def download(self, remote: RemoteFile, dest: Any) -> FetchResult:
        self._wait(remote.url)
        try:
            result = self.inner.download(remote, dest)
        except FetchError as exc:
            self._charge(remote.url, _attempts(exc) - 1)
            raise
        self._charge(remote.url, _requests_made(result) - 1)
        return result

    def head(self, url: str) -> RemoteHead:
        if not isinstance(self.inner, SupportsHead):
            raise FetchError(f"HEAD not supported by {type(self.inner).__name__}")
        self._wait(url)
        return self.inner.head(url)


_ATTEMPTS_RE = re.compile(r"after (\d+) attempt")


def _attempts(exc: BaseException) -> int:
    """Requests a failed call made: ``wsp_fetch`` says "... failed after N attempt(s)"."""
    m = _ATTEMPTS_RE.search(str(exc))
    return int(m.group(1)) if m else 1


def _requests_made(result: object) -> int:
    """HTTP requests a download made, from the hops ``wsp_fetch`` recorded: per source a probe and a
    GET plus each redirect hop. A result without provenance (a non-HTTP fetcher) counts one."""
    prov = getattr(result, "provenance", None)
    sources = cast(Mapping[str, object], prov).get("sources") if isinstance(prov, Mapping) else None
    if not isinstance(sources, list) or not sources:
        return 1
    total = 0
    for src in cast(list[object], sources):
        redirects = cast(Mapping[str, object], src).get("redirects") if isinstance(src, Mapping) else None
        total += 2 + (len(cast(list[object], redirects)) if isinstance(redirects, list) else 0)
    return total


# --- environment ----------------------------------------------------------------------------------


@dataclass
class DiscoverEnv:
    fetcher: Fetcher
    issuer: str
    prefix: str  # document key prefix, e.g. "army"
    origins: tuple[str, ...] = ()
    keys: Mapping[str, str] = field(default_factory=dict[str, str])  # API keys by name (never logged)
    rules: Rules = field(default_factory=Rules)
    max_pages: int | None = None  # per discoverer: listing pages to read (sampling for dry runs)
    partition: str | None = None  # only this partition (state, batch) of a very large collection
    allow_demo_key: bool = False
    totals: dict[str, int] = field(default_factory=dict[str, int])  # discoverer label -> upstream total
    scale: dict[str, float] = field(default_factory=dict[str, float])  # crawl stopped early: pages seen ratio
    notes: list[str] = field(default_factory=list[str])
    # Listing failures that may hide documents (timeouts, server errors, a discoverer that stopped, a
    # sampled crawl). A listing with any is incomplete: nothing may be inferred from what it lacks
    # (no "withdrawn" labels) and a fetch stops before downloading (see CollectionFetcher.run).
    incomplete: list[str] = field(default_factory=list[str])
    # Listings cut short on purpose (``max_pages``, a dry run or a sampled fetch): fetching may go on,
    # but nothing may be inferred from what the sample lacks.
    sampled: list[str] = field(default_factory=list[str])
    cache_dir: Path | None = None  # local disk for listings worth keeping between runs (archive.org)
    # Registry ``access.decode_url_keys``: decode every %XX of a URL before a key is made from it. Off
    # by default because it changes the key (and doc id) of a document already stored; see
    # model.key_stem_from_url.
    decode_url_keys: bool = False

    def listing_end(self, label: str, *, stopped: bool, seen: int, total: int | None = None) -> None:
        """Record how a paged listing ended (2026-10-12 audit, CA-9). ``stopped``: pages remained when
        the loop hit its page limit; ``seen``: raw records read; ``total``: what the API stated. A
        listing that ended early or read under 98 % of its stated total is not complete."""
        if stopped:
            if self.max_pages is not None:
                self.sampled.append(f"{label}: stopped after {self.max_pages} page(s) (max_pages): a sample")
            else:
                self.incomplete.append(f"{label}: stopped at the page limit with pages left to read")
        elif total and seen < 0.98 * total:
            self.incomplete.append(f"{label}: listed {seen:,} of the {total:,} records the API states")

    def get_json(self, url: str) -> Any:
        return json.loads(self.fetcher.get_bytes(url, max_bytes=256 * 1024 * 1024))

    def get_text(self, url: str) -> str:
        return self.fetcher.get_bytes(url, max_bytes=64 * 1024 * 1024).decode("utf-8", "replace")

    def key(self, name: str) -> str:
        value = self.keys.get(name, "")
        if value:
            return value
        if self.allow_demo_key:
            return "DEMO_KEY"
        raise DiscoveryError(f"API key {name!r} not configured ([curator.api_keys] {name} in local.toml)")

    def pages(self) -> int:
        return self.max_pages if self.max_pages is not None else 1_000_000


Discoverer = Callable[[Mapping[str, Any], DiscoverEnv], Iterator[Candidate]]


def https(url: str, *, upgrade: bool = False) -> str:
    if url.startswith("http://") and upgrade:
        return "https://" + url[len("http://") :]
    return url


def origin_ok(url: str, origins: Sequence[str]) -> bool:
    """``url`` is HTTPS on an allowed host (or a subdomain of one), default port, no userinfo.

    Parsed strictly: a netloc with userinfo, a backslash or a non-443 port is refused, because
    HTTP clients and ``urlsplit`` disagree about where such a host begins.
    """
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return False
    if parts.scheme.lower() != "https" or "@" in parts.netloc or "\\" in url or port not in (None, 443):
        return False
    host = (parts.hostname or "").rstrip(".").lower()
    if not host:
        return False
    return not origins or any(host == o or host.endswith("." + o) for o in origins)


def off_origin_hops(result: object, origins: Sequence[str]) -> list[str] | None:
    """Every URL a download touched (each mirror's URL, redirect hop and final URL) that is not
    an allowed origin; ``None`` when ``result`` carries no record of its hops.

    ``wsp_fetch`` records the hops in ``FetchReport.provenance['sources']``. A result without a
    ``provenance`` attribute comes from a fetcher that does not speak HTTP (test fakes) and has no
    hops. A result *with* provenance but no ``sources`` list is unverifiable and returns ``None``;
    callers treat that as a failure.
    """
    prov = getattr(result, "provenance", _NO_PROVENANCE)
    if prov is _NO_PROVENANCE:
        return []
    sources: object = cast(Mapping[str, object], prov).get("sources") if isinstance(prov, Mapping) else None
    if not isinstance(sources, list) or not sources:
        return None
    seen: list[str] = []
    for src in cast(list[object], sources):
        if not isinstance(src, Mapping):
            return None
        s = cast(Mapping[str, Any], src)
        urls = [str(s.get("url") or ""), str(s.get("final_url") or "")]
        for hop in cast(list[Mapping[str, Any]], s.get("redirects") or []):
            here = str(hop.get("url") or "")
            urls.append(here)
            if hop.get("location"):
                urls.append(urljoin(here, str(hop["location"])))
        seen.extend(u for u in urls if u and not origin_ok(u, origins) and u not in seen)
    return seen


_NO_PROVENANCE = object()


def _s(value: object) -> str:
    return "" if value is None else str(value).strip()


def _strs(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, list | tuple):
        return tuple(str(v).strip() for v in cast(Sequence[object], value) if str(v).strip())
    return (str(value).strip(),) if str(value).strip() else ()


_DATE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b(\d{4})-(\d{2})-(\d{2})"), "ymd"),
    (re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b"), "mdy"),
    (re.compile(r"\b(\d{4})(\d{2})(\d{2})\b"), "ymd"),
)


def iso_date(text: str) -> str:
    """First recognisable date in ``text`` as ``YYYY-MM-DD`` (or ``YYYY``), else ``""``."""
    for rx, order in _DATE_PATTERNS:
        if m := rx.search(text):
            if order == "mdy":
                mm, dd, yy = (int(g) for g in m.groups())
            else:
                yy, mm, dd = (int(g) for g in m.groups())
            try:
                return date(yy, mm, dd).isoformat()
            except ValueError:
                continue
    if m := re.search(r"\b(1[89]\d\d|20\d\d)\b", text):
        return m.group(1)
    return ""


# --- index pages -----------------------------------------------------------------------------------


def _node_text(node: LexborNode) -> str:
    return " ".join((node.text(separator=" ") or "").split())


def _links(html: str, base: str) -> list[tuple[str, str, LexborNode]]:
    tree = LexborHTMLParser(html)
    out: list[tuple[str, str, LexborNode]] = []
    for a in tree.css("a[href]"):
        href = (a.attributes.get("href") or "").strip()
        if not href or href.startswith(("#", "mailto:", "javascript:")):
            continue
        out.append((urljoin(base, href.replace(" ", "%20")), _node_text(a), a))
    return out


def _pub_number(pattern: str | None, *texts: str) -> str:
    if not pattern:
        return ""
    rx = re.compile(pattern, re.IGNORECASE)
    for t in texts:
        if m := rx.search(t):
            return (m.group(1) if m.groups() else m.group(0)).strip()
    return ""


def index_page(cfg: Mapping[str, Any], env: DiscoverEnv) -> Iterator[Candidate]:
    urls = _strs(cfg.get("urls")) or _strs(cfg.get("url"))
    doc_rx = re.compile(_s(cfg.get("documents")) or r"\.pdf(?:$|[?#])", re.IGNORECASE)
    follow_rx = re.compile(_s(cfg["follow"]), re.IGNORECASE) if cfg.get("follow") else None
    skip_rx = re.compile(_s(cfg["skip"]), re.IGNORECASE) if cfg.get("skip") else None
    depth = int(cfg.get("depth", 1 if follow_rx else 0))
    max_pages = min(int(cfg.get("max_pages", 500)), env.pages() * 10)
    rewrite = cast(list[str], cfg.get("follow_rewrite") or [])  # [pattern, replacement] for follow links
    upgrade = bool(cfg.get("upgrade_http", False))
    filter_follow = bool(cfg.get("filter_follow", False))
    pub_rx = _s(cfg.get("pub_number")) or None
    label = _s(cfg.get("label")) or "index-page"
    # Sites built with Gatsby send an empty shell; the page's HTML is in /page-data/<path>/page-data.json.
    gatsby = bool(cfg.get("gatsby", False))
    # Only document links whose text matches (e.g. "PDF" where each book also offers an EPUB).
    text_rx = re.compile(_s(cfg["documents_text"]), re.IGNORECASE) if cfg.get("documents_text") else None
    skip_text_rx = (
        re.compile(_s(cfg["documents_skip_text"]), re.IGNORECASE) if cfg.get("documents_skip_text") else None
    )
    strip_rx = re.compile(_s(cfg["title_strip"]), re.IGNORECASE) if cfg.get("title_strip") else None
    # "text": key documents by their (stripped) link text, for sites whose file URLs all share one path
    # (".../FileDownloadpublic.aspx?docid=..."), where a key from the URL would collide.
    key_from_text = _s(cfg.get("key_from")) == "text"
    seen_pages: set[str] = set()
    seen_docs: set[str] = set()
    queue: list[tuple[str, int, Mapping[str, str]]] = [(u, 0, {}) for u in urls]
    visited = 0
    while queue and visited < max_pages:
        page, level, inherited = queue.pop(0)
        if page in seen_pages:
            continue
        seen_pages.add(page)
        try:
            html = env.get_text(page)
            if gatsby:
                html += "\n" + _gatsby_html(env, page)
        except FetchError as exc:
            env.notes.append(f"{label}: {page}: {exc}")
            if not isinstance(exc, NotFoundError):  # a page that is gone lists nothing; a timeout might
                env.incomplete.append(f"{label}: {page}: {exc}")
            continue
        visited += 1
        if cfg.get("rows") and level == 0:
            yield from _rows(cfg, env, html, page, queue, level)
            continue
        for url, text, _node in _links(html, page):
            url = https(url, upgrade=upgrade)
            if skip_rx and skip_rx.search(url):
                continue
            if doc_rx.search(url):
                if url in seen_docs or not origin_ok(url, env.origins):
                    continue
                if text_rx is not None and not text_rx.search(text):
                    continue
                if skip_text_rx is not None and skip_text_rx.search(text):
                    continue
                seen_docs.add(url)
                if strip_rx is not None:
                    text = strip_rx.sub("", text).strip()
                # The key keeps its historical spelling (doc ids never change); the title shown to
                # readers is cleaned: no file extension or URL escapes, and never a sentence fragment
                # taken from a link inside running text (then the page's title or the file name).
                key_title = inherited.get("title") if is_generic_title(text) else text
                key_title = key_title or key_stem_from_url(url)
                linked = display_title(text) if text else ""
                title = (
                    linked
                    if linked and not is_generic_title(linked) and not is_fragment(linked)
                    else display_title(inherited.get("title") or "") or title_from_url(url)
                )
                if key_from_text and inherited.get("title"):
                    if key_title != inherited["title"]:
                        key_title = f"{inherited['title']}: {key_title}"  # a chapter, named with its book
                    if title != display_title(inherited["title"]):
                        title = f"{display_title(inherited['title'])}: {title}"
                pub = inherited.get("pub_number") or _pub_number(pub_rx, text, key_title, url)
                edition = inherited.get("edition_date") or (
                    iso_date(f"{text} {url}") if cfg.get("dates") else ""
                )
                stem = key_title if key_from_text else key_stem_from_url(url, decode=env.decode_url_keys)
                yield Candidate(
                    key=pub_key(env.prefix, pub, edition, stem),
                    url=url,
                    title=title,
                    issuer=env.issuer,
                    pub_number=pub,
                    edition_date=edition,
                    landing_url=page,
                    status=inherited.get("status", ""),
                    discovered_by=label,
                )
            elif follow_rx and level < depth and follow_rx.search(url) and origin_ok(url, env.origins):
                if filter_follow:
                    facts = DocFacts(key=url, pub_number=_pub_number(pub_rx, text), title=text)
                    if classify_metadata(facts, env.rules).verdict is Verdict.DENY:
                        continue  # a denied series is never opened
                if len(rewrite) == 2:
                    url = re.sub(rewrite[0], rewrite[1], url)
                queue.append((url, level + 1, {"title": text} if not is_generic_title(text) else {}))
    if queue and visited >= max_pages:
        pending = len({u for u, _lvl, _i in queue} - seen_pages)
        env.notes.append(f"{label}: stopped after {visited} pages ({pending} more queued): a sample")
        env.incomplete.append(f"{label}: stopped after {visited} pages ({pending} more queued)")
        env.scale[label] = (visited + pending) / max(visited, 1)


def _gatsby_html(env: DiscoverEnv, page: str) -> str:
    """The HTML a Gatsby page renders: every string in its ``page-data.json``, one per line."""
    parts = urlsplit(page)
    path = parts.path.rstrip("/") or "/index"
    data: object = env.get_json(f"{parts.scheme}://{parts.netloc}/page-data{path}/page-data.json")
    out: list[str] = []
    stack: list[object] = [data]
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, Mapping):
            stack.extend(cast(Mapping[str, object], item).values())
        elif isinstance(item, list):
            stack.extend(cast(list[object], item))
    return "\n".join(reversed(out))


def _rows(
    cfg: Mapping[str, Any],
    env: DiscoverEnv,
    html: str,
    page: str,
    queue: list[tuple[str, int, Mapping[str, str]]],
    level: int,
) -> Iterator[Candidate]:
    """Table rows (APD style): one row per publication, a link to its detail page."""
    columns = cast(Mapping[str, int], cfg.get("columns") or {})
    tree = LexborHTMLParser(html)
    detail_docs = _s(cfg.get("detail_documents"))
    # [pattern, replacement]: the document's own URL from the row's link, without a detail-page hop
    # per row (the RFC index links /info/rfc9293/; the text is /rfc/rfc9293.txt). Rows whose link
    # does not match are skipped.
    url_rewrite = cast(list[str], cfg.get("url_rewrite") or [])
    rewrite_rx = re.compile(url_rewrite[0]) if len(url_rewrite) == 2 else None
    for row in tree.css(_s(cfg.get("rows")) or "tr"):
        cells = row.css("td")
        if not cells:
            continue
        fields = {name: _node_text(cells[i]) for name, i in columns.items() if 0 <= int(i) < len(cells)}
        link = row.css_first("a[href]")
        if link is None:
            continue
        href = urljoin(page, (link.attributes.get("href") or "").strip())
        landing = href
        if rewrite_rx is not None:
            if not rewrite_rx.search(href):
                continue
            href = rewrite_rx.sub(url_rewrite[1], href)
        pub = fields.get("pub_number", "")
        title = fields.get("title", "") or pub
        edition = iso_date(fields.get("date", ""))
        cand = Candidate(
            key=pub_key(env.prefix, pub, edition, key_stem_from_url(href, decode=env.decode_url_keys)),
            url=href,
            # An all-capitals title is recased for display only; the screens read it as printed, where
            # "(CUI)" and "(FOUO)" are markings ("(Cui)" is not; 2026-10-12 audit, CA-5).
            title=title.title() if title.isupper() else title,
            issuer=env.issuer,
            pub_number=pub,
            edition_date=edition,
            landing_url=landing,
            status=fields.get("status", ""),
            discovered_by=_s(cfg.get("label")) or "index-page:rows",
            resolve="detail-page" if detail_docs else "",
            extra={
                **({"detail_documents": detail_docs} if detail_docs else {}),
                **({"raw_title": title} if title.isupper() else {}),
            },
        )
        if origin_ok(href, env.origins):
            yield cand
    del queue, level


def resolve_detail_page(cand: Candidate, env: DiscoverEnv) -> list[Candidate]:
    """Second hop: the document link on a detail page (first match of ``detail_documents``)."""
    pattern = re.compile(cand.extra.get("detail_documents") or r"\.pdf(?:$|[?#])", re.IGNORECASE)
    html = env.get_text(cand.url)
    for url, _text, _node in _links(html, cand.url):
        if pattern.search(url) and origin_ok(url, env.origins):
            return [cand.with_url(url)]
    return []


# --- APIs ------------------------------------------------------------------------------------------


def api(cfg: Mapping[str, Any], env: DiscoverEnv) -> Iterator[Candidate]:
    flavor = _s(cfg.get("flavor"))
    impl = _API.get(flavor)
    if impl is None:
        raise DiscoveryError(f"unknown api flavor {flavor!r}; known: {sorted(_API)}")
    return impl(cfg, env)


def nga_msi(cfg: Mapping[str, Any], env: DiscoverEnv) -> Iterator[Candidate]:
    base = _s(cfg.get("url")) or "https://msi.nga.mil/api/publications/stored-pubs?output=json"
    items = cast(list[Json], env.get_json(base))
    env.totals["nga-msi"] = len(items)
    # Sections of one publication share a pubDownloadId; the full-publication file replaces them. An
    # item without an id is a publication of its own (grouping them all under 0 let one full file hide
    # every other id-less item; 2026-10-12 audit, CA-22).
    by_pub: dict[object, list[Json]] = {}
    for i, it in enumerate(items):
        pub_id = it.get("pubDownloadId")
        by_pub.setdefault(int(pub_id) if pub_id else ("item", i), []).append(it)
    for group in by_pub.values():
        full = [it for it in group if it.get("fullPubFlag")]
        for it in full or group:
            s3key = _s(it.get("s3Key"))
            if not s3key:
                continue
            display = _s(it.get("pubDownloadDisplayName"))
            pub = _pub_number(r"(Pub\.?\s*\d+[A-Z]?)", display) or display
            title = _s(it.get("sectionDisplayName")) or display
            url = "https://msi.nga.mil/api/publications/download?" + urlencode(
                {"key": s3key, "type": "download"}
            )
            yield Candidate(
                key=make_key(env.prefix, f"{pub}-{_s(it.get('filenameBase'))}"),
                url=url,
                title=f"{display}: {title}" if title != display else display,
                issuer=env.issuer,
                pub_number=pub,
                edition_date=_s(it.get("sectionLastModified"))[:10],
                size=int(it["fileSize"]) if it.get("fileSize") else None,
                discovered_by="api:nga-msi",
            )


def openstax(cfg: Mapping[str, Any], env: DiscoverEnv) -> Iterator[Candidate]:
    """OpenStax's book catalogue (its CMS API): each live book's print-quality PDF. ``url`` overrides it."""
    base = _s(cfg.get("url")) or "https://openstax.org/apps/cms/api/v2/pages/"
    fields = "title,high_resolution_pdf_url,book_state"  # the API refuses unknown fields (HTTP 400)
    offset = pages = seen = 0
    while pages < env.pages():
        params = {"type": "books.Book", "fields": fields, "limit": 50, "offset": offset}
        data = cast(Json, env.get_json(f"{base}?{urlencode(params)}"))
        pages += 1
        env.totals["openstax"] = int(cast(Json, data.get("meta") or {}).get("total_count") or 0)
        items = cast(list[Json], data.get("items") or [])
        if not items:
            break
        for it in items:
            seen += 1
            meta = cast(Json, it.get("meta") or {})
            url = _s(it.get("high_resolution_pdf_url"))
            # Retired books stay on the site with their PDF; a book still being written has none.
            if not url or _s(it.get("book_state")) not in ("live", "deprecated", "retired", ""):
                continue
            slug = _s(meta.get("slug")) or str(it.get("id"))
            yield Candidate(
                key=make_key(env.prefix, slug),
                url=url,
                title=_s(it.get("title")),
                issuer=env.issuer,
                edition_date=_s(meta.get("first_published_at"))[:10],
                language=(_s(meta.get("locale")) or "en")[:2],
                landing_url=_s(meta.get("html_url")),
                discovered_by="api:openstax",
            )
        offset += len(items)
        if offset >= env.totals["openstax"]:
            break
    env.listing_end(
        "openstax",
        stopped=offset < env.totals.get("openstax", 0),
        seen=seen,
        total=env.totals.get("openstax"),
    )


def govinfo(cfg: Mapping[str, Any], env: DiscoverEnv) -> Iterator[Candidate]:
    """GovInfo collection listing (official GPO API). ``collection``, ``since``, ``package_prefix``."""
    collection = _s(cfg.get("collection")) or "GOVPUB"
    since = _s(cfg.get("since")) or "1990-01-01T00:00:00Z"
    prefixes = _strs(cfg.get("package_prefix"))
    key = env.key(_s(cfg.get("api_key")) or "govinfo")
    url: str | None = f"https://api.govinfo.gov/collections/{collection}/{since}?" + urlencode(
        {"pageSize": 1000, "offsetMark": "*", "api_key": key}
    )
    pages = seen = 0
    while url and pages < env.pages():
        data = cast(Json, env.get_json(url))
        pages += 1
        env.totals[f"govinfo:{collection}"] = int(data.get("count") or 0)
        packages = cast(list[Json], data.get("packages") or [])
        seen += len(packages)
        if not packages:
            url = None
            break
        for pkg in packages:
            pid = _s(pkg.get("packageId"))
            if prefixes and not pid.startswith(prefixes):
                continue
            yield Candidate(
                key=make_key(env.prefix, pid),
                url=f"https://www.govinfo.gov/content/pkg/{pid}/pdf/{pid}.pdf",
                title=_s(pkg.get("title")) or pid,
                issuer=env.issuer,
                pub_number=pid,
                edition_date=_s(pkg.get("dateIssued"))[:10],
                landing_url=f"https://www.govinfo.gov/app/details/{pid}",
                discovered_by=f"api:govinfo:{collection}",
            )
        nxt = _s(data.get("nextPage"))
        url = f"{nxt}&api_key={key}" if nxt and "api_key=" not in nxt else (nxt or None)
    env.listing_end(
        f"govinfo:{collection}",
        stopped=url is not None,
        seen=seen,
        total=env.totals.get(f"govinfo:{collection}"),
    )


def congress_crs(cfg: Mapping[str, Any], env: DiscoverEnv) -> Iterator[Candidate]:
    """CRS reports listed by the official Congress.gov API (metadata; PDFs live on congress.gov)."""
    key = env.key(_s(cfg.get("api_key")) or "congress")
    offset, pages, more = 0, 0, True
    while pages < env.pages():
        data = cast(
            Json,
            env.get_json(
                "https://api.congress.gov/v3/crsreport?"
                + urlencode({"format": "json", "limit": 250, "offset": offset, "api_key": key})
            ),
        )
        pages += 1
        env.totals["congress-crs"] = int(cast(Json, data.get("pagination") or {}).get("count") or 0)
        reports = cast(list[Json], data.get("CRSReports") or [])
        if not reports:
            more = False
            break
        for r in reports:
            rid = _s(r.get("id"))
            version = _s(r.get("version")) or "1"
            yield Candidate(
                key=make_key(env.prefix, rid, _s(r.get("publishDate"))[:10]),
                url=f"https://www.congress.gov/crs_external_products/{rid[:2]}/PDF/{rid}/{rid}.{version}.pdf",
                title=_s(r.get("title")) or rid,
                issuer=env.issuer,
                pub_number=rid,
                edition_date=_s(r.get("publishDate"))[:10],
                status=_s(r.get("status")),
                discovered_by="api:congress-crs",
            )
        offset += len(reports)
        total = env.totals["congress-crs"]
        if total and offset >= total:
            more = False
            break
    env.listing_end("congress-crs", stopped=more, seen=offset, total=env.totals.get("congress-crs"))


def ntrs(cfg: Mapping[str, Any], env: DiscoverEnv) -> Iterator[Candidate]:
    """NASA Technical Reports Server search API (``query``; public records with a PDF only)."""
    size, start, pages, more = 100, 0, 0, True
    while pages < env.pages():
        params = {"q": _s(cfg.get("query")), "page.size": size, "page.from": start}
        if cfg.get("filters"):
            params.update(cast(Mapping[str, Any], cfg["filters"]))
        data = cast(Json, env.get_json("https://ntrs.nasa.gov/api/citations/search?" + urlencode(params)))
        pages += 1
        env.totals["ntrs"] = int(cast(Json, data.get("stats") or {}).get("total") or 0)
        results = cast(list[Json], data.get("results") or [])
        if not results:
            more = False
            break
        for r in results:
            pdf = next(
                (
                    _s(cast(Json, d.get("links") or {}).get("pdf"))
                    for d in cast(list[Json], r.get("downloads") or [])
                    if cast(Json, d.get("links") or {}).get("pdf")
                ),
                "",
            )
            rid = _s(r.get("id"))
            if not pdf:
                continue
            rights = (
                "public-release"
                if _s(r.get("distribution")).upper() == "PUBLIC"
                else _s(r.get("distribution"))
            )
            yield Candidate(
                key=make_key(env.prefix, rid),
                url=urljoin("https://ntrs.nasa.gov/", pdf),
                title=_s(r.get("title")) or rid,
                issuer=env.issuer,
                pub_number=_s(r.get("id")),
                edition_date=_first_publication_date(r.get("publications")),
                subjects=_strs(r.get("subjectCategories")) + _strs(r.get("keywords")),
                abstract=_s(r.get("abstract"))[:2000],
                rights="" if rights == "public-release" else f"distribution: {rights}",
                landing_url=f"https://ntrs.nasa.gov/citations/{rid}",
                discovered_by="api:ntrs",
            )
        start += size
        if env.totals["ntrs"] and start >= env.totals["ntrs"]:
            more = False
            break
    env.listing_end(
        "ntrs", stopped=more, seen=min(start, env.totals.get("ntrs") or start), total=env.totals.get("ntrs")
    )


def _first_publication_date(pubs: object) -> str:
    if isinstance(pubs, list) and pubs:
        first = cast(list[object], pubs)[0]
        if isinstance(first, dict):
            return _s(cast(Json, first).get("publicationDate"))[:10]
    return ""


_XML_ILLEGAL = re.compile(rb"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def xml_root(body: bytes) -> ET.Element:
    """Parse an API's XML answer; control characters XML forbids (seen in OSTI records) are dropped."""
    return ET.fromstring(_XML_ILLEGAL.sub(b"", body))  # noqa: S314 - HTTPS API answer; expat rejects external entities


def _osti_records(body: bytes) -> list[Json]:
    """OSTI answers JSON or XML depending on the query (no Accept header in the port): read both."""
    text = body.lstrip()
    if text.startswith(b"<"):
        root = xml_root(text)
        out: list[Json] = []
        for rec in root.iter("record"):
            row: Json = {child.tag: (child.text or "").strip() for child in rec}
            row["subjects"] = [(s.text or "").strip() for s in rec.iter("subject") if (s.text or "").strip()]
            row["links"] = [dict(link.attrib) for link in rec.iter("link")]
            out.append(row)
        return out
    data: object = json.loads(text or b"[]")
    return cast(list[Json], data) if isinstance(data, list) else []


def osti(cfg: Mapping[str, Any], env: DiscoverEnv) -> Iterator[Candidate]:
    """OSTI.GOV records API (``query`` and optional ``params``); full text through the OSTI purl."""
    page, pages, more, no_text = 1, 0, True, 0
    while pages < env.pages():
        params: dict[str, Any] = {"rows": 100, "page": page}
        if _s(cfg.get("query")):
            params["q"] = _s(cfg.get("query"))
        params.update(cast(Mapping[str, Any], cfg.get("params") or {}))
        body = env.fetcher.get_bytes("https://www.osti.gov/api/v1/records?" + urlencode(params))
        pages += 1
        records = _osti_records(body)
        if not records:
            more = False
            break
        for r in records:
            oid = _s(r.get("osti_id"))
            if not oid:
                continue
            # Without a "fulltext" link the purl is a 404 every time (eleven DOE fundamentals handbook
            # volumes, 2026-10-20).
            links = cast(list[object], r.get("links")) if isinstance(r.get("links"), list) else []
            if links and not any(
                isinstance(x, dict) and cast(Json, x).get("rel") == "fulltext" for x in links
            ):
                no_text += 1
                continue
            yield Candidate(
                key=make_key(env.prefix, f"OSTI-{oid}"),
                url=f"https://www.osti.gov/servlets/purl/{oid}",
                title=_s(r.get("title")) or oid,
                issuer=env.issuer,
                pub_number=_s(r.get("report_number")) or f"OSTI {oid}",
                edition_date=_s(r.get("publication_date"))[:10],
                subjects=_strs(r.get("subjects")),
                abstract=_s(r.get("description"))[:2000],
                landing_url=f"https://www.osti.gov/biblio/{oid}",
                discovered_by="api:osti",
            )
        page += 1
    if no_text:
        env.notes.append(f"osti: {no_text} record(s) without full text at OSTI were skipped")
    env.listing_end("osti", stopped=more, seen=0)


def usgs_pubs(cfg: Mapping[str, Any], env: DiscoverEnv) -> Iterator[Candidate]:
    """USGS Publications Warehouse API (``query`` / ``params``); document links only."""
    page, pages, seen, more = 1, 0, 0, True
    while pages < env.pages():
        params: dict[str, Any] = {"page_size": 100, "page_number": page}
        if cfg.get("query"):
            params["q"] = _s(cfg.get("query"))
        params.update(cast(Mapping[str, Any], cfg.get("params") or {}))
        data = cast(
            Json, env.get_json("https://pubs.usgs.gov/pubs-services/publication/?" + urlencode(params))
        )
        pages += 1
        env.totals["usgs-pubs"] = int(data.get("recordCount") or 0)
        records = cast(list[Json], data.get("records") or [])
        if not records:
            more = False
            break
        seen += len(records)
        for r in records:
            docs = [
                link
                for link in cast(list[Json], r.get("links") or [])
                if _s(cast(Json, link.get("type") or {}).get("text")) == "Document"
            ]
            for i, link in enumerate(docs):
                url = _s(link.get("url"))
                if not url.lower().endswith(".pdf") or not origin_ok(url, env.origins):
                    continue
                index_id = _s(r.get("indexId"))
                size = link.get("size")
                yield Candidate(
                    key=make_key(env.prefix, index_id + (f"-{i}" if i else "")),
                    url=url,
                    title=_s(r.get("title")) or index_id,
                    issuer=env.issuer,
                    pub_number=index_id,
                    edition_date=_s(r.get("publicationYear")),
                    subjects=_strs(_s(cast(Json, r.get("seriesTitle") or {}).get("text"))),
                    abstract=re.sub(r"<[^>]+>", " ", _s(r.get("docAbstract")))[:2000],
                    size=int(size) if isinstance(size, int) else None,
                    landing_url=f"https://pubs.usgs.gov/publication/{index_id}",
                    discovered_by="api:usgs-pubs",
                )
        page += 1
        if seen >= env.totals["usgs-pubs"]:
            more = False
            break
    env.listing_end("usgs-pubs", stopped=more, seen=seen, total=env.totals.get("usgs-pubs"))


def govuk_search(cfg: Mapping[str, Any], env: DiscoverEnv) -> Iterator[Candidate]:
    """GOV.UK search API; each result's attachments are resolved through the content API."""
    start, count, pages, seen, more = 0, 100, 0, 0, True
    base = _s(cfg.get("url")) or "https://www.gov.uk/api/search.json"
    while pages < env.pages():
        params: dict[str, Any] = {"count": count, "start": start, "fields": "link,title,public_timestamp"}
        params.update(cast(Mapping[str, Any], cfg.get("params") or {}))
        sep = "&" if "?" in base else "?"
        data = cast(Json, env.get_json(base + sep + urlencode(params, doseq=True)))
        pages += 1
        env.totals["govuk-search"] = int(data.get("total") or 0)
        results = cast(list[Json], data.get("results") or [])
        if not results:
            more = False
            break
        seen += len(results)
        for r in results:
            link = _s(r.get("link"))
            if not link.startswith("/"):
                continue
            yield Candidate(
                key=make_key(env.prefix, link.strip("/").replace("/", "-")),
                url="https://www.gov.uk/api/content" + link,
                title=_s(r.get("title")) or link,
                issuer=env.issuer,
                edition_date=_s(r.get("public_timestamp"))[:10],
                landing_url="https://www.gov.uk" + link,
                discovered_by="api:govuk-search",
                resolve="govuk-content",
            )
        start += count
        if seen >= env.totals["govuk-search"]:
            more = False
            break
    env.listing_end("govuk-search", stopped=more, seen=seen, total=env.totals.get("govuk-search"))


def resolve_govuk_content(cand: Candidate, env: DiscoverEnv) -> list[Candidate]:
    data = cast(Json, env.get_json(cand.url))
    details = cast(Json, data.get("details") or {})
    out: list[Candidate] = []
    licence = "OGL-UK-3.0"
    for i, att in enumerate(cast(list[Json], details.get("attachments") or [])):
        url = _s(att.get("url"))
        if not url or not origin_ok(url, env.origins):
            continue
        if _s(att.get("attachment_type")) not in ("file", "") and not url.lower().endswith(".pdf"):
            continue
        out.append(
            replace(
                cand.with_url(url),
                key=f"{cand.key}-{i}" if i else cand.key,
                title=_s(att.get("title")) or cand.title,
                size=int(att["file_size"]) if isinstance(att.get("file_size"), int) else None,
                rights=f"license:{licence}",
            )
        )
    if not out and _s(data.get("document_type")) in ("guidance", "detailed_guide"):
        # HTML-only guidance: store the rendered page itself, named as the HTML it is.
        page = cand.with_url(cand.landing_url, rights=f"license:{licence}")
        out.append(replace(page, extra={**page.extra, "suffix": ".html"}))
    if not out and _s(data.get("document_type")):
        raise NoDocuments(f"{cand.landing_url}: a {_s(data.get('document_type'))} without attachments")
    return out


def dspace7(cfg: Mapping[str, Any], env: DiscoverEnv) -> Iterator[Candidate]:
    """DSpace 7 discovery search (WHO IRIS, FAO): ``base``, ``queries``; bitstreams resolved later.
    ``cover_page: true``: the repository adds a cover page to each PDF it serves, so the bitstream's
    size and MD5 (of the stored file) are not the served file's and are not used (NPS Calhoun: 17,702
    of 17,721 downloads failed "size 1183075 != 1014813", 2026-10-21)."""
    base = _s(cfg.get("base")).rstrip("/")
    cover = {"cover_page": "1"} if cfg.get("cover_page") else {}
    for query in _strs(cfg.get("queries")) or ("*",):
        page, pages, seen, more = 0, 0, 0, True
        while pages < env.pages():
            params = {"query": query, "dsoType": "ITEM", "size": 100, "page": page}
            data = cast(Json, env.get_json(f"{base}/server/api/discover/search/objects?" + urlencode(params)))
            pages += 1
            sr = cast(Json, cast(Json, data.get("_embedded") or {}).get("searchResult") or {})
            env.totals[f"dspace7:{query}"] = int(cast(Json, sr.get("page") or {}).get("totalElements") or 0)
            objects = cast(list[Json], cast(Json, sr.get("_embedded") or {}).get("objects") or [])
            if not objects:
                more = False
                break
            seen += len(objects)
            for obj in objects:
                item = cast(Json, cast(Json, obj.get("_embedded") or {}).get("indexableObject") or {})
                meta = cast(Json, item.get("metadata") or {})

                def first(name: str, meta: Json = meta) -> str:
                    vals = cast(list[Json], meta.get(name) or [])
                    return _s(vals[0].get("value")) if vals else ""

                uuid = _s(item.get("uuid"))
                handle = _s(item.get("handle"))
                yield Candidate(
                    key=make_key(env.prefix, handle.replace("/", "-") or uuid),
                    url=f"{base}/server/api/core/items/{uuid}/bundles",
                    title=_s(item.get("name")) or first("dc.title"),
                    issuer=env.issuer,
                    pub_number=first("dc.identifier.govdoc") or handle,
                    edition_date=first("dc.date.issued")[:10],
                    subjects=tuple(
                        _s(v.get("value")) for v in cast(list[Json], meta.get("dc.subject") or [])
                    )[:20],
                    abstract=first("dc.description.abstract")[:2000],
                    rights=first("dc.rights") or first("dc.rights.uri"),
                    language=(first("dc.language.iso") or "en")[:2],
                    landing_url=f"{base}/handle/{handle}" if handle else "",
                    discovered_by=f"api:dspace7:{query}",
                    resolve="dspace7",
                    extra=cover,
                )
            page += 1
            if seen >= env.totals[f"dspace7:{query}"]:
                more = False
                break
        env.listing_end(f"dspace7:{query}", stopped=more, seen=seen, total=env.totals.get(f"dspace7:{query}"))


def resolve_dspace7(cand: Candidate, env: DiscoverEnv) -> list[Candidate]:
    """The item's PDFs in its ORIGINAL bundle. An answer without a bundle list is a failure (``[]``);
    an item whose bundles hold no PDF is :class:`NoDocuments`."""
    bundles = cast(Json, env.get_json(cand.url))
    listed = cast(Json, bundles.get("_embedded") or {}).get("bundles")
    if not isinstance(listed, list):
        return []
    for b in cast(list[Json], listed):
        if _s(b.get("name")) != "ORIGINAL":
            continue
        link = _s(cast(Json, cast(Json, b.get("_links") or {}).get("bitstreams") or {}).get("href"))
        if not link:
            return []
        streams = cast(Json, env.get_json(link))
        bitstreams = cast(Json, streams.get("_embedded") or {}).get("bitstreams")
        if not isinstance(bitstreams, list):
            return []
        out: list[Candidate] = []
        for i, bs in enumerate(cast(list[Json], bitstreams)):
            name = _s(bs.get("name"))
            if not name.lower().endswith(".pdf"):
                continue
            content = _s(cast(Json, cast(Json, bs.get("_links") or {}).get("content") or {}).get("href"))
            if not content:
                continue
            md5 = cast(Json, bs.get("checkSum") or {})
            stored = not cand.extra.get("cover_page")  # the stored file is the served one
            out.append(
                replace(
                    cand.with_url(content),
                    key=f"{cand.key}-{i}" if i else cand.key,
                    size=int(bs["sizeBytes"]) if stored and isinstance(bs.get("sizeBytes"), int) else None,
                    md5=_s(md5.get("value")).lower()
                    if stored and _s(md5.get("checkSumAlgorithm")).upper() == "MD5"
                    else None,
                    language=cand.language,
                )
            )
        if not out:
            raise NoDocuments(f"{cand.landing_url or cand.url}: no PDF in the ORIGINAL bundle")
        return out
    raise NoDocuments(f"{cand.landing_url or cand.url}: no ORIGINAL bundle")


def tnm(cfg: Mapping[str, Any], env: DiscoverEnv) -> Iterator[Candidate]:
    """USGS TNM Access API (``datasets``, ``prod_formats``): one map sheet per candidate."""
    offset, pages, size, more = 0, 0, 1000, True
    dataset = _s(cfg.get("datasets")) or "Historical Topographic Maps"
    while pages < env.pages():
        params: dict[str, Any] = {"datasets": dataset, "max": size, "offset": offset, "outputFormat": "JSON"}
        if cfg.get("prod_formats"):
            params["prodFormats"] = _s(cfg.get("prod_formats"))
        params.update(cast(Mapping[str, Any], cfg.get("params") or {}))
        data = cast(
            Json, env.get_json("https://tnmaccess.nationalmap.gov/api/v1/products?" + urlencode(params))
        )
        pages += 1
        env.totals[f"tnm:{dataset}"] = int(data.get("total") or 0)
        items = cast(list[Json], data.get("items") or [])
        if not items:
            more = False
            break
        for it in items:
            url = _s(it.get("downloadURL"))
            if not url or not origin_ok(url, env.origins):
                continue
            title = _s(it.get("title"))
            state = m.group(1) if (m := re.search(r",\s*([A-Z]{2})\b", title)) else "XX"
            yield Candidate(
                key=make_key(
                    env.prefix, _s(it.get("sourceId")) or key_stem_from_url(url, decode=env.decode_url_keys)
                ),
                url=url,
                title=title,
                issuer=env.issuer,
                pub_number=_s(it.get("sourceId")),
                edition_date=_s(it.get("publicationDate"))[:10],
                size=int(it["sizeInBytes"]) if isinstance(it.get("sizeInBytes"), int) else None,
                landing_url=_s(it.get("metaUrl")),
                partition=state.lower(),
                discovered_by=f"api:tnm:{dataset}",
            )
        offset += len(items)
        if offset >= env.totals[f"tnm:{dataset}"]:
            more = False
            break
    env.listing_end(f"tnm:{dataset}", stopped=more, seen=offset, total=env.totals.get(f"tnm:{dataset}"))


_API: dict[str, Discoverer] = {
    "nga-msi": nga_msi,
    "govinfo": govinfo,
    "congress-crs": congress_crs,
    "ntrs": ntrs,
    "osti": osti,
    "usgs-pubs": usgs_pubs,
    "govuk-search": govuk_search,
    "dspace7": dspace7,
    "tnm": tnm,
    "openstax": openstax,
}


# --- OAI-PMH ---------------------------------------------------------------------------------------

_OAI = "{http://www.openarchives.org/OAI/2.0/}"
_DC = "{http://purl.org/dc/elements/1.1/}"


def oai_pmh(cfg: Mapping[str, Any], env: DiscoverEnv) -> Iterator[Candidate]:
    endpoint = _s(cfg.get("url"))
    params = {"verb": "ListRecords", "metadataPrefix": _s(cfg.get("metadata_prefix")) or "oai_dc"}
    if cfg.get("set"):
        params["set"] = _s(cfg.get("set"))
    if cfg.get("from"):
        params["from"] = _s(cfg.get("from"))
    url: str | None = f"{endpoint}?{urlencode(params)}"
    pages = seen = 0
    while url and pages < env.pages():
        root = xml_root(env.fetcher.get_bytes(url))
        pages += 1
        error = root.find(f"{_OAI}error")
        if error is not None:
            code = error.get("code") or "error"
            if code != "noRecordsMatch":  # an expired resumption token, a bad argument: not the end
                env.incomplete.append(f"oai-pmh: page {pages}: {code}: {(error.text or '').strip()[:200]}")
            url = None
            break
        for rec in root.iter(f"{_OAI}record"):
            seen += 1
            header = rec.find(f"{_OAI}header")
            if header is None or header.get("status") == "deleted":
                continue
            ident = header.findtext(f"{_OAI}identifier") or ""

            def dc(name: str, rec: ET.Element = rec) -> list[str]:
                return [(e.text or "").strip() for e in rec.iter(f"{_DC}{name}") if (e.text or "").strip()]

            links = [i for i in dc("identifier") if i.startswith("http")]
            pdf = next((i for i in links if i.lower().endswith(".pdf")), "")
            yield Candidate(
                key=make_key(env.prefix, ident.rsplit(":", 1)[-1].replace("/", "-")),
                url=https(pdf or (links[0] if links else ""), upgrade=True),
                title=(dc("title") or [ident])[0],
                issuer=env.issuer,
                edition_date=iso_date((dc("date") or [""])[0]),
                subjects=tuple(dc("subject"))[:20],
                abstract=" ".join(dc("description"))[:2000],
                rights=" ".join(dc("rights")),
                language=((dc("language") or ["en"])[0] or "en")[:2],
                landing_url=links[0] if links else "",
                discovered_by="oai-pmh",
                resolve="" if pdf else _s(cfg.get("resolve")),
            )
        token = root.find(f".//{_OAI}resumptionToken")
        if token is not None and token.get("completeListSize"):
            env.totals["oai-pmh"] = int(token.get("completeListSize") or 0)
        url = (
            f"{endpoint}?{urlencode({'verb': 'ListRecords', 'resumptionToken': token.text})}"
            if token is not None and token.text
            else None
        )
    env.listing_end("oai-pmh", stopped=url is not None, seen=seen, total=env.totals.get("oai-pmh"))


# --- archive.org -----------------------------------------------------------------------------------


ARCHIVE_LISTING_TTL = 7 * 86400.0  # a cached archive.org listing is used for a week


def archive_org(cfg: Mapping[str, Any], env: DiscoverEnv) -> Iterator[Candidate]:
    """archive.org scrape API: ``query``; each item is resolved to its original PDF later.

    ``provenance`` (required in the registry) records why the copy is acceptable: an upload by the
    issuing agency, or an item that mirrors an official release one-to-one (PLAN D11).

    Partitions: with ``buckets: N`` an item's batch ``b0001``..``bN`` is a hash of its identifier, so it
    never moves between batches while the collection grows during a weeks-long acquisition. A batch then
    needs the whole listing; a complete listing is kept for a week in ``env.cache_dir`` (one listing of
    985,000 items serves all 100 CIA batches). Without ``buckets`` a batch is a listing page, which
    shifts as items are added (small collections only).
    """
    buckets = int(cfg.get("buckets") or 0)
    for page, it in _archive_items(cfg, env):
        ident = _s(it.get("identifier"))
        subjects = it.get("subject")
        pub = _pub_number(_s(cfg.get("pub_number")) or None, _s(it.get("title")), ident)
        title = _s(it.get("title")) or ident
        volume = _s(it.get("volume")) if cfg.get("title_volume") else ""
        if volume and volume.casefold() not in title.casefold():
            # Volumes of one work share its title (Appian's Roman History, v.1 and v.2).
            title = f"{title.rstrip(' ;,:')}, {volume}"
        yield Candidate(
            key=make_key(env.prefix, ident),
            url=f"https://archive.org/metadata/{quote(ident)}/files",
            title=title,
            issuer=env.issuer,
            pub_number=f"{_s(cfg.get('pub_prefix'))}{pub}" if pub else "",
            edition_date=iso_date(_s(it.get("date"))),
            size=int(it["item_size"]) if isinstance(it.get("item_size"), int) else None,
            subjects=_strs(subjects)[:20],
            abstract=_s(it.get("description"))[:1000] if isinstance(it.get("description"), str) else "",
            landing_url=f"https://archive.org/details/{ident}",
            discovered_by="archive-org",
            resolve="archive-org",
            extra={
                "provenance": _s(cfg.get("provenance")),
                "creator": _s(it.get("creator")),
                **({"prefer": _s(cfg.get("prefer"))} if cfg.get("prefer") else {}),
            },
            partition=f"b{_bucket(ident, buckets):04d}" if buckets else f"b{page:04d}",
        )
        if not buckets and env.partition and env.partition.startswith("b") and env.partition < f"b{page:04d}":
            break  # the wanted page batch has been listed


def _archive_items(cfg: Mapping[str, Any], env: DiscoverEnv) -> Iterator[tuple[int, Json]]:
    """``(listing page number, item)`` from the scrape API, or from a complete cached listing."""
    query = _s(cfg.get("query"))
    # `volume` only where asked (title_volume): the field list keys the cached listing of large collections.
    fields = "identifier,title,date,item_size,subject,creator,description" + (
        ",volume" if cfg.get("title_volume") else ""
    )
    count = int(cfg.get("page_size", 1000))
    whole = env.max_pages is None and env.cache_dir is not None and bool(cfg.get("buckets"))
    cache = None
    if whole and env.cache_dir is not None:
        name = hashlib.sha256(f"{query}|{fields}".encode()).hexdigest()[:16]
        cache = env.cache_dir / f"archive-listing-{name}.jsonl.gz"
        try:
            fresh = time.time() - cache.stat().st_mtime < ARCHIVE_LISTING_TTL
        except OSError:
            fresh = False
        if fresh:
            with gzip.open(cache, "rt", encoding="utf-8") as fh:
                total = 0
                for line in fh:
                    page, item = json.loads(line)
                    total += 1
                    yield int(page), cast(Json, item)
            env.totals["archive-org"] = total
            return
    tmp = None if cache is None else cache.with_name(f"{cache.name}.{os.getpid()}.partial")
    complete = False
    try:
        with contextlib.ExitStack() as stack:
            out = None
            if tmp is not None:
                tmp.parent.mkdir(parents=True, exist_ok=True)
                out = stack.enter_context(gzip.open(tmp, "wt", encoding="utf-8"))
            cursor: str | None = None
            pages = seen = 0
            while pages < env.pages():
                params = {"q": query, "fields": fields, "count": count}
                if cursor:
                    params["cursor"] = cursor
                url = "https://archive.org/services/search/v1/scrape?" + urlencode(params)
                data = cast(Json, env.get_json(url))
                pages += 1
                env.totals["archive-org"] = int(data.get("total") or 0)
                for it in cast(list[Json], data.get("items") or []):
                    seen += 1
                    if out is not None:
                        out.write(json.dumps([pages, it]) + "\n")
                    yield pages, it
                cursor = _s(data.get("cursor")) or None
                if not cursor:
                    break
            env.listing_end(
                "archive-org", stopped=cursor is not None, seen=seen, total=env.totals["archive-org"]
            )
            # every page read, and as many items as the API states: only then is a listing kept
            complete = cursor is None and seen >= 0.98 * env.totals.get("archive-org", 0)
        if complete and tmp is not None and cache is not None:
            tmp.replace(cache)
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)


def _bucket(ident: str, buckets: int) -> int:
    """Stable batch number 1..``buckets`` for an archive.org identifier (independent of listing order)."""
    digest = hashlib.blake2b(ident.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big") % buckets + 1


def resolve_archive_org(cand: Candidate, env: DiscoverEnv) -> list[Candidate]:
    """Pick the item's original PDF (``source: original``) with its MD5 and size."""
    data = cast(Json, env.get_json(cand.url))
    ident = cand.landing_url.rsplit("/", 1)[-1]
    listed = data.get("result")
    if not isinstance(listed, list) or not listed:
        return []  # "{}": a dark or unavailable item, or a failed answer; never "no documents"
    files = cast(list[Json], listed)
    pdfs = [f for f in files if _s(f.get("name")).lower().endswith(".pdf")]
    pdfs.sort(key=lambda f: (_s(f.get("source")) != "original", -int(_s(f.get("size")) or 0)))
    if not pdfs:
        raise NoDocuments(f"{cand.landing_url}: the item holds no PDF")
    f = pdfs[0]
    if cand.extra.get("prefer") == "text-pdf":
        # The Archive's OCR copy of an image-only original ("Additional Text PDF", <name>_text.pdf):
        # the same pages with a text layer, so the screens can read it and search can find it.
        ocr = next((x for x in files if _s(x.get("format")) == "Additional Text PDF"), None)
        if ocr is not None:
            f = ocr
    name = _s(f.get("name"))
    return [
        cand.with_url(
            f"https://archive.org/download/{quote(ident)}/{quote(name)}",
            size=int(_s(f.get("size"))) if _s(f.get("size")).isdigit() else cand.size,
            md5=_s(f.get("md5")).lower() or None,
        )
    ]


# --- S3, git, fixed lists --------------------------------------------------------------------------

_S3 = "{http://s3.amazonaws.com/doc/2006-03-01/}"


def s3_listing(cfg: Mapping[str, Any], env: DiscoverEnv) -> Iterator[Candidate]:
    """A bucket listing; ``newest: N`` keeps only the N keys that sort last (a dated snapshot a day,
    as OpenHistoricalMap's planet-YYMMDD_HHMM.osm.pbf: the newest one, not 1,700 of them)."""
    newest = int(_s(cfg.get("newest")) or 0)
    found = _s3_candidates(cfg, env)
    if newest <= 0:
        yield from found
        return
    yield from sorted(found, key=lambda c: c.url, reverse=True)[:newest]


def _s3_candidates(cfg: Mapping[str, Any], env: DiscoverEnv) -> Iterator[Candidate]:
    bucket = _s(cfg.get("bucket_url")).rstrip("/")
    prefix = _s(cfg.get("prefix"))
    if env.partition and cfg.get("partition_in_prefix"):
        prefix += env.partition.upper() + "/"  # list one state, not the whole bucket
    key_rx = re.compile(_s(cfg.get("key_pattern")) or r".", re.IGNORECASE)
    part_rx = re.compile(_s(cfg["partition"])) if cfg.get("partition") else None
    marker = ""
    pages, more = 0, True
    while pages < env.pages():
        params = {"prefix": prefix, "max-keys": 1000}
        if marker:
            params["marker"] = marker
        root = ET.fromstring(env.fetcher.get_bytes(f"{bucket}/?{urlencode(params)}"))  # noqa: S314 - HTTPS S3 listing
        pages += 1
        contents = list(root.iter(f"{_S3}Contents"))
        for c in contents:
            key = c.findtext(f"{_S3}Key") or ""
            if not key or key.endswith("/") or not key_rx.search(key):
                continue
            etag = (c.findtext(f"{_S3}ETag") or "").strip('"').lower()
            size = int(c.findtext(f"{_S3}Size") or 0)
            part = m.group(1).lower() if part_rx and (m := part_rx.search(key)) else ""
            yield Candidate(
                key=make_key(env.prefix, key.removeprefix(prefix)),
                url=f"{bucket}/{quote(key)}",
                title=display_title(title_from_url(key)),
                issuer=env.issuer,
                edition_date=iso_date(key_stem_from_url(key)),
                size=size,
                md5=etag if re.fullmatch(r"[0-9a-f]{32}", etag) else None,
                partition=part,
                discovered_by="s3-listing",
            )
        if root.findtext(f"{_S3}IsTruncated") != "true":
            more = False
            break
        if not contents:  # "more to come" with nothing on the page: the listing cannot go on
            env.incomplete.append(f"s3-listing: {bucket} {prefix}: truncated page without keys")
            more = False
            break
        marker = contents[-1].findtext(f"{_S3}Key") or ""
    env.listing_end(f"s3-listing:{prefix}", stopped=more, seen=0)


def _org_repos(org: str, cfg: Mapping[str, Any], env: DiscoverEnv) -> Iterator[Json]:
    """Every repository of a GitHub organisation (``org``) whose name matches ``match``: 100 a page, so
    Standard Ebooks' ~1,400 books take 14 requests, not one each (the anonymous API allows 60 an hour)."""
    match = re.compile(_s(cfg.get("match")) or r".")
    page = seen = 0
    while page < env.pages():
        page += 1
        repos = cast(
            list[Json], env.get_json(f"https://api.github.com/orgs/{org}/repos?per_page=100&page={page}")
        )
        if not repos:
            break
        seen += len(repos)
        for info in repos:
            if not info.get("archived") and match.search(_s(info.get("name"))):
                yield info
        if len(repos) < 100:
            break
    env.listing_end(f"git:{org}", stopped=page >= env.pages(), seen=seen)


def git_repo(cfg: Mapping[str, Any], env: DiscoverEnv) -> Iterator[Candidate]:
    listed: list[tuple[str, str, Json | None]] = []
    for repo in _strs(cfg.get("repos")) or _strs(cfg.get("url")):
        m = re.match(r"https://github\.com/([^/]+)/([^/.]+)", repo)
        if not m:
            raise DiscoveryError(f"git discovery supports GitHub repositories, got {repo!r}")
        listed.append((m.group(1), m.group(2), None))
    if cfg.get("org"):
        org = _s(cfg.get("org"))
        listed += [(org, _s(info.get("name")), info) for info in _org_repos(org, cfg, env)]
    for owner, name, known in listed:
        info = known or cast(Json, env.get_json(f"https://api.github.com/repos/{owner}/{name}"))
        yield Candidate(
            key=make_key(env.prefix, f"{owner}-{name}"),
            url=f"https://github.com/{owner}/{name}.git",
            title=_s(info.get("description")) or f"{owner}/{name}",
            issuer=env.issuer,
            edition_date=_s(info.get("pushed_at"))[:10],
            size=int(info.get("size") or 0) * 1024,
            rights=f"license:{_s(cast(Json, info.get('license') or {}).get('spdx_id'))}"
            if info.get("license")
            else "",
            landing_url=f"https://github.com/{owner}/{name}",
            discovered_by="git",
            resolve="git",
            # snapshot: the default branch's files as one zip (a shallow clone), not a mirror bundle of
            # every commit (FRUS's history is 14.5 GB; its refiner reads the files only)
            extra={
                "default_branch": _s(info.get("default_branch")),
                "snapshot": "1" if cfg.get("snapshot") else "",
            },
        )


def fixed_list(cfg: Mapping[str, Any], env: DiscoverEnv) -> Iterator[Candidate]:
    items = cast(list[Json], cfg.get("items") or ([{"url": cfg["url"]}] if cfg.get("url") else []))
    for it in items:
        urls = _strs(it.get("urls")) or _strs(it.get("url"))
        if not urls:
            continue
        pub = _s(it.get("pub_number"))
        title = _s(it.get("title")) or pub or title_from_url(urls[0])
        edition = _s(it.get("edition_date"))
        # The stored file's extension when the URL has none (GEBCO's ".../gebco_2024/zip/").
        suffix = _s(it.get("suffix"))
        extra = {"suffix": suffix} if re.fullmatch(r"\.[a-z0-9]{1,8}", suffix) else {}
        yield Candidate(
            key=pub_key(env.prefix, pub, edition, key_stem_from_url(urls[0], decode=env.decode_url_keys)),
            url=urls[0],
            title=title,
            issuer=env.issuer,
            pub_number=pub,
            edition_date=edition,
            mirrors=urls[1:],
            size=int(it["size"]) if isinstance(it.get("size"), int) else None,
            discovered_by="list",
            extra=extra,
        )


def csv_list(cfg: Mapping[str, Any], env: DiscoverEnv) -> Iterator[Candidate]:
    """A published CSV of documents (EveryCRSReport's reports.csv): ``url`` the CSV; ``base`` what its
    paths are relative to; columns named by ``key``, ``file`` (tried in order: ``[latestHTML,
    latestPDF]``), ``title``, ``date``."""
    text = env.get_text(_s(cfg.get("url")))
    base = _s(cfg.get("base"))
    files = _strs(cfg.get("file"))
    rows = list(csv.DictReader(io.StringIO(text)))
    env.totals["csv"] = len(rows)
    for row in rows:
        path = next((row[c] for c in files if row.get(c)), "")
        key = (row.get(_s(cfg.get("key"))) or "").strip()
        if not path or not key:
            continue
        url = path if path.startswith("https://") else base.rstrip("/") + "/" + path.lstrip("/")
        yield Candidate(
            key=make_key(env.prefix, key),
            url=url,
            title=" ".join((row.get(_s(cfg.get("title"))) or key).split()),
            issuer=env.issuer,
            pub_number=key,
            edition_date=iso_date(row.get(_s(cfg.get("date"))) or ""),
            discovered_by="csv",
        )


_MONTHS = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)  # fmt: skip


def latest_dated(cfg: Mapping[str, Any], env: DiscoverEnv) -> Iterator[Candidate]:
    """The newest of a dated series published at a predictable URL (``url`` with ``{Month}``, ``{MM}``,
    ``{YYYY}``): DISA's quarterly ``U_SRG-STIG_Library_October_2026.zip``. Recent months (``months``,
    default every month) are probed newest first with HEAD, up to ``lookback`` months back."""
    template = _s(cfg.get("url"))
    months = {int(m) for m in cast(list[Any], cfg.get("months") or range(1, 13))}
    lookback = int(cfg.get("lookback", 12))
    today = date.fromisoformat(_s(cfg["today"])) if cfg.get("today") else datetime.now(UTC).date()
    head = getattr(env.fetcher, "head", None)
    if not callable(head):
        raise DiscoveryError("latest-dated needs a fetcher that supports HEAD")
    year, month = today.year, today.month
    for _ in range(lookback + 1):
        if month in months:
            url = template.format(Month=_MONTHS[month - 1], MM=f"{month:02d}", YYYY=year)
            try:
                found = cast(RemoteHead, head(url))
            except NotFoundError:
                found = None
            if found is not None:
                stamp = f"{year}-{month:02d}"
                yield Candidate(
                    key=make_key(env.prefix, f"{_s(cfg.get('series')) or 'release'}-{stamp}"),
                    url=url,
                    title=f"{_s(cfg.get('title')) or title_from_url(url)} ({_MONTHS[month - 1]} {year})",
                    issuer=env.issuer,
                    edition_date=f"{stamp}-01",
                    size=found.size,
                    discovered_by="latest-dated",
                )
                return
        year, month = (year - 1, 12) if month == 1 else (year, month - 1)


DISCOVERERS: dict[str, Discoverer] = {
    "index-page": index_page,
    "api": api,
    "oai-pmh": oai_pmh,
    "archive-org": archive_org,
    "s3-listing": s3_listing,
    "git": git_repo,
    "list": fixed_list,
    "document": fixed_list,
    "csv": csv_list,
    "latest-dated": latest_dated,
}

Resolver = Callable[[Candidate, DiscoverEnv], list[Candidate]]
RESOLVERS: dict[str, Resolver] = {
    "detail-page": resolve_detail_page,
    "govuk-content": resolve_govuk_content,
    "dspace7": resolve_dspace7,
    "archive-org": resolve_archive_org,
}


def discover_all(configs: Sequence[Mapping[str, Any]], env: DiscoverEnv) -> Iterator[Candidate]:
    """Run every configured discoverer; results are unioned (first wins for a repeated key)."""
    seen: set[str] = set()
    for cfg in configs:
        kind = _s(cfg.get("kind"))
        impl = DISCOVERERS.get(kind)
        if impl is None:
            raise DiscoveryError(f"unknown discovery kind {kind!r}; known: {sorted(DISCOVERERS)}")
        try:
            for cand in impl(cfg, env):
                if cand.key in seen or (env.partition and cand.partition != env.partition):
                    continue
                seen.add(cand.key)
                yield cand
        except (FetchError, ValueError, ET.ParseError, DiscoveryError) as exc:
            env.notes.append(f"{kind}: {type(exc).__name__}: {exc}")
            env.incomplete.append(f"{kind}: {type(exc).__name__}: {exc}")
            log.warning("discovery %s failed: %s", kind, exc)


def resolve(cand: Candidate, env: DiscoverEnv) -> list[Candidate]:
    """Turn a landing-page candidate into document candidates (identity when nothing to resolve)."""
    if not cand.resolve or cand.resolve == "git":
        return [cand]
    impl = RESOLVERS.get(cand.resolve)
    if impl is None:
        raise DiscoveryError(f"unknown resolver {cand.resolve!r}")
    found = impl(cand, env)
    docs = [c for c in found if origin_ok(c.url, env.origins)]
    if found and not docs:
        raise NoDocuments(f"{cand.key}: documents only on hosts outside {tuple(env.origins)}")
    return docs
