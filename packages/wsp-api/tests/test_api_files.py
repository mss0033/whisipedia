"""A collection document's stored original (``/v1/files/{doc_id}``) and its map preview."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import IO, Any, cast
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
        "archive_url": None,
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
    # Sent as stored, not gzipped on the fly (2026-10-26 review #117: a 400 MB original went through zlib)
    whole = c.get(url("cp1"), headers={"Accept-Encoding": "gzip"})
    assert "content-encoding" not in whole.headers
    assert whole.headers["content-length"] == str(len(PDF))


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


def test_transfers_beyond_the_cap_wait_then_are_busy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    from wsp_api import files
    from wsp_api.files import _BoundedFile  # pyright: ignore[reportPrivateUsage]

    monkeypatch.setattr(files, "TRANSFER_WAIT", 0.2)
    f = tmp_path / "a.pdf"
    f.write_bytes(PDF)
    sent: list[dict[str, object]] = []

    async def receive() -> dict[str, object]:
        return {"type": "http.disconnect"}

    async def send(message: dict[str, object]) -> None:
        sent.append(message)

    async def run() -> None:
        slots = asyncio.Semaphore(1)
        scope: dict[str, object] = {"type": "http", "method": "GET", "headers": []}
        async with slots:  # the only slot streams another file for longer than the wait
            await _BoundedFile(slots, f)(scope, receive, send)  # type: ignore[arg-type]
        assert sent[0]["status"] == 503
        sent.clear()

        # A slot that frees within the wait: the transfer waits for it instead of failing (a PDF
        # viewer's parallel range requests, 2026-10-18 audit).
        await slots.acquire()
        asyncio.get_running_loop().call_later(0.05, slots.release)
        await _BoundedFile(slots, f)(scope, receive, send)  # type: ignore[arg-type]
        assert sent[0]["status"] == 200
        assert not slots.locked()  # released after the transfer

    asyncio.run(run())


def test_head_answers_with_the_files_headers_and_no_body(setup: tuple[httpx.Client, Path]) -> None:
    """A download manager asks for the size first: HEAD was a 404 (2026-10-19 audit)."""
    c, _ = setup
    r = c.head(url("cp1"))
    assert r.status_code == 200
    assert r.headers["content-length"] == str(len(PDF))
    assert r.headers["content-type"] == "application/pdf"
    assert r.content == b""
    assert c.head(url("nope")).status_code == 404
    assert c.head("/v1/health").status_code == 200


# --- Stored zip archives, browsed without unpacking (2026-11-05, the gNATSGO archive) -------------

ISO = b"""<?xml version="1.0" encoding="UTF-8"?>
<mdb:MD_Metadata xmlns:mdb="http://standards.iso.org/iso/19115/-3/mdb/2.0"
    xmlns:cit="http://standards.iso.org/iso/19115/-3/cit/2.0"
    xmlns:gco="http://standards.iso.org/iso/19115/-3/gco/1.0"
    xmlns:mri="http://standards.iso.org/iso/19115/-3/mri/1.0"
    xmlns:mrs="http://standards.iso.org/iso/19115/-3/mrs/1.0"
    xmlns:mcc="http://standards.iso.org/iso/19115/-3/mcc/1.0"
    xmlns:mrc="http://standards.iso.org/iso/19115/-3/mrc/2.0"
    xmlns:lan="http://standards.iso.org/iso/19115/-3/lan/1.0"
    xmlns:gfc="http://standards.iso.org/iso/19110/gfc/1.1">
  <mdb:contact><cit:CI_Responsibility>
    <cit:role><cit:CI_RoleCode codeList="x" codeListValue="pointOfContact">party who can be contacted for
      acquiring knowledge about the resource</cit:CI_RoleCode></cit:role>
    <cit:party><cit:CI_Organisation>
      <cit:name><gco:CharacterString>National Soil Survey Center</gco:CharacterString></cit:name>
      <cit:contactInfo><cit:CI_Contact><cit:address><cit:CI_Address>
        <cit:country><lan:CountryCode codeList="x" codeListValue="US">US</lan:CountryCode></cit:country>
        <cit:electronicMailAddress><gco:CharacterString>soils@example.gov</gco:CharacterString>
        </cit:electronicMailAddress>
      </cit:CI_Address></cit:address></cit:CI_Contact></cit:contactInfo>
    </cit:CI_Organisation></cit:party>
  </cit:CI_Responsibility></mdb:contact>
  <mdb:identificationInfo><mri:MD_DataIdentification>
    <mri:citation><cit:CI_Citation><cit:title>
      <gco:CharacterString>Soil Survey Geographic Database</gco:CharacterString>
    </cit:title></cit:CI_Citation></mri:citation>
    <mri:abstract><gco:CharacterString>The most detailed soil data.</gco:CharacterString></mri:abstract>
    <mri:spatialResolution><mri:MD_Resolution><mri:equivalentScale><mri:MD_RepresentativeFraction>
      <mri:denominator><gco:Integer>12000</gco:Integer></mri:denominator>
    </mri:MD_RepresentativeFraction></mri:equivalentScale></mri:MD_Resolution></mri:spatialResolution>
    <mri:spatialResolution><mri:MD_Resolution><mri:equivalentScale><mri:MD_RepresentativeFraction>
      <mri:denominator><gco:Integer>24000</gco:Integer></mri:denominator>
    </mri:MD_RepresentativeFraction></mri:equivalentScale></mri:MD_Resolution></mri:spatialResolution>
    <mri:purpose gco:nilReason="missing"/>
  </mri:MD_DataIdentification></mdb:identificationInfo>
  <mdb:referenceSystemInfo><mrs:MD_ReferenceSystem><mrs:referenceSystemIdentifier><mcc:MD_Identifier>
    <mcc:code><gco:CharacterString>5070</gco:CharacterString></mcc:code>
  </mcc:MD_Identifier></mrs:referenceSystemIdentifier></mrs:MD_ReferenceSystem></mdb:referenceSystemInfo>
  <mdb:referenceSystemInfo><mrs:MD_ReferenceSystem><mrs:referenceSystemIdentifier><mcc:MD_Identifier>
    <mcc:code><gco:CharacterString>3338</gco:CharacterString></mcc:code>
  </mcc:MD_Identifier></mrs:referenceSystemIdentifier></mrs:MD_ReferenceSystem></mdb:referenceSystemInfo>
  <mdb:contentInfo><mrc:MD_FeatureCatalogue><mrc:featureCatalogue><gfc:FC_FeatureCatalogue>
    <gfc:featureType><gfc:FC_FeatureType>
      <gfc:typeName>chaashto</gfc:typeName>
      <gfc:definition><gco:CharacterString>The Horizon AASHTO table.</gco:CharacterString></gfc:definition>
      <gfc:aliases><gco:LocalName>Horizon AASHTO</gco:LocalName></gfc:aliases>
      <gfc:carrierOfCharacteristics><gfc:FC_FeatureAttribute>
        <gfc:memberName>aashtocl</gfc:memberName>
        <gfc:definition><gco:CharacterString>AASHTO class.</gco:CharacterString></gfc:definition>
        <gfc:valueMeasurementUnit gco:nilReason="missing"/>
        <gfc:valueType><gco:TypeName><gco:aName><gco:CharacterString>Choice</gco:CharacterString>
        </gco:aName></gco:TypeName></gfc:valueType>
        <gfc:listedValue><gfc:FC_ListedValue>
          <gfc:label><gco:CharacterString>A-1</gco:CharacterString></gfc:label>
          <gfc:code><gco:CharacterString>1</gco:CharacterString></gfc:code>
          <gfc:definition><gco:CharacterString>Granular materials.</gco:CharacterString></gfc:definition>
        </gfc:FC_ListedValue></gfc:listedValue>
        <gfc:listedValue><gfc:FC_ListedValue><gfc:label gco:nilReason="missing"/></gfc:FC_ListedValue>
        </gfc:listedValue>
      </gfc:FC_FeatureAttribute></gfc:carrierOfCharacteristics>
    </gfc:FC_FeatureType></gfc:featureType>
  </gfc:FC_FeatureCatalogue></mrc:featureCatalogue></mrc:MD_FeatureCatalogue></mdb:contentInfo>
</mdb:MD_Metadata>
"""

FGDC = b"""<metadata>
  <idinfo>
    <citation><citeinfo><origin>U.S. Geological Survey</origin><title>Roads of Somewhere</title></citeinfo>
    </citation>
    <descript><abstract>Road centerlines.</abstract><purpose>Navigation.</purpose></descript>
    <ptcontac><cntinfo><cntemail>roads@example.gov</cntemail></cntinfo></ptcontac>
  </idinfo>
  <eainfo><detailed>
    <enttyp><enttypl>roads</enttypl><enttypd>Road lines.</enttypd></enttyp>
    <attr><attrlabl>class</attrlabl><attrdef>Road class.</attrdef>
      <attrdomv><edom><edomv>1</edomv><edomvd>Interstate</edomvd></edom></attrdomv>
      <attrdomv><edom><edomv>2</edomv><edomvd>Local</edomvd></edom></attrdomv>
    </attr>
    <attr><attrlabl>length</attrlabl>
      <attrdomv><rdom><rdommin>0</rdommin><rdommax>99</rdommax><attrunit>km</attrunit></rdom></attrdomv>
    </attr>
  </detailed></eainfo>
  <metainfo><metd>20200101</metd></metainfo>
</metadata>
"""


def lab(*parts: str) -> str:
    """A metadata field's label: its parts joined by the reader's separator."""
    from wsp_api.geometa import LABEL_JOIN

    return LABEL_JOIN.join(parts)


BIG = bytes(range(256)) * 12_288  # 3 MiB: three chunks of the member stream
DAMAGED = 2 * 1024 * 1024 + 4000  # two chunks and a bit: the CRC fails after the first is sent


def _mark_encrypted(path: Path, member: str) -> None:
    """Set the "encrypted" flag of ``member`` in its central directory entry (zipfile cannot write one)."""
    import struct

    raw = bytearray(path.read_bytes())
    at = 0
    while True:
        at = raw.index(b"PK\x01\x02", at)
        n, extra, comment = struct.unpack_from("<HHH", raw, at + 28)
        if bytes(raw[at + 46 : at + 46 + n]) == member.encode():
            raw[at + 8] |= 0x1
            break
        at += 46 + n + extra + comment
    path.write_bytes(bytes(raw))


def _write_archive(path: Path) -> None:
    import zipfile

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("pack/README.txt", "Read this first.\n")
        zf.writestr("pack/meta/soil_iso19115_3.xml", ISO)
        zf.writestr("pack/meta/roads_fgdc.xml", FGDC)
        zf.writestr("pack/meta/plain.xml", b"<root><a>not metadata</a></root>")
        zf.writestr("pack/data/table.csv", "a,b\n1,2\n")
        zf.writestr("pack/data/big.bin", BIG)
        zf.writestr("pack/data/page.html", "<script>alert(1)</script>")
        zf.writestr("pack/data/notes.txt", b"caf\xe9 au lait")  # Windows-1252
        zf.writestr("pack/empty/", b"")
        zf.writestr('Größe "x".txt', "umlaut")
        zf.writestr("pack/locked.txt", b"secret")
    _mark_encrypted(path, "pack/locked.txt")  # refused, never read


@pytest.fixture
def arch(tmp_path: Path) -> Iterator[tuple[httpx.Client, Path]]:
    yield from _archive_client(tmp_path)


def _archive_client(tmp_path: Path, nas_timeout: float = 20.0) -> Iterator[tuple[httpx.Client, Path]]:
    import zipfile

    base = sample_services()
    vault_root = tmp_path / "vault"
    gen_dir = vault_root / SOURCE / GEN / "upstream"
    gen_dir.mkdir(parents=True)
    _write_archive(gen_dir / "gssurgo__GNATSGO-dd1795f4.zip")
    with zipfile.ZipFile(gen_dir / "bad.zip", "w", zipfile.ZIP_STORED) as zf:
        zf.writestr("data.bin", b"A" * DAMAGED)
    raw = bytearray((gen_dir / "bad.zip").read_bytes())
    raw[raw.rindex(b"A" * 100) + 50] = ord("B")  # a flipped byte near the end: the CRC no longer matches
    (gen_dir / "bad.zip").write_bytes(bytes(raw))
    (gen_dir / "fake.zip").write_bytes(b"not a zip at all")
    (gen_dir / "cp1.pdf").write_bytes(PDF)
    docs = base.documents
    assert isinstance(docs, InMemoryDocumentStore)
    for native, ref in [
        ("arch", "file:" + GEN + "/upstream/gssurgo__GNATSGO-dd1795f4.zip"),
        ("bad", "file:" + GEN + "/upstream/bad.zip"),
        ("fake", "file:" + GEN + "/upstream/fake.zip"),
        ("cp1", "file:" + GEN + "/upstream/cp1.pdf"),
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
    settings = ApiSettings(cache_dir=tmp_path / "cache", nas_timeout_seconds=nas_timeout)
    with local_client(create_app(services, settings)) as c:
        yield c, gen_dir


def test_an_archive_document_points_at_its_listing(arch: tuple[httpx.Client, Path]) -> None:
    c, _ = arch
    doc = c.get(f"/v1/documents/{quote(make_doc_id(SOURCE, 'arch'), safe='')}").json()
    assert doc["original"]["archive_url"] == url("arch", "/archive")
    assert doc["original"]["name"] == "GNATSGO.zip"
    pdf = c.get(f"/v1/documents/{quote(make_doc_id(SOURCE, 'cp1'), safe='')}").json()
    assert pdf["original"]["archive_url"] is None
    assert c.get(url("cp1", "/archive")).json()["error"]["code"] == "not_an_archive"


def test_archive_listing_groups_files_by_folder_with_sizes(arch: tuple[httpx.Client, Path]) -> None:
    c, _ = arch
    r = c.get(url("arch", "/archive"))
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "GNATSGO.zip"
    assert body["folder"] == ""
    assert body["truncated"] is False
    assert body["member_url"] == url("arch", "/archive/member")
    assert body["view_url"] == url("arch", "/archive/view")
    folders = {f["path"]: f for f in body["folders"]}
    # Path order, every ancestor present, the explicit empty folder kept.
    assert [f["path"] for f in body["folders"]] == ["", "pack/", "pack/data/", "pack/empty/", "pack/meta/"]
    assert body["members"] == 10  # the folder entry is not a file
    data = folders["pack/data/"]
    assert [m["name"] for m in data["files"]] == ["big.bin", "notes.txt", "page.html", "table.csv"]
    assert data["count"] == 4
    assert data["bytes"] == len(BIG) + 12 + 25 + 8
    assert folders["pack/"]["count"] == 2  # README.txt, locked.txt
    assert folders["pack/"]["total_count"] == 9
    assert folders[""]["total_count"] == 10
    assert folders[""]["total_bytes"] == body["bytes"]
    assert folders["pack/empty/"] == {
        "path": "pack/empty/", "count": 0, "bytes": 0, "total_count": 0, "total_bytes": 0, "files": []
    }  # fmt: skip
    views = {m["name"]: m["view"] for f in body["folders"] for m in f["files"]}
    assert views["soil_iso19115_3.xml"] == "metadata"
    assert views["roads_fgdc.xml"] == "metadata"
    assert views["plain.xml"] == "text"
    assert views["table.csv"] == "text"
    assert views["README.txt"] == "text"
    assert views["big.bin"] is None
    big = next(m for m in data["files"] if m["name"] == "big.bin")
    assert big["path"] == "pack/data/big.bin"
    assert big["size"] == len(BIG)
    assert 0 < big["packed"] < len(BIG)
    assert big["modified"] is not None
    assert big["modified"][4] == "-"


def _all_pages(c: httpx.Client, folder: str = "") -> list[dict[str, Any]]:
    """Every page of a listing, following ``next`` (a bound on the pages: paging must end)."""
    pages: list[dict[str, Any]] = []
    after: str | None = None
    for _ in range(50):
        params = {"folder": folder} if folder else {}
        if after is not None:
            params["after"] = after
        r = c.get(url("arch", "/archive"), params=params)
        assert r.status_code == 200, r.text
        page = r.json()
        pages.append(page)
        if not page["truncated"]:
            assert page["next"] is None
            return pages
        after = page["next"]
        assert isinstance(after, str)
    raise AssertionError("the listing never ended")


def _listed(pages: list[dict[str, Any]]) -> list[str]:
    return [m["path"] for p in pages for f in p["folders"] for m in f["files"]]


def test_archive_listing_is_cut_short_and_paged_to_its_last_file(
    arch: tuple[httpx.Client, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    from wsp_api import archive

    c, _ = arch
    whole = _listed(_all_pages(c))
    assert len(whole) == 10
    monkeypatch.setattr(archive, "LISTED_FILES", 3)
    body = c.get(url("arch", "/archive")).json()
    assert body["truncated"] is True
    assert sum(len(f["files"]) for f in body["folders"]) == 3
    assert body["members"] == 10  # the counts are of the whole, not of what was listed
    data = next(f for f in body["folders"] if f["path"] == "pack/data/")
    assert data["count"] == 4
    assert len(data["files"]) < 4
    # The folders past the last file listed are still there, without their files: the whole tree.
    assert [f["path"] for f in body["folders"]] == ["", "pack/", "pack/data/", "pack/empty/", "pack/meta/"]
    assert body["next"] == _listed([body])[-1]
    # Paged to the end: every file once, in the order of the whole listing.
    pages = _all_pages(c)
    assert len(pages) == 4
    assert _listed(pages) == whole
    assert all(sum(len(f["files"]) for f in p["folders"]) <= 3 for p in pages)


def test_archive_folder_bigger_than_a_page_is_paged_to_its_last_member(
    arch: tuple[httpx.Client, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """2026-11-05 review: a folder of more files than a page was listed again from its start by
    "Show all", so the files past the 2,000th could not be reached at all."""
    from wsp_api import archive

    c, _ = arch
    monkeypatch.setattr(archive, "LISTED_FILES", 3)
    one = c.get(url("arch", "/archive"), params={"folder": "pack/data"}).json()  # slash optional
    assert one["folder"] == "pack/data/"
    assert [f["path"] for f in one["folders"]] == ["pack/data/"]
    assert [m["name"] for m in one["folders"][0]["files"]] == ["big.bin", "notes.txt", "page.html"]
    assert one["members"] == 4
    assert one["truncated"] is True
    assert one["next"] == "pack/data/page.html"
    rest = c.get(url("arch", "/archive"), params={"folder": "pack/data/", "after": one["next"]}).json()
    assert [f["path"] for f in rest["folders"]] == ["pack/data/"]
    assert rest["folders"][0]["count"] == 4  # still the whole folder's
    assert [m["name"] for m in rest["folders"][0]["files"]] == ["table.csv"]
    assert rest["truncated"] is False
    assert rest["next"] is None
    missing = c.get(url("arch", "/archive"), params={"folder": "nope/"})
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "member_not_found"
    # A cursor from another folder is refused, not taken to mean "from the start".
    stray = c.get(url("arch", "/archive"), params={"folder": "pack/data/", "after": "pack/meta/x.xml"})
    assert stray.status_code == 400
    assert stray.json()["error"]["code"] == "invalid_cursor"


def test_archive_listing_pages_a_flat_archive_and_a_tree_of_many_folders(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The top of a flat zip of more files than a page (the review's case), and an archive of more
    folders than a page: both are reached to the end by ``next``, nothing twice."""
    from wsp_api import archive
    from wsp_api.schemas import ArchiveListing

    def pages(entries: tuple[archive.Entry, ...]) -> list[ArchiveListing]:
        out: list[ArchiveListing] = []
        after: str | None = None
        while True:
            page = archive.listing("a.zip", entries, "", member_url="m", view_url="v", after=after)
            out.append(page)
            if page.next is None:
                assert not page.truncated
                return out
            assert page.truncated
            after = page.next
            assert len(out) < 100

    monkeypatch.setattr(archive, "LISTED_FILES", 4)
    monkeypatch.setattr(archive, "LISTED_FOLDERS", 3)
    flat = tuple(archive.Entry(f"tile_{i:03d}.png", i, i, None, False) for i in range(10))
    got = pages(flat)
    assert [len(p.folders[0].files) for p in got] == [4, 4, 2]
    assert [m.path for p in got for m in p.folders[0].files] == [e.path for e in flat]
    assert all(p.folders[0].count == 10 for p in got)
    tree = (
        *(archive.Entry(f"d{i}/f.txt", 1, 1, None, False) for i in range(7)),
        archive.Entry("d9/", 0, 0, None, True),
    )
    got = pages(tree)
    folders = [f.path for p in got for f in p.folders]
    assert sorted(set(folders)) == ["", *[f"d{i}/" for i in range(7)], "d9/"]
    files = [m.path for p in got for f in p.folders for m in f.files]
    assert files == [f"d{i}/f.txt" for i in range(7)]
    assert all(len(p.folders) <= 3 for p in got)


def test_archive_listing_is_read_once_per_archive(
    arch: tuple[httpx.Client, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    import zipfile

    from wsp_api import archive

    c, _ = arch
    opened: list[Path] = []
    real = archive.open_archive

    def counting(path: Path) -> zipfile.ZipFile:
        opened.append(path)
        return real(path)

    monkeypatch.setattr(archive, "open_archive", counting)
    assert c.get(url("arch", "/archive")).status_code == 200
    assert c.get(url("arch", "/archive"), params={"folder": "pack/meta/"}).status_code == 200
    assert len(opened) == 1  # a sealed file: the folder came from the kept listing


def test_archive_folder_index_is_kept_for_the_next_page_and_bounded(
    arch: tuple[httpx.Client, Path], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """2026-11-06 review: each page of a listing grouped and sorted the archive's 200,000 members
    again. The folder index is made once per folder and kept with the entries, within the bounds."""
    import zipfile

    from wsp_api import archive

    c, _ = arch
    built: list[str] = []
    real = archive.folder_index

    def counting(entries: tuple[archive.Entry, ...], folder: str) -> archive.FolderIndex:
        built.append(folder)
        return real(entries, folder)

    monkeypatch.setattr(archive, "folder_index", counting)
    monkeypatch.setattr(archive, "LISTED_FILES", 2)
    first = c.get(url("arch", "/archive")).json()
    assert first["next"]
    assert c.get(url("arch", "/archive"), params={"after": first["next"]}).status_code == 200
    assert c.get(url("arch", "/archive"), params={"folder": "pack/meta"}).status_code == 200
    assert c.get(url("arch", "/archive"), params={"folder": "pack/meta/"}).status_code == 200
    assert built == ["", "pack/meta/"]  # once per folder, whatever the page

    # bounded: folder indexes per archive, and members in all (an index's counted again)
    zips: list[Path] = []
    for i in range(3):
        z = tmp_path / f"z{i}.zip"
        with zipfile.ZipFile(z, "w") as zf:
            for k in range(5):
                zf.writestr(f"d{k}/f.txt", "x")
        zips.append(z)
    cache = archive.Listings(size=4, members=16, folders=2)
    for f in ("", "d0/", "d1/"):
        cache.index(zips[0], f)
    items = vars(cache)["_items"]
    (only,) = items.values()
    assert list(only.indexes) == ["d0/", "d1/"]  # the oldest folder went
    assert only.weight == 5 + 1 + 1
    cache.index(zips[1], "")  # 7 + 10 = 17 > 16: the first archive goes
    assert len(items) == 1
    cache.entries(zips[2])
    assert len(items) == 2


def test_archive_too_big_to_list_and_not_a_zip(
    arch: tuple[httpx.Client, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    from wsp_api import archive

    c, _ = arch
    fake = c.get(url("fake", "/archive"))
    assert fake.status_code == 422
    assert fake.json()["error"]["code"] == "not_an_archive"
    monkeypatch.setattr(archive, "MAX_MEMBERS", 5)
    big = c.get(url("arch", "/archive"))
    assert big.status_code == 413
    assert big.json()["error"]["code"] == "archive_too_large"
    assert "11 files" in big.json()["error"]["message"]  # ten files and one folder entry
    assert c.get(url("arch", "/archive/member"), params={"path": "pack/README.txt"}).status_code == 413


def test_archive_member_streams_unpacked_as_a_download(arch: tuple[httpx.Client, Path]) -> None:
    c, _ = arch
    r = c.get(url("arch", "/archive/member"), params={"path": "pack/data/big.bin"})
    assert r.status_code == 200
    assert r.content == BIG
    assert r.headers["content-length"] == str(len(BIG))
    assert r.headers["content-type"] == "application/octet-stream"
    assert r.headers["content-disposition"] == "attachment; filename=\"big.bin\"; filename*=UTF-8''big.bin"
    assert "content-encoding" not in r.headers
    html = c.get(url("arch", "/archive/member"), params={"path": "pack/data/page.html"})
    assert html.headers["content-type"] == "application/octet-stream"  # never rendered at our origin
    assert html.headers["content-security-policy"].endswith("sandbox")
    named = c.get(url("arch", "/archive/member"), params={"path": 'Größe "x".txt'})
    assert named.content == b"umlaut"
    assert named.headers["content-disposition"] == (
        "attachment; filename=\"Gr__e _x_.txt\"; filename*=UTF-8''Gr%C3%B6%C3%9Fe%20%22x%22.txt"
    )


def test_archive_member_refusals(arch: tuple[httpx.Client, Path]) -> None:
    c, _ = arch
    for path, status, code in [
        ("pack/nope.txt", 404, "member_not_found"),
        ("pack/empty/", 404, "member_not_found"),
        ("pack/locked.txt", 409, "encrypted"),
    ]:
        r = c.get(url("arch", "/archive/member"), params={"path": path})
        assert r.status_code == status, path
        assert r.json()["error"]["code"] == code, path
    assert c.get(url("arch", "/archive/member")).status_code == 422  # path= is required


def test_a_damaged_archive_member_breaks_off(arch: tuple[httpx.Client, Path]) -> None:
    """The CRC is checked at the member's end: the download stops short of its length, so the browser
    reports it failed instead of saving a corrupt file as complete."""
    c, _ = arch
    r = c.get(url("bad", "/archive/member"), params={"path": "data.bin"})
    assert r.status_code == 200
    assert int(r.headers["content-length"]) == DAMAGED
    assert 0 < len(r.content) < DAMAGED


def test_archive_view_reads_text_members(
    arch: tuple[httpx.Client, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    from wsp_api import archive

    c, _ = arch
    readme = c.get(url("arch", "/archive/view"), params={"path": "pack/README.txt"}).json()
    assert readme == {
        "path": "pack/README.txt", "name": "README.txt", "size": 17, "text": "Read this first.\n",
        "truncated": False, "metadata": None,
    }  # fmt: skip
    notes = c.get(url("arch", "/archive/view"), params={"path": "pack/data/notes.txt"}).json()
    assert notes["text"] == "café au lait"
    plain = c.get(url("arch", "/archive/view"), params={"path": "pack/meta/plain.xml"}).json()
    assert plain["metadata"] is None
    assert plain["text"].startswith("<root>")
    binary = c.get(url("arch", "/archive/view"), params={"path": "pack/data/big.bin"})
    assert binary.status_code == 415
    assert binary.json()["error"]["code"] == "not_viewable"
    monkeypatch.setattr(archive, "VIEW_BYTES", 4)
    cut = c.get(url("arch", "/archive/view"), params={"path": "pack/README.txt"}).json()
    assert cut["text"] == "Read"
    assert cut["truncated"] is True


def test_archive_view_reads_iso_metadata_into_sections(arch: tuple[httpx.Client, Path]) -> None:
    c, _ = arch
    r = c.get(url("arch", "/archive/view"), params={"path": "pack/meta/soil_iso19115_3.xml"})
    assert r.status_code == 200
    body = r.json()
    assert body["text"] is None
    meta = body["metadata"]
    assert meta["standard"] == "ISO 19115-3"
    assert meta["title"] == "Soil Survey Geographic Database"
    assert meta["abstract"] == "The most detailed soil data."
    sections = {s["heading"]: {f["label"]: f["value"] for f in s["fields"]} for s in meta["sections"]}
    assert meta["sections"][0]["heading"] == "Identification"
    ident = sections["Identification"]
    assert ident[lab("Citation", "Title")] == "Soil Survey Geographic Database"
    assert ident[lab("Spatial resolution", "Equivalent scale", "Denominator")] == "12000; 24000"
    assert not any("Purpose" in k for k in ident)  # nilReason: nothing to say
    contact = sections["Metadata contact"]
    assert contact["Role"] == "point of contact"  # the code, not the long definition in its text
    assert contact[lab("Contact info", "Address", "Country")] == "US"
    assert contact[lab("Contact info", "Address", "Electronic mail address")] == "soils@example.gov"
    assert sections["Reference systems"][lab("Reference system identifier", "Code")] == "5070; 3338"
    assert "Content" not in sections  # the catalogue is the dictionary, not fields
    [table] = meta["dictionary"]
    assert table["name"] == "chaashto"
    assert table["label"] == "Horizon AASHTO"
    assert table["definition"] == "The Horizon AASHTO table."
    [col] = table["attributes"]
    assert col == {
        "name": "aashtocl", "definition": "AASHTO class.", "type": "Choice", "unit": None,
        "values": ["A-1 (1): Granular materials."], "more_values": 0,
    }  # fmt: skip


def test_archive_view_reads_fgdc_metadata(arch: tuple[httpx.Client, Path]) -> None:
    c, _ = arch
    body = c.get(url("arch", "/archive/view"), params={"path": "pack/meta/roads_fgdc.xml"}).json()
    meta = body["metadata"]
    assert meta["standard"] == "FGDC CSDGM"
    assert meta["title"] == "Roads of Somewhere"
    assert meta["abstract"] == "Road centerlines."
    sections = {s["heading"]: {f["label"]: f["value"] for f in s["fields"]} for s in meta["sections"]}
    assert sections["Identification"][lab("Citation", "Originator")] == "U.S. Geological Survey"
    assert sections["Identification"][lab("Point of contact", "Email")] == "roads@example.gov"
    assert sections["Metadata reference"]["Metadata date"] == "20200101"
    assert "Entities and attributes" not in sections
    [roads] = meta["dictionary"]
    assert roads["name"] == "roads"
    assert roads["definition"] == "Road lines."
    cls, length = roads["attributes"]
    assert cls["values"] == ["1: Interstate", "2: Local"]
    assert length["type"] == "range 0 to 99"
    assert length["unit"] == "km"


def test_metadata_reader_bounds_and_refusals(monkeypatch: pytest.MonkeyPatch) -> None:
    from wsp_api import geometa

    assert geometa.read_metadata(b"<root/>") is None
    assert geometa.read_metadata(b"not xml") is None
    entities = "".join(f'<!ENTITY {chr(98 + i)} "{("&" + chr(97 + i) + ";") * 10}">' for i in range(8))
    bomb = (
        f'<?xml version="1.0"?><!DOCTYPE m [<!ENTITY a "aaaaaaaaaa">{entities}]>'
        "<metadata><idinfo><title>&i;</title></idinfo></metadata>"
    ).encode()
    assert geometa.read_metadata(bomb) is None  # expat refuses the expansion
    monkeypatch.setattr(geometa, "MAX_FIELDS", 1)
    monkeypatch.setattr(geometa, "MAX_VALUES", 1)
    meta = geometa.read_metadata(FGDC)
    assert meta is not None
    ident = next(s for s in meta.sections if s.heading == "Identification")
    assert len(ident.fields) == 1
    assert ident.more > 0
    assert meta.dictionary[0].attributes[0].values == ["1: Interstate"]
    assert meta.dictionary[0].attributes[0].more_values == 1


def test_directory_size_reads_zip64_end_records(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """gNATSGO is 117 GB: its counts are in the ZIP64 record, the 32-bit ones saturated."""
    import struct
    import zipfile

    from wsp_api.archive import directory_size

    path = tmp_path / "z64.zip"
    with monkeypatch.context() as m:
        m.setattr(zipfile, "ZIP_FILECOUNT_LIMIT", 1)  # makes zipfile write the ZIP64 records
        with zipfile.ZipFile(path, "w") as zf:
            for i in range(3):
                zf.writestr(f"f{i}.txt", "x")
    raw = bytearray(path.read_bytes())
    at = raw.rindex(b"PK\x05\x06")
    real = struct.unpack_from("<I", raw, at + 12)[0]
    struct.pack_into("<HHII", raw, at + 8, 0xFFFF, 0xFFFF, 0xFFFFFFFF, 0xFFFFFFFF)
    path.write_bytes(bytes(raw))
    with path.open("rb") as fh:
        assert directory_size(fh) == (3, real)
    plain = tmp_path / "plain.zip"
    with zipfile.ZipFile(plain, "w") as zf:
        zf.writestr("a", "b")
        zf.comment = b"PK\x05\x06 a signature inside the comment"
    with plain.open("rb") as fh:
        assert directory_size(fh)[0] == 1
    (tmp_path / "x").write_bytes(b"nothing")
    with (tmp_path / "x").open("rb") as fh, pytest.raises(zipfile.BadZipFile):
        directory_size(fh)


def test_member_stream_closes_the_archive_whatever_happens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio
    import zipfile

    from wsp_api import files
    from wsp_api.files import _BoundedMember  # pyright: ignore[reportPrivateUsage]

    monkeypatch.setattr(files, "TRANSFER_WAIT", 0.1)
    path = tmp_path / "a.zip"
    _write_archive(path)
    sent: list[dict[str, object]] = []

    async def receive() -> dict[str, object]:
        await asyncio.sleep(10)
        return {"type": "http.disconnect"}

    async def send(message: dict[str, object]) -> None:
        sent.append(message)

    scope: dict[str, object] = {
        "type": "http",
        "method": "GET",
        "headers": [],
        "asgi": {"spec_version": "2.4"},
    }

    async def run() -> None:
        slots = asyncio.Semaphore(1)

        def member() -> tuple[zipfile.ZipFile, _BoundedMember]:
            zf = zipfile.ZipFile(path)
            return zf, _BoundedMember(slots, zf, zf.open("pack/README.txt"), "pack/README.txt")

        zf, resp = member()
        await resp(scope, receive, send)  # type: ignore[arg-type]
        assert sent[0]["status"] == 200
        assert b"".join(cast(bytes, m.get("body", b"")) for m in sent[1:]) == b"Read this first.\n"
        assert zf.fp is None  # closed
        assert not slots.locked()
        sent.clear()
        async with slots:  # busy: refused, and still closed
            zf, resp = member()
            await resp(scope, receive, send)  # type: ignore[arg-type]
        assert sent[0]["status"] == 503
        assert zf.fp is None

    asyncio.run(run())


def test_a_member_opened_after_its_deadline_is_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The NAS answered after the request gave up: the archive it opened is closed, not leaked."""
    import time
    import zipfile

    from wsp_api import archive

    opened: list[zipfile.ZipFile] = []
    real_open, real_member = archive.open_archive, archive.open_member

    def remember(path: Path) -> zipfile.ZipFile:
        opened.append(real_open(path))
        return opened[-1]

    def slow(zf: zipfile.ZipFile, info: zipfile.ZipInfo) -> IO[bytes]:
        time.sleep(0.4)
        return real_member(zf, info)

    monkeypatch.setattr(archive, "open_archive", remember)
    monkeypatch.setattr(archive, "open_member", slow)
    for c, _ in _archive_client(tmp_path, nas_timeout=0.1):
        r = c.get(url("arch", "/archive/member"), params={"path": "pack/README.txt"})
        assert r.status_code == 503
        assert r.json()["error"]["code"] == "timeout"
        deadline = time.monotonic() + 5
        while (not opened or opened[0].fp is not None) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert opened
        assert opened[0].fp is None
