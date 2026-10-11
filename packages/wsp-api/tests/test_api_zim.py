"""ZimAssetStore against real (tiny) ZIM files written with libzim, end to end through the API."""

from dataclasses import replace
from pathlib import Path, PurePosixPath

from libzim.writer import Creator, Hint, Item, StringProvider

from wsp_api import create_app
from wsp_api.addresses import document_href
from wsp_api.schemas import Document
from wsp_api.testing import local_client
from wsp_api.testing.fakes import SAMPLE_GEN_2, InMemoryVault, sample_services
from wsp_api.zim import ZimAssetStore
from wsp_core.ids import make_doc_id
from wsp_core.model import Artifact, Generation, GenerationStatus

SVG = '<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1"/>'


class _Item(Item):
    def __init__(self, path: str, mimetype: str, content: str) -> None:
        super().__init__()
        self._path, self._mimetype, self._content = path, mimetype, content

    def get_path(self) -> str:
        return self._path

    def get_title(self) -> str:
        return self._path

    def get_mimetype(self) -> str:
        return self._mimetype

    def get_contentprovider(self) -> StringProvider:
        return StringProvider(self._content)

    def get_hints(self) -> dict[Hint, int]:
        return {Hint.FRONT_ARTICLE: self._mimetype == "text/html"}


def _zim(path: Path, name: str, items: list[tuple[str, str, str]], redirects: list[tuple[str, str]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with Creator(path).config_indexing(False, "eng").config_verbose(False) as creator:
        creator.set_mainpath(items[0][0])
        for p, mime, body in items:
            creator.add_item(_Item(p, mime, body))
        for p, target in redirects:
            creator.add_redirection(p, p, target, {Hint.FRONT_ARTICLE: True})
        creator.add_metadata("Name", name)
        creator.add_metadata("Language", "eng")
        creator.add_metadata("Date", "2026-09-01")
    return path


def _vault(tmp_path: Path, files: list[str]) -> InMemoryVault:
    vault = InMemoryVault(root=tmp_path)
    arts = tuple(
        Artifact("wiki", SAMPLE_GEN_2, PurePosixPath(f), size=1, sha256="s" * 64, blake3="b" * 64)
        for f in files
    )
    vault.add(Generation("wiki", SAMPLE_GEN_2, GenerationStatus.SEALED, artifacts=arts), promote=True)
    return vault


def test_single_book_html_assets_and_redirects(tmp_path: Path) -> None:
    gen_dir = tmp_path / "wiki" / SAMPLE_GEN_2
    _zim(
        gen_dir / "upstream" / "wiki_en_all_2026-09.zim",
        "wiki_en_all",
        [("A/Page", "text/html", "<p>hi <img src='../I/x.svg'></p>"), ("I/x.svg", "image/svg+xml", SVG)],
        [("A/Old_Page", "A/Page")],
    )
    store = ZimAssetStore(_vault(tmp_path, ["upstream/wiki_en_all_2026-09.zim"]))
    assert store.html("wiki", SAMPLE_GEN_2, "zim:A/Page") == "<p>hi <img src='../I/x.svg'></p>"
    assert store.html("wiki", SAMPLE_GEN_2, "zim:A/Old_Page") is not None  # redirect followed
    asset = store.asset("wiki", SAMPLE_GEN_2, "I/x.svg")
    assert asset is not None
    assert asset.mimetype == "image/svg+xml"
    assert asset.content == SVG.encode()
    assert store.html("wiki", SAMPLE_GEN_2, "zim:I/x.svg") is None  # not HTML
    assert store.asset("wiki", SAMPLE_GEN_2, "nope") is None
    assert store.asset("wiki", "../../etc", "I/x.svg") is None  # unknown generation: nothing opened
    assert store.html("wiki", SAMPLE_GEN_2, "parquet:1") is None


def test_multi_book_paths_are_prefixed_with_the_book_name(tmp_path: Path) -> None:
    up = tmp_path / "wiki" / SAMPLE_GEN_2 / "upstream"
    _zim(up / "a_2026-09.zim", "book_a", [("A/One", "text/html", "<p>one</p>")], [])
    _zim(up / "b_2026-09.zim", "book_b", [("A/Two", "text/html", "<p>two</p>")], [])
    store = ZimAssetStore(_vault(tmp_path, []))  # no artifacts listed: falls back to upstream/*.zim
    assert store.html("wiki", SAMPLE_GEN_2, "zim:book_b/A/Two") == "<p>two</p>"
    assert store.html("wiki", SAMPLE_GEN_2, "zim:book_c/A/Two") is None


def test_document_html_and_image_through_the_api(tmp_path: Path) -> None:
    services = sample_services()
    zim_rel = "upstream/wikipedia_en_2026-09.zim"
    gen_dir = tmp_path / "wikipedia-en-zim" / SAMPLE_GEN_2
    _zim(
        gen_dir / zim_rel,
        "wikipedia_en",
        [
            ("A/Alan_Turing", "text/html", '<p><a href="Enigma">Enigma</a><img src="../I/t.svg"></p>'),
            ("I/t.svg", "image/svg+xml", SVG),
        ],
        [],
    )
    vault = InMemoryVault(root=tmp_path)
    art = Artifact("wikipedia-en-zim", SAMPLE_GEN_2, PurePosixPath(zim_rel), 10, "a" * 64, "b" * 64)
    vault.add(
        Generation("wikipedia-en-zim", SAMPLE_GEN_2, GenerationStatus.SEALED, artifacts=(art,)), promote=True
    )
    turing = services.documents.get(make_doc_id("wikipedia-en-zim", "A/Alan_Turing"))
    assert turing is not None
    services.documents.put(SAMPLE_GEN_2, replace(turing, html_ref="zim:A/Alan_Turing"))  # type: ignore[attr-defined]
    with local_client(create_app(replace(services, vault=vault))) as c:
        doc = Document.model_validate(c.get(document_href(turing.doc_id)).json())
        assert doc.format == "html"
        assert doc.html == (
            f'<p><a href="wsp://wikipedia-en-zim/A/Enigma">Enigma</a>'
            f'<img src="/v1/assets/wikipedia-en-zim/{SAMPLE_GEN_2}/I/t.svg"></p>'
        )
        assert doc.vault is not None
        assert doc.vault.path == f"wikipedia-en-zim/{SAMPLE_GEN_2}/{zim_rel}"
        assert doc.vault.entry == "A/Alan_Turing"
        assert doc.vault.sha256 == "a" * 64
        img = c.get(f"/v1/assets/wikipedia-en-zim/{SAMPLE_GEN_2}/I/t.svg")
        assert img.status_code == 200
        assert img.headers["content-type"].startswith("image/svg+xml")


def test_link_base_is_the_entry_actually_served(tmp_path: Path) -> None:
    up = tmp_path / "wiki" / SAMPLE_GEN_2 / "upstream"
    _zim(up / "askubuntu_2026-07a.zim", "askubuntu", [("questions", "text/html", "<p>front</p>")], [])
    _zim(up / "b_2026-09.zim", "book_b", [("A/Two", "text/html", "<p>two</p>")], [("A/Old", "A/Two")])
    store = ZimAssetStore(_vault(tmp_path, []))
    assert store.books("wiki", SAMPLE_GEN_2) == ["askubuntu", "b"]  # re-release suffix "a" stripped
    assert store.html("wiki", SAMPLE_GEN_2, "zim:askubuntu") == "<p>front</p>"
    # A book front page (no slash) resolves its links inside the book, not at the source root.
    assert store.base("wiki", SAMPLE_GEN_2, "askubuntu") == "askubuntu/questions"
    assert store.base("wiki", SAMPLE_GEN_2, "askubuntu/") == "askubuntu/questions"
    assert store.base("wiki", SAMPLE_GEN_2, "book_b/A/Old") == "book_b/A/Two"
    assert store.base("wiki", SAMPLE_GEN_2, "book_b/A/Missing") == "book_b/A/Missing"
    assert store.base("wiki", SAMPLE_GEN_2, "_books") == "_books"
