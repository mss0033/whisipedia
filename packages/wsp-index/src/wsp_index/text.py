"""Text normalization and per-language analyzers shared by the lexical index and suggest, and the
per-language layout of a Refinery generation (for samples and catalogs that must not follow storage
order)."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any, cast

import numpy as np
import pyarrow.compute as pc
import pyarrow.parquet as pq
import tantivy

if TYPE_CHECKING:
    from wsp_index.refinery import RefineryReader

# BCP-47 primary subtag -> Tantivy (Snowball) stemmer name.
STEMMERS: dict[str, str] = {
    "ar": "arabic",
    "da": "danish",
    "de": "german",
    "el": "greek",
    "en": "english",
    "es": "spanish",
    "fi": "finnish",
    "fr": "french",
    "hu": "hungarian",
    "it": "italian",
    "nl": "dutch",
    "no": "norwegian",
    "nb": "norwegian",
    "pt": "portuguese",
    "ro": "romanian",
    "ru": "russian",
    "sv": "swedish",
    "ta": "tamil",
    "tr": "turkish",
}

FALLBACK_LANG = "any"
"""Pseudo-language for documents whose language has no configured analyzer."""

MAX_TOKEN_LEN = 40
"""Longest indexed token, in characters (longer runs are identifiers, hashes and markup debris)."""

ANALYZER_VERSION = 2
"""Version of :func:`build_analyzer`'s token stream. 1: Tantivy's ``simple`` tokenizer with a
40-*byte* limit (split Devanagari at the virama, kept CJK clauses as one token, dropped Cyrillic words
over 20 letters). 2: :data:`TOKEN_PATTERN` for the fallback and Tamil, and a 40-*character* limit.
Queries are analyzed with the current version, so an index built with another one must be rebuilt
from the Refinery: remove ``<index>/lexical`` *and* ``<index>/index-state.json`` (which records what
the lexical index holds; ``wsp index build`` refuses a ``lexical/`` removed alone) and run ``wsp index
build``, or follow docs/runbooks/rebuild-indexes.md. The lexical index records this number in its
schema signature, so a mismatch is refused instead of silently missing."""

_NO_SPACE_SCRIPTS = r"\p{Han}\p{Hiragana}\p{Katakana}\p{Thai}\p{Lao}\p{Khmer}\p{Myanmar}\p{Tibetan}"
TOKEN_PATTERN = rf"[{_NO_SPACE_SCRIPTS}]|[[\p{{Alphabetic}}\p{{N}}\p{{M}}]--[{_NO_SPACE_SCRIPTS}]]+"
"""Tokens of the fallback analyzer: one character of a script written without spaces (Chinese,
Japanese, Thai, ...), else a run of letters, digits and combining marks. Marks keep Devanagari,
Bengali, Tamil, ... words whole (``नमस्ते`` was two tokens); CJK text becomes one token per character,
and a multi-character query word becomes a phrase of them, i.e. an exact substring match (an n-gram
tokenizer cannot be limited to CJK runs in Tantivy's API, and per-character postings need no
dictionary). Tantivy's regex tokenizer is ~3x slower than ``simple`` (11-13 against 34-41 M chars/s
on Wikipedia text), so languages written with spaces and without virama-style marks keep
``simple`` (:data:`SIMPLE_TOKENIZER_LANGS`)."""

SIMPLE_TOKENIZER_LANGS = frozenset(STEMMERS) - {"ta"}
"""Languages whose words ``simple`` already keeps whole (Latin, Cyrillic, Greek and Arabic scripts)."""

# UTF-8 bytes per character of each language's script (Tantivy's remove_long counts bytes).
_SCRIPT_BYTES: dict[str, int] = {"ta": 3}
_DEFAULT_SCRIPT_BYTES = 2  # Latin with diacritics, Cyrillic, Greek, Arabic, Hebrew
_FALLBACK_SCRIPT_BYTES = 3  # any script of the Basic Multilingual Plane (Devanagari, Thai, Hangul)

_WS = re.compile(r"\s+")


def primary_lang(lang: str) -> str:
    """``"en-GB"`` -> ``"en"``; empty -> fallback."""
    code = lang.strip().lower().replace("_", "-").split("-", 1)[0]
    return code or FALLBACK_LANG


def max_token_bytes(lang: str) -> int:
    """Tantivy's byte limit that keeps tokens of up to :data:`MAX_TOKEN_LEN` characters of ``lang``
    (``remove_long`` keeps tokens *shorter* than its limit, in bytes)."""
    per_char = _SCRIPT_BYTES.get(lang, _DEFAULT_SCRIPT_BYTES) if lang in STEMMERS else _FALLBACK_SCRIPT_BYTES
    return MAX_TOKEN_LEN * per_char + 1


def build_analyzer(lang: str) -> tantivy.TextAnalyzer:
    """Analyzer for ``lang``: tokenizer (``simple`` or :data:`TOKEN_PATTERN`), a length limit of
    :data:`MAX_TOKEN_LEN` characters, lowercase, Snowball stemming if supported, ASCII folding
    (version :data:`ANALYZER_VERSION`).

    Stemming runs on the accented form (the Snowball rules expect it); folding afterwards makes
    ``café``/``cafe`` match. The same analyzer is applied to queries, so both sides agree.
    """
    tokenizer = (
        tantivy.Tokenizer.simple()
        if lang in SIMPLE_TOKENIZER_LANGS
        else tantivy.Tokenizer.regex(TOKEN_PATTERN)
    )
    builder = (
        tantivy.TextAnalyzerBuilder(tokenizer)
        .filter(tantivy.Filter.remove_long(max_token_bytes(lang)))
        .filter(tantivy.Filter.lowercase())
    )
    stemmer = STEMMERS.get(lang)
    if stemmer is not None:
        builder = builder.filter(tantivy.Filter.stemmer(stemmer))
    return builder.filter(tantivy.Filter.ascii_fold()).build()


def normalize_key(text: str) -> str:
    """Case- and accent-insensitive key used for title prefix matching.

    NFKD, drop combining marks, casefold, collapse whitespace. Control characters are removed so the
    result can be joined with ``\\x1f`` safely.
    """
    decomposed = _WS.sub(" ", unicodedata.normalize("NFKD", text))
    stripped = "".join(
        ch for ch in decomposed if not unicodedata.combining(ch) and unicodedata.category(ch) != "Cc"
    )
    return _WS.sub(" ", stripped.casefold()).strip()


# Characters with meaning in Tantivy's query grammar. User queries are plain language (PLAN §10.3),
# so these are neutralized; balanced double quotes are kept for phrase search.
_QUERY_SYNTAX = re.compile(r"""[+\-!(){}\[\]^~*?:\\/<>=&|'`]""")


def sanitize_query(query: str) -> str:
    """Turn free text into a safe query string: syntax characters become spaces, quotes balanced."""
    cleaned = _QUERY_SYNTAX.sub(" ", query)
    if cleaned.count('"') % 2:
        cleaned = cleaned.replace('"', " ")
    # Bare boolean operators would be interpreted by the parser; lowercase them to plain words.
    words = [w.lower() if w in ("AND", "OR", "NOT", "IN") else w for w in _WS.split(cleaned.strip())]
    return " ".join(w for w in words if w)


def _words(text: str) -> frozenset[str]:
    return frozenset(text.split())


# English function words, for callers that ask for them: the lexical index keeps them (drop_stopwords).
STOPWORDS_EN = _words(
    """
        a an and are as at be but by can could did do does for from had has have how i if in into is
        it its me my of on or our should so than that the their them then there these they this
        those to was we were what when where which who whom whose why will with would you your
        """
)


# --- function words, per language ---------------------------------------------------------------------


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
"""Question scaffolding and function words by language (:func:`content_terms`)."""

ALL_STOPWORDS: frozenset[str] = frozenset[str]().union(*STOPWORDS.values())

HOW_TO = re.compile(
    r"^(?:how (?:to|do|does|can|is|are|did|should)|wie (?:funktioniert|macht|kann)|comment\b|cómo\b|come\b"
    r"|hoe\b|jak\b|как\b|hur\b)",
    re.I,
)
QUESTION = re.compile(
    r"^(?:what|who|whom|whose|why|when|where|which|is|are|was|were|do|does|did|can|could|should|would"
    r"|tell me|explain|list)\b|\?$"
    r"|^(?:was|wer|warum|wann|welche|qui|que|quoi|pourquoi|quand|quel|quelle|quién|qué|por qué|cuál"
    r"|chi|che|perché|quale|wat|wie|waarom|kim|kto|co|почему|кто|что|vad|vem|varför)\b",
    re.I,
)
_EDGE_PUNCT = "\"'\u2019\u201c\u201d?!.,;:()[]{}-"
_CYRILLIC = re.compile(r"[\u0400-\u04ff]")


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


LONG_QUERY_WORDS = 6


def content_terms(text: str) -> list[str]:
    """The query's words without the function words of its own language, in order.

    A keyword form for callers that want one (the lexical route keeps every word). Drops only the
    function words of the query's language (:func:`query_language`), and, unless the query is a
    question, only when they are under half of it: "causes of the cold war" loses "of" and "the",
    while "to be or not to be" and "the who" are titles and keep every word. In a title-cased query
    ("Doctor Who") capitalized words are kept too.
    """
    # whitespace words, punctuation trimmed: a regex \w would split Devanagari at its marks
    raw = [w.strip(_EDGE_PUNCT) for w in text.split()]
    # A possessive's "s" (the apostrophe is query syntax: "Arrow's" arrives as "Arrow s") is in
    # nearly every document: one of the longest posting lists, for no meaning.
    raw = [w for i, w in enumerate(raw) if w and not (i and w in ("s", "S"))]
    lang = query_language(raw)
    if lang is None and len(raw) >= LONG_QUERY_WORDS and any(w.casefold() in STOPWORDS["en"] for w in raw):
        # One English function word decides a long query: two are asked so that "el nino" is not
        # Spanish scaffolding, but a run of six words or more is no name. "Arrow's impossibility
        # theorem conditions ... independence of irrelevant alternatives" kept "of" and "s" and took
        # 23 s on the full index, 1.8 s without them (2026-10-16 field exam).
        lang = "en"
    stop = STOPWORDS.get(lang, frozenset[str]()) if lang else frozenset[str]()
    question = bool(QUESTION.search(text) or HOW_TO.match(text))
    if not question and 2 * sum(w.casefold() in stop for w in raw) >= len(raw):
        return raw
    titled = any(w[:1].isupper() for w in raw[1:])
    return [w for w in raw if w.casefold() not in stop or (titled and w[:1].isupper())]


def drop_stopwords(query: str, stopwords: frozenset[str] | None = None) -> str:
    """The query for BM25 without the function words of its own language (:func:`content_terms`).

    The old rule, every English stopword out of every query, broke titles and phrases ("Doctor Who"
    -> "Doctor", "To Be or Not to Be" -> "Not") and content words ("el nino", "vitamin e"). Measured
    on 40 title and question queries over all 7.25M English Wikipedia articles (2026-10-11): MRR@10
    0.408 with the old router + lexical lists, 0.756 keeping every word, 0.785 with this rule, which
    also keeps BM25 off the longest posting lists of question scaffolding ("what is the ...").

    With ``stopwords``: exactly those words are removed (unless nothing else would remain). A query
    with a quoted phrase is never changed.
    """
    if '"' in query:
        return query
    if stopwords is None:
        return " ".join(content_terms(query)) or query
    words = query.split()
    kept = [w for w in words if w.casefold() not in stopwords]
    return " ".join(kept) if kept else query


def language_row_groups(reader: RefineryReader) -> dict[str, list[tuple[int, int, int]]]:
    """``{lang: [(part, row group, rows in that language), ...]}`` of a generation, from each part's
    ``lang`` column (read once per part, a few bytes a row).

    Finer than :func:`language_parts`, so a language that has three rows in a part costs reading the
    row groups holding them, not the part: sampling every rare language of Wiktionary by part read
    the whole dump once per language (hours, 2026-10-12).
    """
    out: dict[str, list[tuple[int, int, int]]] = {}
    for part, path in enumerate(reader.parts):
        pf = pq.ParquetFile(path)
        sizes = [pf.metadata.row_group(i).num_rows for i in range(pf.metadata.num_row_groups)]
        col = pq.read_table(path, columns=["lang"]).column(0).combine_chunks()  # pyright: ignore[reportUnknownMemberType]
        encoded = cast(Any, pc).dictionary_encode(col)
        langs = [str(x or "") for x in encoded.dictionary.to_pylist()]
        codes = np.asarray(encoded.indices.to_numpy(zero_copy_only=False), dtype=np.int64)
        start = 0
        for rg, size in enumerate(sizes):
            counts = np.bincount(codes[start : start + size], minlength=len(langs))
            for code in np.flatnonzero(counts).tolist():
                out.setdefault(langs[code], []).append((part, rg, int(counts[code])))
            start += size
    return out


def language_parts(reader: RefineryReader) -> dict[str, list[tuple[int, int]]]:
    """``{lang: [(part, rows in that language), ...]}`` of a generation, from each part's ``lang``
    column (a few bytes a row; 641 wikipedia-top10 parts read in seconds).

    Parts are not language blocks: a multi-language source writes languages roughly in turn, but
    one language's documents spread over parts far apart (German articles sit among the Cebuano
    parts of wikipedia-top10), so per-language work reads these parts, never a storage-order prefix.
    Shadowed rows of a set generation are counted (one in 32M on wikipedia-top10).
    """
    out: dict[str, list[tuple[int, int]]] = {}
    for part, path in enumerate(reader.parts):
        col = pq.read_table(path, columns=["lang"]).column(0)  # pyright: ignore[reportUnknownMemberType]
        vc = cast(Any, pc).value_counts(col)
        for lang, count in zip(vc.field("values").to_pylist(), vc.field("counts").to_pylist(), strict=True):
            out.setdefault(str(lang or ""), []).append((part, int(count)))
    return out
