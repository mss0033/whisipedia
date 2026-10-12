"""A collection document's stored original (``/v1/files/{doc_id}``) and its map preview."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import cast
from urllib.parse import quote

import httpx
import pytest
from PIL import Image

from wsp_api import ApiSettings, Services, create_app
from wsp_api.originals import display_name, files_csp, stored_file
from wsp_api.testing import local_client
from wsp_api.testing.fakes import InMemoryDocumentStore, InMemoryVault, sample_services
from wsp_core.ids import content_hash, make_doc_id
from wsp_core.model import DocRecord

SOURCE = "noaa-coast-pilot"
GEN = "2026-10-01T00-00-00Z"
PDF = b"%PDF-1.4\n" + b"x" * 5000 + b"\n%%EOF\n"


def rec(native: str, ref: str | None) -> DocRecord:
    return DocRecord(
        doc_id=make_doc_id(SOURCE, native),
        source_id=SOURCE,
        native_id=native,
        title=f"Doc {native}",
        lang="en",
        license="LicenseRef-US-Gov-PD",
        text="Refined text.",
        content_hash=content_hash("Refined text."),
        html_ref=ref,
    )


@pytest.fixture
def setup(tmp_path: Path) -> Iterator[tuple[httpx.Client, Path]]:
    base = sample_services()
    vault_root = tmp_path / "vault"
    gen_dir = vault_root / SOURCE / GEN / "upstream"
    gen_dir.mkdir(parents=True)
    (gen_dir / "cp1.pdf").write_bytes(PDF)
    Image.new("RGB", (3000, 1500), (200, 120, 40)).save(gen_dir / "map.tif")
    Image.new("RGB", (600, 900), (10, 90, 160)).save(gen_dir / "sheet.pdf")  # a one-page map PDF
    (gen_dir / "page.html").write_text("<script>alert(1)</script>")
    (vault_root / SOURCE / "secret.txt").write_text("outside")
    docs = base.documents
    assert isinstance(docs, InMemoryDocumentStore)
    for native, ref in [
        ("cp1", "file:" + GEN + "/upstream/cp1.pdf"),
        ("map", "file:" + GEN + "/upstream/map.tif"),
        ("sheet", "file:" + GEN + "/upstream/sheet.pdf"),
        ("page", "file:" + GEN + "/upstream/page.html"),
        ("escape", "file:" + GEN + "/../secret.txt"),
        ("missing", "file:" + GEN + "/upstream/gone.pdf"),
        ("plain", None),
    ]:
        docs.put(GEN, rec(native, ref))
    services = Services(
        index=base.index,
        documents=docs,
        changes=base.changes,
        vault=InMemoryVault(vault_root),
        ledger=base.ledger,
        sources=base.sources,
    )
    app = create_app(services, ApiSettings(cache_dir=tmp_path / "cache"))
    with local_client(app) as c:
        yield c, tmp_path


def url(native: str, tail: str = "") -> str:
    return f"/v1/files/{quote(make_doc_id(SOURCE, native), safe='')}{tail}"


def test_document_json_points_at_the_original(setup: tuple[httpx.Client, Path]) -> None:
    c, _ = setup
    doc = c.get(f"/v1/documents/{quote(make_doc_id(SOURCE, 'cp1'), safe='')}").json()
    assert doc["original"] == {
        "url": url("cp1"),
        "name": "cp1.pdf",
        "media_type": "application/pdf",
        "inline": True,
        "preview_url": url("cp1", "/preview"),
    }
    tif = c.get(f"/v1/documents/{quote(make_doc_id(SOURCE, 'map'), safe='')}").json()["original"]
    assert tif["preview_url"] == url("map", "/preview")
    assert tif["inline"] is False
    plain = c.get(f"/v1/documents/{quote(make_doc_id(SOURCE, 'plain'), safe='')}").json()
    assert plain["original"] is None


def test_pdf_is_served_inline_frameable_and_ranged(setup: tuple[httpx.Client, Path]) -> None:
    c, _ = setup
    r = c.get(url("cp1"))
    assert r.status_code == 200
    assert r.content == PDF
    assert r.headers["content-type"] == "application/pdf"
    assert r.headers["content-disposition"].startswith("inline")
    csp = r.headers["content-security-policy"]
    assert "frame-ancestors 'self'" in csp
    assert "sandbox" not in csp  # Chrome's PDF viewer refuses to run sandboxed
    part = c.get(url("cp1"), headers={"Range": "bytes=0-7"})
    assert part.status_code == 206
    assert part.content == PDF[:8]


def test_other_files_download_sandboxed(setup: tuple[httpx.Client, Path]) -> None:
    c, _ = setup
    r = c.get(url("page"))
    assert r.status_code == 200
    assert r.headers["content-disposition"].startswith("attachment")
    assert r.headers["content-security-policy"].endswith("sandbox")
    assert "frame-ancestors 'none'" not in r.headers["content-security-policy"]


def test_paths_outside_the_generation_and_missing_files_are_404(setup: tuple[httpx.Client, Path]) -> None:
    c, _ = setup
    for native, code in [("escape", "no_original"), ("missing", "no_original"), ("plain", "no_original")]:
        r = c.get(url(native))
        assert r.status_code == 404, native
        assert r.json()["error"]["code"] == code
    assert c.get(url("nope")).json()["error"]["code"] == "document_not_found"


def test_map_preview_is_a_cached_downscaled_jpeg(setup: tuple[httpx.Client, Path]) -> None:
    c, tmp = setup
    r = c.get(url("map", "/preview"))
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/jpeg"
    cached = list((tmp / "cache" / "previews").glob("*.jpg"))
    assert len(cached) == 1
    with Image.open(cached[0]) as img:
        assert max(img.size) == 2000
    assert c.get(url("map", "/preview")).content == r.content
    assert c.get(url("cp1", "/preview")).json()["error"]["code"] == "no_preview"  # not a real PDF
    assert c.get(url("page", "/preview")).json()["error"]["code"] == "no_preview"


def test_pdf_preview_is_its_first_page(setup: tuple[httpx.Client, Path]) -> None:
    c, tmp = setup
    r = c.get(url("sheet", "/preview"))
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/jpeg"
    (tmp / "p.jpg").write_bytes(r.content)
    with Image.open(tmp / "p.jpg") as img:
        assert img.size[1] == 2000  # portrait sheet: the long side is the height
        red, green, blue = cast(tuple[int, int, int], img.convert("RGB").getpixel((img.size[0] // 2, 1000)))
        assert blue > red
        assert blue > green


def test_stored_file_rejects_odd_refs() -> None:
    assert stored_file(rec("a", "file:g/upstream/a.pdf")) == ("g", "upstream/a.pdf")
    for bad in ["file:g/../x", "file:/abs", "file:g//x", "file:g", "zim:A/x", None]:
        assert stored_file(rec("a", bad)) is None, bad
    assert files_csp("/v1/documents/x", b"application/pdf") is None


def test_display_names_drop_the_fetcher_prefix_and_hash() -> None:
    assert display_name("usgs-htmc__24000-RI_East-Greenwich_1942_geo.pdf-572e2c57.pdf") == (
        "24000-RI_East-Greenwich_1942_geo.pdf"
    )
    assert display_name("faa__00-80T-80-07e0dcb1.pdf") == "00-80T-80.pdf"
    assert display_name("cp1.pdf") == "cp1.pdf"
