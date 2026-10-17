"""``ncbi-bookshelf``: chosen books of the NCBI Bookshelf open-access subset, one document per part."""

# ruff: noqa: E501 - the fixtures are upstream markup as published

from __future__ import annotations

import io
import tarfile
from pathlib import Path

import pytest
from adapters_testkit import make_ctx, make_spec

from wsp_adapters import available_adapters
from wsp_adapters.bookshelf import BASE_URL, LIST_FILE, BookshelfAdapter, chosen, part_record, read_list
from wsp_core.model import FetchPlan, VerdictStatus
from wsp_core.ports import Ctx, SourceAdapter
from wsp_core.testing.adapter_contract import FakeFetcher, SourceAdapterContract

LIST = (
    "File,Title,Publisher,Publication Year,Accession ID,Last Updated (YYYY-MM-DD HH:MM:SS)\n"
    "3d/12/statpearls_NBK430685.tar.gz,StatPearls,StatPearls Publishing,2026,NBK430685,2026-10-11 03:19:14\n"
    "b1/b9/who251730_NBK401773.tar.gz,Guidelines on Core Components of IPC,World Health Organization,2016,NBK401773,2019-06-08 02:37:23\n"
    'ca/84/gene_NBK1116.tar.gz,GeneReviews\xae,"University of Washington, Seattle",1993,NBK1116,2026-10-16 02:42:01\n'
).encode("latin-1")


def nxml(
    part_id: str,
    title: str,
    book: str,
    body: str,
    *,
    label: str = "",
    lic: str = "https://creativecommons.org/licenses/by-nc-nd/4.0/",
) -> bytes:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE book-part-wrapper PUBLIC "-//NLM//DTD BITS Book Interchange DTD v2.0 20151225//EN" "BITS-book2.dtd">
<book-part-wrapper id="{part_id}" content-type="chapter" xmlns:xlink="http://www.w3.org/1999/xlink">
 <book-meta><book-title-group><book-title>{book}</book-title></book-title-group>
  <permissions><license license-type="open-access" xlink:href="{lic}"><license-p>Licence</license-p></license></permissions></book-meta>
 <book-part book-part-type="chapter"><book-part-meta><title-group>{f"<label>{label}</label>" if label else ""}<title>{title}</title></title-group></book-part-meta>
  <body><sec><title>Introduction</title><p>{body} <xref ref-type="bibr" rid="r1">1</xref></p>
   <list><list-item><p>First point.</p></list-item></list>
   <table-wrap><table><tr><th>Drug</th><th>Dose</th></tr><tr><td>Insulin</td><td>0.5 U/kg</td></tr></table></table-wrap></sec></body>
  <back><ref-list><ref id="r1"><mixed-citation>A reference.</mixed-citation></ref></ref-list></back>
 </book-part></book-part-wrapper>""".encode()


def archive(top: str, parts: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in {
            "TOC.nxml": b'<book-part-wrapper id="toc" content-type="toc"/>',
            **parts,
            "fig.jpg": b"\xff",
        }.items():
            info = tarfile.TarInfo(f"{top}/{name}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


STATPEARLS = archive(
    "statpearls_NBK430685",
    {"article-1.nxml": nxml("article-1", "Diabetic Ketoacidosis", "StatPearls", "DKA is an emergency.")},
)
WHO = archive(
    "who251730_NBK401773",
    {
        "ch3.nxml": nxml(
            "ch3",
            "Guiding principles",
            "Guidelines on Core Components of IPC",
            "IPC is relevant to health systems.",
            label="3",
            lic="https://creativecommons.org/licenses/by-nc-sa/3.0/igo",
        )
    },
)
ROUTES = {
    BASE_URL + LIST_FILE: LIST,
    BASE_URL + "3d/12/statpearls_NBK430685.tar.gz": STATPEARLS,
    BASE_URL + "b1/b9/who251730_NBK401773.tar.gz": WHO,
}
SPEC = make_spec(
    "ncbi-bookshelf",
    {
        "adapter": "ncbi-bookshelf",
        "books": ["NBK430685"],
        "publishers": ["World Health Organization"],
        "standalone": ["NBK430685"],
    },
)


def test_registered() -> None:
    assert "ncbi-bookshelf" in available_adapters()


def test_the_list_is_read_and_the_selection_made() -> None:
    books = read_list(LIST)
    assert [b.nbk for b in books] == ["NBK430685", "NBK401773", "NBK1116"]
    assert books[2].title == "GeneReviews®"  # a Latin-1 byte in NLM's list
    assert [b.name for b in chosen(books, ["NBK430685"], ["World Health Organization"])] == [
        "statpearls_NBK430685.tar.gz",
        "who251730_NBK401773.tar.gz",
    ]


def test_a_part_reads_as_its_body_without_references() -> None:
    doc = part_record(nxml("ch3", "Guiding principles", "Core Components of IPC", "IPC matters.", label="3", lic="https://creativecommons.org/licenses/by-nc-sa/3.0/igo"),
                      "NBK401773", "who251730", "ncbi-bookshelf", "LicenseRef-per-item", standalone=False)  # fmt: skip
    assert doc is not None
    assert doc.native_id == "NBK401773/ch3"
    assert doc.title == "3. Guiding principles — Core Components of IPC"
    assert doc.text == (
        "Core Components of IPC: Guiding principles\n\nIntroduction\nIPC matters.\nFirst point.\nDrug | Dose\nInsulin | 0.5 U/kg"
    )
    assert doc.license == "https://creativecommons.org/licenses/by-nc-sa/3.0/igo"
    assert doc.url_native == "https://www.ncbi.nlm.nih.gov/books/n/who251730/ch3/"
    article = part_record(
        nxml("article-1", "Diabetic Ketoacidosis", "StatPearls", "DKA."),
        "NBK430685",
        "statpearls",
        "x",
        "x",
        standalone=True,
    )
    assert article is not None
    assert article.title == "Diabetic Ketoacidosis"  # an article in its own right


class TestBookshelfContract(SourceAdapterContract):
    @pytest.fixture
    def adapter(self) -> SourceAdapter:
        return BookshelfAdapter()

    @pytest.fixture
    def ctx(self, tmp_path: Path) -> Ctx:
        return make_ctx(SPEC, FakeFetcher(ROUTES), tmp_path / "work")

    @pytest.fixture
    def generation_dir(self, tmp_path: Path) -> Path:
        up = tmp_path / "ncbi-bookshelf" / "2026-10-16" / "upstream"
        up.mkdir(parents=True)
        (up / "statpearls_NBK430685.tar.gz").write_bytes(STATPEARLS)
        (up / "who251730_NBK401773.tar.gz").write_bytes(WHO)
        return up.parent

    def test_verify_detects_corruption(self, adapter: SourceAdapter, ctx: Ctx) -> None:
        """NLM publishes no checksums for the archives: verify says so, and finds missing files."""
        release = next(iter(adapter.discover(ctx)))
        remote = release.files[0]
        path = next(iter(adapter.fetch(ctx, adapter.plan(ctx, None, release)))).path
        assert adapter.verify(ctx, remote, path).status is VerdictStatus.UNVERIFIABLE
        path.unlink()
        assert adapter.verify(ctx, remote, path).status is VerdictStatus.MISSING


def test_a_release_reads_its_book_list_across_generations(tmp_path: Path) -> None:
    """A release fetches changed books only; meta/books.json says which books it is made of."""
    old = tmp_path / "ncbi-bookshelf" / "2026-09-01" / "upstream"
    new = tmp_path / "ncbi-bookshelf" / "2026-10-16" / "upstream"
    old.mkdir(parents=True)
    new.mkdir(parents=True)
    (old / "who251730_NBK401773.tar.gz").write_bytes(WHO)
    (new / "statpearls_NBK430685.tar.gz").write_bytes(STATPEARLS)
    ctx = make_ctx(SPEC, FakeFetcher(ROUTES), tmp_path / "work")
    adapter = BookshelfAdapter()
    release = next(iter(adapter.discover(ctx)))
    staged = adapter.finalize(ctx, FetchPlan(release, release.files[:1]), None, {})
    (new.parent / "meta").mkdir()
    (new.parent / staged[0][0]).write_bytes(staged[0][1].read_bytes())
    ids = [d.native_id for d in adapter.documents(ctx, new.parent)]
    assert ids == ["NBK430685/article-1", "NBK401773/ch3"]
    assert adapter.depends_on(ctx, new.parent) == {"2026-09-01"}
