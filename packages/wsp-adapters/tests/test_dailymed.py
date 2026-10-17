"""``dailymed``: FDA drug labels (SPL) from DailyMed's full release, one document per label."""

# ruff: noqa: E501 - the fixtures are upstream markup as published

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest
from adapters_testkit import make_ctx, make_spec

from wsp_adapters import available_adapters
from wsp_adapters.dailymed import FILES_URL, PAGE_URL, DailyMedAdapter, label_record, release_files
from wsp_core.ports import Ctx, SourceAdapter
from wsp_core.testing.adapter_contract import FakeFetcher, SourceAdapterContract


def spl(
    setid: str, name: str, labeler: str, body: str, *, kind: str = "HUMAN PRESCRIPTION DRUG LABEL"
) -> bytes:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<document xmlns="urn:hl7-org:v3">
 <code code="34391-3" displayName="{kind}"/>
 <title>Highlights</title>
 <effectiveTime value="20261007"/>
 <setId root="{setid}"/>
 <versionNumber value="17"/>
 <author><assignedEntity><representedOrganization><name>{labeler}</name></representedOrganization></assignedEntity></author>
 <component><structuredBody>
  <component><section><code code="48780-1" displayName="SPL product data elements section"/>
   <subject><manufacturedProduct><manufacturedProduct><name>{name}</name><formCode displayName="CAPSULE, DELAYED RELEASE"/>
    <asEntityWithGeneric><genericMedicine><name>{name.lower()}</name></genericMedicine></asEntityWithGeneric>
   </manufacturedProduct></manufacturedProduct></subject></section></component>
  <component><section><code code="43683-2"/><excerpt><highlight><text><paragraph>Warnings (5.4) 08/2023</paragraph></text></highlight></excerpt></section></component>
  <component><section><code code="34067-9"/><title>1 INDICATIONS AND USAGE</title>
   <text><paragraph>{body} see (
     <linkHtml href="#S5">5.1</linkHtml>
   )</paragraph><renderMultiMedia referencedObject="F1"/><paragraph>Image</paragraph></text></section></component>
  <component><section><code code="51945-4"/><title>PACKAGE LABEL</title><text><paragraph>NDC 1234</paragraph></text></section></component>
 </structuredBody></component>
</document>""".encode()


def part(labels: list[bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as outer:
        outer.writestr("prescription/", b"")
        for i, xml in enumerate(labels):
            inner = io.BytesIO()
            with zipfile.ZipFile(inner, "w") as z:
                z.writestr(f"{i}.xml", xml)
                z.writestr(f"fig{i}.jpg", b"\xff\xd8")
            outer.writestr(f"prescription/2026_{i}.zip", inner.getvalue())
    return buf.getvalue()


PART = part(
    [
        spl("set-a", "DULOXETINE", "ELI LILLY AND COMPANY", "Duloxetine treats depression."),
        spl(
            "set-b", "DULOXETINE", "REMEDYREPACK INC.", "Duloxetine treats depression."
        ),  # repackaged: same text
        spl("set-c", "Impavido", "Profounda, Inc.", "Impavido treats leishmaniasis."),
    ]
)
PAGE = f"""<html><body><h2>Full Releases</h2>
<li>dm_spl_release_human_rx_part1.zip [ <a href="{FILES_URL}dm_spl_release_human_rx_part1.zip">HTTPS</a> / FTP ]
Number of files: 3 File size: 3.00GB MD5 checksum: {__import__("hashlib").md5(PART).hexdigest()} Last Modified: Oct 16, 2026</li>
<li>dm_spl_release_homeopathic.zip [ HTTPS / FTP ] Number of files: 15,824 File size: 5.49GB MD5 checksum: 21312b53a3206811a81551c91ae4cb1e Last Modified: Oct 15, 2026</li>
</body></html>""".encode()
SPEC = make_spec("dailymed", {"adapter": "dailymed", "sets": ["human_rx"]})


def test_registered() -> None:
    assert "dailymed" in available_adapters()


def test_release_files_take_the_chosen_sets() -> None:
    files, newest = release_files(PAGE.decode(), ["human_rx"])
    assert [f.name for f in files] == ["dm_spl_release_human_rx_part1.zip"]
    assert files[0].url == FILES_URL + "dm_spl_release_human_rx_part1.zip"
    assert files[0].checksums[0].algorithm == "md5"
    assert newest == "2026-10-16"
    assert release_files(PAGE.decode(), ["animal"]) == ([], None)


def test_a_label_reads_as_its_sections_without_codes_cartons_or_highlights() -> None:
    doc = label_record(
        spl("set-a", "DULOXETINE", "ELI LILLY AND COMPANY", "Duloxetine treats depression."), "dailymed", "PD"
    )
    assert doc is not None
    assert doc.native_id == "set-a"
    assert doc.title == "Duloxetine, capsule, delayed release — Eli Lilly And Company"
    assert doc.text == (
        "Human prescription drug label. Duloxetine (duloxetine)\n"
        "Labeler: Eli Lilly And Company; Effective 2026-10-07; version 17\n\n"
        "1 INDICATIONS AND USAGE\nDuloxetine treats depression. see ( 5.1 )"
    )
    assert doc.meta["effective"] == "2026-10-07"
    assert doc.url_native == "https://dailymed.nlm.nih.gov/dailymed/drugInfo.cfm?setid=set-a"


class TestDailyMedContract(SourceAdapterContract):
    @pytest.fixture
    def adapter(self) -> SourceAdapter:
        return DailyMedAdapter()

    @pytest.fixture
    def ctx(self, tmp_path: Path) -> Ctx:
        routes = {PAGE_URL: PAGE, FILES_URL + "dm_spl_release_human_rx_part1.zip": PART}
        return make_ctx(SPEC, FakeFetcher(routes), tmp_path / "work")

    @pytest.fixture
    def generation_dir(self, tmp_path: Path) -> Path:
        up = tmp_path / "dailymed" / "2026-10-16" / "upstream"
        up.mkdir(parents=True)
        (up / "dm_spl_release_human_rx_part1.zip").write_bytes(PART)
        return up.parent


def test_a_repackaged_label_with_the_same_text_is_read_once(tmp_path: Path) -> None:
    up = tmp_path / "dailymed" / "g" / "upstream"
    up.mkdir(parents=True)
    (up / "dm_spl_release_human_rx_part1.zip").write_bytes(PART)
    ctx = make_ctx(SPEC, FakeFetcher(), tmp_path / "work")
    assert [d.native_id for d in DailyMedAdapter().documents(ctx, up.parent)] == ["set-a", "set-c"]
