"""Chunker (rank-oracle #11): books keep their middle "Notes", CJK text is split, no passage cap."""

from __future__ import annotations

from oracle_helpers import doc

from wsp_oracle.chunker import chunk_document, chunk_text, is_wiki_like, units


def words(n: int, tag: str = "w") -> str:
    return " ".join(f"{tag}{i}" for i in range(n))


def test_a_notes_chapter_in_the_middle_does_not_end_a_book() -> None:
    text = "\n".join(
        [
            "Chapter One",
            words(300, "one") + ".",
            "Notes",
            words(300, "notes") + ".",
            "Chapter Two",
            words(600, "two") + ".",
        ]
    )
    chunks = chunk_text("A Book", text, target_words=200)
    joined = " ".join(t for _, t in chunks)
    assert "two599" in joined  # the text after the early "Notes" is kept
    # near the end of a wiki article, a reference section still stops the passages
    wiki = "\n".join(["Intro", words(600, "body") + ".", "References", words(200, "ref") + "."])
    assert "ref0" not in " ".join(t for _, t in chunk_text("A", wiki, target_words=200))
    # ... unless the source is not wiki-like
    assert "ref0" in " ".join(t for _, t in chunk_text("A", wiki, target_words=200, stop_at_references=False))


def test_books_have_no_passage_cap() -> None:
    book = doc("gutenberg", "1", "Long", words(60_000) + ".")
    chunks = chunk_document(book)
    assert len(chunks) > 256
    assert "w59999" in chunks[-1].text
    assert not is_wiki_like("gutenberg")
    assert is_wiki_like("wikipedia-en-zim")


def test_cjk_paragraphs_are_split_at_sentences_by_characters() -> None:
    sentence = "東京は日本の首都であり、多くの人が住んでいる都市である。"  # 28 characters, no spaces
    paragraph = sentence * 40  # 1120 characters: one "word" to str.split
    chunks = chunk_text("東京", paragraph, target_words=200)
    assert len(chunks) >= 5
    assert all(sum(units(w) for w in t.split()) <= 300 for _, t in chunks)
    assert "".join(t.replace(" ", "") for _, t in chunks) == paragraph  # nothing lost
    assert units("東京") == 2
    assert units("Tokyo") == 1
