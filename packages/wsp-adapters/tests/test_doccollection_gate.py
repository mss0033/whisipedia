"""The doc-collection gate fails closed (2026-10-11 audit, adapters C3).

One regression test per bypass the audit reproduced: every one of them used to come back
``allow:content=clean`` or ``allow:marking=...``.
"""

from __future__ import annotations

import sys
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from wsp_adapters.doccollection.adapter import OriginError, download_from_origins, screen_file
from wsp_adapters.doccollection.discovery import off_origin_hops, origin_ok
from wsp_adapters.doccollection.policy import (
    DASHES,
    DocFacts,
    Rules,
    Verdict,
    check_distribution,
    classify_content,
    classify_metadata,
    normalize_pub_number,
    parse_markings,
)
from wsp_adapters.doccollection.testing import PUBLIC_A, RESTRICTED_C, make_pdf
from wsp_core.model import FetchResult, RemoteFile

RULES = Rules()


@pytest.mark.parametrize(
    "text",
    [
        "\ufb01ring tables",  # the fi ligature, as PDF text layers extract it
        "Shaped\u2011Charges",  # U+2011 non-breaking hyphen
        "Weapon\u00adization of agents",  # soft hyphen
        "Weapon\u200bization of agents",  # zero-width space
        "Weapon\u2060ization of agents",  # word joiner
        "weapon-\nization of agents",  # hyphenated across a line break
        "weapon\u2010\n  ization of agents",  # Unicode hyphen across a line break
        "\uff26\uff49\uff52\uff49\uff4e\uff47 \uff54\uff41\uff42\uff4c\uff45\uff53",  # fullwidth
        "F I R I N G  T A B L E S",  # letter-spaced, double space between words
        "F I R I N G T A B L E S",  # letter-spaced, uniform: only the squeezed copy can match
        "Demolition\u2013charge calculations",  # en dash between the words
        "MISSILE\u00a0GUIDANCE",  # no-break space
    ],
)
def test_content_screen_normalizes_before_matching(text: str) -> None:
    decision = classify_content(text, RULES)
    assert decision.verdict is Verdict.DENY, (text, decision)


@pytest.mark.parametrize(
    "title",
    ["\ufb01ring table", "Weapon\u00adization of anthrax", "S H A P E D  C H A R G E S"],
)
def test_metadata_screen_normalizes_titles(title: str) -> None:
    decision = classify_metadata(DocFacts(key="k", title=title), Rules(allow_all=True))
    assert decision.verdict is Verdict.DENY, (title, decision)


def test_neutral_phrases_still_apply_after_normalization() -> None:
    assert classify_content("Nuclear\u00a0power plant safety", RULES).verdict is Verdict.ALLOW
    # A neutral phrase must not hide a deny phrase in a squeezed letter-spaced run.
    assert classify_content("N U C L E A R  W E A P O N  D E S I G N", RULES).verdict is Verdict.DENY


def test_ordinary_text_is_not_letter_spaced() -> None:
    clean = "A plan is a way to reach a goal. Use it for a day or a week at a time."
    assert classify_content(clean, RULES).verdict is Verdict.ALLOW


def test_dash_table_covers_unicode_pd() -> None:
    pd = {chr(c) for c in range(sys.maxunicode + 1) if unicodedata.category(chr(c)) == "Pd"}
    assert pd <= set(DASHES)


@pytest.mark.parametrize(
    "pub",
    [
        "FM-3-22.9",
        "FM\u20113\u201122.9",
        "FM 3\u201322.9",
        "FM 3 - 22.9",
        "fm3-22.9",
        "\uff26\uff2d 3-22.9",
        "FM_3-22.9",
        "FM\u00a03\u221222.9",
    ],
)
def test_pub_number_dash_variants_hit_deny_series(pub: str) -> None:
    rules = Rules(allow_all=True).merged({"deny_series": ["FM 3-22.9"], "deny_series_prefix": ["TC 3-22."]})
    assert normalize_pub_number(pub) == "FM 3-22.9"
    decision = classify_metadata(DocFacts(key="k", pub_number=pub, title="Rifle drill"), rules)
    assert str(decision) == "deny:deny-series=FM 3-22.9"
    prefixed = classify_metadata(DocFacts(key="k", pub_number="TC\u20113\u201122.9", title="Drill"), rules)
    assert str(prefixed) == "deny:deny-series=TC 3-22."


def test_pub_number_keeps_series_with_letters() -> None:
    assert normalize_pub_number("FAA-H-8083-25B") == "FAA-H-8083-25B"
    assert normalize_pub_number("GOVPUB-HS5_100") == "GOVPUB-HS5_100"
    assert normalize_pub_number("fm 5-0") == "FM 5-0"


# --- distribution ------------------------------------------------------------------------------


def test_every_statement_is_read_and_the_most_restrictive_wins() -> None:
    m = parse_markings(
        "DISTRIBUTION STATEMENT A. Approved for public release.\n...\n"
        "Distribution Statement D: DoD and US DoD contractors only."
    )
    assert m.statement == "D"
    assert str(check_distribution(m, ())) == "deny:marking=distribution-D"
    spaced = parse_markings("DISTRIBUTION STATEMENT A.\nD I S T R I B U T I O N  S T A T E M E N T  C")
    assert spaced.statement == "C"


UNMARKED = "MEMORANDUM FOR THE DIRECTOR. Subject: routine matter."


@pytest.mark.parametrize("require", [("public-release",), ("PD",), ("license:CC-BY-4.0",), ("A",)])
def test_requirements_are_checked_not_assumed(require: tuple[str, ...]) -> None:
    decision = check_distribution(parse_markings(UNMARKED), require)
    assert decision.verdict is Verdict.REVIEW, decision


def test_declassified_or_public_release_needs_one_of_them() -> None:
    req = ("declassified", "public-release")
    assert check_distribution(parse_markings(UNMARKED), req, declassified_ok=True).verdict is Verdict.REVIEW
    stamped = parse_markings("Approved for Release: 2004/01/05 CIA-RDP80")
    assert str(check_distribution(stamped, req, declassified_ok=True)) == "allow:marking=declassified"


@pytest.mark.parametrize(
    ("text", "require", "verdict"),
    [
        ("Approved for public release; distribution is unlimited.", "public-release", Verdict.ALLOW),
        ("This work is in the public domain in the United States.", "PD", Verdict.ALLOW),
        ("This publication is not subject to copyright in the United States.", "PD", Verdict.ALLOW),
        ("This work is not in the public domain.", "PD", Verdict.REVIEW),
        ("Licensed under CC BY 4.0.", "license:CC-BY-4.0", Verdict.ALLOW),
        (
            "https://creativecommons.org/licenses/by-nc-sa/3.0/igo",
            "license:CC-BY-NC-SA-3.0-IGO",
            Verdict.ALLOW,
        ),
        (
            "Creative Commons Attribution-NonCommercial-ShareAlike 3.0 IGO",
            "license:CC-BY-NC-SA-3.0-IGO",
            Verdict.ALLOW,
        ),
        ("Licensed under CC BY-NC 4.0.", "license:CC-BY-4.0", Verdict.REVIEW),
        ("Licensed under CC BY 4.0.", "license:CC-BY-NC-SA-3.0-IGO", Verdict.REVIEW),
        ("You may re-use this under the Open Government Licence v3.0.", "license:OGL-UK-3.0", Verdict.ALLOW),
        ("Open Government Licence", "license:OGL-UK-3.0", Verdict.REVIEW),
    ],
)
def test_requirements_match_the_marking(text: str, require: str, verdict: Verdict) -> None:
    assert check_distribution(parse_markings(text), (require,)).verdict is verdict


# --- files: no text means held -----------------------------------------------------------------


def _pdf(tmp_path: Path, name: str, pages: list[list[str]]) -> Path:
    path = tmp_path / name
    path.write_bytes(make_pdf(pages))
    return path


def test_no_text_layer_is_held(tmp_path: Path) -> None:
    scan = _pdf(tmp_path, "scan.pdf", [[], []])
    screen = screen_file(scan, Rules())
    assert str(screen.decision) == "review:text=no-text-layer"
    assert not screen.text_found
    # Only an explicit registry flag lets an image-only scan through (and never past a requirement).
    assert screen_file(scan, Rules(allow_image_only=True)).decision.verdict is Verdict.ALLOW
    assert screen_file(scan, Rules(allow_image_only=True, require=("A",))).decision.verdict is Verdict.REVIEW


def test_extraction_error_is_held(tmp_path: Path) -> None:
    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"%PDF-1.4\n" + b"\x00garbage" * 200)
    screen = screen_file(broken, Rules(allow_image_only=True))
    assert str(screen.decision) == "review:text=extraction-error"


@pytest.mark.parametrize("suffix", [".tif", ".jpg", ".epub", ".bin"])
def test_unsupported_formats_are_held(tmp_path: Path, suffix: str) -> None:
    # EPUB is a zip container: never screened by member names (only a real .zip archive is).
    path = tmp_path / f"doc{suffix}"
    path.write_bytes(b"PK\x03\x04" + b"\x00" * 64)
    screen = screen_file(path, Rules(allow_image_only=True))
    assert str(screen.decision) == "review:text=unsupported-format"


@pytest.mark.parametrize("suffix", [".zip", ".docx"])
def test_a_corrupt_zip_or_word_file_is_held(tmp_path: Path, suffix: str) -> None:
    path = tmp_path / f"doc{suffix}"
    path.write_bytes(b"PK\x03\x04" + b"\x00" * 64)
    assert str(screen_file(path, Rules(allow_image_only=True)).decision) == "review:text=extraction-error"


def test_owner_allow_releases_only_missing_evidence_holds(tmp_path: Path) -> None:
    rules = Rules(require=("public-release",)).merged({"allow_documents": ["NOAA ENC ALL"]})
    path = tmp_path / "enc.zip"
    path.write_bytes(b"PK\x03\x04" + b"\x00" * 64)
    assert screen_file(path, rules).decision.verdict is Verdict.REVIEW
    allowed = screen_file(path, rules, keys=("noaa enc all",))
    assert allowed.decision.verdict is Verdict.ALLOW
    assert "owner=allow" in allowed.decision.rule
    # ... but never past a deny.
    bad = _pdf(tmp_path, "bad.pdf", [["NOAA ENC ALL", "Weaponization of agents", *PUBLIC_A]])
    assert screen_file(bad, rules, keys=("NOAA ENC ALL",)).decision.verdict is Verdict.DENY


def test_a_marked_document_with_text_is_allowed(tmp_path: Path) -> None:
    doc = _pdf(tmp_path, "a.pdf", [PUBLIC_A])
    assert (
        str(screen_file(doc, Rules(require=("public-release",))).decision) == "allow:marking=public-release"
    )


# --- origins -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "ok"),
    [
        ("https://www.armypubs.army.mil/x.pdf", True),
        ("https://WWW.ARMYPUBS.ARMY.MIL./x.pdf", True),
        ("https://www.armypubs.army.mil:443/x.pdf", True),
        ("http://www.armypubs.army.mil/x.pdf", False),
        ("https://www.armypubs.army.mil:8443/x.pdf", False),
        ("https://evil.example\\@www.armypubs.army.mil/x.pdf", False),
        ("https://evil.example%5C@www.armypubs.army.mil/x.pdf", False),
        ("https://evil.example:443@www.armypubs.army.mil/x.pdf", False),
        ("https://www.armypubs.army.mil@evil.example/x.pdf", False),
        ("https://notarmypubs.army.mil/x.pdf", False),
        ("https://[::1/x.pdf", False),
    ],
)
def test_origin_ok_parses_strictly(url: str, ok: bool) -> None:
    assert origin_ok(url, ("armypubs.army.mil",)) is ok


@dataclass(frozen=True, slots=True)
class _Report(FetchResult):
    provenance: dict[str, Any] = field(default_factory=dict[str, Any])


class _RedirectingFetcher:
    """Writes the bytes and reports the hops a real ``HttpFetcher`` would have recorded."""

    def __init__(self, sources: list[dict[str, Any]] | None) -> None:
        self.sources = sources

    def get_bytes(self, url: str, *, max_bytes: int = 64 * 1024 * 1024) -> bytes:
        raise NotImplementedError

    def download(self, remote: RemoteFile, dest: Path) -> FetchResult:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"%PDF-1.4 x")
        prov: dict[str, Any] = {} if self.sources is None else {"sources": self.sources}
        return _Report(dest, 10, "0" * 64, "0" * 64, provenance=prov)


ORIGINS = ("example.gov",)
REMOTE = RemoteFile(url="https://www.example.gov/a.pdf", name="a.pdf")


def _hop(final: str, *redirects: tuple[str, str]) -> list[dict[str, Any]]:
    return [
        {
            "url": REMOTE.url,
            "final_url": final,
            "redirects": [{"url": u, "status": 302, "location": loc} for u, loc in redirects],
        }
    ]


@pytest.mark.parametrize(
    "sources",
    [
        _hop("https://cdn.evil.example/a.pdf", (REMOTE.url, "https://cdn.evil.example/a.pdf")),
        _hop("http://www.example.gov/a.pdf", (REMOTE.url, "http://www.example.gov/a.pdf")),
        # off-origin in the middle of the chain, back on origin at the end
        _hop(
            "https://www.example.gov/b.pdf",
            (REMOTE.url, "https://evil.example/x"),
            ("https://evil.example/x", "https://www.example.gov/b.pdf"),
        ),
        None,  # a fetcher that records provenance but not its hops: unverifiable
    ],
)
def test_every_redirect_hop_must_stay_on_origin(tmp_path: Path, sources: list[dict[str, Any]] | None) -> None:
    dest = tmp_path / "a.pdf"
    with pytest.raises(OriginError):
        download_from_origins(_RedirectingFetcher(sources), REMOTE, dest, ORIGINS)
    assert not dest.exists()  # off-origin bytes are deleted at once


def test_on_origin_redirects_and_relative_locations_pass(tmp_path: Path) -> None:
    sources = _hop("https://www.example.gov/b.pdf", (REMOTE.url, "/b.pdf"))
    result = download_from_origins(_RedirectingFetcher(sources), REMOTE, tmp_path / "a.pdf", ORIGINS)
    assert result.path.is_file()
    assert off_origin_hops(object(), ORIGINS) == []  # no provenance attribute: no HTTP, no hops


def test_configured_off_origin_mirror_is_refused_before_any_request(tmp_path: Path) -> None:
    remote = RemoteFile(url=REMOTE.url, name="a.pdf", mirrors=("https://mirror.example.com/a.pdf",))
    with pytest.raises(OriginError):
        download_from_origins(_RedirectingFetcher([]), remote, tmp_path / "a.pdf", ORIGINS)
    assert not (tmp_path / "a.pdf").exists()


def test_a_word_document_is_read_and_screened(tmp_path: Path) -> None:
    import zipfile

    from wsp_adapters.doccollection.adapter import extract_text

    body = (
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'
        "<w:p><w:r><w:t>Community resilience </w:t></w:r><w:r><w:t>guidance</w:t></w:r></w:p>"
        "<w:p><w:r><w:t>Plan for flooding and power cuts in your area.</w:t></w:r></w:p>"
        "</w:body></w:document>"
    )
    path = tmp_path / "guide.docx"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("word/document.xml", body)
    got = extract_text(path, max_pages=3)
    assert got.problem == ""
    assert got.text == "Community resilience guidance\nPlan for flooding and power cuts in your area."


def test_a_data_file_passes_only_in_a_collection_of_data_files(tmp_path: Path) -> None:
    """A planet extract or bathymetry grid has no text to screen (owner request 2026-10-18): held
    unless the registry says the collection is data files; a readable restricted file stays denied."""
    pbf = tmp_path / "planet-261019_0800.osm.pbf"
    pbf.write_bytes(b"\x00\x00\x00\x0d\n\tOSMHeader" + bytes(range(200)))
    assert screen_file(pbf, Rules()).decision.verdict is Verdict.REVIEW
    assert screen_file(pbf, Rules(data_files=True)).decision.verdict is Verdict.ALLOW
    restricted = tmp_path / "c.pdf"
    restricted.write_bytes(make_pdf([RESTRICTED_C]))
    assert screen_file(restricted, Rules(data_files=True)).decision.verdict is Verdict.DENY
