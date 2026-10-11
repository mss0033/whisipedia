"""Integrity findings: what the guard saw and what it does about it (ADR-0009)."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import IntEnum


class Action(IntEnum):
    """Ordered by severity: the strongest action among a release's findings wins."""

    INFO = 0  # recorded in provenance and the ledger only
    ALERT = 1  # an alert for the owner; the release continues
    HOLD = 2  # the release waits for ``wsp approve`` / ``wsp reject``
    QUARANTINE = 3  # never promoted; the source is paused

    @property
    def label(self) -> str:
        return self.name.lower()

    @classmethod
    def parse(cls, value: str) -> Action:
        try:
            return cls[value.strip().upper()]
        except KeyError:
            raise ValueError(f"unknown integrity action {value!r} (info, alert, hold, quarantine)") from None


@dataclass(frozen=True, slots=True)
class Finding:
    code: str  # stable id, e.g. "mirror-size-disagreement", "origin-unknown-domain"
    action: Action
    message: str
    data: Mapping[str, object] = field(default_factory=dict[str, object])

    def to_json(self) -> dict[str, object]:
        return {
            "code": self.code,
            "action": self.action.label,
            "message": self.message,
            "data": dict(self.data),
        }


def strongest(findings: Iterable[Finding]) -> Action:
    return max((f.action for f in findings), default=Action.INFO)


def summarize(findings: Iterable[Finding], at_least: Action) -> str:
    return "; ".join(f"{f.code}: {f.message}" for f in findings if f.action >= at_least)
