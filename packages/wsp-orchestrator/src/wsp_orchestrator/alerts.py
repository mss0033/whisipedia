"""Alerts: persisted and de-duplicated, so each condition is raised once until resolved (PLAN §12.6)."""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, runtime_checkable

from wsp_orchestrator.clock import Clock
from wsp_orchestrator.store import StateStore

log = logging.getLogger("wsp.orchestrator.alerts")


class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


@dataclass(frozen=True, slots=True)
class Alert:
    key: str  # stable identity, e.g. "retention-window:wikidata"
    severity: Severity
    message: str
    data: Mapping[str, object] = field(default_factory=dict[str, object])


@runtime_checkable
class AlertSink(Protocol):
    """Delivery channel (desktop notification, ntfy, email...). Must not raise for delivery failures."""

    def deliver(self, alert: Alert) -> None: ...


class Alerter:
    def __init__(self, store: StateStore, clock: Clock, sinks: Sequence[AlertSink] = ()) -> None:
        self.store = store
        self.clock = clock
        self.sinks = tuple(sinks)

    def raise_(self, alert: Alert) -> bool:
        """Open the alert and deliver it if it is not already open. Returns True if newly raised."""
        if not self.store.open_alert(
            alert.key, alert.severity.value, alert.message, alert.data, self.clock.now()
        ):
            return False
        log.warning("alert raised", extra={"fields": {"alert": alert.key, "severity": alert.severity.value}})
        for sink in self.sinks:
            try:
                sink.deliver(alert)
            except Exception:
                log.exception("alert sink failed", extra={"fields": {"alert": alert.key}})
        return True

    def resolve(self, key: str) -> bool:
        return self.store.resolve_alert(key, self.clock.now())

    def open_keys(self) -> list[str]:
        return [str(r["key"]) for r in self.store.open_alerts()]
