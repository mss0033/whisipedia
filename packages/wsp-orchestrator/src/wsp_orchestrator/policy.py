"""Policy: promotion sanity checks (PLAN §6.4), auto-pause rules and the candidate policy gate (PLAN §5.4).

Everything here is a pure function over plain values so it can be tested without I/O.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from wsp_core.model import SourceSpec, SourceStatus
from wsp_orchestrator.clock import ensure_utc

# --- Promotion sanity checks (PLAN §6.4) ---------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SanityThresholds:
    max_doc_drop: float = 0.02  # hold if the document count drops by more than 2 %
    size_tolerance: float = 0.10  # hold if size is outside ±10 % of the trend
    trend_window: int = 6  # promoted generations used to fit the size trend


@dataclass(frozen=True, slots=True)
class SanityResult:
    ok: bool
    reasons: tuple[str, ...] = ()
    expected_size: float | None = None


def size_trend(sizes: Sequence[int]) -> float | None:
    """Least-squares linear projection of the next size from past sizes (oldest first)."""
    n = len(sizes)
    if n == 0:
        return None
    if n == 1:
        return float(sizes[0])
    mean_x = (n - 1) / 2
    mean_y = sum(sizes) / n
    sxx = sum((x - mean_x) ** 2 for x in range(n))
    sxy = sum((x - mean_x) * (y - mean_y) for x, y in enumerate(sizes))
    projected = mean_y + (sxy / sxx) * (n - mean_x)
    return projected if projected > 0 else float(sizes[-1])


def check_promotion(
    previous_doc_counts: Sequence[int],
    previous_sizes: Sequence[int],
    *,
    doc_count: int | None,
    size_bytes: int | None,
    thresholds: SanityThresholds = SanityThresholds(),  # noqa: B008 - immutable
) -> SanityResult:
    """Decide whether a candidate generation may be promoted automatically or must be held for review.

    ``previous_*`` are values of earlier promoted generations, oldest first. With no history the
    candidate passes (there is nothing to compare against).
    """
    reasons: list[str] = []
    if previous_doc_counts and doc_count is not None:
        prev = previous_doc_counts[-1]
        if prev > 0 and (prev - doc_count) / prev > thresholds.max_doc_drop:
            drop = (prev - doc_count) / prev
            reasons.append(
                f"document count dropped {drop:.1%} ({prev} -> {doc_count}),"
                f" limit {thresholds.max_doc_drop:.0%}"
            )
    expected = size_trend(previous_sizes[-thresholds.trend_window :]) if previous_sizes else None
    if expected is not None and size_bytes is not None and expected > 0:
        deviation = (size_bytes - expected) / expected
        if abs(deviation) > thresholds.size_tolerance:
            reasons.append(
                f"size {size_bytes} is {deviation:+.1%} from trend {expected:.0f},"
                f" limit ±{thresholds.size_tolerance:.0%}"
            )
    return SanityResult(ok=not reasons, reasons=tuple(reasons), expected_size=expected)


# --- Auto-pause rules (PLAN §5.4) ---------------------------------------------------------------


class PauseReason(StrEnum):
    LICENSE_CHANGED = "license-changed"
    CHECKSUM_FAILURES = "checksum-failures"
    STALE = "stale"
    FORMAT_CHANGED = "format-changed"


@dataclass(frozen=True, slots=True)
class PauseThresholds:
    consecutive_checksum_failures: int = 2
    stale_factor: float = 3.0


@dataclass(frozen=True, slots=True)
class Observation:
    """What a probe or discovery saw upstream.

    ``license`` and ``format`` come from ``ReleaseRef.meta["license"]`` / ``meta["format"]`` by convention
    (see docs/components/orchestrator.md); ``None`` means "not reported" and never triggers a pause.
    """

    license: str | None = None
    format: str | None = None
    newest_published: datetime | None = None


@dataclass(frozen=True, slots=True)
class PauseFinding:
    reason: PauseReason
    detail: str


def _norm_license(value: str) -> str:
    return value.strip().casefold()


def auto_pause_findings(
    spec: SourceSpec,
    *,
    observation: Observation,
    known_format: str | None,
    checksum_failures: int,
    last_new_release_at: datetime | None,
    cadence_period: timedelta,
    now: datetime,
    thresholds: PauseThresholds = PauseThresholds(),  # noqa: B008 - immutable
) -> list[PauseFinding]:
    out: list[PauseFinding] = []
    if observation.license is not None and _norm_license(observation.license) != _norm_license(spec.license):
        out.append(
            PauseFinding(
                PauseReason.LICENSE_CHANGED,
                f"upstream reports {observation.license!r}, registry has {spec.license!r}",
            )
        )
    if checksum_failures >= thresholds.consecutive_checksum_failures:
        out.append(
            PauseFinding(PauseReason.CHECKSUM_FAILURES, f"{checksum_failures} consecutive checksum failures")
        )
    newest = observation.newest_published or last_new_release_at
    if newest is not None:
        age = ensure_utc(now) - ensure_utc(newest)
        limit = cadence_period * thresholds.stale_factor
        if age > limit:
            out.append(
                PauseFinding(
                    PauseReason.STALE,
                    f"newest upstream release is {age.days} days old,"
                    f" over {thresholds.stale_factor:g}x cadence ({limit.days} days)",
                )
            )
    if observation.format is not None and known_format is not None and observation.format != known_format:
        out.append(
            PauseFinding(
                PauseReason.FORMAT_CHANGED, f"format changed from {known_format!r} to {observation.format!r}"
            )
        )
    return out


# --- Candidate policy gate (PLAN §5.2-§5.4) -----------------------------------------------------

RUBRIC_WEIGHTS: Mapping[str, float] = {
    "authority": 0.20,
    "density": 0.15,
    "uniqueness": 0.15,
    "license": 0.15,
    "bulk": 0.10,
    "health": 0.10,
    "longevity": 0.10,
    "cost": 0.05,
}
LICENSE_HARD_GATE = 3


class CandidateKind(StrEnum):
    NEW_VERSION = "new-version"  # a newer release of an existing active source
    NEW_VARIANT = "new-variant"  # e.g. another flavour or language of an existing active source
    KIWIX_TITLE = "kiwix-title"  # a Kiwix catalog title
    NEW_SOURCE = "new-source"  # anything else


class Decision(StrEnum):
    AUTO_PROMOTE = "auto-promote"
    REVIEW = "review"  # open a PR for the owner
    REJECT = "reject"  # hard gate failed; recorded, never activated


@dataclass(frozen=True, slots=True)
class Candidate:
    """A finding from a discovery bot (PLAN §5.3)."""

    key: str  # stable identity for de-duplication, e.g. "kiwix:wikipedia_en_all_maxi"
    name: str
    kind: CandidateKind
    license: str
    domain: tuple[str, ...]
    found_by: str
    scores: Mapping[str, int] = field(default_factory=dict[str, int])
    size_bytes: int | None = None
    existing_source_id: str | None = None
    category: str | None = None
    url: str | None = None
    registry_entry: Mapping[str, Any] = field(default_factory=dict[str, Any])  # drafted sources.yaml entry


@dataclass(frozen=True, slots=True)
class GateConfig:
    license_allowlist: frozenset[str] = frozenset()
    kiwix_categories: frozenset[str] = frozenset()
    domain_budget_bytes: Mapping[str, int] = field(default_factory=dict[str, int])
    domain_used_bytes: Mapping[str, int] = field(default_factory=dict[str, int])
    min_score: float = 3.5


@dataclass(frozen=True, slots=True)
class CheckResult:
    name: str
    passed: bool
    detail: str


@dataclass(frozen=True, slots=True)
class Proposal:
    """Structured outcome of the gate; the lead turns REVIEW proposals into registry PRs."""

    candidate: Candidate
    decision: Decision
    score: float | None
    breakdown: Mapping[str, float]
    checks: tuple[CheckResult, ...]
    title: str
    body: str

    def to_json(self) -> dict[str, Any]:
        c = self.candidate
        return {
            "decision": self.decision.value,
            "score": self.score,
            "breakdown": dict(self.breakdown),
            "checks": [{"name": k.name, "passed": k.passed, "detail": k.detail} for k in self.checks],
            "title": self.title,
            "body": self.body,
            "candidate": {
                "key": c.key,
                "name": c.name,
                "kind": c.kind.value,
                "license": c.license,
                "domain": list(c.domain),
                "found_by": c.found_by,
                "scores": dict(c.scores),
                "size_bytes": c.size_bytes,
                "existing_source_id": c.existing_source_id,
                "category": c.category,
                "url": c.url,
                "registry_entry": dict(c.registry_entry),
            },
        }


def rubric_score(scores: Mapping[str, int]) -> tuple[float | None, dict[str, float]]:
    """Weighted score per PLAN §5.2, or ``None`` if any axis is missing."""
    breakdown = {axis: weight * scores[axis] for axis, weight in RUBRIC_WEIGHTS.items() if axis in scores}
    if len(breakdown) != len(RUBRIC_WEIGHTS):
        return None, breakdown
    return round(sum(breakdown.values()), 4), breakdown


def evaluate_candidate(candidate: Candidate, cfg: GateConfig, sources: Sequence[SourceSpec]) -> Proposal:
    score, breakdown = rubric_score(candidate.scores)
    by_id = {s.id: s for s in sources}
    checks: list[CheckResult] = []

    license_axis = candidate.scores.get("license")
    hard_fail = license_axis is not None and license_axis < LICENSE_HARD_GATE
    checks.append(
        CheckResult(
            "license-hard-gate",
            not hard_fail,
            f"license axis {license_axis}" if license_axis is not None else "license axis not scored",
        )
    )
    allowed = candidate.license in cfg.license_allowlist
    checks.append(
        CheckResult(
            "license-allowlist", allowed, f"{candidate.license!r} is {'' if allowed else 'not '}allow-listed"
        )
    )

    existing = by_id.get(candidate.existing_source_id) if candidate.existing_source_id else None
    if candidate.kind in (CandidateKind.NEW_VERSION, CandidateKind.NEW_VARIANT):
        related = existing is not None and existing.status is SourceStatus.ACTIVE
        detail = (
            f"variant/version of active source {existing.id!r}"
            if related and existing
            else f"existing source {candidate.existing_source_id!r} is not active"
        )
        if related and existing and existing.license != candidate.license:
            related, detail = False, f"license differs from {existing.id!r} ({existing.license!r})"
    elif candidate.kind is CandidateKind.KIWIX_TITLE:
        related = candidate.category is not None and candidate.category in cfg.kiwix_categories
        detail = f"Kiwix category {candidate.category!r} {'allowed' if related else 'not allowed'}"
    else:
        related, detail = False, "new source: needs owner review"
    checks.append(CheckResult("relation", related, detail))

    budget_ok, budget_detail = _fits_budget(candidate, cfg)
    checks.append(CheckResult("storage-budget", budget_ok, budget_detail))
    checks.append(
        CheckResult(
            "score",
            score is not None and score >= cfg.min_score,
            f"score {score} (min {cfg.min_score})" if score is not None else "incomplete rubric scores",
        )
    )

    if hard_fail:
        decision = Decision.REJECT
    elif all(c.passed for c in checks):
        decision = Decision.AUTO_PROMOTE
    else:
        decision = Decision.REVIEW
    verb = {Decision.AUTO_PROMOTE: "add", Decision.REVIEW: "propose", Decision.REJECT: "exclude"}[decision]
    title = f"registry: {verb} {candidate.name} ({candidate.kind.value})"
    return Proposal(
        candidate=candidate,
        decision=decision,
        score=score,
        breakdown=breakdown,
        checks=tuple(checks),
        title=title,
        body=_render_body(candidate, decision, score, breakdown, checks),
    )


def _fits_budget(c: Candidate, cfg: GateConfig) -> tuple[bool, str]:
    if c.size_bytes is None:
        return False, "size unknown"
    for domain in c.domain:
        budget = cfg.domain_budget_bytes.get(domain)
        if budget is None:
            return False, f"no storage budget for domain {domain!r}"
        used = cfg.domain_used_bytes.get(domain, 0)
        if used + c.size_bytes > budget:
            return False, f"domain {domain!r}: {used} + {c.size_bytes} > budget {budget}"
    return True, "fits every domain budget"


def _render_body(
    c: Candidate,
    decision: Decision,
    score: float | None,
    breakdown: Mapping[str, float],
    checks: Sequence[CheckResult],
) -> str:
    lines = [
        f"Candidate **{c.name}** found by `{c.found_by}` - decision: **{decision.value}**.",
        "",
        f"- kind: {c.kind.value}",
        f"- license: {c.license}",
        f"- domain: {', '.join(c.domain)}",
        f"- size: {c.size_bytes if c.size_bytes is not None else 'unknown'} bytes",
        "",
        "| Axis | Score | Weight | Weighted |",
        "|---|---|---|---|",
    ]
    for axis, weight in RUBRIC_WEIGHTS.items():
        raw = c.scores.get(axis)
        lines.append(
            f"| {axis} | {raw if raw is not None else '-'} | {weight:.2f} | {breakdown.get(axis, 0.0):.2f} |"
        )
    lines += [f"| **total** | | | **{score if score is not None else 'incomplete'}** |", "", "Checks:", ""]
    lines += [f"- [{'x' if k.passed else ' '}] {k.name}: {k.detail}" for k in checks]
    return "\n".join(lines) + "\n"
