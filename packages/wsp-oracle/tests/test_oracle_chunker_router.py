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


ROUTER = Router.from_sources(["rfc", "wikipedia-en-zim", "wikidata", "gutenberg", "who-iris", "us-code"])


@pytest.mark.parametrize(
    ("query", "intent", "lexical", "title"),
    [
        ("define sesquipedalian", Intent.DEFINE, "sesquipedalian", "sesquipedalian"),
        ("what does serendipity mean?", Intent.DEFINE, "serendipity", "serendipity"),
        ("map of Lisbon", Intent.LOCATE, "Lisbon", "Lisbon"),
        ("where is mount kilimanjaro", Intent.LOCATE, "mount kilimanjaro", "mount kilimanjaro"),
        ("rfc 9110", Intent.NAVIGATE, "9110", "9110"),
        ("rfc9110", Intent.NAVIGATE, "9110", "9110"),
        ("wsp:rfc/9110", Intent.NAVIGATE, "wsp:rfc/9110", "wsp:rfc/9110"),
        ("Q42", Intent.NAVIGATE, "Q42", "Q42"),
        # a site with words is a soft prior: the rest is routed, the full text searched
        ("wikipedia Alan Turing", Intent.LOOKUP, "wikipedia Alan Turing", "Alan Turing"),
        # every word reaches BM25: function words are part of titles and phrases
        (
            "how do vaccines train the immune system",
            Intent.HOW_TO,
            "how do vaccines train the immune system",
            "",
        ),
        ("Alan Turing", Intent.LOOKUP, "Alan Turing", "Alan Turing"),
        ("why is the sky blue", Intent.ASK, "why is the sky blue", ""),
        ("Doctor Who", Intent.LOOKUP, "Doctor Who", "Doctor Who"),
        ("To Be or Not to Be", Intent.ASK, "To Be or Not to Be", ""),
        ("causes of the cold war?", Intent.ASK, "causes of the cold war?", ""),
        ("vitamin e deficiency", Intent.LOOKUP, "vitamin e deficiency", "vitamin e deficiency"),
        ("el nino", Intent.LOOKUP, "el nino", "el nino"),
    ],
)
def test_router_intents(query: str, intent: Intent, lexical: str, title: str) -> None:
    r = ROUTER.route(query)
    assert r.intent is intent
    assert r.lexical == lexical
    assert r.title == title
    assert r.dense


@pytest.mark.parametrize(
    "query",
    [
        "who invented the telephone",  # "who" of who-iris is not a site word
        "joint pain",
        "us presidents",  # "us" of us-code
        "open source software",
        "army ranks",
        "standard model",
        "the who",
    ],
)
def test_common_words_never_name_a_site(query: str) -> None:
    router = Router.from_sources(
        ["who-iris", "joint-publications", "us-code", "open-library", "army-pubs-public", "standard-ebooks"]
    )
    r = router.route(query)
    assert r.site == ""
    assert not r.scoped
    assert r.lexical == query


def test_site_words_are_explicit() -> None:
    router = Router.from_sources(["who-iris", "rfc", "pubmed", "wikipedia-top10", "wikipedia-top10-history"])
    assert set(router.sites) == {
        "who-iris",
        "rfc",
        "pubmed",
        "pmid",
        "wikipedia",
        "wikipedia-top10",
        "wikipedia-top10-history",
    }
    assert router.sources_for("wikipedia") == ("wikipedia-top10", "wikipedia-top10-history")
    # the full id of a hyphenated source works; its fragments do not
    r = router.route("who-iris malaria guidelines")
    assert (r.site, r.scoped, r.prefer_sources) == ("who-iris", False, ("who-iris",))
    # identifiers scope hard, words are a soft prior
    assert router.route("pmid 31452104").scoped
    assert router.route("rfc 9110").scoped
    assert router.route("rfc-9110").scoped
    soft = router.route("rfc http caching")
    assert (soft.site, soft.scoped, soft.lexical, soft.prefer_sources) == (
        "rfc",
        False,
        "rfc http caching",
        ("rfc",),
    )
    # registry site words: extra names for a source, never a function word
    extra = Router.from_sources(["nist-csrc"], {"nist-csrc": ["csrc", "nist", "the", "de"], "absent": ["x"]})
    assert set(extra.sites) == {"nist-csrc", "csrc", "nist"}


def test_builtin_site_words_are_not_function_words() -> None:
    from wsp_oracle.router import ALL_STOPWORDS, SITE_WORDS

    assert not set(SITE_WORDS) & ALL_STOPWORDS
    for word in SITE_WORDS:
        assert word.isalnum()
        assert word == word.casefold()


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
    assert ROUTER.route("wikipedia Alan Turing").prefer_sources == ("wikipedia-en-zim",)
    assert "wiktionary" in ROUTER.route("define x").prefer
    assert Router().route("Q42").intent is Intent.LOOKUP  # no wikidata source: just a short query
    assert Router().route("   ").intent is Intent.ASK


@pytest.mark.parametrize(
    ("text", "terms"),
    [
        ("what is the rfc 9110 about?", ["rfc", "9110"]),
        ("causes of the cold war", ["causes", "cold", "war"]),  # "war" is not German "war" here
        (
            "To Be or Not to Be",
            ["To", "Be", "or", "Not", "to", "Be"],
        ),  # a title: most words are function words
        ("to be or not to be", ["to", "be", "or", "not", "to", "be"]),
        ("Doctor Who", ["Doctor", "Who"]),  # one function word: no language, nothing dropped
        ("doctor who", ["doctor", "who"]),
        ("vitamin e deficiency", ["vitamin", "e", "deficiency"]),  # Italian "e" needs Italian context
        ("el nino", ["el", "nino"]),
        ("The Lord of the Rings", ["The", "Lord", "of", "the", "Rings"]),
        ("history of the Roman Empire", ["history", "Roman", "Empire"]),
        ("wie funktioniert die Photosynthese", ["funktioniert", "Photosynthese"]),
        ("что такое фотосинтез", ["такое", "фотосинтез"]),
    ],
)
def test_content_terms_use_only_the_query_language(text: str, terms: list[str]) -> None:
    assert content_terms(text) == terms


def test_query_language() -> None:
    from wsp_oracle.router import query_language

    assert query_language(["what", "is", "the", "capital"]) == "en"
    assert query_language(["el", "nino"]) is None
    assert query_language(["où", "est", "la", "tour"]) == "fr"
    assert query_language(["кто", "это"]) == "ru"
