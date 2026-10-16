"""A PDF page's text in reading order: a two-column page left column first.

pdfium returns a page's text in the order the PDF draws it, and a two-column layout may draw its right
column first: in the FAA's Pilot's Handbook of Aeronautical Knowledge a sentence on carburetor icing
ran on into the float carburetor of the other column, page after page (2026-10-16 field exam).

The page's text is split into pdfium's lines and each line placed by the boxes of its first and last
characters. A page whose lines leave a vertical gutter (at most a quarter of them cross it, and both
sides hold prose-wide lines, so a two-column table is left alone) is read top to bottom in bands: a
band of full-width lines as it stands, a band of column lines left column then right. Every
character is kept exactly once; only the order of whole lines changes.
"""

from __future__ import annotations

import re
from statistics import median
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    import pypdfium2

_LINES = re.compile(r"[^\r\n]*(?:\r\n|\n|\r)?")
MIN_LINES = 6  # fewer boxed lines: no layout to read
MAX_CROSSING = 0.25  # share of lines that may cross the gutter (headings, full-width figures, notes);
# on a one-column page nearly every line does
MIN_PROSE_WIDTH = 0.25  # median line width on each side, as a share of the page: columns, not a table

Box = tuple[float, float, float, float]  # left, bottom, right, top (PDF points, y up)


def page_text(page: pypdfium2.PdfPage, textpage: pypdfium2.PdfTextPage) -> str:
    """The text of ``page`` in reading order (see the module)."""
    text = textpage.get_text_range()
    if len(text) != textpage.count_chars():  # a character outside the BMP: indices no longer match
        return text
    lines: list[tuple[int, int, Box]] = []
    for m in _LINES.finditer(text):
        start, end = m.span()
        if start == end:
            break
        body = m.group().rstrip("\r\n")
        first = start + len(body) - len(body.lstrip())
        last = start + len(body.rstrip()) - 1
        if last < first:
            continue  # a blank line: nothing to place
        a, z = _charbox(textpage, first), _charbox(textpage, last)
        lines.append((start, end, (min(a[0], z[0]), min(a[1], z[1]), max(a[2], z[2]), max(a[3], z[3]))))
    width = page.get_width()
    gutter = _gutter([box for _s, _e, box in lines], width)
    if gutter is None:
        return text

    def side(box: Box) -> str:
        return "L" if box[2] <= gutter else "R" if box[0] >= gutter else "S"

    bands: list[tuple[bool, list[tuple[int, int, Box]]]] = []
    for line in sorted(lines, key=lambda ln: -ln[2][3]):
        full = side(line[2]) == "S"
        if bands and bands[-1][0] == full:
            bands[-1][1].append(line)
        else:
            bands.append((full, [line]))
    out: list[str] = []
    for full, band in bands:
        groups = [band] if full else [[ln for ln in band if side(ln[2]) == s] for s in "LR"]
        for group in groups:
            for start, end, _box in sorted(group):  # within a column, pdfium's own order
                piece = text[start:end]
                out.append(piece if piece.endswith(("\n", "\r")) else piece + "\r\n")
    return "".join(out)


def _charbox(textpage: pypdfium2.PdfTextPage, index: int) -> Box:
    box = textpage.get_charbox(index)  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    return cast(Box, tuple(box))  # pyright: ignore[reportUnknownArgumentType]


def _gutter(boxes: list[Box], width: float) -> float | None:
    """The x of a vertical gutter between two columns of prose, or None."""
    if len(boxes) < MIN_LINES or width <= 0:
        return None
    best: tuple[int, float] | None = None
    for x in range(int(width * 0.3), int(width * 0.7)):
        cross = sum(1 for b in boxes if b[0] < x < b[2])
        left = [b[2] - b[0] for b in boxes if b[2] <= x]
        right = [b[2] - b[0] for b in boxes if b[0] >= x]
        if len(left) < 3 or len(right) < 3:
            continue
        prose = min(median(left), median(right)) >= MIN_PROSE_WIDTH * width
        if prose and (best is None or cross < best[0]):
            best = (cross, float(x))
    if best is None or best[0] > MAX_CROSSING * len(boxes):
        return None
    return best[1]
