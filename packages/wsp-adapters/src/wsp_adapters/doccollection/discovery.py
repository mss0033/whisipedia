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
    A public bucket listing (``bucket_url``, ``prefix``, ``key_pattern``); ETag = MD5 when single-part.
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

import json
import logging
import re
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date
from typing import Any, cast
from urllib.parse import quote, urlencode, urljoin, urlsplit
from xml.etree import ElementTree as ET

from selectolax.lexbor import LexborHTMLParser, LexborNode

from wsp_adapters.doccollection.model import (
    Candidate,
    is_generic_title,
    make_key,
    pub_key,
    title_from_url,
)
from wsp_adapters.doccollection.policy import DocFacts, Rules, Verdict, classify_metadata
from wsp_core.errors import FetchError
from wsp_core.model import FetchResult, RemoteFile, RemoteHead
from wsp_core.ports import Fetcher, SupportsHead

log = logging.getLogger(__name__)

Json = dict[str, Any]


class DiscoveryError(RuntimeError):
    pass


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
        self._last: dict[str, float] = {}
        self.requests: dict[str, int] = {}

    def _wait(self, url: str) -> None:
        host = urlsplit(url).netloc.lower()
        gap = max(self.interval, self.per_host.get(host, 0.0))
        last = self._last.get(host)
        if last is not None:
            delay = gap - (self.clock() - last)
            if delay > 0:
                self.sleep(delay)
        self._last[host] = self.clock()
        self.requests[host] = self.requests.get(host, 0) + 1

    def get_bytes(self, url: str, *, max_bytes: int = 64 * 1024 * 1024) -> bytes:
        self._wait(url)
        return self.inner.get_bytes(url, max_bytes=max_bytes)

    def download(self, remote: RemoteFile, dest: Any) -> FetchResult:
        self._wait(remote.url)
        return self.inner.download(remote, dest)

    def head(self, url: str) -> RemoteHead:
        if not isinstance(self.inner, SupportsHead):
            raise FetchError(f"HEAD not supported by {type(self.inner).__name__}")
        self._wait(url)
        return self.inner.head(url)


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
    parts = urlsplit(url)
    if parts.scheme != "https":
        return False
    host = parts.netloc.lower()
    return not origins or any(host == o or host.endswith("." + o) for o in origins)


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
        except FetchError as exc:
            env.notes.append(f"{label}: {page}: {exc}")
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
                seen_docs.add(url)
                title = inherited.get("title") if is_generic_title(text) else text
                title = title or title_from_url(url)
                pub = inherited.get("pub_number") or _pub_number(pub_rx, text, title, url)
                edition = inherited.get("edition_date") or (
                    iso_date(f"{text} {url}") if cfg.get("dates") else ""
                )
                yield Candidate(
                    key=pub_key(env.prefix, pub, edition, title_from_url(url)),
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
        env.scale[label] = (visited + pending) / max(visited, 1)


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
    for row in tree.css(_s(cfg.get("rows")) or "tr"):
        cells = row.css("td")
        if not cells:
            continue
        fields = {name: _node_text(cells[i]) for name, i in columns.items() if 0 <= int(i) < len(cells)}
        link = row.css_first("a[href]")
        if link is None:
            continue
        href = urljoin(page, (link.attributes.get("href") or "").strip())
        pub = fields.get("pub_number", "")
        title = fields.get("title", "") or pub
        edition = iso_date(fields.get("date", ""))
        cand = Candidate(
            key=pub_key(env.prefix, pub, edition, title_from_url(href)),
            url=href,
            title=title.title() if title.isupper() else title,
            issuer=env.issuer,
            pub_number=pub,
            edition_date=edition,
            landing_url=href,
            status=fields.get("status", ""),
            discovered_by=_s(cfg.get("label")) or "index-page:rows",
            resolve="detail-page" if detail_docs else "",
            extra={"detail_documents": detail_docs} if detail_docs else {},
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
    by_pub: dict[int, list[Json]] = {}
    for it in items:
        by_pub.setdefault(int(it.get("pubDownloadId") or 0), []).append(it)
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


def govinfo(cfg: Mapping[str, Any], env: DiscoverEnv) -> Iterator[Candidate]:
    """GovInfo collection listing (official GPO API). ``collection``, ``since``, ``package_prefix``."""
    collection = _s(cfg.get("collection")) or "GOVPUB"
    since = _s(cfg.get("since")) or "1990-01-01T00:00:00Z"
    prefixes = _strs(cfg.get("package_prefix"))
    key = env.key(_s(cfg.get("api_key")) or "govinfo")
    url: str | None = f"https://api.govinfo.gov/collections/{collection}/{since}?" + urlencode(
        {"pageSize": 1000, "offsetMark": "*", "api_key": key}
    )
    pages = 0
    while url and pages < env.pages():
        data = cast(Json, env.get_json(url))
        pages += 1
        env.totals[f"govinfo:{collection}"] = int(data.get("count") or 0)
        for pkg in cast(list[Json], data.get("packages") or []):
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


def congress_crs(cfg: Mapping[str, Any], env: DiscoverEnv) -> Iterator[Candidate]:
    """CRS reports listed by the official Congress.gov API (metadata; PDFs live on congress.gov)."""
    key = env.key(_s(cfg.get("api_key")) or "congress")
    offset, pages = 0, 0
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


def ntrs(cfg: Mapping[str, Any], env: DiscoverEnv) -> Iterator[Candidate]:
    """NASA Technical Reports Server search API (``query``; public records with a PDF only)."""
    size, start, pages = 100, 0, 0
    while pages < env.pages():
        params = {"q": _s(cfg.get("query")), "page.size": size, "page.from": start}
        if cfg.get("filters"):
            params.update(cast(Mapping[str, Any], cfg["filters"]))
        data = cast(Json, env.get_json("https://ntrs.nasa.gov/api/citations/search?" + urlencode(params)))
        pages += 1
        env.totals["ntrs"] = int(cast(Json, data.get("stats") or {}).get("total") or 0)
        results = cast(list[Json], data.get("results") or [])
        if not results:
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


def _first_publication_date(pubs: object) -> str:
    if isinstance(pubs, list) and pubs:
        first = cast(list[object], pubs)[0]
        if isinstance(first, dict):
            return _s(cast(Json, first).get("publicationDate"))[:10]
    return ""


def _osti_records(body: bytes) -> list[Json]:
    """OSTI answers JSON or XML depending on the query (no Accept header in the port): read both."""
    text = body.lstrip()
    if text.startswith(b"<"):
        root = ET.fromstring(text)  # noqa: S314 - HTTPS OSTI response; expat rejects external entities
        out: list[Json] = []
        for rec in root.iter("record"):
            row: Json = {child.tag: (child.text or "").strip() for child in rec}
            row["subjects"] = [(s.text or "").strip() for s in rec.iter("subject") if (s.text or "").strip()]
            out.append(row)
        return out
    data: object = json.loads(text or b"[]")
    return cast(list[Json], data) if isinstance(data, list) else []


def osti(cfg: Mapping[str, Any], env: DiscoverEnv) -> Iterator[Candidate]:
    """OSTI.GOV records API (``query`` and optional ``params``); full text through the OSTI purl."""
    page, pages = 1, 0
    while pages < env.pages():
        params: dict[str, Any] = {"rows": 100, "page": page}
        if _s(cfg.get("query")):
            params["q"] = _s(cfg.get("query"))
        params.update(cast(Mapping[str, Any], cfg.get("params") or {}))
        body = env.fetcher.get_bytes("https://www.osti.gov/api/v1/records?" + urlencode(params))
        pages += 1
        records = _osti_records(body)
        if not records:
            break
        for r in records:
            oid = _s(r.get("osti_id"))
            if not oid:
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


def usgs_pubs(cfg: Mapping[str, Any], env: DiscoverEnv) -> Iterator[Candidate]:
    """USGS Publications Warehouse API (``query`` / ``params``); document links only."""
    page, pages = 1, 0
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
            break
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


def govuk_search(cfg: Mapping[str, Any], env: DiscoverEnv) -> Iterator[Candidate]:
    """GOV.UK search API; each result's attachments are resolved through the content API."""
    start, count, pages = 0, 100, 0
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
            break
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
        # HTML-only guidance: store the rendered page itself.
        out.append(cand.with_url(cand.landing_url, rights=f"license:{licence}"))
    return out


def dspace7(cfg: Mapping[str, Any], env: DiscoverEnv) -> Iterator[Candidate]:
    """DSpace 7 discovery search (WHO IRIS, FAO): ``base``, ``queries``; bitstreams resolved later."""
    base = _s(cfg.get("base")).rstrip("/")
    for query in _strs(cfg.get("queries")) or ("*",):
        page, pages = 0, 0
        while pages < env.pages():
            params = {"query": query, "dsoType": "ITEM", "size": 100, "page": page}
            data = cast(Json, env.get_json(f"{base}/server/api/discover/search/objects?" + urlencode(params)))
            pages += 1
            sr = cast(Json, cast(Json, data.get("_embedded") or {}).get("searchResult") or {})
            env.totals[f"dspace7:{query}"] = int(cast(Json, sr.get("page") or {}).get("totalElements") or 0)
            objects = cast(list[Json], cast(Json, sr.get("_embedded") or {}).get("objects") or [])
            if not objects:
                break
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
                )
            page += 1


def resolve_dspace7(cand: Candidate, env: DiscoverEnv) -> list[Candidate]:
    bundles = cast(Json, env.get_json(cand.url))
    for b in cast(list[Json], cast(Json, bundles.get("_embedded") or {}).get("bundles") or []):
        if _s(b.get("name")) != "ORIGINAL":
            continue
        link = _s(cast(Json, cast(Json, b.get("_links") or {}).get("bitstreams") or {}).get("href"))
        if not link:
            continue
        streams = cast(Json, env.get_json(link))
        out: list[Candidate] = []
        for i, bs in enumerate(
            cast(list[Json], cast(Json, streams.get("_embedded") or {}).get("bitstreams") or [])
        ):
            name = _s(bs.get("name"))
            if not name.lower().endswith(".pdf"):
                continue
            content = _s(cast(Json, cast(Json, bs.get("_links") or {}).get("content") or {}).get("href"))
            if not content:
                continue
            md5 = cast(Json, bs.get("checkSum") or {})
            out.append(
                replace(
                    cand.with_url(content),
                    key=f"{cand.key}-{i}" if i else cand.key,
                    size=int(bs["sizeBytes"]) if isinstance(bs.get("sizeBytes"), int) else None,
                    md5=_s(md5.get("value")).lower()
                    if _s(md5.get("checkSumAlgorithm")).upper() == "MD5"
                    else None,
                    language=cand.language,
                )
            )
        return out
    return []


def tnm(cfg: Mapping[str, Any], env: DiscoverEnv) -> Iterator[Candidate]:
    """USGS TNM Access API (``datasets``, ``prod_formats``): one map sheet per candidate."""
    offset, pages, size = 0, 0, 1000
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
            break
        for it in items:
            url = _s(it.get("downloadURL"))
            if not url or not origin_ok(url, env.origins):
                continue
            title = _s(it.get("title"))
            state = m.group(1) if (m := re.search(r",\s*([A-Z]{2})\b", title)) else "XX"
            yield Candidate(
                key=make_key(env.prefix, _s(it.get("sourceId")) or title_from_url(url)),
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
    pages = 0
    while url and pages < env.pages():
        root = ET.fromstring(env.fetcher.get_bytes(url))  # noqa: S314 - HTTPS OAI response; expat rejects external entities
        pages += 1
        for rec in root.iter(f"{_OAI}record"):
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


# --- archive.org -----------------------------------------------------------------------------------


def archive_org(cfg: Mapping[str, Any], env: DiscoverEnv) -> Iterator[Candidate]:
    """archive.org scrape API: ``query``; each item is resolved to its original PDF later.

    ``provenance`` (required in the registry) records why the copy is acceptable: an upload by the
    issuing agency, or an item that mirrors an official release one-to-one (PLAN D11).
    """
    query = _s(cfg.get("query"))
    fields = "identifier,title,date,item_size,subject,creator,description"
    cursor: str | None = None
    pages = 0
    count = int(cfg.get("page_size", 1000))
    while pages < env.pages():
        params = {"q": query, "fields": fields, "count": count}
        if cursor:
            params["cursor"] = cursor
        data = cast(Json, env.get_json("https://archive.org/services/search/v1/scrape?" + urlencode(params)))
        pages += 1
        env.totals["archive-org"] = int(data.get("total") or 0)
        for it in cast(list[Json], data.get("items") or []):
            ident = _s(it.get("identifier"))
            subjects = it.get("subject")
            pub = _pub_number(_s(cfg.get("pub_number")) or None, _s(it.get("title")), ident)
            yield Candidate(
                key=make_key(env.prefix, ident),
                url=f"https://archive.org/metadata/{quote(ident)}/files",
                title=_s(it.get("title")) or ident,
                issuer=env.issuer,
                pub_number=f"{_s(cfg.get('pub_prefix'))}{pub}" if pub else "",
                edition_date=iso_date(_s(it.get("date"))),
                size=int(it["item_size"]) if isinstance(it.get("item_size"), int) else None,
                subjects=_strs(subjects)[:20],
                abstract=_s(it.get("description"))[:1000] if isinstance(it.get("description"), str) else "",
                landing_url=f"https://archive.org/details/{ident}",
                discovered_by="archive-org",
                resolve="archive-org",
                extra={"provenance": _s(cfg.get("provenance")), "creator": _s(it.get("creator"))},
                partition=f"b{pages:04d}",
            )
        cursor = _s(data.get("cursor")) or None
        if env.partition and env.partition.startswith("b") and env.partition <= f"b{pages:04d}":
            break  # the wanted batch has been listed
        if not cursor:
            break


def resolve_archive_org(cand: Candidate, env: DiscoverEnv) -> list[Candidate]:
    """Pick the item's original PDF (``source: original``) with its MD5 and size."""
    data = cast(Json, env.get_json(cand.url))
    ident = cand.landing_url.rsplit("/", 1)[-1]
    files = cast(list[Json], data.get("result") or [])
    pdfs = [f for f in files if _s(f.get("name")).lower().endswith(".pdf")]
    pdfs.sort(key=lambda f: (_s(f.get("source")) != "original", -int(_s(f.get("size")) or 0)))
    if not pdfs:
        return []
    f = pdfs[0]
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
    bucket = _s(cfg.get("bucket_url")).rstrip("/")
    prefix = _s(cfg.get("prefix"))
    if env.partition and cfg.get("partition_in_prefix"):
        prefix += env.partition.upper() + "/"  # list one state, not the whole bucket
    key_rx = re.compile(_s(cfg.get("key_pattern")) or r".", re.IGNORECASE)
    part_rx = re.compile(_s(cfg["partition"])) if cfg.get("partition") else None
    marker = ""
    pages = 0
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
                title=title_from_url(key),
                issuer=env.issuer,
                edition_date=iso_date(title_from_url(key)),
                size=size,
                md5=etag if re.fullmatch(r"[0-9a-f]{32}", etag) else None,
                partition=part,
                discovered_by="s3-listing",
            )
        if root.findtext(f"{_S3}IsTruncated") != "true" or not contents:
            break
        marker = contents[-1].findtext(f"{_S3}Key") or ""


def git_repo(cfg: Mapping[str, Any], env: DiscoverEnv) -> Iterator[Candidate]:
    for repo in _strs(cfg.get("repos")) or _strs(cfg.get("url")):
        m = re.match(r"https://github\.com/([^/]+)/([^/.]+)", repo)
        if not m:
            raise DiscoveryError(f"git discovery supports GitHub repositories, got {repo!r}")
        owner, name = m.groups()
        info = cast(Json, env.get_json(f"https://api.github.com/repos/{owner}/{name}"))
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
            extra={"default_branch": _s(info.get("default_branch"))},
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
        yield Candidate(
            key=pub_key(env.prefix, pub, edition, title_from_url(urls[0])),
            url=urls[0],
            title=title,
            issuer=env.issuer,
            pub_number=pub,
            edition_date=edition,
            mirrors=urls[1:],
            size=int(it["size"]) if isinstance(it.get("size"), int) else None,
            discovered_by="list",
        )


DISCOVERERS: dict[str, Discoverer] = {
    "index-page": index_page,
    "api": api,
    "oai-pmh": oai_pmh,
    "archive-org": archive_org,
    "s3-listing": s3_listing,
    "git": git_repo,
    "list": fixed_list,
    "document": fixed_list,
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
            log.warning("discovery %s failed: %s", kind, exc)


def resolve(cand: Candidate, env: DiscoverEnv) -> list[Candidate]:
    """Turn a landing-page candidate into document candidates (identity when nothing to resolve)."""
    if not cand.resolve or cand.resolve == "git":
        return [cand]
    impl = RESOLVERS.get(cand.resolve)
    if impl is None:
        raise DiscoveryError(f"unknown resolver {cand.resolve!r}")
    return [c for c in impl(cand, env) if origin_ok(c.url, env.origins)]
