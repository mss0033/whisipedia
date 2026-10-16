"""ZimAssetStore against real (tiny) ZIM files written with libzim, end to end through the API."""

from dataclasses import replace
from pathlib import Path, PurePosixPath

from wsp_api import create_app
from wsp_api.addresses import document_href
from wsp_api.schemas import Document
from wsp_api.testing import local_client
from wsp_api.testing.fakes import SAMPLE_GEN_2, InMemoryVault, sample_services
from wsp_api.testing.zims import write_zim
from wsp_api.zim import ZimAssetStore
from wsp_core.ids import make_doc_id
from wsp_core.model import Artifact, Generation, GenerationStatus

SVG = '<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1"/>'


def _zim(path: Path, name: str, items: list[tuple[str, str, str]], redirects: list[tuple[str, str]]) -> Path:
    return write_zim(path, name, items, redirects)


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


def test_page_reports_the_entry_actually_served(tmp_path: Path) -> None:
    up = tmp_path / "wiki" / SAMPLE_GEN_2 / "upstream"
    _zim(up / "askubuntu_2026-07a.zim", "askubuntu", [("questions", "text/html", "<p>front</p>")], [])
    _zim(up / "b_2026-09.zim", "book_b", [("A/Two", "text/html", "<p>two</p>")], [("A/Old", "A/Two")])
    store = ZimAssetStore(_vault(tmp_path, []))
    # Book names are the ZIMs' `Name` metadata (as the adapter assigns native ids), not file names.
    assert store.books("wiki", SAMPLE_GEN_2) == ["askubuntu", "book_b"]
    assert store.html("wiki", SAMPLE_GEN_2, "zim:askubuntu") == "<p>front</p>"

    def served(native_id: str) -> tuple[str, str] | None:
        page = store.page("wiki", SAMPLE_GEN_2, native_id)
        return None if page is None else (page.native_id, page.book_root)

    # A book front page (no slash) resolves its links inside the book, not at the source root.
    assert served("askubuntu") == ("askubuntu/questions", "askubuntu/")
    assert served("askubuntu/") == ("askubuntu/questions", "askubuntu/")
    assert served("book_b/A/Old") == ("book_b/A/Two", "book_b/")
    assert served("book_b/A/Missing") is None
    assert served("_books") == ("_books", "")
    assert store.exists("wiki", SAMPLE_GEN_2, "book_b/A/Old")
    assert not store.exists("wiki", SAMPLE_GEN_2, "book_b/A/Missing")
    assert store.home("wiki", SAMPLE_GEN_2) == "_books"


STUB = (
    "<html><head><title>{title}</title>"
    '<meta http-equiv="refresh" content="0;URL=\'{url}\'" /></head>'
    '<body><a href="{url}">{title}</a></body></html>'
)


def test_a_section_redirect_stub_is_served_as_the_page_it_points_at(tmp_path: Path) -> None:
    gen_dir = tmp_path / "wiki" / SAMPLE_GEN_2
    article = "<html><head><title>IPv6 address</title></head><body><h2 id='SLAAC'>SLAAC</h2></body></html>"
    _zim(
        gen_dir / "upstream" / "wiki_en_all_2026-09.zim",
        "wiki_en_all",
        [
            ("A/IPv6_address", "text/html", article),
            ("A/SLAAC", "text/html", STUB.format(title="SLAAC", url="./IPv6_address#SLAAC")),
            ("A/Chain", "text/html", STUB.format(title="Chain", url="SLAAC")),
            ("A/Gone", "text/html", STUB.format(title="Gone", url="./Missing#X")),
            ("A/Away", "text/html", STUB.format(title="Away", url="https://example.org/x")),
            ("A/Loop", "text/html", STUB.format(title="Loop", url="./Loop")),
        ],
        [],
    )
    store = ZimAssetStore(_vault(tmp_path, ["upstream/wiki_en_all_2026-09.zim"]))
    page = store.page("wiki", SAMPLE_GEN_2, "A/SLAAC")
    assert page is not None
    assert (page.native_id, page.html, page.fragment) == ("A/IPv6_address", article, "SLAAC")
    chained = store.page("wiki", SAMPLE_GEN_2, "A/Chain")
    assert chained is not None
    assert (chained.native_id, chained.fragment) == ("A/IPv6_address", "SLAAC")
    # A stub whose target is absent, or outside the archive, stays itself (its link still shows).
    for native in ("A/Gone", "A/Away"):
        kept = store.page("wiki", SAMPLE_GEN_2, native)
        assert kept is not None
        assert (kept.native_id, kept.fragment) == (native, "")
    assert store.page("wiki", SAMPLE_GEN_2, "A/Loop") is None  # never loops


def test_a_dump_link_outside_its_articles_is_the_zim_page_for_that_title(tmp_path: Path) -> None:
    from wsp_api.testing.fakes import SAMPLE_SOURCES
    from wsp_core.model import DocRecord, SourceSpec, SourceStatus, Tier

    dump = SourceSpec(
        id="wiki-dump",
        name="Wikipedia dump (top articles)",
        tier=Tier.T1,
        status=SourceStatus.ACTIVE,
        license="CC-BY-SA-4.0",
        legal_basis="open-license",
        domain=("encyclopedia",),
        adapter="wikimedia-dumps",
        raw={
            "access": {
                "adapter": "wikimedia-dumps",
                "rendition": [{"lang": "en", "source": "wikipedia-en-zim"}],
            }
        },
    )
    services = sample_services(sources=(*SAMPLE_SOURCES, dump))
    zim_rel = "upstream/wikipedia_en_2026-09.zim"
    article = "<html><head><title>IPv6 address</title></head><body><h2 id='SLAAC'>SLAAC</h2></body></html>"
    _zim(
        tmp_path / "wikipedia-en-zim" / SAMPLE_GEN_2 / zim_rel,
        "wikipedia_en",
        [
            ("IPv6_address", "text/html", article),
            ("IPv6_stateless", "text/html", STUB.format(title="SLAAC", url="./IPv6_address#SLAAC")),
        ],
        [],
    )
    vault = InMemoryVault(root=tmp_path)
    art = Artifact("wikipedia-en-zim", SAMPLE_GEN_2, PurePosixPath(zim_rel), 10, "a" * 64, "b" * 64)
    vault.add(
        Generation("wikipedia-en-zim", SAMPLE_GEN_2, GenerationStatus.SEALED, artifacts=(art,)), promote=True
    )
    ipv6 = DocRecord(
        make_doc_id("wiki-dump", "en/IPv6"),
        "wiki-dump",
        "en/IPv6",
        "IPv6",
        "en",
        "CC-BY-SA-4.0",
        "IPv6 text",
        links=("wsp:wiki-dump/en/IPv6_stateless", "wsp:wiki-dump/en/Nowhere"),
    )
    services.documents.put(SAMPLE_GEN_2, ipv6)  # type: ignore[attr-defined]
    with local_client(create_app(replace(services, vault=vault))) as c:
        links = {x["href"]: x["target"] for x in c.get(document_href(ipv6.doc_id)).json()["links"]}
        assert links == {
            "wsp:wiki-dump/en/IPv6_stateless": "wsp:wikipedia-en-zim/IPv6_stateless",
            "wsp:wiki-dump/en/Nowhere": None,
        }
        doc = Document.model_validate(
            c.get(document_href(make_doc_id("wiki-dump", "en/IPv6_stateless"))).json()
        )
        assert doc.doc_id == "wsp:wikipedia-en-zim/IPv6_stateless"
        assert doc.title == "IPv6 address"
        assert doc.section == "SLAAC"
        assert doc.html is not None
        assert "SLAAC" in doc.html
        res = c.get("/go/wsp://wiki-dump/en/IPv6_stateless").json()
        assert res["address"] == "wsp://wikipedia-en-zim/IPv6_stateless"
        assert c.get("/go/wsp://wiki-dump/en/Nowhere").status_code == 404


def test_a_mindtouch_app_stub_is_served_as_its_page(tmp_path: Path) -> None:
    import json

    stub = (
        "<html><head><title>Spring and Neap Tides</title>"
        '<meta http-equiv="refresh" content="0;URL=\'../index.html#/Ocean/Tides\'" />'
        "</head><body></body></html>"
    )
    other = stub.replace("Spring and Neap Tides", "Ocean").replace("#/Ocean/Tides", "#/Ocean")
    body = (
        '<p>Spring tides occur at new and full moon. <a href="#/Ocean">Up</a><img src="content/f/a.svg"></p>'
    )
    shared = {"pages": [{"id": "1", "title": "Ocean", "path": "Ocean"}, {"id": "2", "path": "Ocean/Tides"}]}
    _zim(
        tmp_path / "wiki" / SAMPLE_GEN_2 / "upstream" / "libretexts.org_en_geo_2026-01.zim",
        "libretexts.org_en_geo",
        [
            ("index.html", "text/html", "<html><body><div id='app'></div></body></html>"),
            ("index/page_2", "text/html", stub),
            ("index/page_1", "text/html", other),
            ("content/page_content_2.json", "application/json", json.dumps({"htmlBody": body})),
            ("content/shared.json", "application/json", json.dumps(shared)),
            ("content/f/a.svg", "image/svg+xml", SVG),
        ],
        [],
    )
    store = ZimAssetStore(_vault(tmp_path, ["upstream/libretexts.org_en_geo_2026-01.zim"]))
    page = store.page("wiki", SAMPLE_GEN_2, "index/page_2")
    assert page is not None
    assert page.native_id == "index/page_2"
    assert "Spring tides occur at new and full moon." in page.html
    assert '<a href="page_1">Up</a>' in page.html
    assert '<img src="../content/f/a.svg">' in page.html
    # No content entry: the stub stays itself, never the app's index page.
    bare = store.page("wiki", SAMPLE_GEN_2, "index/page_1")
    assert bare is not None
    assert bare.native_id == "index/page_1"
