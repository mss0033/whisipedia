from datetime import UTC, date, datetime

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from wsp_core.model import Generation, GenerationStatus
from wsp_orchestrator.retention import (
    GenerationInfo,
    GfsPolicy,
    generation_date,
    plan_retention,
)

SEALED = GenerationStatus.SEALED


def monthly(start_year: int, months: int) -> list[GenerationInfo]:
    out: list[GenerationInfo] = []
    for i in range(months):
        y, m = divmod(start_year * 12 + i, 12)
        out.append(GenerationInfo(f"{y}-{m + 1:02d}-01", date(y, m + 1, 1)))
    return out


def test_default_policy_keeps_last3_monthly12_yearly_forever() -> None:
    gens = monthly(2020, 72)  # 2020-01 .. 2025-12
    plan = plan_retention(gens, GfsPolicy())
    kept = set(plan.keep)
    # last 12 months (2025) all kept; before that one per year: the newest of each year
    assert {f"2025-{m:02d}-01" for m in range(1, 13)} <= kept
    assert {"2020-12-01", "2021-12-01", "2022-12-01", "2023-12-01", "2024-12-01"} <= kept
    assert len(kept) == 12 + 5
    assert set(plan.prune) == {g.generation_id for g in gens} - kept
    assert plan.prune == tuple(sorted(plan.prune)), "prune list is oldest first"


def test_weekly_generations_in_one_month() -> None:
    gens = [GenerationInfo(f"2026-10-{d:02d}", date(2026, 10, d)) for d in (1, 8, 15, 22, 29)]
    plan = plan_retention(gens, GfsPolicy(last=3, monthly=12))
    assert plan.keep == ("2026-10-15", "2026-10-22", "2026-10-29")
    assert plan.prune == ("2026-10-01", "2026-10-08")
    assert "last 3" in plan.reasons["2026-10-29"]
    assert "newest" in plan.reasons["2026-10-29"]


def test_current_last_copy_and_unsealed_are_never_pruned() -> None:
    gens = [
        GenerationInfo("2026-01-01", date(2026, 1, 1)),
        GenerationInfo("2026-01-08", date(2026, 1, 8), holds_last_copy=True),
        GenerationInfo("2026-01-15", date(2026, 1, 15)),
        GenerationInfo("2026-01-22", date(2026, 1, 22), status=GenerationStatus.DEGRADED),
        GenerationInfo("2026-01-29", date(2026, 1, 29), status=GenerationStatus.STAGING),
        GenerationInfo("2025-12-01", date(2025, 12, 1), status=GenerationStatus.PRUNED),
        GenerationInfo("2026-01-30", date(2026, 1, 30)),
    ]
    plan = plan_retention(gens, GfsPolicy(last=1, monthly=0, yearly=0), current="2026-01-01")
    assert plan.prune == ("2026-01-15",)
    assert "2025-12-01" not in plan.keep  # already pruned: neither kept nor pruned again
    assert plan.reasons["2026-01-08"] == ("last copy of content gone upstream",)


def test_from_registry() -> None:
    assert GfsPolicy.from_registry(None) == GfsPolicy(3, 12, None)
    assert GfsPolicy.from_registry({"gfs": {"last": 2, "monthly": 6, "yearly": 5}}) == GfsPolicy(2, 6, 5)
    assert GfsPolicy.from_registry({"gfs": {"last": 2, "monthly": 12, "yearly": "forever"}}) == GfsPolicy(2)
    assert GfsPolicy.from_registry({"base": "yearly", "segments": "P2Y"}) is None
    with pytest.raises(ValueError, match="at least"):
        GfsPolicy(last=0)


def test_generation_date() -> None:
    sealed = datetime(2026, 3, 4, tzinfo=UTC)
    assert generation_date(Generation("s", "2026-10-01T00", SEALED)) == date(2026, 10, 1)
    assert generation_date(Generation("s", "r42", SEALED, sealed_at=sealed)) == date(2026, 3, 4)
    # Kiwix bundle releases: "<YYYY-MM>-<digest>" is dated by its seal (pruning failed on day 70)
    assert generation_date(Generation("s", "2026-10-70d64628", SEALED, sealed_at=sealed)) == date(2026, 3, 4)
    assert generation_date(Generation("s", "2026-10-26596a80", SEALED, sealed_at=sealed)) == date(2026, 3, 4)
    assert generation_date(Generation("s", "2026-10-12-r2", SEALED)) == date(2026, 10, 12)
    assert generation_date(Generation("s", "2026-02-31", SEALED, sealed_at=sealed)) == date(2026, 3, 4)
    with pytest.raises(ValueError, match="cannot date"):
        generation_date(Generation("s", "r42", SEALED))


def test_duplicate_ids_rejected() -> None:
    g = GenerationInfo("a", date(2026, 1, 1))
    with pytest.raises(ValueError, match="duplicate"):
        plan_retention([g, g], GfsPolicy())


# --- properties ----------------------------------------------------------------------------------

statuses = st.sampled_from(list(GenerationStatus))
gen_lists = st.lists(
    st.tuples(st.dates(date(2015, 1, 1), date(2030, 12, 31)), statuses, st.booleans()),
    max_size=60,
).map(
    lambda rows: [
        GenerationInfo(f"{d.isoformat()}-{i}", d, status, last) for i, (d, status, last) in enumerate(rows)
    ]
)
policies = st.builds(
    GfsPolicy,
    last=st.integers(1, 6),
    monthly=st.integers(0, 24),
    yearly=st.one_of(st.none(), st.integers(0, 5)),
)


@settings(max_examples=300, deadline=None)
@given(gens=gen_lists, policy=policies, data=st.data())
def test_plan_invariants(gens: list[GenerationInfo], policy: GfsPolicy, data: st.DataObject) -> None:
    ids = [g.generation_id for g in gens]
    current = data.draw(st.one_of(st.none(), st.sampled_from(ids))) if ids else None
    plan = plan_retention(gens, policy, current=current)
    keep, prune = set(plan.keep), set(plan.prune)
    by_id = {g.generation_id: g for g in gens}
    sealed = [g for g in gens if g.status is SEALED]

    assert not keep & prune
    # only sealed generations are ever pruned
    assert all(by_id[p].status is SEALED for p in prune)
    # every sealed generation is decided; nothing pruned stays unaccounted for
    assert {g.generation_id for g in sealed} <= keep | prune
    # hard guarantees
    assert all(not by_id[p].holds_last_copy for p in prune)
    assert current not in prune
    if sealed:
        newest = max(sealed, key=lambda g: (g.taken, g.generation_id))
        assert newest.generation_id in keep
    # at least min(last, |sealed|) sealed generations survive
    assert len(keep & {g.generation_id for g in sealed}) >= min(policy.last, len(sealed))
    # with yearly=forever every year that had a sealed generation still has one
    if policy.yearly is None:
        years_before = {g.taken.year for g in sealed}
        years_after = {by_id[k].taken.year for k in keep if by_id[k].status is SEALED}
        assert years_before == years_after


@settings(max_examples=200, deadline=None)
@given(gens=gen_lists, policy=policies)
def test_plan_is_idempotent(gens: list[GenerationInfo], policy: GfsPolicy) -> None:
    first = plan_retention(gens, policy)
    survivors = [g for g in gens if g.generation_id not in set(first.prune)]
    assert plan_retention(survivors, policy).prune == ()


@settings(max_examples=200, deadline=None)
@given(gens=gen_lists, policy=policies)
def test_few_generations_are_never_pruned(gens: list[GenerationInfo], policy: GfsPolicy) -> None:
    sealed = [g for g in gens if g.status is SEALED]
    if len(sealed) <= policy.last:
        assert plan_retention(gens, policy).prune == ()
