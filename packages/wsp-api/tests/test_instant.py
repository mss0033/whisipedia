"""The "define …" dictionary box comes from the library's Wiktionary entry (audit 2026-10-12 F11)."""

from __future__ import annotations

from collections.abc import Callable

from wsp_api.instant import defined_term, definition, senses_of
from wsp_core.ids import DocId, content_hash, make_doc_id
from wsp_core.model import DocRecord

# The refined text of the real entry (templates stripped), abridged.
ENTRY = """English
Etymology
From , root from , from + (an adjective from ).
Pronunciation
Adjective
Long; polysyllabic.
Pertaining to or given to the use of overly long words.
Derived terms
Translations
Bulgarian:
German: , , ,
Noun
A long word.
1830, On the Art of Rising in Prose The New Monthly Magazine, page: 162:
“The fine old fellow,” as a Northern contemporary of ours patronizingly calls him
A person who uses long words.
Translations
German: ,
Latin
Adjective
Of a foot and a half.
"""


def store(*docs: DocRecord) -> Callable[[DocId], DocRecord | None]:
    by_id: dict[DocId, DocRecord] = {d.doc_id: d for d in docs}
    return by_id.get


def entry(native: str, text: str) -> DocRecord:
    return DocRecord(
        doc_id=make_doc_id("wiktionary-en", native),
        source_id="wiktionary-en",
        native_id=native,
        title=native,
        lang="en",
        license="CC-BY-SA-4.0",
        text=text,
        content_hash=content_hash(text),
    )


def test_queries_that_ask_for_a_definition() -> None:
    assert defined_term("define sesquipedalian") == "sesquipedalian"
    assert defined_term("Definition of  ice cream?") == "ice cream"
    assert defined_term("what does ephemeral mean?") == "ephemeral"
    for other in ("sesquipedalian", "define", "define 42", "how to define a function in python please now"):
        assert defined_term(other) is None, other


def test_senses_of_the_first_language_skip_quotations() -> None:
    senses = senses_of(ENTRY)
    assert [(s.part_of_speech, s.definition) for s in senses] == [
        ("adjective", "Long; polysyllabic."),
        ("adjective", "Pertaining to or given to the use of overly long words."),
        ("noun", "A long word."),
        ("noun", "A person who uses long words."),
    ]


def test_definition_reads_the_library_entry() -> None:
    get = store(
        entry("sesquipedalian", ENTRY), entry("Paris", "English\nProper noun\nThe capital of France.\n")
    )
    answer = definition("define sesquipedalian", get)
    assert answer is not None
    assert answer.doc_id == make_doc_id("wiktionary-en", "sesquipedalian")
    assert answer.license == "CC-BY-SA-4.0"
    assert answer.senses[0].definition == "Long; polysyllabic."
    paris = definition("define Paris", get)
    assert paris is not None
    assert paris.senses[0].part_of_speech == "proper noun"
    assert definition("define nonexistentword", get) is None
    assert definition("sesquipedalian", get) is None
    empty: dict[DocId, DocRecord] = {}
    assert definition("define sesquipedalian", empty.get) is None
