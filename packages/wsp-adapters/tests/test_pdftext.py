"""A two-column PDF page is read left column first (round-2 field exam: the FAA handbook)."""

from __future__ import annotations

from pathlib import Path

import pypdfium2

from wsp_adapters.doccollection.testing import make_pdf
from wsp_adapters.pdftext import page_text

LEFT = [f"left column line {n} about the float carburetor" for n in range(8)]
RIGHT = [f"right column line {n} about carburetor icing" for n in range(8)]


def _text(tmp_path: Path, lines: list[str | tuple[float, float, str]]) -> str:
    path = tmp_path / "page.pdf"
    path.write_bytes(make_pdf([lines]))
    pdf = pypdfium2.PdfDocument(str(path))
    try:
        page = pdf[0]
        textpage = page.get_textpage()
        try:
            return page_text(page, textpage)
        finally:
            textpage.close()
            page.close()
    finally:
        pdf.close()


def _order(text: str, needles: list[str]) -> list[int]:
    return [text.index(n) for n in needles]


def test_a_right_column_drawn_first_is_read_after_the_left(tmp_path: Path) -> None:
    heading = (50.0, 760.0, "Chapter 7: Aircraft Systems, a heading across the whole page width")
    right = [(320.0, 700.0 - 14 * n, t) for n, t in enumerate(RIGHT)]
    left = [(50.0, 700.0 - 14 * n, t) for n, t in enumerate(LEFT)]
    footer = (50.0, 60.0, "A footnote that runs across the whole width of the page at the bottom")
    text = _text(tmp_path, [heading, *right, *left, footer])
    pos = _order(text, ["Chapter 7", LEFT[0], LEFT[7], RIGHT[0], RIGHT[7], "A footnote"])
    assert pos == sorted(pos)
    for line in [*LEFT, *RIGHT]:
        assert text.count(line) == 1  # every line once


def test_a_single_column_page_keeps_its_text(tmp_path: Path) -> None:
    lines: list[str | tuple[float, float, str]] = [*LEFT, *RIGHT]
    text = _text(tmp_path, lines)
    assert _order(text, [LEFT[0], RIGHT[7]]) == sorted(_order(text, [LEFT[0], RIGHT[7]]))


def test_a_two_column_table_is_read_row_by_row(tmp_path: Path) -> None:
    # Short cells on both sides of a gap: a table, not columns of prose.
    rows = [(50.0, 700.0 - 14 * n, f"Item {n}") for n in range(8)]
    values = [(400.0, 700.0 - 14 * n, f"{n * 7} kg") for n in range(8)]
    text = _text(tmp_path, [r for pair in zip(rows, values, strict=True) for r in pair])
    assert text.index("Item 1") < text.index("0 kg") < text.index("Item 2") or text.index(
        "0 kg"
    ) < text.index("Item 1")
