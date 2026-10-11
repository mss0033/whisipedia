"""Query function words (kept by default) and the per-script analyzers (CJK, Indic, long words)."""

from __future__ import annotations

from pathlib import Path

import pytest

from wsp_index import TantivyConfig, TantivyIndex
from wsp_index.text import (
    ANALYZER_VERSION,
    MAX_TOKEN_LEN,
    STOPWORDS_EN,
    build_analyzer,
    drop_stopwords,
    max_token_bytes,
)
from wsp_index_helpers import doc


def test_only_the_query_language_function_words_go() -> None:
    # the old list broke titles: "Doctor Who" -> "Doctor", "To Be or Not to Be" -> "Not"
    for q in ("Doctor Who", "To Be or Not to Be", "the who", "el nino", "vitamin e deficiency", "नमस्ते दुनिया"):
        assert drop_stopwords(q) == q
    assert drop_stopwords("how do vaccines work") == "vaccines work"
    assert drop_stopwords("causes of the cold war") == "causes cold war"


def test_explicit_stopwords_still_drop() -> None:
    assert drop_stopwords("how do vaccines work", STOPWORDS_EN) == "vaccines work"
    assert drop_stopwords("The Who", STOPWORDS_EN) == "The Who"  # nothing would remain: unchanged
    assert drop_stopwords('"to be or not to be"', STOPWORDS_EN) == '"to be or not to be"'


@pytest.mark.parametrize(
    ("lang", "text", "tokens"),
    [
        ("any", "नमस्ते दुनिया", ["नमस्ते", "दुनिया"]),  # the virama no longer splits words
        ("any", "東京大学へ", ["東", "京", "大", "学", "へ"]),  # one token per character
        ("any", "abc東京", ["abc", "東", "京"]),
        ("any", "한국어 사전", ["한국어", "사전"]),  # Hangul is written with spaces
        ("any", "Straße Café", ["strasse", "cafe"]),
        ("en", "Doctor Who's", ["doctor", "who", "s"]),
        ("ta", "தமிழ்நாடு", ["தமிழ்நாடு"]),
    ],
)
def test_analyzer_tokens(lang: str, text: str, tokens: list[str]) -> None:
    assert build_analyzer(lang).analyze(text) == tokens


def test_length_limit_counts_characters() -> None:
    assert ANALYZER_VERSION == 2
    ru = "\u0430" * MAX_TOKEN_LEN  # 40 Cyrillic letters = 80 bytes: kept (the byte limit dropped 21+)
    assert len(build_analyzer("ru").analyze(ru)) == 1  # (the stemmer may shorten it)
    assert build_analyzer("ru").analyze(ru + "\u0430") == []
    hi = "क" * MAX_TOKEN_LEN  # 120 bytes
    assert build_analyzer("any").analyze(hi) == [hi]
    assert max_token_bytes("en") == 2 * MAX_TOKEN_LEN + 1
    assert max_token_bytes("any") == 3 * MAX_TOKEN_LEN + 1


def test_cjk_and_indic_search(tmp_path: Path) -> None:
    idx = TantivyIndex(tmp_path / "lex", TantivyConfig(writer_heap_bytes=20_000_000, writer_threads=1))
    idx.upsert(
        [
            doc("w", "ja/1", "東京大学", "東京大学は日本の国立大学である。", lang="ja"),
            doc("w", "ja/2", "京都", "京都は日本の都市である。大学が多い。", lang="ja"),
            doc("w", "hi/1", "नमस्ते", "नमस्ते एक अभिवादन है।", lang="hi"),
        ]
    )
    idx.commit()
    # a CJK word is a phrase of characters: 東京大学 matches the first page, not 京都 + 大学
    assert [h.doc_id for h in idx.search("東京大学")] == ["wsp:w/ja/1"]
    assert {h.doc_id for h in idx.search("京都")} == {"wsp:w/ja/2"}
    assert [h.doc_id for h in idx.search("नमस्ते")] == ["wsp:w/hi/1"]
    idx.close()
