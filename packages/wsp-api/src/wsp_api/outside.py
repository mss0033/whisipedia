"""Links to the open web that the library holds: an outside URL to the document that copies it.

Pages link to Wikipedia, xkcd, the Python documentation, Stack Overflow, Project Gutenberg or an
RFC by their web address; the library holds all of these, yet such a link read "This site is not in
the library" (2026-10-17 gap census). :class:`OutsideLinks` maps a URL to ``(source id, native id)``:

* **Archived websites** (Kiwix ZIMs), by the site each archive copies (:class:`Site`, read from the
  archive by ``wsp_api.zimread.site_of``): a wiki copy (mwoffliner) by its ``/wiki/<title>`` path, a
  Q&A site (sotoki) by ``/questions/<id>/<slug>``, a crawled website (zimit) by host and path.
* **Other sources** by fixed rules (:data:`RULES`): Gutenberg ebooks, RFCs, PubMed, Open Library,
  CourtListener opinions, the US Code and the eCFR.

Only sources that are searchable and have a current generation are linked (``held``); anything
else stays an outside link, as before.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from urllib.parse import parse_qs, unquote, urlsplit


@dataclass(frozen=True, slots=True)
class Site:
    """The website an archive copies: ``kind`` is ``wiki``, ``qa`` or ``web``; ``book`` is the native
    id prefix of the archive in its source (``"<book>/"`` in a bundle, else ``""``)."""

    kind: str
    host: str
    source_id: str
    book: str = ""


Target = tuple[str, str]  # (source id, native id)
Rule = Callable[[str, str, str], Target | None]  # (host, path, query) -> target


def _gutenberg(host: str, path: str, query: str) -> Target | None:
    if host not in ("gutenberg.org", "gutenberg.net"):
        return None
    m = re.match(r"^/(?:ebooks|files|cache/epub)/(\d+)(?:[/.-]|$)", path)
    return ("gutenberg", m.group(1)) if m else None


def _rfc(host: str, path: str, query: str) -> Target | None:
    if host not in ("rfc-editor.org", "tools.ietf.org", "datatracker.ietf.org", "ietf.org"):
        return None
    m = re.match(r"^/(?:rfc/|html/|doc/html/|doc/|info/)?rfc0*(\d+)(?:\.txt|\.html|\.pdf)?/?$", path, re.I)
    return ("rfc", f"rfc:RFC-{m.group(1)}") if m else None


def _rfc_doi(host: str, path: str, query: str) -> Target | None:
    """An RFC's DOI (``doi.org/10.17487/RFC9293``): Wikipedia cites RFCs by DOI beside the title link."""
    if host not in ("doi.org", "dx.doi.org"):
        return None
    m = re.match(r"^/10\.17487/rfc0*(\d+)/?$", unquote(path), re.I)
    return ("rfc", f"rfc:RFC-{m.group(1)}") if m else None


def _pubmed(host: str, path: str, query: str) -> Target | None:
    if host == "pubmed.ncbi.nlm.nih.gov":
        m = re.match(r"^/(\d+)/?$", path)
    elif host == "ncbi.nlm.nih.gov":
        m = re.match(r"^/pubmed/(\d+)/?$", path)
    else:
        return None
    return ("pubmed", m.group(1)) if m else None


def _open_library(host: str, path: str, query: str) -> Target | None:
    if host != "openlibrary.org":
        return None
    m = re.match(r"^/(books|works|authors)/(OL\d+[MWA])(?:/|$)", path)
    return ("open-library", f"{m.group(1)}/{m.group(2)}") if m else None


def _courtlistener(host: str, path: str, query: str) -> Target | None:
    if host != "courtlistener.com":
        return None
    m = re.match(r"^/opinion/(\d+)(?:/|$)", path)
    return ("courtlistener", m.group(1)) if m else None


def _us_code(host: str, path: str, query: str) -> Target | None:
    if host == "law.cornell.edu":
        m = re.match(r"^/uscode/text/(\d+a?)/([0-9A-Za-z-]+)/?$", path)
    elif host == "uscode.house.gov":
        req = " ".join(parse_qs(query).get("req", []))
        m = re.search(r"title(\d+a?)-section([0-9A-Za-z-]+)", req)
    else:
        return None
    return ("us-code", f"t{m.group(1)}/s{m.group(2)}") if m else None


def _ecfr(host: str, path: str, query: str) -> Target | None:
    if host == "ecfr.gov":
        m = re.match(r"^/current/title-(\d+)/(?:.*/)?section-([0-9A-Za-z.-]+)/?$", path)
    elif host == "law.cornell.edu":
        m = re.match(r"^/cfr/text/(\d+)/([0-9A-Za-z.-]+)/?$", path)
    else:
        return None
    return ("ecfr-federal-register", f"t{m.group(1)}/s{m.group(2)}") if m else None


RULES: tuple[Rule, ...] = (
    _gutenberg,
    _rfc,
    _rfc_doi,
    _pubmed,
    _open_library,
    _courtlistener,
    _us_code,
    _ecfr,
)


def _host(netloc: str) -> str:
    host = netloc.lower().rsplit("@", 1)[-1].split(":", 1)[0]
    return host.removeprefix("www.")


def _wiki_title(path: str, query: str) -> str | None:
    if path.startswith("/wiki/"):
        title = path[len("/wiki/") :]
    elif path in ("/w/index.php", "/index.php"):
        title = " ".join(parse_qs(query).get("title", []))
    elif path.count("/") == 1 and len(path) > 1:  # Appropedia, energypedia: /<Title>
        title = path[1:]
    else:
        return None
    title = unquote(title).replace(" ", "_")
    return title or None


_WIKI_HOST = re.compile(r"^([a-z]{2,3}(?:-[a-z]+)?|simple)\.((?:m\.)?wik[a-z]+\.org)$")


def _language_prefix(host: str, path: str) -> tuple[str, str]:
    """``simple.wikipedia.org/wiki/en:`` is English Wikipedia's main page and ``…/wiki/fr:Paris``
    French Wikipedia's article: Wikimedia's interlanguage links keep the language in the title.
    Simple Wiktionary's front page linked every sister project this way (2026-10-17 trawl)."""
    wiki = _WIKI_HOST.match(host)
    m = re.match(r"^/wiki/([a-z]{2,3}(?:-[a-z]+)?|simple):(.*)$", unquote(path)) if wiki else None
    if wiki is None or m is None:
        return host, path
    return f"{m.group(1)}.{wiki.group(2)}", "/wiki/" + (m.group(2) or "Main_Page")


class OutsideLinks:
    """URL -> library document, for the sources ``held`` says the library can open."""

    def __init__(self, sites: Iterable[Site], held: Callable[[str], bool]) -> None:
        self._held = held
        self._sites: dict[str, Site] = {}
        for site in sites:
            host = _host(site.host)
            self._sites.setdefault(host, site)
            lang, dot, rest = host.partition(".")
            if site.kind == "wiki" and dot and rest.count(".") == 1:  # en.wikipedia.org: en.m.wikipedia.org
                self._sites.setdefault(f"{lang}.m.{rest}", site)

    @property
    def hosts(self) -> int:
        return len(self._sites)

    def site(self, host: str) -> Site | None:
        """The site that copies ``host``: its own, else (a crawled site) its parent domain's, since a
        crawl keeps the subdomains it fetched under their own names (xkcd.com holds imgs.xkcd.com)."""
        host = _host(host)
        found = self._sites.get(host)
        labels = host.split(".")
        for i in range(1, len(labels) - 1):
            if found is not None:
                break
            parent = self._sites.get(".".join(labels[i:]))
            found = parent if parent is not None and parent.kind == "web" else None
        return found

    def target(self, url: str) -> tuple[Target, str] | None:
        """``((source id, native id), fragment)`` of the library's copy of ``url``, or ``None``."""
        try:
            parts = urlsplit(url.strip())
        except ValueError:
            return None
        if parts.scheme not in ("http", "https") or not parts.netloc:
            return None
        host, path, query = _host(parts.netloc), parts.path or "/", parts.query
        for rule in RULES:
            found = rule(host, path, query)
            if found is not None:
                return (found, parts.fragment) if self._held(found[0]) else None
        host, path = _language_prefix(host, path)
        site = self.site(host)
        if site is None or not self._held(site.source_id):
            return None
        native = self._native(site, host, path, query)
        return None if native is None else ((site.source_id, site.book + native), parts.fragment)

    @staticmethod
    def _native(site: Site, host: str, path: str, query: str) -> str | None:
        if site.kind == "wiki":
            return _wiki_title(path, query)
        if site.kind == "qa":
            # sotoki keeps "questions/<id>" as a redirect to the question: any slug, or none, or /q/<id>
            m = re.match(r"^/(?:questions|q)/(\d+)(?:/|$)", path)
            return f"questions/{m.group(1)}" if m else None
        native = (host if _host(site.host) != host else site.host.lower()) + unquote(path)
        return f"{native}?{query}" if query else native
