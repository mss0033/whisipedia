"""Retention planner: which sealed generations may be pruned (PLAN §6.4).

Grandfather-father-son: keep the last ``last`` generations, the newest generation of each of the
``monthly`` most recent months that have generations, and the newest of each year (forever by default).
Months and years are counted over *existing* generations, not wall-clock time, so downtime never
causes extra pruning.

Hard guarantees, whatever the policy says:

* the newest sealed generation and the current (promoted) generation are never pruned;
* a generation that ``holds_last_copy`` (it is the last copy of something gone from upstream) is
  never pruned;
* only ``SEALED`` generations are ever pruned; staging and degraded ones are left alone;
* **known-good anchoring** (ADR-0009): the newest generation that passed every pre-promotion check
  (``known_good``: it was promoted) is never pruned, and neither is the newest one that is also
  ``settled`` (promoted ``integrity.settle_days`` ago and fully verified by the scrub since). While
  a new generation is still settling, the older anchor therefore stays.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date

from wsp_core.model import Generation, GenerationStatus

_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")


@dataclass(frozen=True, slots=True)
class GfsPolicy:
    last: int = 3
    monthly: int = 12
    yearly: int | None = None  # None = forever

    def __post_init__(self) -> None:
        if self.last < 1:
            raise ValueError("retention must keep at least the last generation")
        if self.monthly < 0 or (self.yearly is not None and self.yearly < 0):
            raise ValueError("retention counts must be non-negative")

    @classmethod
    def from_registry(cls, retention: Mapping[str, object] | None) -> GfsPolicy | None:
        """Read ``retention: {gfs: {last, monthly, yearly}}`` from a registry entry.

        Returns ``None`` for non-GFS policies (e.g. base + segments), which the planner does not prune.
        A missing ``retention`` block yields the PLAN default (3 / 12 / forever).
        """
        if not retention:
            return cls()
        gfs = retention.get("gfs")
        if not isinstance(gfs, Mapping):
            return None
        g: Mapping[str, object] = gfs  # pyright: ignore[reportUnknownVariableType]
        yearly = g.get("yearly", "forever")
        return cls(
            last=int(str(g.get("last", 3))),
            monthly=int(str(g.get("monthly", 12))),
            yearly=None if yearly in (None, "forever") else int(str(yearly)),
        )


@dataclass(frozen=True, slots=True)
class GenerationInfo:
    generation_id: str
    taken: date
    status: GenerationStatus = GenerationStatus.SEALED
    holds_last_copy: bool = False
    known_good: bool = False  # passed every check before promotion (it was promoted)
    settled: bool = False  # known good, old enough and verified by the scrub since

    @classmethod
    def from_generation(
        cls, g: Generation, *, holds_last_copy: bool = False, known_good: bool = False, settled: bool = False
    ) -> GenerationInfo:
        return cls(g.generation_id, generation_date(g), g.status, holds_last_copy, known_good, settled)


def generation_date(g: Generation) -> date:
    """Date a generation represents: a leading ``YYYY-MM-DD`` in its id, else its seal time."""
    m = _DATE_RE.match(g.generation_id)
    if m:
        return date(int(m[1]), int(m[2]), int(m[3]))
    if g.sealed_at is not None:
        return g.sealed_at.date()
    raise ValueError(f"cannot date generation {g.source_id}/{g.generation_id}")


@dataclass(frozen=True, slots=True)
class RetentionPlan:
    keep: tuple[str, ...]
    prune: tuple[str, ...]
    reasons: Mapping[str, tuple[str, ...]] = field(default_factory=dict[str, tuple[str, ...]])


def plan_retention(
    generations: Sequence[GenerationInfo], policy: GfsPolicy, *, current: str | None = None
) -> RetentionPlan:
    ids = [g.generation_id for g in generations]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate generation ids")
    reasons: dict[str, list[str]] = {}

    def keep(gid: str, why: str) -> None:
        reasons.setdefault(gid, []).append(why)

    sealed = sorted(
        (g for g in generations if g.status is GenerationStatus.SEALED),
        key=lambda g: (g.taken, g.generation_id),
        reverse=True,
    )
    for g in generations:
        if g.status in (GenerationStatus.STAGING, GenerationStatus.DEGRADED):
            keep(g.generation_id, f"status {g.status.value}")
    if sealed:
        keep(sealed[0].generation_id, "newest")
    for g in sealed:
        if g.generation_id == current:
            keep(g.generation_id, "current")
        if g.holds_last_copy:
            keep(g.generation_id, "last copy of content gone upstream")
    for g in sealed[: policy.last]:
        keep(g.generation_id, f"last {policy.last}")
    good = next((g for g in sealed if g.known_good), None)
    if good is not None:
        keep(good.generation_id, "newest known-good")
    anchor = next((g for g in sealed if g.known_good and g.settled), None)
    if anchor is not None:
        keep(anchor.generation_id, "known-good anchor (settled and scrubbed)")

    seen_months: list[tuple[int, int]] = []
    seen_years: list[int] = []
    for g in sealed:  # newest first, so the first generation seen in a bucket is its newest
        month = (g.taken.year, g.taken.month)
        if month not in seen_months:
            seen_months.append(month)
            if len(seen_months) <= policy.monthly:
                keep(g.generation_id, f"monthly {month[0]}-{month[1]:02d}")
        if g.taken.year not in seen_years:
            seen_years.append(g.taken.year)
            if policy.yearly is None or len(seen_years) <= policy.yearly:
                keep(g.generation_id, f"yearly {g.taken.year}")

    kept = tuple(g.generation_id for g in generations if g.generation_id in reasons)
    pruned = tuple(g.generation_id for g in sealed[::-1] if g.generation_id not in reasons)
    return RetentionPlan(keep=kept, prune=pruned, reasons={k: tuple(v) for k, v in reasons.items()})
