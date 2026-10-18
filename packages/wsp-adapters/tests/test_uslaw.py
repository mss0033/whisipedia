"""``us-law``: the United States Code (USLM) and the eCFR, one document per section."""

# ruff: noqa: E501, RUF001 - the fixtures are upstream XML as published (long lines, en dashes)

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest
from adapters_testkit import make_ctx, make_spec

from wsp_adapters import available_adapters
from wsp_adapters.uslaw import (
    ECFR_BASE,
    ECFR_TITLES,
    USCODE_PAGE,
    UsLawAdapter,
    ecfr_name,
    ecfr_sections,
    uscode_sections,
)
from wsp_core.model import VerdictStatus
from wsp_core.ports import Ctx, SourceAdapter
from wsp_core.testing.adapter_contract import FakeFetcher, SourceAdapterContract

NS = 'xmlns="http://xml.house.gov/schemas/uslm/1.0"'
USC26 = f"""<?xml version="1.0" encoding="UTF-8"?>
<uscDoc {NS} identifier="/us/usc/t26"><main><title identifier="/us/usc/t26"><chapter>
<section identifier="/us/usc/t26/s501"><num value="501">§ 501.</num><heading> Exemption from tax</heading>
<subsection identifier="/us/usc/t26/s501/a"><num value="a">(a)</num><heading> Exemption</heading>
<content>An organization described in <ref href="/us/usc/t26/s401">section 401(a)</ref> shall be exempt.</content>
</subsection>
<subsection identifier="/us/usc/t26/s501/c"><num value="c">(c)</num><chapeau>The following:</chapeau>
<paragraph><num value="1">(1)</num><content>Any corporation.</content></paragraph></subsection>
<sourceCredit>(Aug. 16, 1954, ch. 736, 68A Stat. 163.)</sourceCredit>
<notes><note><p>Amendments: see <ref href="/us/usc/t42/s1">42 U.S.C. 1</ref>.</p>
<quotedContent><section identifier="/us/usc/t26/s9999"><num>§ 9999.</num><content>Quoted text of an act.</content></section></quotedContent>
</note></notes>
</section>
<section identifier="/us/usc/t26/s502" status="repealed"><num value="502">§ 502.</num><heading> Repealed. Pub. L. 1–1</heading></section>
</chapter></title></main></uscDoc>
"""

ECFR = """<?xml version="1.0" encoding="UTF-8" ?>
<DLPSTEXTCLASS><TEXT><BODY><ECFRBRWS>
<DIV1 N="21" TYPE="TITLE"><HEAD>Title 21—Food and Drugs</HEAD>
<DIV5 N="101" TYPE="PART"><HEAD>PART 101—FOOD LABELING</HEAD>
<AUTH><HED>Authority:</HED><PSPACE>21 U.S.C. 321.</PSPACE></AUTH>
<DIV8 N="§ 101.1" TYPE="SECTION"><HEAD>§ 101.1   Principal display panel.</HEAD>
<P>The term <I>principal display panel</I> means the part of a label most likely displayed.</P>
<CITA TYPE="N">[42 FR 14308, Mar. 15, 1977]</CITA></DIV8>
<DIV8 N="§ 101.2" TYPE="SECTION"><HEAD>§ 101.2   [Reserved]</HEAD></DIV8>
<DIV9 N="Appendix A to Part 101" TYPE="APPENDIX"><HEAD>Appendix A to Part 101—Monier-Williams Procedure</HEAD>
<P>Apparatus and reagents.</P></DIV9>
</DIV5>
<DIV5 N="102" TYPE="PART"><HEAD>PART 102—COMMON OR USUAL NAMES</HEAD>
<DIV8 N="§ 102.5" TYPE="SECTION"><HEAD>§ 102.5   General principles.</HEAD><P>A common name.</P></DIV8>
</DIV5></DIV1></ECFRBRWS></BODY></TEXT></DLPSTEXTCLASS>
"""


def usc_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("usc26.xml", USC26)
    return buf.getvalue()


PAGE = b'<a href="download/releasepoints/us/pl/119/121/xml_uscAll@119-121.zip">XML</a>'
ZIP_URL = "https://uscode.house.gov/download/releasepoints/us/pl/119/121/xml_uscAll@119-121.zip"
USC_SPEC = make_spec("us-code", {"adapter": "us-law", "kind": "uscode"})
ECFR_SPEC = make_spec("ecfr", {"adapter": "us-law", "kind": "ecfr"})


def test_registered() -> None:
    assert "us-law" in available_adapters()


def test_us_code_sections_are_documents_and_quoted_sections_are_not() -> None:
    docs = {d.native_id: d for d in uscode_sections(USC26.encode(), "us-code", "PD")}
    assert sorted(docs) == ["t26/s501", "t26/s502"]  # § 9999 is quoted inside a note
    s501 = docs["t26/s501"]
    assert s501.title == "26 U.S.C. § 501: Exemption from tax"
    assert s501.text == (
        "26 U.S.C. § 501. Exemption from tax\n\n(a) Exemption\n\nAn organization described in section 401(a) "
        "shall be exempt.\n\n(c) The following:\n\n(1) Any corporation.\n\n(Aug. 16, 1954, ch. 736, 68A Stat. 163.)"
    )
    assert "Amendments" not in s501.text  # editorial notes are left out
    assert s501.links == ("wsp:us-code/t26/s401",)  # a reference inside the notes is not a link
    assert "26 USC 501" in s501.aliases
    assert docs["t26/s502"].text.endswith("Repealed. Pub. L. 1–1")


def test_cross_references_in_the_text_and_to_parts_of_sections_are_links() -> None:
    """Few references are marked in the XML: 26 U.S.C. 501 named section 509 as plain text and its
    two links went to other titles; "/us/usc/t30/s902/d" (a subsection) was no link (2026-10-17)."""
    xml = f"""<uscDoc {NS} identifier="/us/usc/t26"><main><title identifier="/us/usc/t26">
<section identifier="/us/usc/t26/s501"><num value="501">§ 501.</num><heading> Exempt</heading>
<content>Under section 509(a)(2), section 1795 of title 12, section 170(b) of this title, section 401(a)
of the Social Security Act, this section and <ref href="/us/usc/t30/s902/d">30 U.S.C. 902(d)</ref>.</content>
</section></title></main></uscDoc>"""
    (doc,) = uscode_sections(xml.encode(), "us-code", "PD")
    assert doc.links == (
        "wsp:us-code/t26/s509",
        "wsp:us-code/t12/s1795",
        "wsp:us-code/t26/s170",
        "wsp:us-code/t30/s902",
    )


def test_ecfr_sections_and_appendices_carry_their_part() -> None:
    docs = {d.native_id: d for d in ecfr_sections(ECFR.encode(), 21, "ecfr", "PD")}
    assert sorted(docs) == ["t21/appendix-Appendix-A-to-Part-101", "t21/s101.1", "t21/s101.2", "t21/s102.5"]
    s = docs["t21/s101.1"]
    assert s.title == "21 CFR 101.1: Principal display panel"
    assert s.text == (
        "§ 101.1 Principal display panel.\n\nThe term principal display panel means the part of a label most "
        "likely displayed.\n\n[42 FR 14308, Mar. 15, 1977]"
    )
    assert s.meta["part"] == "PART 101—FOOD LABELING"
    assert docs["t21/s102.5"].meta["part"] == "PART 102—COMMON OR USUAL NAMES"
    assert s.url_native == "https://www.ecfr.gov/current/title-21/section-101.1"
    assert docs["t21/appendix-Appendix-A-to-Part-101"].title.startswith("21 CFR Appendix A to Part 101")


def test_discover_finds_the_current_release_point(tmp_path: Path) -> None:
    ctx = make_ctx(USC_SPEC, FakeFetcher({USCODE_PAGE: PAGE, ZIP_URL: usc_zip()}), tmp_path)
    (release,) = UsLawAdapter().discover(ctx)
    assert [f.url for f in release.files] == [ZIP_URL]
    assert release.meta["release_point"] == "119-121"


def test_kind_is_required(tmp_path: Path) -> None:
    ctx = make_ctx(make_spec("x", {"adapter": "us-law"}), FakeFetcher(), tmp_path)
    with pytest.raises(ValueError, match="kind"):
        list(UsLawAdapter().discover(ctx))


class _Unverifiable:
    def test_verify_detects_corruption(self, adapter: SourceAdapter, ctx: Ctx) -> None:
        """Neither upstream publishes checksums: verify says so, and finds missing files."""
        release = next(iter(adapter.discover(ctx)))
        remote = release.files[0]
        path = next(iter(adapter.fetch(ctx, adapter.plan(ctx, None, release)))).path
        assert adapter.verify(ctx, remote, path).status is VerdictStatus.UNVERIFIABLE
        path.unlink()
        assert adapter.verify(ctx, remote, path).status is VerdictStatus.MISSING


class TestUsCodeContract(_Unverifiable, SourceAdapterContract):
    @pytest.fixture
    def adapter(self) -> SourceAdapter:
        return UsLawAdapter()

    @pytest.fixture
    def ctx(self, tmp_path: Path) -> Ctx:
        return make_ctx(USC_SPEC, FakeFetcher({USCODE_PAGE: PAGE, ZIP_URL: usc_zip()}), tmp_path / "work")

    @pytest.fixture
    def generation_dir(self, tmp_path: Path) -> Path:
        up = tmp_path / "us-code" / "2026-10-09" / "upstream"
        up.mkdir(parents=True)
        (up / "xml_uscAll@119-121.zip").write_bytes(usc_zip())
        return up.parent


class TestEcfrContract(_Unverifiable, SourceAdapterContract):
    @pytest.fixture
    def adapter(self) -> SourceAdapter:
        return UsLawAdapter()

    @pytest.fixture
    def ctx(self, tmp_path: Path) -> Ctx:
        routes = {f"{ECFR_BASE}title-{t}/{ecfr_name(t)}": ECFR.encode() for t in ECFR_TITLES}
        return make_ctx(ECFR_SPEC, FakeFetcher(routes), tmp_path / "work")

    @pytest.fixture
    def generation_dir(self, tmp_path: Path) -> Path:
        up = tmp_path / "ecfr" / "2026-10-16" / "upstream"
        up.mkdir(parents=True)
        for t in ECFR_TITLES:
            (up / ecfr_name(t)).write_bytes(ECFR.encode())
        return up.parent


def test_table_cells_are_kept_apart() -> None:
    """14 CFR 91.155's minima read "Less than 10,000 feet MSL3 statute miles500 feet below" (2026-10-17)."""
    xml = """<DLPSTEXTCLASS><DIV5 N="91" TYPE="PART"><HEAD>PART 91</HEAD>
<DIV8 N="§ 91.155" TYPE="SECTION"><HEAD>§ 91.155 Basic VFR weather minimums.</HEAD><DIV><TABLE>
<TR><TH>Airspace</TH><TH>Flight visibility</TH><TH>Distance from clouds</TH></TR>
<TR><TD>Less than 10,000 feet MSL</TD><TD>3 statute miles</TD><TD>500 feet below.
</TD></TR><TR><TD> </TD><TD></TD><TD>1,000 feet above.</TD></TR></TABLE></DIV></DIV8></DIV5></DLPSTEXTCLASS>"""
    (doc,) = ecfr_sections(xml.encode(), 14, "ecfr", "PD")
    assert "Airspace | Flight visibility | Distance from clouds" in doc.text
    assert "Less than 10,000 feet MSL | 3 statute miles | 500 feet below.\n\n1,000 feet above." in doc.text
