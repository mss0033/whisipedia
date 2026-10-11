"""The D15 non-English budget planner (pure function)."""

from __future__ import annotations

from wsp_cli.multilingual import Held, LangCandidate, held_by_language, plan_document, plan_language
from wsp_core.model import SourceSpec, SourceStatus, Tier

GB = 10**9


def kiwix(project: str, flavour: str, gb: float, score: float = 4.0, lang: str = "de") -> LangCandidate:
    return LangCandidate(lang, project, "kiwix", f"{project}_{lang}_all", flavour, int(gb * GB), score)


CANDS = [
    kiwix("wikipedia", "maxi", 120, 4.6),
    kiwix("wikipedia", "nopic", 45, 4.6),
    kiwix("wikipedia", "mini", 12, 4.6),
    kiwix("wiktionary", "nopic", 4, 4.3),
    kiwix("wiktionary", "maxi", 6, 4.3),
    kiwix("wikisource", "maxi", 30, 4.1),
    kiwix("wikibooks", "maxi", 2, 3.6),
    kiwix("wikiquote", "maxi", 1, 3.6, lang="fr"),
]


def test_one_flavour_per_project_within_budget_with_upgrades() -> None:
    plan = plan_language("de", CANDS, 100 * GB)
    names = {c.project: c.flavour for c in plan.selected}
    assert names == {"wikipedia": "nopic", "wiktionary": "maxi", "wikisource": "maxi", "wikibooks": "maxi"}
    assert plan.selected_bytes <= 100 * GB
    assert all(c.lang == "de" for c in plan.selected)


def test_bigger_budget_takes_the_maxi_wikipedia() -> None:
    plan = plan_language("de", CANDS, 300 * GB)
    assert {c.project: c.flavour for c in plan.selected}["wikipedia"] == "maxi"
    assert plan.total_bytes == (120 + 6 + 30 + 2) * GB


def test_held_material_counts_and_dumps_of_held_wikis_are_skipped() -> None:
    dump = LangCandidate("de", "wikipedia", "dump", "dewiki", "dump", 8 * GB, 4.6)
    plan = plan_language("de", [*CANDS, dump], 60 * GB, [Held("wikipedia-top10", "wikipedia", 8 * GB)])
    assert plan.held_bytes == 8 * GB
    assert all(c.kind != "dump" for c in plan.selected)
    assert plan.total_bytes <= 60 * GB


def test_deterministic_and_documented() -> None:
    a = plan_language("de", CANDS, 100 * GB)
    b = plan_language("de", list(reversed(CANDS)), 100 * GB)
    assert [c.selector_name for c in a.selected] == [c.selector_name for c in b.selected]
    doc = plan_document([a], budget_tb=0.1, at="2026-10-10T00:00:00+00:00")
    de = doc["languages"]["de"]
    assert "wikipedia_de_all@nopic" in de["kiwix_selector"]
    assert de["total_gb"] <= 100


def test_held_by_language_reads_wikipedia_dump_sources() -> None:
    spec = SourceSpec(
        id="wikipedia-top10",
        name="Wikipedia",
        tier=Tier.T1,
        status=SourceStatus.ACTIVE,
        license="CC-BY-SA-4.0",
        legal_basis="open-license",
        domain=("encyclopedia",),
        adapter="wikimedia-dumps",
        raw={"languages": ["en", "de"]},
    )
    held = held_by_language([spec], {"dewiki": 9 * GB, "enwiki": 25 * GB})
    assert held["de"] == [Held("wikipedia-top10", "wikipedia", 9 * GB)]
