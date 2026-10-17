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
