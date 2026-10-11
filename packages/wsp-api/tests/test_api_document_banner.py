"""Historical / superseded banners on documents (PLAN D13): ``currency`` and ``banner`` fields."""

from __future__ import annotations

from wsp_api.convert import document
from wsp_core.banners import HISTORICAL_BANNER, banner_for
from wsp_core.ids import content_hash, make_doc_id
from wsp_core.model import DocRecord


def rec(meta: dict[str, str]) -> DocRecord:
    return DocRecord(
        doc_id=make_doc_id("usda-farmers-bulletins", "usda-fb:FB-1"),
        source_id="usda-farmers-bulletins",
        native_id="usda-fb:FB-1",
        title="Canning fruits and vegetables at home",
        lang="en",
        license="LicenseRef-US-Gov-PD",
        text="Old advice.",
        content_hash=content_hash("Old advice."),
        meta=meta,
    )


def test_historical_document_carries_the_banner() -> None:
    out = document(rec({"currency": "historical", "historical": "true"}), spec=None)
    assert out.currency == "historical"
    assert out.banner == HISTORICAL_BANNER == "Historical document — methods may be outdated or unsafe."


def test_superseded_and_plain_documents() -> None:
    sup = document(rec({"currency": "superseded", "banner": "Superseded by FM 5-0 (2022-11-04)."}), spec=None)
    assert (sup.currency, sup.banner) == ("superseded", "Superseded by FM 5-0 (2022-11-04).")
    plain = document(rec({}), spec=None)
    assert plain.currency is None
    assert plain.banner is None
    assert banner_for({"currency": "rescinded"}) == "Rescinded by the issuer — do not follow."
