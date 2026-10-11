"""Deterministic query router (PLAN §9.3: rules, < 5 ms).

Classifies a query into one intent and rewrites it per retriever:

==========  =======================================  =============================================
intent      triggers (en, plus de/fr/es/it/nl/...)    rewrite
==========  =======================================  =============================================
navigate    ``wsp:`` ids, ``<site> <id>`` (rfc 9110),  ``site`` = the named site, ``title`` = the rest
            ``Q42``, ``wikipedia Alan Turing``
define      define X, definition/meaning of X,        lexical = X, ``title`` = X, prefers dictionaries
            what does X mean
locate      map of X, where is X, X on a map          lexical = X, ``title`` = X, prefers map sources
compare     X vs Y, compare X and Y, difference       ``parts`` = [X, Y] (each gets a slot in the top)
            between X and Y
how-to      how to/do/does/can ..., wie ..., comment  question words dropped for lexical
lookup      up to three words, no question words      ``title`` = the query (exact title wins)
ask         anything else                             question words dropped for lexical
==========  =======================================  =============================================

The dense retriever always gets the original wording; the lexical one gets the rewrite without
question scaffolding, since BM25 ORs every term. Navigational detection complements the API's
exact ``navigate_to`` resolver: the router says "this looks like a page name", the resolver checks
whether exactly that page exists.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum


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
    """Rewritten text for BM25 (question scaffolding removed)."""
    dense: str
    """Text for the embedder (the original wording)."""
    title: str = ""
    """A probable exact page title; an exact title match is promoted to the top."""
    site: str = ""
    """A site named in the query (``rfc``, ``wikipedia``); restricts sources when it resolves."""
    parts: tuple[str, ...] = ()
    """Compared entities (``compare`` only)."""
    prefer: tuple[str, ...] = field(default=())
    """Source-id fragments to boost (``wiktionary`` for define, ``osm``/``maps`` for locate)."""


# Question scaffolding and function words, removed from the lexical query only. Content words
# ("work", "cause") stay: they carry meaning for BM25.
_STOP = frozenset(
    [
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "been",
        "by",
        "can",
        "could",
        "did",
        "do",
        "does",
        "for",
        "from",
        "had",
        "has",
        "have",
        "how",
        "i",
        "in",
        "into",
        "is",
        "it",
        "its",
        "me",
        "my",
        "of",
        "on",
        "or",
        "our",
        "should",
        "that",
        "the",
        "their",
        "them",
        "then",
        "there",
        "these",
        "this",
        "those",
        "to",
        "was",
        "we",
        "were",
        "what",
        "when",
        "where",
        "which",
        "who",
        "whom",
        "whose",
        "why",
        "will",
        "with",
        "would",
        "you",
        "your",
        "about",
        "tell",
        "explain",
        "please",
        "der",
        "die",
        "das",
        "ein",
        "eine",
        "einen",
        "und",
        "ist",
        "sind",
        "war",
        "wie",
        "was",
        "wer",
        "wo",
        "warum",
        "wann",
        "welche",
        "welcher",
        "des",
        "dem",
        "den",
        "im",
        "in",
        "zu",
        "von",
        "mit",
        "auf",
        "für",
        "funktioniert",
        "le",
        "la",
        "les",
        "un",
        "une",
        "des",
        "et",
        "est",
        "sont",
        "qui",
        "que",
        "quoi",
        "où",
        "comment",
        "pourquoi",
        "quand",
        "quel",
        "quelle",
        "du",
        "de",
        "au",
        "aux",
        "el",
        "los",
        "las",
        "uno",
        "una",
        "y",
        "es",
        "son",
        "quién",
        "qué",
        "dónde",
        "cómo",
        "por",
        "cuál",
        "cuándo",
        "del",
        "al",
        "lo",
        "il",
        "gli",
        "uno",
        "e",
        "è",
        "sono",
        "chi",
        "che",
        "cosa",
        "dove",
        "come",
        "perché",
        "quando",
        "quale",
        "della",
        "di",
        "het",
        "een",
        "en",
        "is",
        "zijn",
        "wie",
        "wat",
        "waar",
        "hoe",
        "waarom",
        "wanneer",
        "welke",
        "van",
        "jest",
        "są",
        "kto",
        "co",
        "gdzie",
        "jak",
        "dlaczego",
        "kiedy",
        "który",
        "była",
        "był",
        "это",
        "что",
        "кто",
        "где",
        "как",
        "почему",
        "когда",
        "какой",
        "är",
        "och",
        "en",
        "ett",
        "vad",
        "vem",
        "var",
        "hur",
        "varför",
        "när",
        "vilken",
    ]
)

_NAV_ID = re.compile(r"^wsp:(//)?[a-z0-9][a-z0-9.-]*/\S")
_QID = re.compile(r"^Q[1-9]\d*$")
_SITE_NUMBER = re.compile(r"^([a-z][a-z.-]*?)[\s#:-]*(\d[\w./-]*)$", re.I)
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
_DEFINE_PREFER = ("wiktionary", "dictionary")
_LOCATE_PREFER = ("osm", "maps", "wikivoyage", "geonames")
_HOW_TO_PREFER = ("wikihow", "wikibooks", "stackexchange")


def content_terms(text: str) -> list[str]:
    """Words worth matching lexically, in order, without duplicates or function words."""
    seen: dict[str, None] = {}
    for w in _WORD.findall(text):
        k = w.casefold().strip("'\u2019-")
        if k and (k not in _STOP or k.isdigit()):
            seen.setdefault(w.strip("'\u2019-"), None)
    return list(seen)


def _lexical(text: str) -> str:
    terms = content_terms(text)
    return " ".join(terms) if terms else text


def _clean(text: str) -> str:
    return text.strip().strip("?!.\"'“”").strip()


class Router:
    """Rule-based intent classification and query rewriting.

    ``sites`` maps a lowercase site word (``rfc``, ``wikipedia``, ``gutenberg``) to the source ids it
    names; :meth:`from_sources` derives it from source ids (first ``-`` part and the full id).
    """

    def __init__(self, sites: Mapping[str, Sequence[str]] | None = None) -> None:
        self.sites: dict[str, tuple[str, ...]] = {k.casefold(): tuple(v) for k, v in (sites or {}).items()}

    @classmethod
    def from_sources(cls, source_ids: Iterable[str]) -> Router:
        sites: dict[str, list[str]] = {}
        for sid in source_ids:
            for key in {sid, sid.split("-")[0], sid.split(".")[0]}:
                sites.setdefault(key.casefold(), []).append(sid)
        return cls(sites)

    def sources_for(self, site: str) -> tuple[str, ...]:
        return self.sites.get(site.casefold(), ())

    def _navigate(self, q: str) -> Route | None:
        if _NAV_ID.match(q) or (_QID.match(q) and "wikidata" in self.sites):
            return Route(Intent.NAVIGATE, q, q, q, title=q)
        head, _, rest = q.partition(" ")
        rest = rest.strip()
        if not rest and (m := _SITE_NUMBER.match(q)):
            head, rest = m.group(1), m.group(2)
        if rest and head.casefold() in self.sites:
            return Route(Intent.NAVIGATE, q, _lexical(rest), rest, title=rest, site=head.casefold())
        return None

    def route(self, query: str) -> Route:
        q = " ".join(query.split())
        if not q:
            return Route(Intent.ASK, query, "", "")
        nav = self._navigate(q)
        if nav is not None:
            return nav
        if m := _DEFINE.match(q):
            x = _clean(m.group("x") or m.group("y") or "")
            if x:
                return Route(Intent.DEFINE, q, _lexical(x), q, title=x, prefer=_DEFINE_PREFER)
        if m := _LOCATE.match(q):
            x = _clean(m.group("x") or m.group("y") or "")
            if x:
                return Route(Intent.LOCATE, q, _lexical(x), q, title=x, prefer=_LOCATE_PREFER)
        if m := _COMPARE.match(q):
            pair = [g for g in m.groups() if g]
            if len(pair) == 2:
                a, b = _clean(pair[0]), _clean(pair[1])
                a = re.sub(r"^compare\s+", "", a, flags=re.I)
                if a and b and len(a.split()) <= 6 and len(b.split()) <= 6:
                    return Route(Intent.COMPARE, q, _lexical(f"{a} {b}"), q, parts=(a, b))
        if _HOW_TO.match(q):
            return Route(Intent.HOW_TO, q, _lexical(q), q, prefer=_HOW_TO_PREFER)
        if not _QUESTION.search(q) and len(q.split()) <= 3:
            return Route(Intent.LOOKUP, q, q, q, title=_clean(q))
        return Route(Intent.ASK, q, _lexical(q), q)
