from datetime import UTC, datetime, timedelta

import pytest

from wsp_core.model import SourceStatus
from wsp_orchestrator.policy import (
    Candidate,
    CandidateKind,
    Decision,
    GateConfig,
    Observation,
    PauseReason,
    check_promotion,
    evaluate_candidate,
    rubric_score,
    size_trend,
)
from wsp_orchestrator.testing import make_spec

NOW = datetime(2026, 10, 15, tzinfo=UTC)
MONTH = timedelta(days=30)
GOOD = {
    "authority": 4,
    "density": 4,
    "uniqueness": 4,
    "license": 5,
    "bulk": 4,
    "health": 4,
    "longevity": 4,
    "cost": 3,
}


# --- sanity checks -------------------------------------------------------------------------------


@pytest.mark.parametrize(("new", "ok"), [(1000, True), (980, True), (979, False), (1500, True)])
def test_doc_count_drop_limit(new: int, ok: bool) -> None:
    assert check_promotion([900, 1000], [], doc_count=new, size_bytes=None).ok is ok


def test_size_trend_projects_growth() -> None:
    assert size_trend([]) is None
    assert size_trend([100]) == 100
    assert size_trend([100, 110, 120]) == pytest.approx(130)
    assert size_trend([300, 200, 100]) == 100  # projection <= 0 falls back to the last value


@pytest.mark.parametrize(
    ("size", "ok"), [(1300, True), (1430, True), (1431, False), (1169, False), (1171, True)]
)
def test_size_within_ten_percent_of_trend(size: int, ok: bool) -> None:
    result = check_promotion([], [1000, 1100, 1200], doc_count=None, size_bytes=size)
    assert result.expected_size == pytest.approx(1300)
    assert result.ok is ok


def test_no_history_passes() -> None:
    assert check_promotion([], [], doc_count=0, size_bytes=0).ok


# --- auto-pause ----------------------------------------------------------------------------------


def _findings(**kw: object) -> set[PauseReason]:
    from wsp_orchestrator.policy import auto_pause_findings

    args: dict[str, object] = {
        "observation": Observation(),
        "known_format": None,
        "checksum_failures": 0,
        "last_new_release_at": NOW - timedelta(days=10),
        "cadence_period": MONTH,
        "now": NOW,
    }
    args.update(kw)
    spec = make_spec("src", license="CC-BY-SA-4.0")
    return {f.reason for f in auto_pause_findings(spec, **args)}  # type: ignore[arg-type]


def test_healthy_source_is_not_paused() -> None:
    assert _findings(observation=Observation(license="cc-by-sa-4.0", format="zim6")) == set()


def test_license_change_pauses() -> None:
    assert _findings(observation=Observation(license="CC-BY-NC-4.0")) == {PauseReason.LICENSE_CHANGED}


def test_checksum_failures_pause_at_two() -> None:
    assert _findings(checksum_failures=1) == set()
    assert _findings(checksum_failures=2) == {PauseReason.CHECKSUM_FAILURES}


def test_stale_beyond_three_cadences() -> None:
    assert _findings(last_new_release_at=NOW - 3 * MONTH) == set()
    assert _findings(last_new_release_at=NOW - 3 * MONTH - timedelta(days=1)) == {PauseReason.STALE}
    published = Observation(newest_published=NOW - 4 * MONTH)
    assert _findings(observation=published, last_new_release_at=NOW) == {PauseReason.STALE}


def test_format_change_pauses() -> None:
    assert _findings(observation=Observation(format="zim6"), known_format="zim6") == set()
    assert _findings(observation=Observation(format="zim7"), known_format="zim6") == {
        PauseReason.FORMAT_CHANGED
    }
    assert _findings(observation=Observation(format="zim7"), known_format=None) == set()


# --- policy gate ---------------------------------------------------------------------------------

ACTIVE = make_spec("wikipedia-en-zim", license="CC-BY-SA-4.0")
CANDIDATE_SRC = make_spec("cand", status=SourceStatus.CANDIDATE, license="CC-BY-SA-4.0")
CFG = GateConfig(
    license_allowlist=frozenset({"CC-BY-SA-4.0", "CC0-1.0"}),
    kiwix_categories=frozenset({"wikipedia", "stack_exchange"}),
    domain_budget_bytes={"encyclopedia": 1000},
    domain_used_bytes={"encyclopedia": 600},
)


def cand(**kw: object) -> Candidate:
    base: dict[str, object] = {
        "key": "kiwix:wikipedia_en_all_maxi_2026-11",
        "name": "Wikipedia EN maxi 2026-11",
        "kind": CandidateKind.NEW_VERSION,
        "license": "CC-BY-SA-4.0",
        "domain": ("encyclopedia",),
        "found_by": "kiwix-opds",
        "scores": GOOD,
        "size_bytes": 300,
        "existing_source_id": "wikipedia-en-zim",
    }
    base.update(kw)
    return Candidate(**base)  # type: ignore[arg-type]


def test_rubric_weights_sum_to_one_and_score() -> None:
    score, breakdown = rubric_score({k: 5 for k in GOOD})
    assert score == pytest.approx(5.0)
    assert rubric_score(GOOD)[0] == pytest.approx(4.1)
    assert rubric_score({"license": 5})[0] is None
    assert len(breakdown) == 8


def test_new_version_of_active_source_auto_promotes() -> None:
    p = evaluate_candidate(cand(), CFG, [ACTIVE])
    assert p.decision is Decision.AUTO_PROMOTE
    assert all(c.passed for c in p.checks)
    assert p.title.startswith("registry: add")
    assert "| **total** |" in p.body
    js = p.to_json()
    assert js["decision"] == "auto-promote"
    assert js["candidate"]["existing_source_id"] == "wikipedia-en-zim"


@pytest.mark.parametrize(
    ("override", "failing"),
    [
        (
            {"license": "CC-BY-NC-4.0", "kind": CandidateKind.KIWIX_TITLE, "category": "wikipedia"},
            "license-allowlist",
        ),
        ({"size_bytes": 500}, "storage-budget"),
        ({"size_bytes": None}, "storage-budget"),
        ({"domain": ("maps",)}, "storage-budget"),
        ({"scores": {**GOOD, "authority": 1, "density": 1, "uniqueness": 2}}, "score"),
        ({"scores": {"license": 5}}, "score"),
        ({"existing_source_id": "cand"}, "relation"),
        ({"kind": CandidateKind.NEW_SOURCE, "existing_source_id": None}, "relation"),
        ({"kind": CandidateKind.KIWIX_TITLE, "category": "gutenberg"}, "relation"),
    ],
)
def test_anything_else_goes_to_review(override: dict[str, object], failing: str) -> None:
    p = evaluate_candidate(cand(**override), CFG, [ACTIVE, CANDIDATE_SRC])
    assert p.decision is Decision.REVIEW
    assert [c.name for c in p.checks if not c.passed] == [failing]


def test_kiwix_title_in_allowed_category_auto_promotes() -> None:
    p = evaluate_candidate(
        cand(kind=CandidateKind.KIWIX_TITLE, category="stack_exchange", existing_source_id=None),
        CFG,
        [ACTIVE],
    )
    assert p.decision is Decision.AUTO_PROMOTE


def test_new_version_with_changed_license_needs_review() -> None:
    cfg = GateConfig(
        license_allowlist=frozenset({"CC-BY-SA-4.0", "CC0-1.0"}),
        domain_budget_bytes={"encyclopedia": 10_000},
    )
    p = evaluate_candidate(cand(license="CC0-1.0"), cfg, [ACTIVE])
    assert p.decision is Decision.REVIEW
    assert "license differs" in next(c.detail for c in p.checks if c.name == "relation")


def test_license_hard_gate_rejects() -> None:
    p = evaluate_candidate(cand(scores={**GOOD, "license": 2}), CFG, [ACTIVE])
    assert p.decision is Decision.REJECT
    assert p.title.startswith("registry: exclude")
