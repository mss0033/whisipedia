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
    body = "<p>Spring tides occur during the new and full moons.</p>"
    with Creator(path).config_indexing(False, "eng").config_verbose(False) as creator:
        creator.set_mainpath("index/page_2")
        creator.add_item(_Item("index/page_2", "text/html", stub, "Spring and Neap Tides"))
        creator.add_item(_Item("index/page_3", "text/html", stub.replace("Tides'", "Gone'"), "Gone"))
        creator.add_item(
            _Item("content/page_content_2.json", "application/json", json.dumps({"htmlBody": body}))
        )
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
    assert "new and full moons" not in docs["index/page_3"].text  # no content entry: the stub as it is
