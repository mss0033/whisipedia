"""Instant answers above the results (PLAN §11.3): a dictionary box for "define <word>", and a map for
"where is <place>" (the Atlas's place index, linked to the place's encyclopedia article).

Read from the library itself, the Wiktionary entry for the word, never generated. The refined text of
an entry keeps its section headings on their own lines ("English", "Adjective", "Noun", "Translations")
and each sense as a line below its part of speech; dated quotations follow the sense they illustrate.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence

from wsp_api.schemas import DefinitionAnswer, DefinitionSense, MapAnswer
from wsp_core.gazetteer import Place
from wsp_core.ids import DocId, make_doc_id
from wsp_core.model import DocRecord
from wsp_core.titles import shown_title

DICTIONARY = "wiktionary-en"
MAX_SENSES = 4
MAX_SENSE_CHARS = 300

_DEFINE = re.compile(
    r"^\s*(?:define|definition\s+of|meaning\s+of|what\s+does)\s+(?P<term>[^\W\d_][\w'\u2019 -]{0,48}?)"
    r"(?:\s+mean)?\s*\??\s*$",
    re.IGNORECASE,
)
PARTS_OF_SPEECH = frozenset(
    {
        "Adjective",
        "Adverb",
        "Article",
        "Conjunction",
        "Determiner",
        "Interjection",
        "Noun",
        "Numeral",
        "Particle",
        "Phrase",
        "Prefix",
        "Preposition",
        "Prepositional phrase",
        "Pronoun",
        "Proper noun",
        "Proverb",
        "Suffix",
        "Verb",
    }
)
# Headings that end a part of speech's senses.
_SECTIONS = frozenset(
    {
        "Alternative forms",
        "Anagrams",
        "Antonyms",
        "Coordinate terms",
        "Derived terms",
        "Descendants",
        "Etymology",
        "Further reading",
        "Hypernyms",
        "Hyponyms",
        "Pronunciation",
        "Quotations",
        "References",
        "Related terms",
        "See also",
        "Synonyms",
        "Translations",
        "Usage notes",
    }
)
_QUOTATION = re.compile(
    "^(?:c\\.\\s*)?\\d{3,4}s?\\b|^[\u201c\"\u2018']"
)  # "1830, On the Art of …", "“The fine …"


def defined_term(query: str) -> str | None:
    """The word of a "define …" query, or ``None``."""
    m = _DEFINE.match(query)
    return None if m is None else " ".join(m["term"].split())


def definition(query: str, get: Callable[[DocId], DocRecord | None]) -> DefinitionAnswer | None:
    """The dictionary box for a "define …" query, from the library's Wiktionary entry for the word."""
    term = defined_term(query)
    if term is None:
        return None
    for native in dict.fromkeys((term, term.lower())):  # "define Paris" keeps the capital first
        doc = get(make_doc_id(DICTIONARY, native.replace(" ", "_")))
        if doc is None:
            continue
        senses = senses_of(doc.text)
        if senses:
            return DefinitionAnswer(
                term=doc.title or term, senses=senses, doc_id=doc.doc_id, license=doc.license
            )
    return None


def senses_of(text: str) -> list[DefinitionSense]:
    """The first senses of the entry's first language (English for English Wiktionary)."""
    senses: list[DefinitionSense] = []
    pos: str | None = None
    languages = 0
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line in PARTS_OF_SPEECH:
            pos = line
            continue
        if line in _SECTIONS or line.startswith("Etymology "):
            pos = None
            continue
        if pos is None:
            if _is_language_heading(line):
                languages += 1
                if languages > 1:  # the next language's entry: stop
                    break
            continue
        if _QUOTATION.match(line):
            continue
        senses.append(DefinitionSense(part_of_speech=pos.lower(), definition=_clip(line)))
        if len(senses) >= MAX_SENSES:
            break
    return senses


def _is_language_heading(line: str) -> bool:
    """A language name on a line of its own ("English", "Old French"): short, capitalised, no
    punctuation. Section headings were consumed before this is asked."""
    words = line.split()
    return 0 < len(words) <= 3 and all(w[:1].isupper() and w.isalpha() for w in words)


def _clip(text: str) -> str:
    return text if len(text) <= MAX_SENSE_CHARS else text[: MAX_SENSE_CHARS - 1].rstrip() + "…"


# --- "where is <place>" ------------------------------------------------------------------------------

_LOCATE = re.compile(
    r"^\s*(?:where\s+is|where's|map\s+of|location\s+of|show\s+me)\s+(?P<a>.+?)\s*\??\s*$"
    r"|^\s*(?P<b>.+?)\s+on\s+a\s+map\s*\??\s*$",
    re.IGNORECASE,
)
ENCYCLOPEDIA = "wikipedia-top10"  # its native ids are <lang>/<Title>, the OSM wikipedia tag's form
ZOOM_FOR_KIND = {
    "place=country": 5, "place=state": 6, "place=region": 7, "place=province": 7, "place=county": 9,
    "place=district": 10, "place=city": 11, "place=town": 12, "place=municipality": 12,
    "place=village": 13, "place=island": 11, "place=archipelago": 8,
}  # fmt: skip
PLACE_CANDIDATES = 3


def located_place(query: str) -> str | None:
    """The place of a "where is …", "map of …" or "… on a map" query, or ``None``."""
    m = _LOCATE.match(query)
    if m is None:
        return None
    place = " ".join((m["a"] or m["b"] or "").split()).removeprefix("the ").strip()
    return place or None


def _describe(p: Place) -> str:
    kind = p.kind.partition("=")[2].replace("_", " ").capitalize() or "Place"
    return f"{kind}, population {p.population:,}" if p.population else kind


def map_answer(
    query: str,
    find: Callable[[str], Sequence[Place]],
    get: Callable[[DocId], DocRecord | None],
) -> MapAnswer | None:
    """The map box for a "where is …" query: the gazetteer's best match that has an article in the
    library, which names it (Lisboa is shown as "Lisbon") and is where "The place »" leads. The server
    never answered it before 2026-10-18; only the Navigator's mock did."""
    place = located_place(query)
    if place is None:
        return None
    for p in list(find(place))[:PLACE_CANDIDATES]:
        lang, _, title = (p.wikipedia or "").partition(":")
        if not title:
            continue
        doc = get(make_doc_id(ENCYCLOPEDIA, f"{lang}/{title.replace(' ', '_')}"))
        if doc is None:
            continue
        return MapAnswer(
            place=shown_title(doc.title) or p.name,
            lat=p.lat,
            lon=p.lon,
            zoom=ZOOM_FOR_KIND.get(p.kind, 14),
            description=_describe(p),
            doc_id=doc.doc_id,
            license=doc.license,
        )
    return None
