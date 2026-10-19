"""A long PDF read as its chapters: each chapter leads to the whole and to its neighbours (2026-10-18
audit: a chapter of Lessons In Industrial Instrumentation had no way to the book or the next one)."""

from types import SimpleNamespace
from typing import Any, cast

from wsp_api.views import Library

WHOLE_TEXT = (
    "Lessons In Industrial Instrumentation\nTony R. Kuphaldt\n\nContents\n"
    "Front matter (page 1)\nCalculus (page 5)\nClosed-loop control (page 2370)"
)


def _doc(native: str, text: str, **meta: str) -> Any:
    return SimpleNamespace(
        doc_id=f"wsp:liii/{native}", source_id="liii", native_id=native, text=text, title=native, meta=meta
    )


WHOLE = _doc("liii:LIII", WHOLE_TEXT, pages="1-3311")
DOCS = {WHOLE.doc_id: WHOLE}
fake = SimpleNamespace(s=SimpleNamespace(documents=SimpleNamespace(get=DOCS.get)))
parts = cast(Any, Library.parts)


def test_a_chapter_knows_its_whole_and_its_neighbours() -> None:
    got = parts(fake, _doc("liii:LIII/p5", "…", pages="5-2369", part_of="liii:LIII"))
    assert got.whole.address == "wsp://liii/liii:LIII"
    assert [(p.title, p.address, p.first_page) for p in got.parts] == [
        ("Front matter", "wsp://liii/liii:LIII/p1", 1),
        ("Calculus", "wsp://liii/liii:LIII/p5", 5),
        ("Closed-loop control", "wsp://liii/liii:LIII/p2370", 2370),
    ]
    assert got.index == 1


def test_the_whole_lists_its_parts() -> None:
    got = parts(fake, WHOLE)
    assert got.index is None
    assert len(got.parts) == 3


def test_other_documents_have_no_parts() -> None:
    assert parts(fake, _doc("x", "Contents\nnot a book (page 2)")) is None
    assert parts(fake, _doc("y", "text", part_of="gone")) is None  # its whole is not held
