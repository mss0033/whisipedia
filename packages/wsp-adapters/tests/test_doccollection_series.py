"""Numberless releases of one publication: the series rule labels older releases superseded."""

from __future__ import annotations

from wsp_adapters.doccollection.policy import (
    Currency,
    CurrencyRules,
    Edition,
    assign_currency,
    title_date,
)

SERIES = {"series": [r"^(?!.*diagram)(?=.*?(20\d\d-20\d\d))(?=.*?\b(technician|general|extra)\b)"]}

TITLES = {
    "t1": "2026-2030 Technician Pool and Syllabus Public Release December 18 2025.pdf",
    "t2": "2026-2030 Technician Pool and Syllabus Public Release Feb 19 2026.pdf",
    "td": "2026-2030 Technician Pool 3 Diagrams.pdf",
    "e3": "2024-2028 Extra Class Question Pool and Syllabus Public Release with 3rd Errata Sept 25 2025.pdf",
    "e4": "2024-2028 Extra Class Question Pool and Syllabus Public Release with 4th Errata Feb 4 2026.pdf",
    "g5": "2023-2027 General Class Pool and Syllabus 2023-2027 Public Release"
    " with 5th Errata Nov 8 2024 .pdf",
    "g6": "General Class Pool and Syllabus 2023-2027 Public Release with 6th Errata Feb 4 2026.pdf",
}


def test_title_dates() -> None:
    assert title_date(TITLES["t1"]) == "2025-12-18"
    assert title_date(TITLES["e3"]) == "2025-09-25"
    assert title_date("Field Manual 2022") == ""


def test_series_editions_are_superseded_by_the_newer_release() -> None:
    # Edition dates as discovery found them: only the pool's first year; fetched in key order.
    eds = [
        Edition(k, "", t[:4] if t[0] == "2" else "", f"2026-10-11T00:00:0{i}", t)
        for i, (k, t) in enumerate(sorted(TITLES.items()))
    ]
    labels = assign_currency(eds, CurrencyRules.from_access({"currency": SERIES}))
    current = {k for k, v in labels.items() if v.currency is Currency.CURRENT}
    assert current == {"t2", "td", "e4", "g6"}
    assert labels["t1"].superseded_by == "t2"
    assert labels["t1"].banner == f"Superseded by {TITLES['t2']} (2026-02-19)."
    assert labels["g5"].superseded_by == "g6"


def test_without_the_rule_nothing_changes() -> None:
    eds = [Edition(k, "", "", "", t) for k, t in TITLES.items()]
    labels = assign_currency(eds, CurrencyRules())
    assert all(v.currency is Currency.CURRENT for v in labels.values())
