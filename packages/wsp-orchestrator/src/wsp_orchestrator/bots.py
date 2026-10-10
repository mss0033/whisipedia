"""Curator bots (PLAN §5.3, §5.5): small jobs that look at upstream (or at local logs) on a schedule.

A bot returns :class:`Candidate` findings, which the orchestrator runs through the policy gate
(:func:`wsp_orchestrator.policy.evaluate_candidate`), plus alerts to raise or resolve and a short
summary for the ledger and the System Monitor. Bots never write the registry themselves: the gate
decides, the proposal sink files the outcome, and auto-promotions go to the local registry overlay.

Concrete bots live in :mod:`wsp_orchestrator.curator`.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Protocol, runtime_checkable

from wsp_core.model import SourceSpec
from wsp_core.ports import Ctx, Fetcher, SourceAdapter
from wsp_orchestrator.alerts import Alert
from wsp_orchestrator.policy import Candidate, GateConfig


@dataclass(frozen=True, slots=True)
class BotContext:
    fetcher: Fetcher
    sources: Sequence[SourceSpec]
    now: datetime
    user_agent: str
    workdir: Path  # local scratch for this bot
    gate: GateConfig
    adapter_for: Callable[[SourceSpec], SourceAdapter]  # registry adapter resolution
    ctx_for: Callable[[SourceSpec], Ctx]  # adapter context (fetcher, scratch dir, user agent)
    previous: Mapping[str, object] = field(default_factory=dict[str, object])  # last run's notes


@dataclass(frozen=True, slots=True)
class BotResult:
    candidates: Sequence[Candidate] = ()
    alerts: Sequence[Alert] = ()
    resolved: Sequence[str] = ()  # alert keys whose condition has cleared
    notes: Mapping[str, object] = field(default_factory=dict[str, object])  # summary (JSON-safe)


@runtime_checkable
class Bot(Protocol):
    name: str  # stable id, also the job key: "kiwix-opds", "gap-miner", ...
    cadence: str | None  # ISO 8601 duration; None follows the global update cycle
    network: bool  # informational: the bot talks to upstream

    def run(self, ctx: BotContext) -> BotResult: ...
