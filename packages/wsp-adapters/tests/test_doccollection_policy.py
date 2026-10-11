"""The deterministic document filter (PLAN §13, D13; research doc §3, §5.3)."""

from __future__ import annotations

from pathlib import Path

import pytest

from wsp_adapters.doccollection.policy import (
    Currency,
    CurrencyRules,
    DocFacts,
    Edition,
    Rules,
    Stage,
    Verdict,
    assign_currency,
    check_distribution,
    classify_content,
    classify_metadata,
    load_rules,
    normalize_pub_number,
    parse_markings,
)
from wsp_core.banners import HISTORICAL_BANNER

REPO = Path(__file__).resolve().parents[3]
APD = load_rules({"rules": "registry/filters/army-pubs-public.yaml", "default": "review"}, repo_root=REPO)


def decide(pub: str, title: str, rules: Rules = APD) -> str:
    return str(classify_metadata(DocFacts(key=f"k:{pub}", pub_number=pub, title=title), rules))


@pytest.mark.parametrize(
    ("pub", "title", "expected"),
    [
        ("FM 5-0", "Planning and Orders Production", "allow:series=FM 5-0"),
        ("ADP 6-22", "Army Leadership and the Profession", "allow:series=ADP"),
        ("TC 4-02.1", "First Aid", "allow:series=TC 4-02.1"),
        ("FM 3-09", "Fire Support and Field Artillery Operations", "deny:safety=fire support"),
        ("ATP 3-09.32", "Multi-Service TTP for Joint Application of Firepower", "deny:deny-series=ATP 3-09"),
        ("TC 3-22.9", "Rifle and Carbine", "deny:deny-series=TC 3-22."),
        ("ATP 3-60", "Targeting", "deny:deny-series=ATP 3-60"),
        ("", "Joint Targeting Process", "deny:safety=joint targeting"),
        ("FM 3-01.13", "(U) Air Defense Planning Data (S/Nf)", "deny:marking=classified-portion"),
        ("ATP 3-34.4", "Engineer Reference", "allow:series=ATP 3-34.4"),
        ("ATP 9-99", "Unheard-Of Series", "review:default"),
        ("FM 6-27", "Commander's Handbook on the Law of Land Warfare", "allow:series=FM 6-27"),
        ("ATP 4-02.2", "Medical Evacuation in a Chemical Environment", "review:keyword=chemical"),
    ],
)
def test_army_series_rules(pub: str, title: str, expected: str) -> None:
    assert decide(pub, title) == expected


def test_deny_beats_allow_and_owner_decisions_are_remembered() -> None:
    rules = APD.merged({"allow_series_prefix": ["TC 3-"]})
    assert decide("TC 3-22.9", "Rifle and Carbine", rules).startswith("deny:")
    owner = APD.merged({"allow_documents": ["ATP 9-99"], "deny_documents": ["FM 1"]})
    assert decide("ATP 9-99", "Unheard-Of Series", owner) == "allow:owner=allow"
    assert decide("FM 1", "The Army", owner) == "deny:owner=deny"


def test_neutral_phrases_keep_defensive_documents_out_of_review() -> None:
    rules = Rules(allow_all=True)
    assert decide("", "Bomb Threat Guidance", rules) == "allow:collection=allow-all"
    assert decide("", "Radiological Emergency Medical Management", rules) == "allow:collection=allow-all"
    assert decide("", "Weaponization of Biological Agents", rules) == "deny:safety=weaponization"
    assert decide("", "Improvised Explosive Device Awareness", rules) == "review:keyword=improvised explosive"


def test_restricted_rights_field_is_denied_before_fetch() -> None:
    rules = Rules(allow_all=True)
    got = classify_metadata(
        DocFacts(key="x", title="Report", rights="Distribution authorized to U.S. Government agencies only"),
        rules,
    )
    assert got.verdict is Verdict.DENY
    assert got.alert
    assert got.rule == "marking=distribution-limited"


def test_normalize_pub_number() -> None:
    assert normalize_pub_number("fm5-0") == "FM 5-0"
    assert normalize_pub_number(" ATP  3-39.32 ") == "ATP 3-39.32"


@pytest.mark.parametrize(
    ("text", "statement", "restricted", "export"),
    [
        ("DISTRIBUTION STATEMENT A. Approved for public release; distribution is unlimited.", "A", (), ()),
        ("DISTRIBUTION RESTRICTION: Approved for public release; distribution is unlimited.", "A", (), ()),
        (
            "DISTRIBUTION STATEMENT D. Distribution authorized to DoD and U.S. DoD contractors only",
            "D",
            ("distribution-limited",),
            (),
        ),
        ("FOR OFFICIAL USE ONLY", None, ("fouo",), ()),
        ("CUI\nControlled by: DoD", None, ("cui",), ()),
        (
            "This document contains technical data whose export is restricted (ITAR)",
            None,
            (),
            ("itar", "export-warning"),
        ),
        ("Protecting Controlled Unclassified Information in Nonfederal Systems", None, (), ()),
    ],
)
def test_parse_markings(
    text: str, statement: str | None, restricted: tuple[str, ...], export: tuple[str, ...]
) -> None:
    m = parse_markings(text)
    assert m.statement == statement
    assert m.restricted == restricted
    assert m.export_control == export


def test_distribution_gate() -> None:
    a = parse_markings("Approved for public release; distribution is unlimited.")
    assert check_distribution(a, ("A",)).verdict is Verdict.ALLOW
    none = parse_markings("Planning handbook")
    held = check_distribution(none, ("A",))
    assert held.verdict is Verdict.REVIEW
    assert held.stage is Stage.DISTRIBUTION
    c = parse_markings("DISTRIBUTION STATEMENT C. Distribution authorized to U.S. Government agencies")
    rejected = check_distribution(c, ("A",))
    assert rejected.verdict is Verdict.DENY
    assert rejected.alert
    itar = parse_markings(
        "Approved for public release. Subject to the International Traffic in Arms Regulations."
    )
    assert check_distribution(itar, ()).rule.startswith("marking=export-control")


def test_declassified_banners_need_a_release_stamp() -> None:
    stamped = parse_markings("SECRET\nApproved For Release 2006/11/27 : CIA-RDP84-00499R000900030045-6")
    assert check_distribution(stamped, (), declassified_ok=True).verdict is Verdict.ALLOW
    assert check_distribution(stamped, ()).verdict is Verdict.DENY
    bare = parse_markings("SECRET\nCable traffic")
    assert check_distribution(bare, (), declassified_ok=True).verdict is Verdict.DENY


def test_content_screen() -> None:
    rules = Rules()
    assert (
        classify_content("Planning and orders production\nContents\nChapter 1 Fundamentals", rules).verdict
        is Verdict.ALLOW
    )
    assert classify_content("SNIPER TRAINING\nContents", rules).verdict is Verdict.DENY
    body = "x" * 4000 + "\nAppendix: joint targeting\nAnnex: fire support\nAnnex: munitions"
    assert classify_content(body, rules).rule == "safety=joint targeting+fire support+munitions"
    one = "x" * 4000 + "\nAppendix: munitions handling safety"
    assert classify_content(one, rules).verdict is Verdict.REVIEW


def test_currency_superseded_chain_and_historical_rules() -> None:
    eds = [
        Edition("army:FM-5-0:2014", "FM 5-0", "2014-03-01"),
        Edition("army:FM-5-0:2022", "FM 5-0", "2022-11-04"),
        Edition("army:FM-5-0:2010", "FM 5-0", "2010-03-18"),
        Edition("army:FM-6-0:2022", "FM 6-0", "2022-05-16"),
    ]
    labels = assign_currency(eds, CurrencyRules())
    assert labels["army:FM-5-0:2022"].currency is Currency.CURRENT
    assert labels["army:FM-5-0:2014"].currency is Currency.SUPERSEDED
    assert labels["army:FM-5-0:2014"].superseded_by == "army:FM-5-0:2022"
    assert labels["army:FM-5-0:2010"].superseded_by == "army:FM-5-0:2014"
    assert labels["army:FM-5-0:2014"].banner == "Superseded by FM 5-0 (2022-11-04)."
    assert labels["army:FM-6-0:2022"].banner is None
    old = assign_currency(eds, CurrencyRules(historical_before="2012-01-01"))
    assert old["army:FM-5-0:2010"].historical
    assert old["army:FM-5-0:2010"].banner == HISTORICAL_BANNER
    bulletins = assign_currency(
        [Edition("usda-fb:FB-1", "FB 1", "1922")], CurrencyRules(collection=Currency.HISTORICAL)
    )
    assert bulletins["usda-fb:FB-1"].currency is Currency.HISTORICAL
    assert bulletins["usda-fb:FB-1"].banner == "Historical document — methods may be outdated or unsafe."


def test_every_registry_rules_file_loads() -> None:
    for path in sorted((REPO / "registry" / "filters").glob("*.yaml")):
        rules = load_rules({"rules": str(path)})
        assert rules.safety, path.name


def test_deny_phrase_only_in_an_abstract_is_held_for_review() -> None:
    facts = DocFacts(
        key="who:1",
        title="Operational manual on larval source management",
        abstract="Guidance on vector control; see also fire control of vegetation.",
    )
    got = classify_metadata(facts, Rules(allow_all=True))
    assert str(got) == "review:safety-abstract=fire control"


def test_builtin_review_can_be_switched_off_for_official_histories_only() -> None:
    rules = Rules(allow_all=True, builtin_review=False)
    assert decide("", "Soviet Nuclear Weapons Programme, 1962", rules) == "allow:collection=allow-all"
    held = decide("", "Weapons design handbook", rules.merged({"review_keywords": ["weapons design"]}))
    assert held == "review:keyword=weapons design"
    assert decide("", "Weaponization of agents", rules) == "deny:safety=weaponization"
