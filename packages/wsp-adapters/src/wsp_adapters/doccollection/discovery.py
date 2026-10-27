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
``dspace-sitemap``
    A DSpace 7 repository through its sitemaps and item pages (what its robots.txt allows); topical
    selection on item metadata (``topics``), ``max_items`` pages a run, read pages kept between runs.

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
import sqlite3
import threading
import time
import zipfile
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from html import unescape as html_unescape
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
from wsp_core.errors import FetchError, NotFoundError, RedirectRefusedError, RobotsDisallowedError
from wsp_core.model import FetchResult, RemoteFile, RemoteHead
from wsp_core.ports import Fetcher, SupportsHead
from wsp_core.redirects import redirect_guard, unguarded
from wsp_core.robots import Robots
from wsp_core.robots import parse as parse_robots

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

    With ``robots`` (our user agent), each host's robots.txt is read once: a URL it disallows is a
    :class:`RobotsDisallowedError`, and its ``Crawl-delay`` is the least interval for that host. Set by
    hand per source, four crawl delays had been missed and two hosts disallowing every crawler were
    being fetched (2026-10-21). A robots.txt that cannot be read sets no rules. Every redirect a
    request takes is checked the same way before it is followed (:func:`wsp_core.redirects.redirect_guard`;
    BODC's GEBCO link led to CEDA, whose robots.txt disallows every agent, 2026-10-23).
    """

    def __init__(
        self,
        inner: Fetcher,
        *,
        interval: float = 1.0,
        per_host: Mapping[str, float] | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        robots: str | None = None,
    ) -> None:
        self.inner = inner
        self.interval = interval
        self.per_host = {k.lower(): v for k, v in (per_host or {}).items()}
        self.clock = clock
        self.sleep = sleep
        self.robots = robots
        self._rules: dict[str, Robots | None] = {}  # "scheme://host" -> its robots.txt
        self._last: dict[str, float] = {}  # host -> start time of its latest (reserved) request
        self.requests: dict[str, int] = {}
        self._lock = threading.Lock()

    def _robots(self, url: str) -> Robots | None:
        parts = urlsplit(url)
        site = f"{parts.scheme}://{parts.netloc.lower()}"
        with self._lock:
            if site in self._rules:
                return self._rules[site]
        rules: Robots | None = None
        try:
            with unguarded():  # robots.txt itself may be redirected; it is not a page we crawl
                body = self.inner.get_bytes(f"{site}/robots.txt", max_bytes=512 * 1024)
        except (FetchError, OSError, ValueError):
            body = None
        if body is not None:
            # RFC 9309 (wildcards, longest match); a byte-order mark no longer hides the first rule
            # (armypubs.army.mil). An HTML page answered for /robots.txt sets no rules.
            rules = parse_robots(body)
        with self._lock:
            self._rules[site] = rules
            self.requests[parts.netloc.lower()] = self.requests.get(parts.netloc.lower(), 0) + 1
        return rules

    def _wait(self, url: str) -> None:
        """Reserve this host's next start slot, then sleep until it. Thread-safe: concurrent callers
        get consecutive slots, so requests may overlap but never *start* closer than the interval."""
        host = urlsplit(url).netloc.lower()
        gap = max(self.interval, self.per_host.get(host, 0.0))
        if self.robots is not None and (rules := self._robots(url)) is not None:
            if not rules.can_fetch(self.robots, url):
                raise RobotsDisallowedError(f"{url}: disallowed by {host}/robots.txt")
            delay = rules.crawl_delay(self.robots)
            gap = max(gap, float(delay) if delay else 0.0)
        with self._lock:
            now = self.clock()
            last = self._last.get(host)
            start = now if last is None else max(now, last + gap)
            self._last[host] = start
            self.requests[host] = self.requests.get(host, 0) + 1
        if start > now:
            self.sleep(start - now)

    def _hop(self, url: str) -> None:
        """A redirect target: refused when its host's robots.txt disallows it."""
        rules = self._robots(url)
        if rules is not None and self.robots is not None and not rules.can_fetch(self.robots, url):
            host = urlsplit(url).netloc.lower()
            raise RedirectRefusedError(f"redirected to {url}: disallowed by {host}/robots.txt")

    def _guarded(self) -> AbstractContextManager[None]:
        return redirect_guard(self._hop) if self.robots is not None else nullcontext()

    def _charge(self, url: str, extra: int) -> None:
        """Count ``extra`` more requests a call made to ``url``'s host (a probe, redirect hops, retries)
        and push the host's next start back by as many intervals, so the budget is per HTTP request,
        not per call (2026-10-12 audit, CA-21)."""
        if extra <= 0:
            return
        host = urlsplit(url).netloc.lower()
        gap = max(self.interval, self.per_host.get(host, 0.0))
        rules = self._rules.get(f"{urlsplit(url).scheme}://{host}")
        if rules is not None and self.robots is not None and (delay := rules.crawl_delay(self.robots)):
            gap = max(gap, float(delay))
        with self._lock:
            self._last[host] = max(self._last.get(host, self.clock()), self.clock()) + extra * gap
            self.requests[host] = self.requests.get(host, 0) + extra

    def get_bytes(self, url: str, *, max_bytes: int = 64 * 1024 * 1024) -> bytes:
        self._wait(url)
        try:
            with self._guarded():
                return self.inner.get_bytes(url, max_bytes=max_bytes)
        except FetchError as exc:
            self._charge(url, _attempts(exc) - 1)
            raise

    def download(self, remote: RemoteFile, dest: Any) -> FetchResult:
        self._wait(remote.url)
        try:
            with self._guarded():
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
        with self._guarded():
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

    FTP only where an origin names it as ``ftp://HOST`` (exactly that host, port 21): an issuer's FTP
    service the owner allowed (D16, 2026-10-24). A bare host never admits FTP, and an origin list
    with no HTTPS host admits no other HTTPS host.
    """
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return False
    scheme = parts.scheme.lower()
    if "@" in parts.netloc or "\\" in url:
        return False
    host = (parts.hostname or "").rstrip(".").lower()
    if not host:
        return False
    if scheme == "ftp":
        return port in (None, 21) and f"ftp://{host}" in origins
    if scheme != "https" or port not in (None, 443):
        return False
    hosts = [o for o in origins if "://" not in o]
    return not origins or any(host == o or host.endswith("." + o) for o in hosts)


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
    """Document links on index pages. ``newest``: a regex with groups ``series`` and ``date`` matched
    on each document URL; of a dated series only the newest file is kept (FoodData Central lists every
    release since 2019 on one page). URLs it does not match pass unchanged.

    ``partition``: these pages are a partition of their own, read only when it is fetched.
    ``language``: the documents' language code (default English).
    ``md5_sidecars``: a document listed with a ``<file>.md5`` beside it gets that digest (NCBI's FTP
    directories), so its download is verified instead of corroborated by a second download."""
    part = _s(cfg.get("partition"))
    if part and env.partition and env.partition != part:
        return
    found = _index_page(cfg, env)
    if part:
        found = (replace(c, partition=part) for c in found)
    if lang := _s(cfg.get("language")):  # the documents' language (DILA's French law: "fr")
        found = (replace(c, language=lang) for c in found)
    if cfg.get("md5_sidecars"):
        found = (_with_md5_sidecar(c, env) for c in found)
    if not cfg.get("newest"):
        yield from found
        return
    newest_rx = re.compile(_s(cfg["newest"]))
    # ``newest_date_format``: how the date group reads when it does not sort as text (NLM's RxNorm files
    # are dated MMDDYYYY: RxNorm_full_10052026.zip).
    fmt = _s(cfg.get("newest_date_format"))
    best: dict[str, tuple[str, Candidate]] = {}
    for cand in found:
        m = newest_rx.search(cand.url)
        if m is None:
            yield cand
            continue
        series, stamp = m.group("series"), m.group("date")
        if fmt:
            try:
                stamp = datetime.strptime(stamp, fmt).strftime("%Y-%m-%d")
            except ValueError:
                continue  # not a date of this series: not a candidate for the newest
        if series not in best or stamp > best[series][0]:
            best[series] = (stamp, cand)
    for _stamp, cand in best.values():
        yield cand


_MD5_HEX = re.compile(r"\b([0-9a-fA-F]{32})\b")


def _with_md5_sidecar(cand: Candidate, env: DiscoverEnv) -> Candidate:
    """``cand`` with the digest of its ``.md5`` sidecar; unchanged when there is none."""
    try:
        m = _MD5_HEX.search(
            env.fetcher.get_bytes(cand.url + ".md5", max_bytes=4096).decode("ascii", "replace")
        )
    except NotFoundError:
        return cand
    return replace(cand, md5=m.group(1).lower()) if m else cand


def _index_page(cfg: Mapping[str, Any], env: DiscoverEnv) -> Iterator[Candidate]:
    urls = _strs(cfg.get("urls")) or _strs(cfg.get("url"))
    doc_rx = re.compile(_s(cfg.get("documents")) or r"\.pdf(?:$|[?#])", re.IGNORECASE)
    follow_rx = re.compile(_s(cfg["follow"]), re.IGNORECASE) if cfg.get("follow") else None
    skip_rx = re.compile(_s(cfg["skip"]), re.IGNORECASE) if cfg.get("skip") else None
    depth = int(cfg.get("depth", 1 if follow_rx else 0))
    max_pages = min(int(cfg.get("max_pages", 500)), env.pages() * 10)
    rewrite = cast(list[str], cfg.get("follow_rewrite") or [])  # [pattern, replacement] for follow links
    # [pattern, replacement] turning a document link into its file: ECPA links "/works/<id>.shtml",
    # whose TEI is "/works/<id>/<id>.xml".
    doc_rewrite = cast(list[str], cfg.get("documents_rewrite") or [])
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
                if len(doc_rewrite) == 2:
                    url = re.sub(doc_rewrite[0], doc_rewrite[1], url)
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


_LOC = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.IGNORECASE)


def sitemap(cfg: Mapping[str, Any], env: DiscoverEnv) -> Iterator[Candidate]:
    """A site's sitemap (``url``; with ``expand``, a sitemap index whose sitemaps are read): each
    ``<loc>`` matching ``match`` is a landing page (``rewrite``: ``[pattern, replacement]`` applied
    first), whose document link (``detail_documents``) is found when it is resolved. MIT OpenCourseWare
    lists one sitemap per course; ``…/sitemap.xml`` becomes ``…/download/``, whose zip is the course
    (2026-10-21).

    ``key_pattern``: a regex on the page URL whose groups, joined by ``-``, name the document, where
    every URL ends alike (legislation.gov.uk's ``/ukpga/2010/15/data.html``). ``partition``: this
    sitemap is a partition of its own, read only when that partition is fetched. ``upgrade_http``:
    ``http://`` locations are fetched over HTTPS. With ``expand``, a sitemap of the index that cannot
    be read leaves the listing incomplete, and the others are still read; ``expand_match`` reads only
    the sitemaps it matches (each act is in legislation.gov.uk's "final" sitemaps once, and again in
    "revised" ones: 35,000 sitemaps for statutory instruments alone at a 5 s crawl delay)."""
    part = _s(cfg.get("partition"))
    if part and env.partition and env.partition != part:
        return
    match = re.compile(_s(cfg.get("match")) or r".", re.IGNORECASE)
    rewrite = cast(list[str], cfg.get("rewrite") or [])
    detail_docs = _s(cfg.get("detail_documents"))
    title_from = re.compile(_s(cfg["title_from"]), re.IGNORECASE) if cfg.get("title_from") else None
    key_rx = re.compile(_s(cfg["key_pattern"])) if cfg.get("key_pattern") else None
    upgrade = bool(cfg.get("upgrade_http", False))
    locs = _LOC.findall(env.get_text(https(_s(cfg.get("url")), upgrade=upgrade)))
    if cfg.get("expand"):  # a sitemap index: the pages are in the sitemaps it names
        pages: list[str] = []
        only = re.compile(_s(cfg["expand_match"])) if cfg.get("expand_match") else None
        wanted = [u for u in locs if only is None or only.search(u)]
        if len(wanted) > env.pages():
            env.listing_end("sitemap", stopped=True, seen=0)  # a sample (max_pages), or the page limit
            wanted = wanted[: env.pages()]
        for u in wanted:
            try:
                pages += _LOC.findall(env.get_text(https(u, upgrade=upgrade)))
            except FetchError as exc:
                env.notes.append(f"sitemap: {u}: {exc}")
                if not isinstance(exc, NotFoundError):
                    env.incomplete.append(f"sitemap: {u}: {exc}")
        locs = pages
    seen = 0
    for loc in dict.fromkeys(locs):
        if not match.search(loc):
            continue
        url = https(re.sub(rewrite[0], rewrite[1], loc) if len(rewrite) == 2 else loc, upgrade=upgrade)
        if not origin_ok(url, env.origins):
            continue
        km = key_rx.search(url) if key_rx else None
        if key_rx and km is None:
            continue
        seen += 1
        slug = (
            "-".join(g for g in km.groups() if g)
            if km
            else url.rstrip("/").split("/")[-2 if url.endswith("/download/") else -1]
        )
        m = title_from.search(url) if title_from else None
        name = (m.group(1) if m else slug).replace("-", " ")
        yield Candidate(
            key=make_key(env.prefix, slug),
            url=url,
            title=display_title(name),
            issuer=env.issuer,
            pub_number=slug,
            landing_url=url,
            discovered_by="sitemap",
            resolve="detail-page" if detail_docs else "",
            partition=part,
            extra={"detail_documents": detail_docs} if detail_docs else {},
        )
    env.listing_end("sitemap", stopped=False, seen=seen)


def resolve_detail_page(cand: Candidate, env: DiscoverEnv) -> list[Candidate]:
    """Second hop: the document link on a detail page (first match of ``detail_documents``)."""
    pattern = re.compile(cand.extra.get("detail_documents") or r"\.pdf(?:$|[?#])", re.IGNORECASE)
    html = env.get_text(cand.url)
    for url, _text, _node in _links(html, cand.url):
        if pattern.search(url) and origin_ok(url, env.origins):
            return [cand.with_url(url)]
    return []


# --- DSpace 7 sitemaps and item pages --------------------------------------------------------------

# A DSpace 7 site's stock robots.txt disallows its REST API (/server/api/*, search included), OAI-PMH
# (/server/oai/*), /search and /browse, with Crawl-delay: 10. It allows the sitemaps, the item pages
# and the bitstream content endpoint (``Allow: /server/api/core/bitstreams``). WHO IRIS, NPS Calhoun
# and the FAO Knowledge Repository were paused when the robots-aware throttle refused the dspace7
# search (2026-10-21); this reads only what robots.txt allows (2026-10-24).
#
# The item page is server-side rendered (Angular Universal). Its Highwire tags carry the title, date,
# language, keywords and *one* PDF (``citation_pdf_url``, not always the first file: WHO's 10665/102539
# names its second PDF), and no rights; there are no Dublin Core tags. The page also embeds the REST
# answers it was rendered from (``<script id="dspace-angular-state">``): the item's whole metadata
# record (dc.rights, every subject field, the handle) and its ORIGINAL bundle's files in order with
# their sizes. That is the record the dspace7 search returned, so documents keep their keys.

DSPACE_STATE = re.compile(r'<script id="dspace-angular-state" type="application/json">(.*?)</script>', re.S)
DSPACE_ITEM_URL = r"/(?:items|entities/publication|handle)/[^?#]+$"
DSPACE_UUID = re.compile(
    r"/(?:items|entities/[a-z]+)/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
)
DSPACE_HANDLE = re.compile(r"/handle/(\d[\d.]*/[^/?#]+)")
DSPACE_MAX_ITEMS = 5000  # item pages a run reads by default: about 14 hours at a 10 s crawl delay
DSPACE_RECORD_VERSION = 1  # bumped when the record read from a page changes, so cached pages are read again
DSPACE_RECORD_TTL = 365 * 86400.0  # a page whose sitemap date never moves is still read once a year
_URL_BLOCK = re.compile(r"<url>(.*?)</url>", re.S | re.IGNORECASE)
_LASTMOD = re.compile(r"<lastmod>\s*([^<\s]+)\s*</lastmod>", re.IGNORECASE)
_LANGUAGES = {
    "english": "en",
    "french": "fr",
    "spanish": "es",
    "arabic": "ar",
    "chinese": "zh",
    "russian": "ru",
    "portuguese": "pt",
    "german": "de",
    "italian": "it",
}


def _angular_state(html: str) -> Json | None:
    """The REST answers an Angular Universal page was rendered with. Angular before 16 escapes the
    JSON (``&q;`` for a quote, ``&a;`` for an ampersand, ...); later versions embed it as it is."""
    m = DSPACE_STATE.search(html)
    if m is None:
        return None
    raw = m.group(1)
    try:
        data: object = json.loads(raw)
    except ValueError:
        raw = raw.replace("&q;", '"').replace("&s;", "'").replace("&l;", "<").replace("&g;", ">")
        try:
            data = json.loads(raw.replace("&a;", "&"))  # last: "&a;q;" is an escaped "&q;", not a quote
        except ValueError:
            return None
    return cast(Json, data) if isinstance(data, dict) else None


def _meta_tags(html: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for node in LexborHTMLParser(html).css("head meta[name]"):
        name = (node.attributes.get("name") or "").strip()
        value = (node.attributes.get("content") or "").strip()
        if name and value:
            out.setdefault(name, []).append(value)
    return out


def _language(value: str) -> str:
    v = value.strip().lower()
    if re.fullmatch(r"[a-z]{2}(?:[_-][a-z]{2})?|[a-z]{3}", v):
        return v[:2]
    return _LANGUAGES.get(v, "")


def parse_dspace_item(html: str, page_url: str) -> Json | None:
    """The record :func:`dspace_sitemap` keeps for one item page, or ``None`` when the page does not
    show its item: a client-side shell (NPS Calhoun answers every page with an empty ``<ds-app>``,
    2026-10-24), another item's page, or an item whose file list is not on the page.

    The record: ``uuid``, ``handle``, ``title``, ``date``, ``pub_number``, ``subjects``, ``abstract``,
    ``rights``, ``language``, ``topic_text`` (series and descriptions, for ``topics``), ``withdrawn``,
    ``files`` (``[index, bitstream uuid, name, size]`` in the ORIGINAL bundle's order) and
    ``files_total`` (the bundle's file count, which may exceed the files the page lists)."""
    state = _angular_state(html)
    if state is None:
        return None
    meta = _meta_tags(html)
    core = cast(Json, cast(Json, state.get("NGRX_STATE") or {}).get("core") or {})
    objects = cast(Json, core.get("cache/object") or {})

    def data(href: str) -> Json:
        entry = objects.get(href)
        return cast(Json, cast(Json, entry).get("data") or {}) if isinstance(entry, dict) else {}

    def paged(prefix: str) -> list[Json]:
        """The paginated lists cached under ``prefix`` (a list's URL varies with its query)."""
        return [
            data(h) for h in objects if h.startswith(prefix) and isinstance(data(h).get("pageInfo"), dict)
        ]

    def page_links(listing: Json) -> list[str]:
        return [
            _s(p.get("href"))
            for p in cast(list[Json], cast(Json, listing.get("_links") or {}).get("page") or [])
        ]

    uuid_m = DSPACE_UUID.search(page_url)
    handle_m = DSPACE_HANDLE.search(page_url) or DSPACE_HANDLE.search(
        (meta.get("citation_abstract_html_url") or [""])[0]
    )
    item: Json | None = None
    self_href = ""
    for href in objects:
        d = data(href)
        if d.get("type") == "item" and (
            (uuid_m is not None and d.get("uuid") == uuid_m.group(1))
            or (uuid_m is None and handle_m is not None and d.get("handle") == handle_m.group(1))
        ):
            item, self_href = d, href
            break
    if item is None:
        # A restricted item is in the sitemap too, and its page renders the site's frame: the item's
        # own request was refused (WHO IRIS: "401 Authentication is required" for items edited the
        # day before, 2026-10-24). That is an answer, kept until the item's sitemap date moves.
        ident = uuid_m.group(1) if uuid_m else (quote(handle_m.group(1), safe="") if handle_m else "")
        for entry in cast(Json, core.get("data/request") or {}).values():
            req = cast(Json, cast(Json, entry).get("request") or {}) if isinstance(entry, dict) else {}
            resp = cast(Json, cast(Json, entry).get("response") or {}) if isinstance(entry, dict) else {}
            href = _s(req.get("href"))
            refused = resp.get("statusCode") in (401, 403, 404, 410)
            if refused and ident and ident in href and ("/core/items/" in href or "/pid/find" in href):
                return {"v": DSPACE_RECORD_VERSION, "gone": True, "status": resp.get("statusCode")}
        return None  # never guess: the page renders other items too (FAO's related items)
    md = cast(dict[str, list[Json]], item.get("metadata") or {})

    def values(name: str) -> list[str]:
        return [_s(v.get("value")) for v in md.get(name) or [] if _s(v.get("value"))]

    def first(*names: str) -> str:
        return next((v for n in names for v in values(n)), "")

    # The ORIGINAL bundle's files in order: the item's bundle list names its bundles, and the first
    # page of each bundle's bitstreams (5 on these sites) is in the state.
    bundle_lists = paged(f"{self_href}/bundles")
    if not bundle_lists:
        return None  # the file section was not rendered: nothing says the item has no files
    files: list[list[object]] = []
    total = 0
    for bundle_href in page_links(bundle_lists[0]):
        bundle = data(bundle_href)
        if _s(bundle.get("_name") or bundle.get("name")) != "ORIGINAL":
            continue
        listings = paged(f"{bundle_href}/bitstreams")
        if not listings:
            return None
        listing = max(listings, key=lambda p: len(page_links(p)))
        total = int(cast(Json, listing.get("pageInfo") or {}).get("totalElements") or 0)
        for i, href in enumerate(page_links(listing)):
            bs = data(href)
            if not bs.get("uuid"):
                return None  # a file the page names but does not describe
            titles = cast(list[Json], cast(Json, bs.get("metadata") or {}).get("dc.title") or [])
            name = _s(bs.get("name") or bs.get("_name")) or next((_s(t.get("value")) for t in titles), "")
            size = bs.get("sizeBytes")
            files.append([i, _s(bs.get("uuid")), name, size if isinstance(size, int) else None])
        break
    subjects = [
        v
        for name in md
        if ".subject" in name and not name.endswith(("Identifiers", "qualifier"))
        for v in values(name)
    ] or [k.strip() for v in meta.get("citation_keywords") or [] for k in v.split(";") if k.strip()]
    rights = dict.fromkeys(values("dc.rights") + values("dc.rights.license") + values("dc.rights.uri"))
    handle = _s(item.get("handle"))
    return {
        "v": DSPACE_RECORD_VERSION,
        "uuid": _s(item.get("uuid")),
        "handle": handle,
        "title": first("dc.title") or _s(item.get("name")) or (meta.get("citation_title") or [""])[0],
        "date": first("dc.date.issued")[:10] or iso_date((meta.get("citation_date") or [""])[0]),
        "pub_number": first("dc.identifier.govdoc") or handle,
        "subjects": list(dict.fromkeys(subjects))[:20],
        "abstract": (first("dc.description.abstract") or (meta.get("description") or [""])[0])[:2000],
        "rights": " ; ".join(rights),
        "language": _language(first("dc.language.iso", "dc.language"))
        or _language((meta.get("citation_language") or [""])[0])
        or "en",
        "topic_text": " ".join(values("dc.relation.ispartofseries") + values("dc.description"))[:2000],
        "withdrawn": bool(item.get("withdrawn")),
        "files": files,
        "files_total": max(total, len(files)),
    }


class _Topics:
    """``topics``: the dspace7 ``queries`` kept as a metadata filter, since /search is disallowed.
    A topic is quoted phrases and bare words that must all occur, or alternatives joined by ``OR``.
    Matched on whole words (case and punctuation ignored: "drinking-water" matches "drinking water")
    in the title, subjects, abstract, series and descriptions. The search also read the full text,
    so a topic selects fewer items than its query did: the ones that are about it."""

    def __init__(self, topics: Sequence[str]) -> None:
        self.alternatives: list[list[str]] = []
        for topic in topics:
            for alt in re.split(r"\s+OR\s+", topic):
                terms = [self.norm(p or w) for p, w in re.findall(r'"([^"]+)"|(\S+)', alt)]
                if terms := [t for t in terms if t]:
                    self.alternatives.append(terms)

    @staticmethod
    def norm(text: str) -> str:
        return " ".join(re.findall(r"[^\W_]+", text.casefold()))

    def match(self, record: Mapping[str, Any]) -> bool:
        if not self.alternatives:
            return True
        parts = [record.get("title"), *cast(list[str], record.get("subjects") or [])]
        parts += [record.get("abstract"), record.get("topic_text")]
        text = f" {self.norm(' '.join(_s(p) for p in parts))} "
        return any(all(f" {t} " in text for t in terms) for terms in self.alternatives)


class _ItemCache:
    """Records read from DSpace item pages, by page URL, with the sitemap ``<lastmod>`` they were read
    at (SQLite in the source's workdir; in memory without one). At 10 s a page WHO IRIS's ~277,000
    items are a month of reading: a run reads ``max_items`` pages and keeps them, so later runs read
    only pages not read yet and pages whose sitemap date moved. Committed every 20 pages, so a run
    killed after hours keeps what it read."""

    def __init__(self, path: Path | None) -> None:
        self._db = sqlite3.connect(":memory:" if path is None else path, isolation_level=None)
        if path is not None:
            self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS items"
            " (url TEXT PRIMARY KEY, lastmod TEXT NOT NULL, at REAL NOT NULL, rec TEXT NOT NULL)"
        )
        self._pending = 0

    @classmethod
    def open(cls, cache_dir: Path | None, base: str) -> _ItemCache:
        if cache_dir is not None:
            name = hashlib.sha256(base.encode()).hexdigest()[:12]
            try:
                cache_dir.mkdir(parents=True, exist_ok=True)
                return cls(cache_dir / f"dspace-items-{name}.sqlite")
            except (OSError, sqlite3.Error) as exc:
                log.warning("dspace item cache unavailable in %s: %s", cache_dir, exc)
        return cls(None)

    def get(self, url: str, lastmod: str, *, now: float) -> Json | None:
        """The record read at this ``lastmod`` or a later one, unless it is a year old."""
        row = self._db.execute("SELECT lastmod, at, rec FROM items WHERE url = ?", (url,)).fetchone()
        if row is None or (lastmod and str(row[0]) < lastmod) or now - float(row[1]) > DSPACE_RECORD_TTL:
            return None
        rec = cast(Json, json.loads(row[2]))
        return rec if rec.get("v") == DSPACE_RECORD_VERSION else None

    def put(self, url: str, lastmod: str, rec: Json, *, now: float) -> None:
        if self._pending == 0:
            self._db.execute("BEGIN")
        self._db.execute(
            "INSERT OR REPLACE INTO items VALUES (?, ?, ?, ?)", (url, lastmod, now, json.dumps(rec))
        )
        self._pending += 1
        if self._pending >= 20:
            self.flush()

    def flush(self) -> None:
        if self._pending:
            self._db.execute("COMMIT")
            self._pending = 0

    def close(self) -> None:
        self.flush()
        self._db.close()


def _dspace_sitemaps(base: str, cfg: Mapping[str, Any], env: DiscoverEnv, label: str) -> dict[str, str]:
    """Item page URL -> its sitemap ``<lastmod>`` ("" when none), from every sitemap of the index."""
    item_rx = re.compile(_s(cfg.get("items")) or DSPACE_ITEM_URL)
    match_rx = re.compile(_s(cfg["match"])) if cfg.get("match") else None
    maps = [https(u, upgrade=True) for u in _LOC.findall(env.get_text(f"{base}/sitemap_index.xml"))]
    maps = [u for u in maps if origin_ok(u, env.origins)]
    if len(maps) > env.pages():
        env.sampled.append(f"{label}: read {env.pages()} of {len(maps)} sitemaps (max_pages): a sample")
        maps = maps[: env.pages()]
    listed: dict[str, str] = {}
    for u in maps:
        try:
            text = env.get_text(u)
        except FetchError as exc:  # a sitemap the index names: its items are unknown this run
            env.notes.append(f"{label}: {u}: {exc}")
            env.incomplete.append(f"{label}: {u}: {exc}")
            continue
        for block in _URL_BLOCK.findall(text):
            if (loc := _LOC.search(block)) is None:
                continue
            url = https(html_unescape(loc.group(1)), upgrade=True)
            if (
                item_rx.search(url)
                and (match_rx is None or match_rx.search(url))
                and origin_ok(url, env.origins)
            ):
                lastmod = _LASTMOD.search(block)
                listed[url] = lastmod.group(1) if lastmod else ""
    return listed


def dspace_sitemap(cfg: Mapping[str, Any], env: DiscoverEnv) -> Iterator[Candidate]:
    """A DSpace 7 repository through what its robots.txt allows: the sitemaps (``base``
    ``/sitemap_index.xml``) for its item pages, each page's server-rendered record for the item's
    metadata and PDFs (:func:`parse_dspace_item`), and the bitstream content endpoint for the bytes.

    ``items``: regex for item page URLs in the sitemaps (default ``/items/``, ``/entities/publication/``
    and ``/handle/``); ``match``: a further regex on them (FAO's handle names the language,
    ``.../ca8818en``). ``topics``: the old ``queries`` as a metadata filter (:class:`_Topics`).
    ``max_items``: item pages read per run (default 5,000; five per ``max_pages`` in a dry run); pages
    read before are kept in ``env.cache_dir`` and read again only when their sitemap ``<lastmod>``
    moves. ``cover_page``: as in :func:`dspace7`.

    Keys are the dspace7 flavor's (``<prefix>:<handle>``, ``-<n>`` for the n-th file of the ORIGINAL
    bundle) and so is the URL (the content endpoint), so documents already held are recognised and
    not fetched again. A run that leaves pages unread, or reads a topical selection, is a *sample*:
    what it finds is fetched, and nothing is inferred withdrawn from what it lacks."""
    base = _s(cfg.get("base")).rstrip("/")
    label = "dspace-sitemap"
    topics = _Topics(_strs(cfg.get("topics")))
    budget = int(cfg.get("max_items", DSPACE_MAX_ITEMS))
    if env.max_pages is not None:
        budget = min(budget, 5 * env.max_pages)
    cover = {"cover_page": "1"} if cfg.get("cover_page") else {}
    listed = _dspace_sitemaps(base, cfg, env, label)
    env.totals[label] = len(listed)
    partial_files = 0

    def candidates(rec: Json, page: str) -> Iterator[Candidate]:
        nonlocal partial_files
        if rec.get("gone") or rec.get("withdrawn") or not topics.match(rec):
            return
        files = cast(list[list[Any]], rec.get("files") or [])
        if int(rec.get("files_total") or 0) > len(files):
            partial_files += 1
        handle = _s(rec.get("handle"))
        key = make_key(env.prefix, handle.replace("/", "-") or _s(rec.get("uuid")))
        for index, bitstream, name, size in files:
            if not _s(name).lower().endswith(".pdf"):
                continue
            yield Candidate(
                key=f"{key}-{index}" if index else key,
                url=f"{base}/server/api/core/bitstreams/{bitstream}/content",
                title=_s(rec.get("title")) or _s(name),
                issuer=env.issuer,
                pub_number=_s(rec.get("pub_number")),
                edition_date=_s(rec.get("date")),
                size=size if isinstance(size, int) and not cover else None,
                subjects=tuple(cast(list[str], rec.get("subjects") or [])),
                abstract=_s(rec.get("abstract")),
                rights=_s(rec.get("rights")),
                language=_s(rec.get("language")) or "en",
                landing_url=f"{base}/handle/{handle}" if handle else page,
                discovered_by=label,
                extra=cover,
            )

    cache = _ItemCache.open(env.cache_dir, base)
    try:
        now = time.time()
        pending: list[tuple[str, str]] = []
        for url, lastmod in listed.items():
            rec = cache.get(url, lastmod, now=now)
            if rec is None:
                pending.append((lastmod, url))
            else:
                yield from candidates(rec, url)
        pending.sort(reverse=True)  # new and changed items first (ISO dates sort as text)
        read = failed = 0
        for lastmod, url in pending[:budget]:
            if failed >= 10 and failed > read:
                break  # a site that stopped rendering its pages would cost max_items requests for nothing
            why = "no item record on the page"
            try:
                rec = parse_dspace_item(env.get_text(url), url)
            except NotFoundError:
                rec = {"v": DSPACE_RECORD_VERSION, "gone": True}  # listed but not there, until its date moves
            except FetchError as exc:
                rec, why = None, str(exc)
            if rec is None:
                failed += 1
                if failed <= 20:
                    env.notes.append(f"{label}: {url}: {why}")
                continue
            read += 1
            cache.put(url, lastmod, rec, now=time.time())
            yield from candidates(rec, url)
        if unread := len(pending) - read:
            env.sampled.append(
                f"{label}: {unread:,} of {len(listed):,} item pages not read yet ({read:,} read this run, "
                f"max_items {budget:,}): later runs read them"
            )
        if (failed > read and (failed >= 10 or read == 0)) or failed > max(10, read // 5):
            # Mostly failures is a site problem (Calhoun's empty shells), not a few pages left unread.
            env.incomplete.append(f"{label}: {failed} of {read + failed} item pages read could not be used")
        if topics.alternatives:
            env.sampled.append(
                f"{label}: a topical selection (topics); the items it leaves out are still listed"
            )
        if partial_files:
            env.sampled.append(f"{label}: {partial_files} item(s) list more files than their page shows")
    finally:
        cache.close()


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
    """GovInfo collection listing (official GPO API). ``collection``, ``since``, ``package_prefix``;
    ``package_skip``: prefixes left out (Serial Set volumes, which GPO also files under BUDGET, CRPT and
    the House and Senate Journals); ``format``: ``pdf`` or ``htm`` (GAO reports have no PDF);
    ``partition: true``: each collection is a partition of its own, listed only when it is fetched."""
    collection = _s(cfg.get("collection")) or "GOVPUB"
    part = collection.lower() if cfg.get("partition") else ""
    if part and env.partition and env.partition != part:
        return
    since = _s(cfg.get("since")) or "1990-01-01T00:00:00Z"
    prefixes = _strs(cfg.get("package_prefix"))
    skip = _strs(cfg.get("package_skip"))
    fmt = _s(cfg.get("format")) or "pdf"
    if fmt not in ("pdf", "htm"):
        raise DiscoveryError(f"govinfo: unknown format {fmt!r}")
    folder = "html" if fmt == "htm" else "pdf"
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
            if (prefixes and not pid.startswith(prefixes)) or (skip and pid.startswith(skip)):
                continue
            yield Candidate(
                key=make_key(env.prefix, pid),
                url=f"https://www.govinfo.gov/content/pkg/{pid}/{folder}/{pid}.{fmt}",
                title=_s(pkg.get("title")) or pid,
                issuer=env.issuer,
                pub_number=pid,
                edition_date=_s(pkg.get("dateIssued"))[:10],
                landing_url=f"https://www.govinfo.gov/app/details/{pid}",
                discovered_by=f"api:govinfo:{collection}",
                partition=part,
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
        params: dict[str, Any] = {"page.size": size, "page.from": start}
        if _s(cfg.get("query")):  # an empty q= matches nothing (2026-10-21)
            params["q"] = _s(cfg.get("query"))
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
            # Only public records: the API's distribution parameter does not filter (2026-10-21).
            if not pdf or _s(r.get("distribution")).upper() != "PUBLIC":
                continue
            yield Candidate(
                key=make_key(env.prefix, rid),
                url=urljoin("https://ntrs.nasa.gov/", pdf),
                title=_s(r.get("title")) or rid,
                issuer=env.issuer,
                pub_number=_s(r.get("id")),
                edition_date=_first_publication_date(r.get("publications")),
                subjects=_strs(r.get("subjectCategories")) + _strs(r.get("keywords")),
                abstract=_s(r.get("abstract"))[:2000],
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


def _crossref_date(issued: object) -> str:
    parts = cast(Json, issued).get("date-parts") if isinstance(issued, dict) else None
    first = cast(list[object], parts)[0] if isinstance(parts, list) and parts else None
    nums = [x for x in cast(list[object], first) if isinstance(x, int)] if isinstance(first, list) else []
    if not nums or len(nums) != len(cast(list[object], first)):
        return ""
    return "-".join(f"{x:02d}" if i else f"{x:04d}" for i, x in enumerate(nums))


def crossref(cfg: Mapping[str, Any], env: DiscoverEnv) -> Iterator[Candidate]:
    """Every work registered under a DOI ``prefix`` (Crossref REST API, cursor paging), where the
    issuer's own deposit names the document's file (``resource.primary.URL``; NIST's 10.6028 points at
    the PDF on nvlpubs.nist.gov). ``exclude`` (regex on the DOI) leaves out what another source holds;
    ``component`` works (a part of another deposit) are skipped."""
    prefix = _s(cfg.get("prefix"))
    if not prefix:
        raise DiscoveryError("crossref: no prefix")
    exclude = re.compile(_s(cfg.get("exclude")), re.I) if _s(cfg.get("exclude")) else None
    select = "DOI,type,title,resource,issued,container-title,subject"
    cursor, pages, seen, total, more = "*", 0, 0, None, True
    skipped = 0
    while pages < env.pages():
        query = urlencode({"rows": 1000, "cursor": cursor, "select": select})
        body = env.get_json(f"https://api.crossref.org/prefixes/{quote(prefix)}/works?{query}")
        pages += 1
        msg = cast(Json, body.get("message") or {}) if isinstance(body, dict) else {}
        if total is None and isinstance(msg.get("total-results"), int):
            total = cast(int, msg["total-results"])
        items = cast(list[Json], msg.get("items") or [])
        if not items:
            more = False
            break
        for it in items:
            seen += 1
            doi = _s(it.get("DOI")).lower()
            resource = cast(Json, it.get("resource") or {})
            url = _s(cast(Json, resource.get("primary") or {}).get("URL"))
            if not doi or not url or it.get("type") == "component" or (exclude and exclude.search(doi)):
                skipped += 1
                continue
            titles = _strs(it.get("title"))
            container = _strs(it.get("container-title"))
            yield Candidate(
                key=make_key(env.prefix, doi.partition("/")[2]),
                url=https(url, upgrade=True),
                title=titles[0] if titles else doi,
                issuer=env.issuer,
                pub_number=f"doi:{doi}",
                edition_date=_crossref_date(it.get("issued")),
                subjects=_strs(it.get("subject")) + container[:1],
                landing_url=f"https://doi.org/{doi}",
                discovered_by="api:crossref",
            )
        nxt = _s(msg.get("next-cursor"))
        if not nxt or nxt == cursor:
            more = False
            break
        cursor = nxt
    if skipped:
        env.notes.append(f"crossref: {skipped} work(s) skipped (components, excluded or without a file URL)")
    env.totals["crossref"] = total or 0
    env.listing_end("crossref", stopped=more, seen=seen, total=total)


ASSIST = "https://quicksearch.dla.mil"
_ASSIST_TOKEN = re.compile(r"ImageRedirector\.aspx\?token=(\d+)\.\d+")
_ASSIST_DATE = re.compile(r"(\d{2})-([A-Z]{3})-(\d{4})")


def _assist_date(text: str) -> str:
    """``18-MAY-2022`` -> ``2022-05-18``."""
    months = {m[:3].upper(): i for i, m in enumerate(_MONTHS, start=1)}
    m = _ASSIST_DATE.search(text)
    if not m or m.group(2) not in months:
        return ""
    return f"{m.group(3)}-{months[m.group(2)]:02d}-{m.group(1)}"


def _css_text(tree: LexborHTMLParser, sel: str) -> str:
    node = tree.css_first(sel)
    return " ".join(node.text().split()) if node is not None else ""


def dla_assist(cfg: Mapping[str, Any], env: DiscoverEnv) -> Iterator[Candidate]:
    """DLA ASSIST QuickSearch, the DoD standardization library (D18, 2026-10-24): every document detail
    page of an ident-number range (``start`` to ``stop``; ``partition`` names the range), its newest
    revision whose distribution statement is A. The site has no listing a GET can read (its searches
    are ASP.NET postbacks); a detail page is ``qsDocDetails.aspx?ident_number=N`` and its image
    ``WMX/Default.aspx?token=T`` redirects to the PDF.

    ``skip_fsc_groups``: Federal Supply Classification groups left out by their first two digits
    (10 weapons, 11 nuclear ordnance, 12 fire control, 13 ammunition and explosives, 14 guided
    missiles); the deny core screens the titles of the rest."""
    part = _s(cfg.get("partition"))
    if part and env.partition and env.partition != part:
        return
    start, stop = int(cfg["start"]), int(cfg["stop"])
    groups = {int(g) for g in cast(list[Any], cfg.get("skip_fsc_groups") or [])}
    skipped: dict[str, int] = {}
    pages, n = 0, start
    while n < stop and pages < env.pages():
        ident, n = n, n + 1
        try:
            html = env.get_text(f"{ASSIST}/qsDocDetails.aspx?ident_number={ident}")
        except NotFoundError:
            continue
        pages += 1
        tree = LexborHTMLParser(html)
        # The id has its own span; its cell also says "Scroll down to access document images", which
        # became part of every key when the cell was read whole (2026-10-24).
        doc_id = _css_text(tree, "span#general_doc_idLabel")
        if not doc_id:
            continue  # no document under this number
        fsc = _css_text(tree, "span#general_fsc_codeLabel")
        if fsc[:2].isdigit() and int(fsc[:2]) in groups:
            skipped["FSC group excluded"] = skipped.get("FSC group excluded", 0) + 1
            continue
        chosen: tuple[str, str] | None = None
        for row in tree.css("table#GVRevisionHistory tr"):
            tds = row.css("td")
            link = row.css_first("a")
            m = _ASSIST_TOKEN.search(link.attributes.get("href") or "") if link is not None else None
            if m and len(tds) >= 4 and " ".join(tds[2].text().split()) == "A":
                chosen = (m.group(1), _assist_date(tds[3].text()))
                break
        if chosen is None:
            skipped["no Distribution A image"] = skipped.get("no Distribution A image", 0) + 1
            continue
        title = _css_text(tree, "span#general_titleLabel")
        yield Candidate(
            key=make_key(env.prefix, doc_id),
            url=f"{ASSIST}/WMX/Default.aspx?token={chosen[0]}",
            title=f"{doc_id}: {title}" if title else doc_id,
            issuer=env.issuer,
            pub_number=doc_id,
            edition_date=chosen[1] or _assist_date(_css_text(tree, "span#general_doc_dateLabel")),
            subjects=tuple(
                x for x in (f"FSC {fsc}" if fsc else "", _css_text(tree, "span#doc_categoryLabel")) if x
            ),
            abstract=_css_text(tree, "span#scope_scope_descriptionLabel")[:2000],
            rights="Distribution Statement A",
            status=_css_text(tree, "span#general_statusLabel").upper(),
            landing_url=f"{ASSIST}/qsDocDetails.aspx?ident_number={ident}",
            discovered_by="api:dla-assist",
            partition=part,
            extra={"suffix": ".pdf"},
        )
    for why, count in sorted(skipped.items()):
        env.notes.append(f"dla-assist: {count} document(s) skipped: {why}")
    env.listing_end("dla-assist", stopped=n < stop, seen=pages)


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


def wbdg(cfg: Mapping[str, Any], env: DiscoverEnv) -> Iterator[Candidate]:
    """The Whole Building Design Guide's federal facility criteria (UFC, UFGS, ECB, NAVFAC, AF and
    others): ``/api/documents`` a page at a time; ``statuses`` (default ``[ACTIVE]``) keeps the
    criteria in force. Each document's files are resolved later (``/api/documents/<id>`` names them on
    the criteria's S3 bucket, which does not list)."""
    base = (_s(cfg.get("base")) or "https://www.wbdg.org").rstrip("/")
    statuses = {s.upper() for s in _strs(cfg.get("statuses"))} or {"ACTIVE"}
    page, pages, seen, more = 1, 0, 0, True
    while pages < env.pages():
        data = cast(Json, env.get_json(f"{base}/api/documents?" + urlencode({"limit": 100, "page": page})))
        pages += 1
        meta = cast(Json, cast(Json, data.get("meta") or {}).get("pagination") or {})
        env.totals["wbdg"] = int(meta.get("total") or 0)
        docs = cast(list[Json], data.get("data") or [])
        if not docs:
            more = False
            break
        seen += len(docs)
        for d in docs:
            if _s(d.get("status")).upper() not in statuses or d.get("canOpen") is False:
                continue
            if not cast(list[Json], d.get("federalAgencies") or []):
                continue  # a private body's guide the site also lists (Green Seal, NIBS)
            number = _s(d.get("documentNumber"))
            did = _s(d.get("id"))
            yield Candidate(
                key=make_key(env.prefix, number or did),
                url=f"{base}/api/documents/{did}",
                title=_s(d.get("title")) or number,
                issuer=env.issuer,
                pub_number=number,
                edition_date=_s(d.get("publishDate"))[:10],
                abstract=re.sub(r"<[^>]+>", " ", _s(d.get("summary")))[:2000].strip(),
                landing_url=f"{base}{_s(d.get('urlAlias'))}" if d.get("urlAlias") else "",
                discovered_by="api:wbdg",
                resolve="wbdg",
            )
        if int(meta.get("totalPages") or 0) <= page:
            more = False
            break
        page += 1
    env.listing_end("wbdg", stopped=more, seen=seen, total=env.totals.get("wbdg"))


def resolve_wbdg(cand: Candidate, env: DiscoverEnv) -> list[Candidate]:
    """A WBDG document's current files (its PDFs on the criteria bucket)."""
    data = cast(Json, cast(Json, env.get_json(cand.url)).get("data") or {})
    files = [
        m
        for m in cast(list[Json], data.get("mediaFiles") or [])
        if m.get("isCurrent") and not m.get("isArchived") and _s(m.get("fileUrl"))
    ]
    if not files:
        raise NoDocuments(f"{cand.landing_url or cand.url}: no current file")
    return [
        replace(
            cand.with_url(_s(m.get("fileUrl"))),
            key=f"{cand.key}-{i}" if i else cand.key,
            size=int(m["fileSize"]) if isinstance(m.get("fileSize"), int) else None,
        )
        for i, m in enumerate(files)
    ]


_IIS_ROW = re.compile(r"(?:(?P<size>\d+)|&lt;dir&gt;)\s*<A HREF=\"(?P<href>[^\"]+)\">", re.IGNORECASE)
_ETSI_VERSION = re.compile(r"/(\d{2})\.(\d{2})\.(\d{2})_(\d{2})/$")


def _iis_listing(env: DiscoverEnv, url: str) -> list[tuple[str, int | None]]:
    """``(absolute href, size or None for a folder)`` of an IIS directory listing, parent excluded."""
    text = env.get_text(url)
    out: list[tuple[str, int | None]] = []
    for m in _IIS_ROW.finditer(text):
        href = urljoin(url, html_unescape(m.group("href")))
        if len(href) <= len(url):  # [To Parent Directory]
            continue
        out.append((href, int(m.group("size")) if m.group("size") else None))
    return out


def etsi(cfg: Mapping[str, Any], env: DiscoverEnv) -> Iterator[Candidate]:
    """ETSI deliverables (``www.etsi.org/deliver``, an open IIS tree: type, number range, number,
    version): for each deliverable the newest published version (``_60``; the newest of any stage when
    none is published yet), its PDFs. ``types``: the deliverable folders (default the current kinds)."""
    base = (_s(cfg.get("base")) or "https://www.etsi.org/deliver").rstrip("/")
    types = _strs(cfg.get("types")) or (
        "etsi_en",
        "etsi_es",
        "etsi_eg",
        "etsi_ts",
        "etsi_tr",
        "etsi_gs",
        "etsi_gr",
        "etsi_sr",
    )
    pages, seen = 0, 0
    budget = env.pages() * 1000  # listing requests a run may make
    for kind in types:
        for rng, _ in _iis_listing(env, f"{base}/{kind}/"):
            for number, _ in _iis_listing(env, rng):
                pages += 2
                if pages > budget:
                    env.listing_end("etsi", stopped=True, seen=seen)
                    return
                versions = [
                    (tuple(int(g) for g in m.groups()), href)
                    for href, _ in _iis_listing(env, number)
                    if (m := _ETSI_VERSION.search(href))
                ]
                if not versions:
                    continue
                published = [v for v in versions if v[0][3] == 60]
                (ver, folder) = max(published or versions)
                for href, size in _iis_listing(env, folder):
                    if size is None or not href.lower().endswith(".pdf"):
                        continue
                    seen += 1
                    name = href.rsplit("/", 1)[-1]
                    num = number.rstrip("/").rsplit("/", 1)[-1]
                    label = kind.removeprefix("etsi_").upper()
                    yield Candidate(
                        key=make_key(env.prefix, name.removesuffix(".pdf")),
                        url=href,
                        title=f"ETSI {label} {num} V{ver[0]}.{ver[1]}.{ver[2]}",
                        issuer=env.issuer,
                        pub_number=f"ETSI {label} {num}",
                        edition_date="",
                        size=size,
                        landing_url=folder,
                        discovered_by=f"etsi:{kind}",
                    )
    env.listing_end("etsi", stopped=False, seen=seen)


_DOE_RECORD = re.compile(
    r'\{"ID":"(?P<id>[^"]+)","Title":"(?P<title>(?:[^"\\]|\\.)*)","Type":"(?P<type>[^"]*)"'
)


def doe_technical_standards(cfg: Mapping[str, Any], env: DiscoverEnv) -> Iterator[Candidate]:
    """DOE's technical standards (``url``: energy.gov's current-standards page, whose table is a JSON
    list in the page): each standard's article page, resolved to its PDF (``detail_documents``)."""
    page = env.get_text(_s(cfg.get("url")) or "https://www.energy.gov/ehss/technical-standards-current")
    detail_docs = _s(cfg.get("detail_documents")) or r"/sites/default/files/.+\.pdf$"
    seen = 0
    for m in _DOE_RECORD.finditer(page):
        title_html = json.loads(f'"{m.group("title")}"')
        node = re.search(r'href="(/node/\d+|/ehss/articles/[^"]+)"', title_html)
        if node is None:
            continue
        seen += 1
        number = m.group("id").split(" (")[0].strip()
        yield Candidate(
            key=make_key(env.prefix, number),
            url=urljoin("https://www.energy.gov/", node.group(1)),
            title=f"{number}: {html_unescape(re.sub(r'<[^>]+>', '', title_html)).strip()}",
            issuer=env.issuer,
            pub_number=number,
            subjects=(m.group("type"),) if m.group("type") else (),
            landing_url=urljoin("https://www.energy.gov/", node.group(1)),
            discovered_by="doe-technical-standards",
            resolve="detail-page",
            extra={"detail_documents": detail_docs},
        )
    env.listing_end("doe-technical-standards", stopped=False, seen=seen)


_ITU_REC = re.compile(r'parent=(?P<id>[TR]-REC-[A-Z]+\.[0-9A-Za-z.]+)"')
_ITU_ROW = re.compile(
    r'parent=(?P<id>[TR]-REC-[^"&]+)"[^<]*<strong>(?P<label>[^<]+)</strong>.*?</td>\s*<td[^>]*>(?P<title>.*?)</td>'
    r"\s*<td[^>]*>\s*(?P<status>[A-Za-z ]+)",
    re.S,
)


def itu_recommendations(cfg: Mapping[str, Any], env: DiscoverEnv) -> Iterator[Candidate]:
    """ITU-T or ITU-R Recommendations in force (``sector``: ``T`` or ``R``; ``series``: the letters):
    each series page lists its Recommendations, each Recommendation's page its versions with their
    status; every version (and amendment, appendix) "In force" is a document, in English."""
    sector = _s(cfg.get("sector")).upper() or "T"
    seen = 0

    def text(url: str) -> str:  # ITU's pages are Windows-1252
        return html_unescape(env.fetcher.get_bytes(url).decode("cp1252", "replace"))

    # A Recommendation page that fails (a 503 among some 5,000, 2026-10-21) is asked again at the end
    # of the run; failing then too, the listing is incomplete as usual.
    later: list[tuple[str, str]] = []

    def pages() -> Iterator[tuple[str, str, str]]:
        for letter in _strs(cfg.get("series")):
            series = text(f"https://www.itu.int/rec/{sector}-REC-{letter}/en")
            for rec in dict.fromkeys(m.group("id") for m in _ITU_REC.finditer(series)):
                try:
                    yield letter, rec, text(f"https://www.itu.int/rec/{rec}/en")
                except FetchError:
                    later.append((letter, rec))
        for letter, rec in later:
            yield letter, rec, text(f"https://www.itu.int/rec/{rec}/en")

    for letter, rec, page in pages():
        for row in _ITU_ROW.finditer(page):
            if not row.group("status").strip().lower().startswith("in force"):
                continue
            vid = row.group("id")
            if sector == "R":
                url = f"https://www.itu.int/dms_pubrec/itu-r/rec/{letter.lower()}/{vid}!!PDF-E.pdf"
            else:
                url = f"https://www.itu.int/rec/dologin_pub.asp?lang=e&id={vid}!!PDF-E&type=items"
            label = " ".join(row.group("label").split())
            title = " ".join(re.sub(r"<[^>]+>", " ", row.group("title")).split()).split(" Note - ")[0]
            seen += 1
            yield Candidate(
                key=make_key(env.prefix, vid.replace("!", "-")),
                url=url,
                title=f"ITU-{sector} {label}: {title}" if title else f"ITU-{sector} {label}",
                issuer=env.issuer,
                pub_number=f"ITU-{sector} {label}",
                edition_date=_itu_date(vid),
                landing_url=f"https://www.itu.int/rec/{rec}/en",
                discovered_by=f"itu:{sector}-{letter}",
                extra={"suffix": ".pdf"},
            )
    env.listing_end(f"itu-{sector}", stopped=False, seen=seen)


def _itu_date(vid: str) -> str:
    """``T-REC-G.711-198811-I``: November 1988."""
    m = re.search(r"-(\d{4})(\d{2})-[A-Z]", vid)
    return f"{m.group(1)}-{m.group(2)}" if m else ""


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
    "crossref": crossref,
    "dla-assist": dla_assist,
    "usgs-pubs": usgs_pubs,
    "govuk-search": govuk_search,
    "dspace7": dspace7,
    "wbdg": wbdg,
    "etsi": etsi,
    "doe-technical-standards": doe_technical_standards,
    "itu-recommendations": itu_recommendations,
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
    # Where the objects are read when that is not the bucket: OpenHistoricalMap's bucket still lists
    # but answers 403 to reads; planet.openhistoricalmap.org serves the same keys (2026-10-21).
    files = _s(cfg.get("download_url")).rstrip("/") or bucket
    prefix = _s(cfg.get("prefix"))
    # ``partition_sep``: the character the bucket's folder names use where a partition name may only
    # have "-" (NARA's "rg_56/" is partition rg-56: partition names refused "rg_56", 2026-10-24).
    sep = _s(cfg.get("partition_sep"))
    if env.partition and cfg.get("partition_prefix"):
        # list one partition, not the whole bucket: NARA's "descriptions/record-groups/{partition}/"
        folder = env.partition.replace("-", sep) if sep else env.partition
        prefix = _s(cfg["partition_prefix"]).format(partition=folder)
    elif env.partition and cfg.get("partition_in_prefix"):
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
            if sep:
                part = part.replace(sep, "-")
            yield Candidate(
                key=make_key(
                    env.prefix,
                    key.removeprefix(_s(cfg.get("prefix")) if cfg.get("partition_prefix") else prefix),
                ),
                url=f"{files}/{quote(key)}",
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


def stored_candidates(cfg: Mapping[str, Any], env: DiscoverEnv) -> Iterator[Candidate]:
    """Candidates recorded earlier (``items``, :meth:`Candidate.to_json`): the files the owner downloaded
    by hand (``wsp collections manual``), whose listing the library may not read. No request is made,
    and the listing is a sample: what it lacks is never inferred withdrawn."""
    items = cast(list[Mapping[str, Any]], cfg.get("items") or [])
    env.sampled.append(f"candidates: {len(items)} file(s) downloaded by hand, not the issuer's listing")
    for raw in items:
        yield Candidate.from_json(raw)


def fixed_list(cfg: Mapping[str, Any], env: DiscoverEnv) -> Iterator[Candidate]:
    items = cast(list[Json], cfg.get("items") or ([{"url": cfg["url"]}] if cfg.get("url") else []))
    for it in items:
        urls = _strs(it.get("urls")) or _strs(it.get("url"))
        # ``landing`` alone: a page where a person finds the file (a store page, a Box share), saved
        # as ``save_as`` by the owner's daily downloads (wsp_cli.manual). With ``resolve_landing``
        # (a host that allows us) the library opens the page and takes its first document link
        # (``detail_documents``, default a PDF) instead: a landing page fetched as the document made
        # NRC's NUREG-1350 an HTML page that differed on every download (2026-10-24).
        page = not urls and bool(_s(it.get("landing")))
        resolve_landing = page and bool(cfg.get("resolve_landing"))
        if page:
            urls = (_s(it.get("landing")),)
        if not urls:
            continue
        pub = _s(it.get("pub_number"))
        title = _s(it.get("title")) or pub or title_from_url(urls[0])
        edition = _s(it.get("edition_date"))
        # The stored file's extension when the URL has none (GEBCO's ".../gebco_2024/zip/").
        suffix = _s(it.get("suffix"))
        extra = {"suffix": suffix} if re.fullmatch(r"\.[a-z0-9]{1,8}", suffix) else {}
        if save_as := _s(it.get("save_as")):  # the name a manual download is saved under
            extra["save_as"] = save_as
        if resolve_landing:
            extra["detail_documents"] = _s(cfg.get("detail_documents")) or r"\.pdf(?:$|[?#])"
        elif page:
            extra["page"] = "1"
        yield Candidate(
            key=pub_key(env.prefix, pub, edition, key_stem_from_url(urls[0], decode=env.decode_url_keys)),
            url=urls[0],
            title=title,
            issuer=env.issuer,
            pub_number=pub,
            edition_date=edition,
            mirrors=urls[1:],
            size=int(it["size"]) if isinstance(it.get("size"), int) else None,
            landing_url=_s(it.get("landing")),
            discovered_by="list",
            resolve="detail-page" if resolve_landing else "",
            extra=extra,
        )


def _csv_date(text: str) -> str:
    """A catalogue's date column: a bare year of any century (TCP's books are 1473-1800, which
    ``iso_date`` does not take from running text), else the first date ``iso_date`` finds."""
    text = text.strip()
    return text if re.fullmatch(r"1[0-9]\d\d|20\d\d", text) else iso_date(text)


def csv_list(cfg: Mapping[str, Any], env: DiscoverEnv) -> Iterator[Candidate]:
    """A published CSV of documents (EveryCRSReport's reports.csv): ``url`` the CSV; ``base`` what its
    paths are relative to; columns named by ``key``, ``file`` (tried in order: ``[latestHTML,
    latestPDF]``), ``title``, ``date``. ``url_template`` builds the URL from the row's columns where
    the CSV names no file (``.../{TCP}/master/{TCP}.xml``); ``partition`` likewise names each row's
    partition (``"{TCP[0]}"``, lower-cased): only that partition's rows are listed when it is fetched.

    A ``.zip`` URL is a zipped CSV (its first ``.csv`` member: Aozora Bunko's catalogue). ``where``
    (``{column: value}``) keeps the rows that match (Aozora's ``作品著作権フラグ: なし``, the works out
    of copyright); a key listed again (one row per author or translator) is listed once;
    ``title_template`` builds the title from the row's columns; ``language`` is the documents'."""
    url = _s(cfg.get("url"))
    if url.lower().endswith(".zip"):
        raw = env.fetcher.get_bytes(url, max_bytes=256 * 1024 * 1024)
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            member = next((n for n in zf.namelist() if n.lower().endswith(".csv")), None)
            if member is None:
                raise DiscoveryError(f"{url}: no CSV in the zip")
            text = zf.read(member).decode("utf-8-sig", "replace")
    else:
        text = env.get_text(url)
    base = _s(cfg.get("base"))
    files = _strs(cfg.get("file"))
    template = _s(cfg.get("url_template"))
    part_template = _s(cfg.get("partition"))
    title_template = _s(cfg.get("title_template"))
    where = {str(k): str(v) for k, v in cast(Mapping[str, Any], cfg.get("where") or {}).items()}
    lang = _s(cfg.get("language"))
    rows = [
        r
        for r in csv.DictReader(io.StringIO(text))
        if all((r.get(k) or "").strip() == v for k, v in where.items())
    ]
    env.totals["csv"] = len(rows)
    seen: set[str] = set()
    for row in rows:
        key = (row.get(_s(cfg.get("key"))) or "").strip()
        if not key or key in seen:
            continue
        seen.add(key)
        try:
            path = template.format_map(row) if template else next((row[c] for c in files if row.get(c)), "")
            part = part_template.format_map(row).lower() if part_template else ""
        except (KeyError, IndexError):
            continue  # a row without the columns the templates name
        if not path or not key or (env.partition and part != env.partition):
            continue
        url = path if path.startswith("https://") else base.rstrip("/") + "/" + path.lstrip("/")
        try:
            title = title_template.format_map(row) if title_template else ""
        except (KeyError, IndexError):
            title = ""
        cand = Candidate(
            key=make_key(env.prefix, key),
            url=url,
            title=" ".join((title or row.get(_s(cfg.get("title"))) or key).split()),
            issuer=env.issuer,
            pub_number=key,
            edition_date=_csv_date(row.get(_s(cfg.get("date"))) or ""),
            discovered_by="csv",
            partition=part,
        )
        yield replace(cand, language=lang) if lang else cand


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
    "sitemap": sitemap,
    "dspace-sitemap": dspace_sitemap,
    "document": fixed_list,
    "csv": csv_list,
    "candidates": stored_candidates,
    "latest-dated": latest_dated,
}

Resolver = Callable[[Candidate, DiscoverEnv], list[Candidate]]
RESOLVERS: dict[str, Resolver] = {
    "detail-page": resolve_detail_page,
    "govuk-content": resolve_govuk_content,
    "dspace7": resolve_dspace7,
    "wbdg": resolve_wbdg,
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
