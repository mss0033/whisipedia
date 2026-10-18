from wsp_api import pdftext
from wsp_api.testing.zims import tiny_pdf


def test_text_reads_as_the_refiner_reads_it() -> None:
    """The USDA canning guide's tables space their words with glyphs mapped to no character: served
    pages read "Recommended�process�time" (2026-10-17)."""
    assert pdftext.clean("Recommended�process�time") == "Recommended process time"
    assert pdftext.clean("fol￾\r\nlowing the line") == "following the line"
    assert pdftext.clean("hyphen­ated\r\nnext") == "hyphenated\nnext"


def test_pages_are_read_with_a_form_feed_between() -> None:
    assert pdftext.pdf_text(tiny_pdf(["Green beans", "Pressure canner"])) == "Green beans\fPressure canner"
    assert pdftext.pdf_text(b"not a pdf") == ""
