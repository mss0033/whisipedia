"""Deterministic query router (PLAN §9.3: rules, < 5 ms).

Classifies a query into one intent and rewrites it per retriever:

==========  =======================================  =============================================
intent      triggers (en, plus de/fr/es/it/nl/...)    rewrite
==========  =======================================  =============================================
navigate    ``wsp:`` ids, ``<site> <id>`` (rfc 9110),  ``site`` = the named site, ``title`` = the id,
            ``Q42``                                   searched only there (``scoped``)
define      define X, definition/meaning of X,        lexical = X, ``title`` = X, prefers dictionaries
            what does X mean
locate      map of X, where is X, X on a map          lexical = X, ``title`` = X, prefers map sources
compare     X vs Y, compare X and Y, difference       ``parts`` = [X, Y] (each gets a slot in the top)
            between X and Y
how-to      how to/do/does/can ..., wie ..., comment  the query (see "Function words" below)
lookup      up to three words, no question words      ``title`` = the query (exact title wins)
ask         anything else                             the query
==========  =======================================  =============================================

**Sites.** A *site word* names a group of sources (``rfc``, ``wikipedia``, ``gutenberg``). Site words
are explicit: the built-in :data:`SITE_WORDS` (proper names and acronyms only, matched against the
library's source ids), the source ids that contain a ``-`` (``who-iris``, ``us-code``: never a word
someone types by accident), and per-source ``site_words`` from the registry. They are never derived
from fragments of source ids (``who-iris`` must not make "who invented the telephone" a WHO IRIS
query) and never a function word of any language. A site word followed by one identifier (a token
with a digit: ``rfc 9110``, ``arxiv 2101.00001``, ``pmid 31452104``) *scopes* the search to that site;
followed by anything else (``wikipedia alan turing``, ``gutenberg printing press``) it is only a soft
prior: the query is routed as if the site word were absent, searched everywhere with its full text,
and the site's sources are preferred (``prefer``).

**Function words.** The lexical rewrite is the query itself: BM25's IDF already discounts "the",
"of" and "who", and dropping them breaks titles ("Doctor Who", "The Who", "To Be or Not to Be") and
content words that are function words in another language ("el nino", "vitamin e"). Measured on 40
title and question queries against 0.8M English Wikipedia articles (2026-10-11, see
``docs/audits/2026-10-11/rank-oracle.md``), keeping every word scored best. :func:`content_terms`
remains for callers that need a short keyword form (the site fallback); it drops only the function
words of the query's own language, never a capitalized word of a title-cased query, and nothing when
function words are most of the query (a title or a quotation).

The dense retriever always gets the original wording. Navigational detection complements the API's
exact ``navigate_to`` resolver: the router says "this looks like a page name", the resolver checks
whether exactly that page exists.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from fnmatch import fnmatchcase


class Intent(StrEnum):
    LOOKUP = "lookup"
    DEFINE = "define"
    HOW_TO = "how-to"
    NAVIGATE = "navigate"
    LOCATE = "locate"
    COMPARE = "compare"
    ASK = "ask"


@dataclass(frozen=True, slots=True)
class Route:
    intent: Intent
    query: str
    lexical: str
    """Text for BM25."""
    dense: str
    """Text for the embedder (the original wording)."""
    title: str = ""
    """A probable exact page title; an exact title match is promoted to the top."""
    site: str = ""
    """A site named in the query (``rfc``, ``wikipedia``)."""
    scoped: bool = False
    """True when ``site`` restricts the search (``<site> <identifier>``); False: a soft prior."""
    parts: tuple[str, ...] = ()
    """Compared entities (``compare`` only)."""
    prefer: tuple[str, ...] = field(default=())
    """Source-id fragments to boost (``wiktionary`` for define, ``osm``/``maps`` for locate)."""
    prefer_sources: tuple[str, ...] = ()
    """Exact source ids to boost: the sources of a site named as a soft prior (``wikipedia ...``)."""


# --- site words -------------------------------------------------------------------------------------

SITE_WORDS: Mapping[str, tuple[str, ...]] = {
    # word -> glob patterns over source ids. Proper names and acronyms only: none of these is an
    # English word or a function word of any language in STOPWORDS (tested).
    "rfc": ("rfc", "rfc-*"),
    "wikipedia": ("wikipedia-*", "wikipedia"),
    "wikidata": ("wikidata", "wikidata-*"),
    "wiktionary": ("wiktionary", "wiktionary-*"),
    "wikisource": ("wikisource", "wikisource-*", "kiwix-wikisource-*"),
    "gutenberg": ("gutenberg", "gutenberg-*"),
    "pubmed": ("pubmed", "pubmed-*"),
    "pmid": ("pubmed", "pubmed-*"),
    "pmc": ("pmc-oa",),
    "arxiv": ("arxiv", "arxiv-*"),
    "stackexchange": ("stackexchange", "stackexchange-*"),
    "mdwiki": ("mdwiki",),
    "medlineplus": ("medlineplus",),
    "openalex": ("openalex",),
    "cwe": ("mitre-attack-cwe-capec",),
    "capec": ("mitre-attack-cwe-capec",),
}
"""Built-in site words for the registry's well-known sources (``[sources] site_words`` adds more)."""

# --- function words, per language ---------------------------------------------------------------------


def _words(text: str) -> frozenset[str]:
    return frozenset(text.split())


STOPWORDS: Mapping[str, frozenset[str]] = {
    "en": _words(
        """
        a an and are as at be been by can could did do does for from had has have how i in into is
        it its me my of on or our should that the their them then there these this those to was we
        were what when where which who whom whose why will with would you your about tell explain
        please
        """
    ),
    "de": _words(
        """
        der die das ein eine einen und ist sind war wie was wer wo warum wann welche welcher des dem
        den im in zu von mit auf für
        """
    ),
    "fr": _words(
        """
        le la les un une des et est sont qui que quoi où comment pourquoi quand quel quelle du de au
        aux
        """
    ),
    "es": _words("el los las uno una y es son quién qué dónde cómo por cuál cuándo del al lo"),
    "it": _words("il gli uno e è sono chi che cosa dove come perché quando quale della di"),
    "nl": _words("het een en is zijn wie wat waar hoe waarom wanneer welke van"),
    "pl": _words("jest są kto co gdzie jak dlaczego kiedy który była był"),
    "ru": _words("это что кто где как почему когда какой"),
    "sv": _words("är och en ett vad vem var hur varför när vilken"),
}
"""Question scaffolding and function words by language (used by :func:`content_terms` only)."""

ALL_STOPWORDS: frozenset[str] = frozenset[str]().union(*STOPWORDS.values())

_NAV_ID = re.compile(r"^wsp:(//)?[a-z0-9][a-z0-9.-]*/\S")
_QID = re.compile(r"^Q[1-9]\d*$")
_SITE_NUMBER = re.compile(r"^([a-z][a-z.]*?)[\s#:-]*(\d[\w./-]*)$", re.I)
_IDENTIFIER = re.compile(r"^(?=[^\s]*\d)[\w./:#-]+$")
_DEFINE = re.compile(
    r"^(?:define|definition of|meaning of|what does (?P<x>.+?) mean\??$|what is the meaning of"
    r"|bedeutung von|définition de|definición de|significado de|significato di|betekenis van)\s*(?P<y>.*)$",
    re.I,
)
_LOCATE = re.compile(
    r"^(?:map of|maps? of|where is|where are|location of|directions to|karte von|wo ist|wo liegt"
    r"|carte de|où est|où se trouve|mapa de|dónde está|mappa di|dove si trova|kaart van|waar ligt)"
    r"\s+(?P<x>.+?)\??$"
    r"|^(?P<y>.+?)\s+(?:on a map|map)$",
    re.I,
)
_COMPARE = re.compile(
    r"^(?:compare\s+)?(?P<a>.+?)\s+(?:vs\.?|versus|compared to|compared with)\s+(?P<b>.+?)\??$"
    r"|^compare\s+(?P<c>.+?)\s+(?:and|with|to)\s+(?P<d>.+?)\??$"
    r"|^(?:what is the )?differences? between\s+(?:an? |the )?(?P<e>.+?)"
    r"\s+and\s+(?:an? |the )?(?P<f>.+?)\??$"
    r"|^(?:unterschied zwischen|différence entre|diferencia entre|differenza tra)\s+(?P<g>.+?)"
    r"\s+(?:und|et|y|e)\s+(?P<h>.+?)\??$",
    re.I,
)
_HOW_TO = re.compile(
    r"^(?:how (?:to|do|does|can|is|are|did|should)|wie (?:funktioniert|macht|kann)|comment\b|cómo\b|come\b"
    r"|hoe\b|jak\b|как\b|hur\b)",
    re.I,
)
_QUESTION = re.compile(
    r"^(?:what|who|whom|whose|why|when|where|which|is|are|was|were|do|does|did|can|could|should|would"
    r"|tell me|explain|list)\b|\?$"
    r"|^(?:was|wer|warum|wann|welche|qui|que|quoi|pourquoi|quand|quel|quelle|quién|qué|por qué|cuál"
    r"|chi|che|perché|quale|wat|wie|waarom|kim|kto|co|почему|кто|что|vad|vem|varför)\b",
    re.I,
)
_WORD = re.compile(r"[\w'\u2019-]+", re.UNICODE)
_CYRILLIC = re.compile(r"[\u0400-\u04ff]")
_DEFINE_PREFER = ("wiktionary", "dictionary")
_LOCATE_PREFER = ("osm", "maps", "wikivoyage", "geonames")
_HOW_TO_PREFER = ("wikihow", "wikibooks", "stackexchange")


def query_language(words: Sequence[str]) -> str | None:
    """The language whose function words the query uses most, if that is clear.

    At least two function-word hits are needed (one is too weak: "el nino" is not Spanish scaffolding
    and "vitamin e" is not Italian), and a tie is no answer, except that English wins a tie it is
    part of (the library's interface language).
    """
    folded = [w.casefold() for w in words]
    if any(_CYRILLIC.search(w) for w in folded):
        return "ru"
    hits = {lang: sum(w in stop for w in folded) for lang, stop in STOPWORDS.items()}
    best = max(hits.values(), default=0)
    if best < 2:
        return None
    top = [lang for lang, n in hits.items() if n == best]
    if len(top) == 1:
        return top[0]
    return "en" if "en" in top else None


def content_terms(text: str) -> list[str]:
    """The query's words without the function words of its own language, in order.

    A keyword form for callers that want one (the lexical route keeps every word). Drops only the
    function words of the query's language (:func:`query_language`), and, unless the query is a
    question, only when they are under half of it: "causes of the cold war" loses "of" and "the",
    while "to be or not to be" and "the who" are titles and keep every word. In a title-cased query
    ("Doctor Who") capitalized words are kept too.
    """
    raw = [w.strip("'\u2019-") for w in _WORD.findall(text)]
    raw = [w for w in raw if w]
    lang = query_language(raw)
    stop = STOPWORDS.get(lang, frozenset[str]()) if lang else frozenset[str]()
    question = bool(_QUESTION.search(text) or _HOW_TO.match(text))
    if not question and 2 * sum(w.casefold() in stop for w in raw) >= len(raw):
        return raw
    titled = any(w[:1].isupper() for w in raw[1:])
    return [w for w in raw if w.casefold() not in stop or (titled and w[:1].isupper())]


def _clean(text: str) -> str:
    return text.strip().strip("?!.\"'“”").strip()


def is_identifier(token: str) -> bool:
    """A single token with a digit (``9110``, ``2101.00001``, ``CWE-79``): what a site scope needs."""
    return bool(_IDENTIFIER.match(token))


class Router:
    """Rule-based intent classification and query rewriting.

    ``sites`` maps a lowercase site word to the source ids it names; :meth:`from_sources` builds it
    from the library's source ids (see the module docstring for which words qualify).
    """

    def __init__(self, sites: Mapping[str, Sequence[str]] | None = None) -> None:
        self.sites: dict[str, tuple[str, ...]] = {
            k.casefold(): tuple(dict.fromkeys(v)) for k, v in (sites or {}).items() if v
        }

    @classmethod
    def from_sources(
        cls,
        source_ids: Iterable[str],
        site_words: Mapping[str, Sequence[str]] | None = None,
        *,
        builtin: Mapping[str, Sequence[str]] = SITE_WORDS,
    ) -> Router:
        """Site words for these sources.

        ``site_words`` maps a source id to its extra site words (the registry's ``site_words``);
        a word that is a function word in any language is ignored. ``builtin`` maps a site word to
        glob patterns over source ids (default :data:`SITE_WORDS`).
        """
        ids = list(dict.fromkeys(source_ids))
        sites: dict[str, list[str]] = {}

        def add(word: str, sid: str) -> None:
            w = word.strip().casefold()
            if w and w not in ALL_STOPWORDS and " " not in w:
                bucket = sites.setdefault(w, [])
                if sid not in bucket:
                    bucket.append(sid)

        for sid in ids:
            if "-" in sid:
                add(sid, sid)
        for word, patterns in builtin.items():
            for sid in ids:
                if any(fnmatchcase(sid, p) for p in patterns):
                    add(word, sid)
        for sid, words in (site_words or {}).items():
            if sid in ids:
                for w in words:
                    add(w, sid)
        return cls(sites)

    def sources_for(self, site: str) -> tuple[str, ...]:
        return self.sites.get(site.casefold(), ())

    def _site(self, q: str) -> tuple[str, str] | None:
        """``(site word, rest)`` when the query starts with a site word."""
        head, _, rest = q.partition(" ")
        rest = rest.strip()
        if not rest and (m := _SITE_NUMBER.match(q)):
            head, rest = m.group(1), m.group(2)
        if rest and head.casefold() in self.sites:
            return head.casefold(), rest
        return None

    def route(self, query: str) -> Route:
        q = " ".join(query.split())
        if not q:
            return Route(Intent.ASK, query, "", "")
        if _NAV_ID.match(q) or (_QID.match(q) and "wikidata" in self.sites):
            return Route(Intent.NAVIGATE, q, q, q, title=q)
        named = self._site(q)
        if named is not None:
            site, rest = named
            sources = self.sources_for(site)
            if is_identifier(rest):
                return Route(Intent.NAVIGATE, q, rest, rest, title=rest, site=site, scoped=True)
            inner = self._classify(rest)
            return replace(inner, query=q, lexical=q, dense=q, site=site, prefer_sources=sources)
        return self._classify(q)

    def _classify(self, q: str) -> Route:
        if m := _DEFINE.match(q):
            x = _clean(m.group("x") or m.group("y") or "")
            if x:
                return Route(Intent.DEFINE, q, x, q, title=x, prefer=_DEFINE_PREFER)
        if m := _LOCATE.match(q):
            x = _clean(m.group("x") or m.group("y") or "")
            if x:
                return Route(Intent.LOCATE, q, x, q, title=x, prefer=_LOCATE_PREFER)
        if m := _COMPARE.match(q):
            pair = [g for g in m.groups() if g]
            if len(pair) == 2:
                a, b = _clean(pair[0]), _clean(pair[1])
                a = re.sub(r"^compare\s+", "", a, flags=re.I)
                if a and b and len(a.split()) <= 6 and len(b.split()) <= 6:
                    return Route(Intent.COMPARE, q, f"{a} {b}", q, parts=(a, b))
        if _HOW_TO.match(q):
            return Route(Intent.HOW_TO, q, q, q, prefer=_HOW_TO_PREFER)
        if not _QUESTION.search(q) and len(q.split()) <= 3:
            return Route(Intent.LOOKUP, q, q, q, title=_clean(q))
        return Route(Intent.ASK, q, q, q)
