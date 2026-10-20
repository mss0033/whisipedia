"""A PDF longer than ``pdf_max_pages`` is read as its chapters, not cut off (2026-10-18: Lessons in
Industrial Instrumentation is 3,311 pages and only its first 400 reached the index)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest
from adapters_testkit import make_ctx, make_spec

from wsp_adapters.doccollection.adapter import CATALOG, DocCollectionAdapter, long_pdf_sections, pdf_text
from wsp_adapters.doccollection.catalog import write_catalog
from wsp_adapters.doccollection.model import DocumentMeta
from wsp_adapters.doccollection.testing import make_pdf
from wsp_adapters.pdftext import Section, plan_sections
from wsp_core.testing.adapter_contract import FakeFetcher


def test_chapters_are_the_top_outline_entries_with_the_front_matter_before_them() -> None:
    marks = [(0, "Preface", 2), (0, "Calculus", 5), (1, "Derivatives", 6), (0, "Physics", 20)]
    assert plan_sections(30, marks, chunk=100) == [
        Section("Front matter", 0, 2),
        Section("Preface", 2, 5),
        Section("Calculus", 5, 20),
        Section("Physics", 20, 30),
    ]


def test_a_single_wrapping_entry_gives_way_to_its_children() -> None:
    marks = [(0, "The Book", 0), (1, "One", 0), (1, "Two", 10)]
    assert plan_sections(20, marks, chunk=100) == [Section("One", 0, 10), Section("Two", 10, 20)]


def test_a_long_chapter_splits_at_its_entries_and_its_title_page_joins_the_first() -> None:
    marks = [(0, "Intro", 0), (0, "Physics", 10), (1, "Units", 11), (1, "Energy", 40), (0, "End", 100)]
    assert plan_sections(110, marks, chunk=50) == [
        Section("Intro", 0, 10),
        Section("Physics: Units", 10, 40),  # page 10 is Physics's title page
        Section("Physics: Energy (pages 41-90)", 40, 90),  # still too long, no entries of its own: runs
        Section("Physics: Energy (pages 91-100)", 90, 100),
        Section("End", 100, 110),
    ]


def test_without_an_outline_runs_of_pages() -> None:
    assert plan_sections(250, [], chunk=100) == [
        Section("Pages 1-100", 0, 100),
        Section("Pages 101-200", 100, 200),
        Section("Pages 201-250", 200, 250),
    ]
    assert plan_sections(0, []) == []


def test_every_page_is_in_exactly_one_section() -> None:
    marks = [(0, "A", 3), (0, "B", 3), (1, "B1", 5), (0, "C", 7), (2, "deep", 8), (0, "D", 90)]
    for n in (91, 150, 400):
        sections = plan_sections(n, marks, chunk=20)
        pages = [p for s in sections for p in range(s.start, s.stop)]
        assert pages == list(range(n))
        assert all(s.stop - s.start <= 20 for s in sections if s.title != "A")


def _book(tmp_path: Path) -> Path:
    pages = [[f"Page {i + 1} text"] for i in range(12)]
    pages[4] = ["Chapter one begins: the carburetor ices below 20 C"]
    pages[9] = ["Chapter two: anemometer calibration"]
    path = tmp_path / "book.pdf"
    path.write_bytes(make_pdf(pages, [(0, "Contents", 1), (0, "Chapter One", 4), (0, "Chapter Two", 9)]))
    return path


def test_a_section_is_read_from_its_first_page(tmp_path: Path) -> None:
    path = _book(tmp_path)
    assert pdf_text(path, 2, first=9).split("\n\f\n") == [
        "Chapter two: anemometer calibration",
        "Page 11 text",
    ]
    assert long_pdf_sections(path, 20) == []  # short enough to read whole
    assert [s.title for s in long_pdf_sections(path, 5)] == [
        "Front matter",
        "Contents",
        "Chapter One",
        "Chapter Two",
    ]


def test_a_long_pdf_is_the_book_and_its_chapters(tmp_path: Path) -> None:
    gen = tmp_path / "lib" / "2026-10-18"
    (gen / "upstream").mkdir(parents=True)
    (gen / "upstream" / "book.pdf").write_bytes(_book(tmp_path).read_bytes())
    meta = DocumentMeta(
        doc_key="test:BOOK",
        file="upstream/book.pdf",
        issuer="Test Agency",
        title="Field Handbook",
        origin_url="https://pubs.example.gov/book.pdf",
    )
    write_catalog(gen / CATALOG, [meta])
    spec = make_spec(
        "test-pubs",
        {
            "adapter": "doc-collection",
            "issuer": "Test Agency",
            "key_prefix": "test",
            "origins": ["pubs.example.gov"],
            "discovery": [{"kind": "list", "urls": ["https://pubs.example.gov/book.pdf"]}],
            "filter": {"allow_all": True},
            "pdf_max_pages": 5,
        },
        license_="LicenseRef-US-Gov-PD",
    )
    docs = list(
        DocCollectionAdapter(throttle=False).documents(make_ctx(spec, FakeFetcher(), tmp_path / "w"), gen)
    )
    book, *chapters = docs
    assert book.native_id == "test:BOOK"  # the id the whole PDF always had
    assert book.title == "Field Handbook"
    assert (
        "Contents\nFront matter (page 1)\nContents (page 2)\nChapter One (page 5)\nChapter Two (page 10)"
        in book.text
    )
    assert [d.native_id for d in chapters] == [
        "test:BOOK/p1",
        "test:BOOK/p2",
        "test:BOOK/p5",
        "test:BOOK/p10",
    ]
    two = chapters[-1]
    assert two.title == "Chapter Two — Field Handbook"
    assert two.text.startswith(
        "Field Handbook\nChapter Two (pages 10-12)\n\nChapter two: anemometer calibration"
    )
    assert "Page 12 text" in two.text  # past pdf_max_pages: read, not cut off
    assert two.url_native == "https://pubs.example.gov/book.pdf#page=10"
    assert two.html_ref == book.html_ref  # the original is the whole PDF
    assert two.meta["pages"] == "10-12"
    assert two.meta["part_of"] == "test:BOOK"


def test_a_source_indexed_by_its_catalog_never_reads_its_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """USGS topo (2026-10-19): reading 60,000 map PDFs over the NAS held the heavy lock for days; with
    ``index_text: false`` a document is its catalog record, and the file is never opened."""
    gen = tmp_path / "lib" / "2026-10-12.me"
    (gen / "upstream").mkdir(parents=True)
    (gen / "upstream" / "map.pdf").write_bytes(_book(tmp_path).read_bytes())
    meta = DocumentMeta(
        doc_key="usgs-htmc:ME_Farmington_1924",
        file="upstream/map.pdf",
        issuer="US Geological Survey",
        title="Farmington, ME (1924), 1:62,500",
        edition_date="1924",
    )
    write_catalog(gen / CATALOG, [meta])
    spec = make_spec(
        "usgs-topo",
        {
            "adapter": "doc-collection",
            "issuer": "US Geological Survey",
            "key_prefix": "usgs-htmc",
            "origins": ["prd-tnm.s3.amazonaws.com"],
            "discovery": [{"kind": "list", "urls": ["https://prd-tnm.s3.amazonaws.com/map.pdf"]}],
            "filter": {"allow_all": True},
            "pdf_max_pages": 5,
            "index_text": False,
        },
        license_="LicenseRef-US-Gov-PD",
    )
    import wsp_adapters.doccollection.adapter as adapter_module

    def never(*_: object, **__: object) -> object:
        raise AssertionError("the map file was read")

    for name in ("document_text", "long_pdf_sections", "pdf_metadata_date", "pdf_text"):
        monkeypatch.setattr(adapter_module, name, never)
    (doc,) = list(
        DocCollectionAdapter(throttle=False).documents(make_ctx(spec, FakeFetcher(), tmp_path / "w"), gen)
    )
    assert doc.title == "Farmington, ME (1924), 1:62,500"
    assert doc.text.startswith("Farmington, ME (1924), 1:62,500\nIssuer: US Geological Survey")
    assert "carburetor" not in doc.text
    assert doc.html_ref == "file:2026-10-12.me/upstream/map.pdf"  # the reader still shows the map


def test_an_unreadable_page_is_left_out_and_the_rest_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One damaged page in a USGS publication ended the whole index run (2026-10-20)."""
    import pypdfium2

    path = _book(tmp_path)
    real = cast(
        Callable[[pypdfium2.PdfDocument, int], pypdfium2.PdfPage],
        pypdfium2.PdfDocument.get_page,  # pyright: ignore[reportUnknownMemberType]
    )

    def damaged(self: pypdfium2.PdfDocument, i: int) -> pypdfium2.PdfPage:
        if i == 1:
            raise pypdfium2.PdfiumError("Failed to load page.")
        return real(self, i)

    monkeypatch.setattr(pypdfium2.PdfDocument, "__getitem__", damaged)  # pdf[i], as pdf_text reads it
    pages = pdf_text(path, 3).split("\n\f\n")
    assert pages == ["Page 1 text", "[Page 2 of this PDF could not be read.]", "Page 3 text"]
