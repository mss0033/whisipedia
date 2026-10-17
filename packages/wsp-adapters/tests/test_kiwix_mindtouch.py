"""LibreTexts ZIMs (openZIM MindTouch scraper): the refiner reads each page's body, not its app stub."""

from __future__ import annotations

import json
from pathlib import Path

from libzim.writer import Creator, Hint, Item, StringProvider

from wsp_adapters._common import WorkerPool
from wsp_adapters.kiwix import read_zim, zim_params
from wsp_core.model import DocRecord


class _Item(Item):
    def __init__(self, path: str, mimetype: str, content: str, title: str = "") -> None:
        super().__init__()
        self._path, self._mimetype, self._content, self._title = path, mimetype, content, title

    def get_path(self) -> str:
        return self._path

    def get_title(self) -> str:
        return self._title or self._path

    def get_mimetype(self) -> str:
        return self._mimetype

    def get_contentprovider(self) -> StringProvider:
        return StringProvider(self._content)

    def get_hints(self) -> dict[Hint, int]:
        return {Hint.FRONT_ARTICLE: self._mimetype == "text/html"}


def test_a_mindtouch_page_is_refined_from_its_content_entry(tmp_path: Path) -> None:
    path = tmp_path / "libretexts.org_en_geo_2026-01.zim"
    stub = (
        "<html><head><title>Spring and Neap Tides</title>"
        '<meta http-equiv="refresh" content="0;URL=\'../index.html#/Ocean/Tides\'" />'
        "</head><body></body></html>"
    )
    body = (
        "<p>Spring tides occur during the new and full moons.</p>"
        '<div class="mt-contentreuse-widget" data-page="Ocean/Neap%3A_Tides"></div>'
    )
    with Creator(path).config_indexing(False, "eng").config_verbose(False) as creator:
        creator.set_mainpath("index/page_2")
        creator.add_item(_Item("index/page_2", "text/html", stub, "Spring and Neap Tides"))
        creator.add_item(_Item("index/page_3", "text/html", stub.replace("Tides'", "Gone'"), "Gone"))
        creator.add_item(
            _Item("content/page_content_2.json", "application/json", json.dumps({"htmlBody": body}))
        )
        reused = {"htmlBody": "<p>Neap tides come at the quarter moons.</p>"}
        creator.add_item(_Item("content/page_content_9.json", "application/json", json.dumps(reused)))
        shared = {"pages": [{"id": 2, "path": "Ocean/Tides"}, {"id": 9, "path": "Ocean/Neap:_Tides"}]}
        creator.add_item(_Item("content/shared.json", "application/json", json.dumps(shared)))
        creator.add_metadata("Name", "libretexts.org_en_geo")
        creator.add_metadata("Language", "eng")
    with WorkerPool(1) as pool:
        docs = {
            e.native_id: e
            for e in read_zim(
                path, zim_params(path, "lt", "CC-BY-4.0", False), workdir=tmp_path / "w", pool=pool
            )
            if isinstance(e, DocRecord)
        }
    assert docs["index/page_2"].title == "Spring and Neap Tides"
    assert "Spring tides occur during the new and full moons." in docs["index/page_2"].text
    assert "Neap tides come at the quarter moons." in docs["index/page_2"].text  # a reused page's text
    assert "new and full moons" not in docs["index/page_3"].text  # no content entry: the stub as it is


def test_a_nautilus_archive_is_refined_as_its_catalogue(tmp_path: Path) -> None:
    """The USDA canning guide's eight PDFs were neither listed nor searchable (2026-10-17)."""
    from adapters_testkit import pages_pdf

    path = tmp_path / "usda-2015_en_2025-04.zim"
    database = (
        "var DATABASE = [{'_id': '00004', 'ti': 'Part 4', 'dsc': 'Vegetables', 'aut': 'USDA', "
        "'fp': ['GUIDE04.pdf']}];"
    )
    with Creator(path).config_indexing(False, "eng").config_verbose(False) as creator:
        creator.set_mainpath("home")
        creator.add_item(_Item("home", "text/html", "<html><title>{{ title }}</title></html>", "home"))
        creator.add_item(_Item("database.js", "application/javascript", database))
        pdf = pages_pdf(["Green beans: pressure canner", "Process 20 minutes"]).decode("latin-1")
        creator.add_item(_Item("files/GUIDE04.pdf", "application/pdf", pdf))
        creator.add_metadata("Name", "usda-2015_en")
        creator.add_metadata("Language", "eng")
        creator.add_metadata("Scraper", "nautiluszim 1.1.1")
    with WorkerPool(1) as pool:
        docs = {
            e.native_id: e
            for e in read_zim(path, zim_params(path, "usda", "PD", False), workdir=tmp_path / "w", pool=pool)
            if isinstance(e, DocRecord)
        }
    doc = docs["nautilus/00004"]
    assert doc.title == "Part 4: Vegetables"
    assert doc.html_ref == "zim:nautilus/00004"
    assert "Green beans: pressure canner" in doc.text
    assert "Process 20 minutes" in doc.text
    assert "By USDA" in doc.text
