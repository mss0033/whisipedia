"""``rfc 9110`` names the RFC however its source keys it (2026-10-17: the library's are rfc:RFC-9110)."""

from dataclasses import replace

from wsp_api.search import navigate_to
from wsp_api.testing.fakes import SAMPLE_SOURCES, sample_services


def test_a_document_collection_key_is_found_by_its_number() -> None:
    sources = [
        replace(
            s, adapter="doc-collection", raw={"access": {"adapter": "doc-collection", "key_prefix": "rfc"}}
        )
        if s.id == "rfc"
        else s
        for s in SAMPLE_SOURCES
    ]
    services = sample_services(sources=sources)
    held = {"wsp:rfc/rfc:RFC-9110"}
    assert navigate_to(services, "rfc 9110", exists=held.__contains__) == "wsp:rfc/rfc:RFC-9110"
    assert navigate_to(services, "RFC9110", exists=held.__contains__) == "wsp:rfc/rfc:RFC-9110"
    assert navigate_to(services, "rfc 9999", exists=held.__contains__) is None


def test_a_legal_citation_names_its_section() -> None:
    from wsp_core.model import SourceSpec, SourceStatus, Tier

    law = [
        SourceSpec(id=sid, name=sid, tier=Tier.T1, status=SourceStatus.ACTIVE, license="PD",
                   legal_basis="public-domain", domain=("law",), adapter="us-law")
        for sid in ("us-code", "ecfr-federal-register")
    ]  # fmt: skip
    services = sample_services(sources=[*SAMPLE_SOURCES, *law])
    held = {"wsp:us-code/t26/s501", "wsp:ecfr-federal-register/t21/s101.9"}
    for q in ("26 USC 501", "26 U.S.C. § 501", "26 usc 501", "26 U.S.C.A. § 501", "26 U.S.C. sec. 501"):
        assert navigate_to(services, q, exists=held.__contains__) == "wsp:us-code/t26/s501", q
    for q in ("21 CFR 101.9", "21 C.F.R. § 101.9"):
        assert navigate_to(services, q, exists=held.__contains__) == "wsp:ecfr-federal-register/t21/s101.9", q
    assert navigate_to(services, "26 USC 502", exists=held.__contains__) is None
    assert navigate_to(services, "21 CFR 101", exists=held.__contains__) is None  # a part, not a section
