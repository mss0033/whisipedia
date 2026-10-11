"""Text normalization and per-language analyzers shared by the lexical index and suggest, and the
per-language layout of a Refinery generation (for samples and catalogs that must not follow storage
order)."""

from __future__ import annotations

import re
import unicodedata
from typing import TYPE_CHECKING, Any, cast

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
from the Refinery (remove ``<index>/lexical`` and run ``wsp index build``); the lexical index should
record this number in its schema signature so a mismatch is refused instead of silently missing."""

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


def drop_stopwords(query: str, stopwords: frozenset[str] | None = None) -> str:
    """The query for BM25; function words are only removed when ``stopwords`` is given.

    Default: every word is kept. Dropping English function words broke titles and phrases ("Doctor
    Who" -> "Doctor", "To Be or Not to Be" -> "Not", "history of the internet"), and IDF already
    discounts them. On 40 title and question queries over 0.95M English Wikipedia articles
    (2026-10-11) keeping them raised MRR@10 from 0.483 to 0.761 and success@1 from 0.400 to 0.650
    (the cost: 13 -> 23 ms per snippet-free query at that size; see the rank-oracle audit).

    With ``stopwords``: those words are removed unless the query has a quoted phrase or nothing else
    would remain.
    """
    if stopwords is None or '"' in query:
        return query
    words = query.split()
    kept = [w for w in words if w.casefold() not in stopwords]
    return " ".join(kept) if kept else query


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
