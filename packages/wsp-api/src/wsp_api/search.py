"""Small, deterministic search helpers: intent rules, navigational queries and snippet highlights.

The navigational resolver answers "does this query name exactly one page?" using only exact matches:

* a doc id or address: ``wsp:rfc/9110``, ``wsp://rfc/9110``;
* ``<site> <id or title>``: ``rfc 9110``, ``rfc9110``, ``wikipedia Alan Turing``, ``gutenberg 84``,
  where ``<site>`` names a source (see :func:`wsp_api.sites.sources_named`) and the rest is a native id
  (also tried with spaces as ``_`` and with the ZIM ``A/`` namespace) or an exact title;
* a Wikidata QID (``Q42``) when a ``wikidata`` source exists.

No fuzzy matching: a wrong jump is worse than a results page.
"""

from __future__ import annotations

import html
import re
from collections.abc import Callable, Iterable, Sequence
from typing import cast

from wsp_api.schemas import QueryIntent
from wsp_api.services import Services
from wsp_api.sites import sources_named
from wsp_core.ids import DocId, InvalidDocIdError, make_doc_id, parse_doc_id
from wsp_core.model import SearchHit

STOPWORDS = frozenset(
    [
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "how",
        "in",
        "is",
        "it",
        "of",
        "on",
        "or",
        "that",
        "the",
        "to",
        "was",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
        "with",
        "about",
        "does",
        "do",
    ]
)

_INTENTS: tuple[tuple[re.Pattern[str], QueryIntent], ...] = (
    (re.compile(r"^(define|definition of|meaning of)\b", re.I), QueryIntent.DEFINE),
    (re.compile(r"\b(map of|where is|located)\b", re.I), QueryIntent.LOCATE),
    (re.compile(r"^how (to|do|does|can|is)\b", re.I), QueryIntent.HOW_TO),
    (re.compile(r"\b(vs\.?|versus)\b|^compare\b", re.I), QueryIntent.COMPARE),
)
_SITE_NUMBER = re.compile(r"^([a-z][a-z.-]*?)[\s#:-]*(\d[\w./-]*)$", re.I)
_QID = re.compile(r"^Q[1-9]\d*$")


def terms(query: str) -> list[str]:
    seen: dict[str, None] = {}
    for raw in re.split(r"[^\w]+", query.casefold()):
        if len(raw) >= 2 and raw not in STOPWORDS:
            seen[raw] = None
    return list(seen)


def intent(query: str, *, navigational: bool) -> QueryIntent:
    if navigational:
        return QueryIntent.NAVIGATE
    for pattern, kind in _INTENTS:
        if pattern.search(query):
            return kind
    return QueryIntent.LOOKUP if len(query.split()) <= 3 else QueryIntent.ASK


_TAG = re.compile(r"<[^>]+>")


def plain_snippet(snippet: str) -> str:
    """Index snippets carry HTML highlight markup; the API returns plain text plus ``highlights``."""
    return html.unescape(_TAG.sub("", snippet))


def highlights(snippet: str, query_terms: Iterable[str]) -> list[tuple[int, int]]:
    """``[start, end)`` code-point ranges of words starting with a query term (``vaccine`` → ``vaccines``)."""
    words = sorted({t for t in query_terms if t}, key=len, reverse=True)
    if not words:
        return []
    pattern = re.compile(r"(?<!\w)(?:" + "|".join(re.escape(w) for w in words) + r")\w*", re.I)
    return [(m.start(), m.end()) for m in pattern.finditer(snippet)]


def _exists(services: Services, doc_id: DocId) -> bool:
    return services.documents.get(doc_id) is not None


def _title_hits(services: Services, text: str, source_id: str) -> Sequence[SearchHit]:
    """Lexical, snippet-free hits in one source: enough to compare titles (not a ranked search)."""
    cheap = getattr(services.index, "search_hits", None)
    if callable(cheap):
        return cast(Sequence[SearchHit], cheap(text, limit=20, offset=0, sources=[source_id]))
    return services.index.search(text, limit=20, sources=[source_id])


def _native_candidates(rest: str) -> list[str]:
    under = rest.replace(" ", "_")
    upper = under[:1].upper() + under[1:]  # MediaWiki titles: "alan turing" is "Alan_turing", a redirect
    return list(dict.fromkeys([rest, under, upper, f"A/{under}", f"A/{upper}"]))


def navigate_to(
    services: Services, query: str, exists: Callable[[DocId], bool] | None = None
) -> DocId | None:
    """The page a navigational query names, if it exists. ``exists`` defaults to the document store;
    the API also counts pages served straight from a ZIM (E2E-7)."""

    def _check(doc_id: DocId) -> bool:
        return exists(doc_id) if exists is not None else _exists(services, doc_id)

    q = query.strip()
    if q.startswith("wsp:"):
        raw = q.removeprefix("wsp://") if q.startswith("wsp://") else q.removeprefix("wsp:")
        try:
            source_id, native_id = parse_doc_id("wsp:" + raw)
            doc_id = make_doc_id(source_id, native_id)
        except InvalidDocIdError:
            return None
        return doc_id if _check(doc_id) else None
    if _QID.match(q) and services.source("wikidata") is not None:
        doc_id = make_doc_id("wikidata", q)
        return doc_id if _check(doc_id) else None

    head, _, rest = q.partition(" ")
    rest = rest.strip()
    if not rest and (m := _SITE_NUMBER.match(q)):
        head, rest = m.group(1), m.group(2)
    if not rest:
        return None
    for source_id in sources_named(head, services.sources):
        for native_id in _native_candidates(rest):
            try:
                doc_id = make_doc_id(source_id, native_id)
            except InvalidDocIdError:
                continue
            if _check(doc_id):
                return doc_id
        title = rest.casefold()
        if not any(t.casefold() == title for t in services.index.suggest(rest, limit=20)):
            continue
        for h in _title_hits(services, rest, source_id):
            if h.title.casefold() == title:
                return h.doc_id
    return None
