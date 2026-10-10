"""Text normalization and per-language analyzers shared by the lexical index and suggest."""

from __future__ import annotations

import re
import unicodedata

import tantivy

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

_WS = re.compile(r"\s+")


def primary_lang(lang: str) -> str:
    """``"en-GB"`` -> ``"en"``; empty -> fallback."""
    code = lang.strip().lower().replace("_", "-").split("-", 1)[0]
    return code or FALLBACK_LANG


def build_analyzer(lang: str) -> tantivy.TextAnalyzer:
    """Analyzer for ``lang``: simple tokenizer, lowercase, Snowball stemming if supported, ASCII folding.

    Stemming runs on the accented form (the Snowball rules expect it); folding afterwards makes
    ``café``/``cafe`` match. The same analyzer is applied to queries, so both sides agree.
    """
    builder = (
        tantivy.TextAnalyzerBuilder(tantivy.Tokenizer.simple())
        .filter(tantivy.Filter.remove_long(MAX_TOKEN_LEN))
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
