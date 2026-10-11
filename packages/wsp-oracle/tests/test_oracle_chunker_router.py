"""Chunker (section-aware passages, stable ids) and router (intents, rewrites)."""

from __future__ import annotations

import pytest
from oracle_helpers import doc

from wsp_oracle.chunker import chunk_document, chunk_text, doc_of, make_chunk_id, split_chunk_id
from wsp_oracle.router import Intent, Router, content_terms


def words(n: int, tag: str = "w") -> str:
    return " ".join(f"{tag}{i}" for i in range(n))


def test_chunk_ids_roundtrip_and_hash_in_doc_id() -> None:
    cid = make_chunk_id("wsp:src/C#_(language)", 3)
    assert split_chunk_id(cid) == ("wsp:src/C#_(language)", 3)
    assert doc_of(cid) == "wsp:src/C#_(language)"
    assert doc_of("wsp:src/C#") == "wsp:src/C#"
    with pytest.raises(ValueError, match="chunk id"):
        split_chunk_id("wsp:src/no-number")


def test_sections_are_respected_and_remembered() -> None:
    text = "\n".join(
        [
            "Topic",
            words(120, "lead") + ".",
            "Early life",
            words(150, "early") + ".",
            "Career",
            words(80, "career") + ".",
        ]
    )
    chunks = chunk_text("Topic", text, target_words=200)
    assert [s for s, _ in chunks] == ["", "Early life", "Career"]
    assert all(not (("early" in t) and ("career" in t)) for _, t in chunks)


def test_long_paragraph_splits_at_sentences_and_bounds() -> None:
    sentence = words(30) + "."
    text = " ".join([sentence] * 30)  # 900 words, one paragraph
    chunks = chunk_text("T", text, target_words=200)
    assert len(chunks) >= 4
    assert all(len(t.split()) <= 300 for _, t in chunks)
    assert all(t.endswith(".") for _, t in chunks)


def test_run_on_text_is_cut_by_words() -> None:
    chunks = chunk_text("T", words(1000), target_words=200)
    assert all(len(t.split()) <= 300 for _, t in chunks)
    assert sum(len(t.split()) for _, t in chunks) == 1000


def test_short_tail_merges_and_reference_sections_drop() -> None:
    text = "\n".join([words(195) + ".", words(10, "tail") + ".", "References", "Smith, J. (2001). A book."])
    chunks = chunk_text("T", text, target_words=200)
    assert len(chunks) == 1
    assert "tail0" in chunks[0][1]
    assert "Smith" not in chunks[0][1]


def test_chunk_document_is_stable_and_prefixes_title() -> None:
    d = doc("encyclo", "X", "Alan Turing", "Early life\n" + words(50) + ".\nWork\n" + words(60) + ".")
    a, b = chunk_document(d), chunk_document(d)
    assert a == b
    assert [c.chunk_id for c in a] == [f"{d.doc_id}#{i}" for i in range(len(a))]
    assert a[0].embed_text().startswith("Alan Turing — Early life\n")


def test_empty_document_yields_title_chunk() -> None:
    d = doc("encyclo", "Empty", "Empty page", "")
    (only,) = chunk_document(d)
    assert only.text == "Empty page"


def test_max_chunks() -> None:
    assert len(chunk_text("T", words(5000), target_words=100, max_chunks=7)) == 7


ROUTER = Router.from_sources(["rfc", "wikipedia-en-zim", "wikidata", "gutenberg"])


@pytest.mark.parametrize(
    ("query", "intent", "lexical", "title"),
    [
        ("define sesquipedalian", Intent.DEFINE, "sesquipedalian", "sesquipedalian"),
        ("what does serendipity mean?", Intent.DEFINE, "serendipity", "serendipity"),
        ("map of Lisbon", Intent.LOCATE, "Lisbon", "Lisbon"),
        ("where is mount kilimanjaro", Intent.LOCATE, "mount kilimanjaro", "mount kilimanjaro"),
        ("rfc 9110", Intent.NAVIGATE, "9110", "9110"),
        ("rfc9110", Intent.NAVIGATE, "9110", "9110"),
        ("wikipedia Alan Turing", Intent.NAVIGATE, "Alan Turing", "Alan Turing"),
        ("wsp:rfc/9110", Intent.NAVIGATE, "wsp:rfc/9110", "wsp:rfc/9110"),
        ("Q42", Intent.NAVIGATE, "Q42", "Q42"),
        ("how do vaccines train the immune system", Intent.HOW_TO, "vaccines train immune system", ""),
        ("Alan Turing", Intent.LOOKUP, "Alan Turing", "Alan Turing"),
        ("why is the sky blue", Intent.ASK, "sky blue", ""),
        ("wie funktioniert die Photosynthese", Intent.HOW_TO, "Photosynthese", ""),
    ],
)
def test_router_intents(query: str, intent: Intent, lexical: str, title: str) -> None:
    r = ROUTER.route(query)
    assert r.intent is intent
    assert r.lexical == lexical
    assert r.title == title
    assert r.dense


@pytest.mark.parametrize(
    ("query", "parts"),
    [
        ("alligator vs crocodile", ("alligator", "crocodile")),
        ("compare mitosis and meiosis", ("mitosis", "meiosis")),
        ("difference between a virus and a bacterium", ("virus", "bacterium")),
        ("weather versus climate", ("weather", "climate")),
    ],
)
def test_router_compare(query: str, parts: tuple[str, str]) -> None:
    r = ROUTER.route(query)
    assert r.intent is Intent.COMPARE
    assert r.parts == parts


def test_router_site_and_preferences() -> None:
    assert ROUTER.route("rfc 9110").site == "rfc"
    assert ROUTER.sources_for("wikipedia") == ("wikipedia-en-zim",)
    assert "wiktionary" in ROUTER.route("define x").prefer
    assert Router().route("Q42").intent is Intent.LOOKUP  # no wikidata source: just a short query
    assert Router().route("   ").intent is Intent.ASK


def test_content_terms_drop_function_words_keep_numbers() -> None:
    assert content_terms("What is the RFC 9110 about?") == ["RFC", "9110"]
