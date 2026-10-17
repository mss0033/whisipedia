from __future__ import annotations

import bz2
import gzip
import io
import tarfile
import zipfile
from pathlib import Path

import pytest
from adapters_testkit import build_zim, make_ctx, make_spec

from wsp_adapters import available_adapters
from wsp_adapters.gutenberg import FEEDS_URL, GutenbergAdapter, parse_rdf, strip_boilerplate
from wsp_core.model import VerdictStatus
from wsp_core.ports import Ctx, SourceAdapter
from wsp_core.testing.adapter_contract import FakeFetcher, SourceAdapterContract

SPEC = make_spec("gutenberg", {"adapter": "gutenberg"}, license_="LicenseRef-PD-US+PG-terms")

CATALOG = (
    "Text#,Type,Issued,Title,Language,Authors,Subjects,LoCC,Bookshelves\n"
    '1,Text,1971-12-01,Book One,en,"Author, A.",Testing -- Fixtures,PZ,Category: Fiction\n'
    '2,Text,1972-01-01,"Livre Deux",fr,"Auteur, B.",,PQ,\n'
    "3,Sound,1999-05-01,Audio Three,en,,,,\n"
)
TEXTS = {
    1: "Header\n*** START OF THE PROJECT GUTENBERG EBOOK BOOK ONE ***\n\nIt was a dark night.\n\n"
    "*** END OF THE PROJECT GUTENBERG EBOOK BOOK ONE ***\nLicense text\n",
    2: "Pas de marqueurs ici.\n",
}


def rdf(number: int, summary: str, alternative: str = "") -> bytes:
    alt = f"<dcterms:alternative>{alternative}</dcterms:alternative>" if alternative else ""
    return f"""<?xml version="1.0" encoding="utf-8"?>
<rdf:RDF xml:base="http://www.gutenberg.org/" xmlns:pgterms="http://www.gutenberg.org/2009/pgterms/"
  xmlns:dcterms="http://purl.org/dc/terms/" xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
  <pgterms:ebook rdf:about="ebooks/{number}">
    <dcterms:creator><pgterms:agent rdf:about="2009/agents/1">
      <pgterms:webpage rdf:resource="https://en.wikipedia.org/wiki/A_Author"/>
      <pgterms:webpage rdf:resource="ebooks/author/1"/>
    </pgterms:agent></dcterms:creator>
    {alt}
    <pgterms:marc520>{summary} (This is an automatically generated summary.)</pgterms:marc520>
  </pgterms:ebook>
</rdf:RDF>""".encode()


def _tar(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def feeds() -> dict[str, bytes]:
    texts = _tar({f"cache/epub/{n}/pg{n}.txt": t.encode() for n, t in TEXTS.items()})
    zbuf = io.BytesIO()
    with zipfile.ZipFile(zbuf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("txt-files.tar", texts)
    rdfs = _tar(
        {
            "cache/epub/1/pg1.rdf": rdf(1, "A dark tale.", "Book the First"),
            "cache/epub/9/pg9.rdf": rdf(9, "x"),
        }
    )
    return {
        "pg_catalog.csv.gz": gzip.compress(CATALOG.encode(), mtime=0),
        "rdf-files.tar.bz2": bz2.compress(rdfs),
        "txt-files.tar.zip": zbuf.getvalue(),
    }


@pytest.fixture
def generation_dir(tmp_path: Path) -> Path:
    """``books/gutenberg/2026-10-10`` next to a ``books/gutenberg-zim`` reading copy."""
    shelf = tmp_path / "library" / "books"
    up = shelf / "gutenberg" / "2026-10-10" / "upstream"
    up.mkdir(parents=True)
    for name, data in feeds().items():
        (up / name).write_bytes(data)
    zim_up = shelf / "gutenberg-zim" / "2025-11" / "upstream"
    build_zim(
        zim_up / "gutenberg_mul_all_2025-11.zim",
        name="gutenberg_mul_all",
        articles=[
            # Author pages are "<author>.<author id>" and author ids overlap ebook numbers: author 1
            # sorts before book 1, and author 3 has no book of that number with an HTML edition.
            ("A. Author.1", "A. Author", "<p>an author</p>"),
            ("Book One.1", "Book One", "<p>Book one</p>"),
            ("Book One_cover.1", "Book One", "<p>cover</p>"),
            ("Livre Deux.2", "Livre Deux", "<p>deux</p>"),
            ("Livre Deux_cover.2", "Livre Deux", "<p>couverture</p>"),
            ("Audio Three_cover.3", "Audio Three", "<p>audio</p>"),
            ("Someone Else.4", "Someone Else", "<p>an author with no book 4</p>"),
        ],
    )
    (shelf / "gutenberg-zim" / "CURRENT.txt").write_text("2025-11\n")
    return up.parent


class TestGutenbergContract(SourceAdapterContract):
    @pytest.fixture
    def adapter(self) -> SourceAdapter:
        return GutenbergAdapter()

    @pytest.fixture
    def ctx(self, tmp_path: Path) -> Ctx:
        return make_ctx(SPEC, FakeFetcher({FEEDS_URL + n: d for n, d in feeds().items()}), tmp_path / "work")

    @pytest.fixture
    def generation_dir(self, generation_dir: Path) -> Path:
        return generation_dir

    def test_verify_detects_corruption(self, adapter: SourceAdapter, ctx: Ctx) -> None:
        """Gutenberg publishes no checksums for its feeds; verify reports that, and missing files."""
        release = next(iter(adapter.discover(ctx)))
        remote = release.files[0]
        path = next(iter(adapter.fetch(ctx, adapter.plan(ctx, None, release)))).path
        assert adapter.verify(ctx, remote, path).status is VerdictStatus.UNVERIFIABLE
        path.unlink()
        assert adapter.verify(ctx, remote, path).status is VerdictStatus.MISSING


def test_books_with_texts_catalog_only_and_reading_copy(generation_dir: Path, tmp_path: Path) -> None:
    assert "gutenberg" in available_adapters()
    ctx = make_ctx(SPEC, FakeFetcher(), tmp_path / "work")
    docs = {d.native_id: d for d in GutenbergAdapter().documents(ctx, generation_dir)}
    assert list(docs) == ["1", "2", "3"]
    one = docs["1"]
    assert one.title == "Book One"
    assert one.text.startswith("Book One\nBy Author, A.\nProject Gutenberg eBook #1, released 1971-12-01.\n")
    assert "Summary: A dark tale." in one.text
    assert "automatically generated" not in one.text
    assert one.text.endswith("It was a dark night.")
    assert "License text" not in one.text
    assert one.aliases == ("Book the First",)
    assert one.meta == {"byline": "A. Author"}  # searched with the title (wsp_index.lexical)
    assert one.links == ("wsp:gutenberg-zim/Book One.1", "https://en.wikipedia.org/wiki/A_Author")
    assert one.url_native == "https://www.gutenberg.org/ebooks/1"
    assert docs["2"].lang == "fr"
    assert docs["2"].text.endswith("Pas de marqueurs ici.")
    assert docs["2"].links[0] == "wsp:gutenberg-zim/Livre Deux.2"
    assert docs["3"].text.endswith("Catalog record only: no plain-text edition in the feed.")
    assert docs["3"].links[0] == "wsp:gutenberg-zim/Audio Three_cover.3"  # no HTML edition: its cover page
    assert "type Sound" in docs["3"].text
    assert (tmp_path / "work" / "gutenberg-zim-paths.json").is_file()


def test_unchanged_feed_files_are_read_from_the_earlier_generation(
    generation_dir: Path, tmp_path: Path
) -> None:
    """Audit 2026-10-11 C1/H3: the 11 GB text archive is not fetched again when only the catalog
    changed; the new generation reads it from the one that holds it, and retention keeps that one."""
    later = generation_dir.parent / "2026-10-11+0a1b2c3d" / "upstream"
    later.mkdir(parents=True)
    (later / "pg_catalog.csv.gz").write_bytes(feeds()["pg_catalog.csv.gz"])
    ctx = make_ctx(SPEC, FakeFetcher(), tmp_path / "work")
    adapter = GutenbergAdapter()
    docs = {d.native_id: d for d in adapter.documents(ctx, later.parent)}
    assert docs["1"].text.endswith("It was a dark night.")  # from txt-files.tar.zip of 2026-10-10
    assert adapter.depends_on(ctx, later.parent) == frozenset({"2026-10-10"})
    assert adapter.depends_on(ctx, generation_dir) == frozenset()


def test_helpers() -> None:
    assert strip_boilerplate("no markers") == "no markers"
    summary, alternatives, pages = parse_rdf(rdf(5, "Short."))
    assert (summary, alternatives, pages) == ("Short.", (), ("https://en.wikipedia.org/wiki/A_Author",))


def test_byline_names_the_authors_as_a_reader_does() -> None:
    from wsp_adapters.gutenberg import byline

    assert byline("Livy, 59 BCE-16; Spillan, D. (Daniel), -1854 [Translator]") == "Livy"
    assert byline("Austen, Jane, 1775-1817") == "Jane Austen"
    assert byline("Shelley, Mary Wollstonecraft, 1797-1851") == "Mary Wollstonecraft Shelley"
    assert byline("Marx, Karl, 1818-1883; Engels, Friedrich, 1820-1895") == "Karl Marx, Friedrich Engels"
    assert byline("Homer, 751? BCE-651? BCE; Butler, Samuel, 1835-1902 [Translator]") == "Homer"
    assert byline("Smith, J. [Editor]") == ""
    assert byline("") == ""


def test_an_author_page_is_never_a_books_illustrated_edition(generation_dir: Path, tmp_path: Path) -> None:
    from wsp_adapters.gutenberg import reading_copy_zim, zim_book_paths

    zim = reading_copy_zim(generation_dir, "gutenberg-zim")
    assert zim is not None
    paths = zim_book_paths(zim, tmp_path / "paths.json")
    assert paths == {1: "Book One.1", 2: "Livre Deux.2", 3: "Audio Three_cover.3"}
    assert zim_book_paths(zim, tmp_path / "paths.json") == paths  # from the cache
