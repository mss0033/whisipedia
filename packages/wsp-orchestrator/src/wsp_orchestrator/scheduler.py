"""Scheduler: turns cadences into queued jobs, with catch-up semantics (PLAN §6.5, §6.7, §8.2, §8.5).

On every :meth:`Scheduler.tick` each schedule entry whose next due time (computed from its last
success) has passed gets one job - so after downtime every overdue job runs once, not once per missed
period. Failed entries are retried no sooner than ``job_retry_gap`` after the last attempt.

Schedule entries:

* ``discover`` per active source - the source cadence (global ``acquirer.cycle`` unless overridden);
* ``health_probe`` per active source - ``acquirer.health_probe_interval`` (weekly);
* ``prune`` per active source - the source cadence;
* ``discovery_bots`` - the global cycle;
* ``drill`` - ``orchestrator.drill_interval`` (monthly);
* ``scrub_slice`` - continuous: slice *i* of *n* is due at ``cycle_start + i * (scrub_cycle / n)``,
  so a full 30-day pass completes on time and missed slices run back-to-back after downtime.

It also raises **retention-window alerts**: if a source's cycle is overdue by half of its upstream
retention window (per adapter, e.g. Wikimedia keeps ~60 days of dump runs), the release may vanish
upstream before it is fetched.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from wsp_core.model import SourceSpec, SourceStatus
from wsp_orchestrator.alerts import Alert, Alerter, Severity
from wsp_orchestrator.clock import Clock
from wsp_orchestrator.durations import (
    Cadence,
    CalendarCadence,
    IntervalCadence,
    cadence_for_cycle,
    cadence_from_duration,
)
from wsp_orchestrator.jobs import Job, JobKind, JobQueue
from wsp_orchestrator.settings import OrchestratorSettings
from wsp_orchestrator.store import StateStore, parse_ts, ts

log = logging.getLogger("wsp.orchestrator.scheduler")

GLOBAL_KEY = "global"
PARITY_KEY = "backfill"
SCRUB_KEY = "rolling"
_SCRUB_START = "scrub_cycle_start"
_SCRUB_CURSOR = "scrub_next_slice"


@dataclass(frozen=True, slots=True)
class ScheduleEntry:
    kind: JobKind
    key: str
    cadence: Cadence
    payload: Mapping[str, Any] = field(default_factory=dict[str, Any])
    source_id: str | None = None


@dataclass(frozen=True, slots=True)
class DueEntry:
    entry: ScheduleEntry
    due_at: datetime | None  # None: never ran


class Scheduler:
    def __init__(
        self,
        *,
        store: StateStore,
        queue: JobQueue,
        clock: Clock,
        settings: OrchestratorSettings,
        sources: Sequence[SourceSpec],
        alerter: Alerter | None = None,
        bots: Mapping[str, str | None] | None = None,
        parity: bool = False,
    ) -> None:
        self.bots = dict(bots or {})  # bot name -> ISO cadence (None: the global cycle)
        self.parity = parity
        self.store = store
        self.queue = queue
        self.clock = clock
        self.settings = settings
        self.sources = list(sources)
        self.alerter = alerter
        self.cycle: CalendarCadence = cadence_for_cycle(settings.cycle, anchor_day=settings.anchor_day)

    # --- cadences ------------------------------------------------------------------------------

    def source_cadence(self, spec: SourceSpec) -> Cadence:
        """A registry ``update.cadence`` is a deliberate override; without one the source follows the
        global ``acquirer.cycle`` (PLAN §6.7)."""
        if spec.cadence is None:
            return self.cycle
        return cadence_from_duration(spec.cadence, anchor_day=self.settings.anchor_day)

    def active_sources(self) -> list[SourceSpec]:
        return [s for s in self.sources if s.status is SourceStatus.ACTIVE]

    def entries(self) -> list[ScheduleEntry]:
        out: list[ScheduleEntry] = []
        probe = IntervalCadence(self.settings.health_probe_interval)
        for spec in self.active_sources():
            cadence = self.source_cadence(spec)
            payload = {"source_id": spec.id}
            if not spec.partitioned:  # ADR-0010: acquired one partition per run by `wsp collections fetch`
                out.append(ScheduleEntry(JobKind.DISCOVER, spec.id, cadence, payload, spec.id))
                out.append(ScheduleEntry(JobKind.HEALTH_PROBE, spec.id, probe, payload, spec.id))
            out.append(ScheduleEntry(JobKind.PRUNE, spec.id, cadence, payload, spec.id))
        out.append(ScheduleEntry(JobKind.DISCOVERY_BOTS, GLOBAL_KEY, self.cycle))
        for name, iso in sorted(self.bots.items()):
            bot_cadence = (
                self.cycle if iso is None else cadence_from_duration(iso, anchor_day=self.settings.anchor_day)
            )
            out.append(ScheduleEntry(JobKind.DISCOVERY_BOTS, name, bot_cadence, {"bot": name}))
        if self.parity:
            parity = IntervalCadence(self.settings.parity_interval)
            out.append(ScheduleEntry(JobKind.PARITY, PARITY_KEY, parity, {"backfill": True}))
        out.append(ScheduleEntry(JobKind.DRILL, GLOBAL_KEY, IntervalCadence(self.settings.drill_interval)))
        return out

    # --- due computation -----------------------------------------------------------------------

    def due(self, now: datetime | None = None) -> list[DueEntry]:
        now = now or self.clock.now()
        out: list[DueEntry] = []
        for e in self.entries():
            last_success, last_attempt = self.store.schedule_marks(e.kind.value, e.key)
            due_at = e.cadence.next_due(last_success)
            if due_at is not None and due_at > now:
                continue
            failed_since = last_attempt is not None and (last_success is None or last_attempt > last_success)
            if failed_since and last_attempt is not None and now < last_attempt + self.settings.job_retry_gap:
                continue
            paused = e.source_id is not None and self.store.source_health(e.source_id).paused
            if paused and e.kind is not JobKind.HEALTH_PROBE:  # paused sources are still probed
                continue
            out.append(DueEntry(e, due_at))
        return out

    def tick(self) -> list[Job]:
        """Enqueue everything due (catch-up included) and refresh retention-window alerts."""
        now = self.clock.now()
        enqueued: list[Job] = []
        for d in self.due(now):
            job = self.queue.enqueue(d.entry.kind, d.entry.key, now=now, payload=d.entry.payload)
            if job is not None:
                enqueued.append(job)
                log.info(
                    "job scheduled",
                    extra={
                        "fields": {
                            "kind": d.entry.kind.value,
                            "key": d.entry.key,
                            "due_at": d.due_at.isoformat() if d.due_at else None,
                            "overdue_s": (now - d.due_at).total_seconds() if d.due_at else None,
                        }
                    },
                )
        scrub = self._tick_scrub(now)
        if scrub is not None:
            enqueued.append(scrub)
        self.check_retention_windows(now)
        return enqueued

    def next_wakeup(self) -> datetime | None:
        """Earliest future time at which :meth:`tick` would enqueue something."""
        times: list[datetime] = []
        for e in self.entries():
            due_at = e.cadence.next_due(self.store.last_success(e.kind.value, e.key))
            if due_at is not None:
                times.append(due_at)
        start, cursor = self.scrub_position()
        times.append(start + cursor * self.settings.scrub_slice_interval)
        return min(times) if times else None

    # --- scrub ---------------------------------------------------------------------------------

    def scrub_position(self) -> tuple[datetime, int]:
        start = parse_ts(self.store.get_meta(_SCRUB_START))
        if start is None:
            start = self.clock.now()
            self.store.set_meta(_SCRUB_START, ts(start))
        return start, int(self.store.get_meta(_SCRUB_CURSOR) or 0)

    def _tick_scrub(self, now: datetime) -> Job | None:
        start, cursor = self.scrub_position()
        if now < start + cursor * self.settings.scrub_slice_interval:
            return None
        payload = {"slice": cursor, "slices": self.settings.scrub_slices, "cycle_start": ts(start)}
        return self.queue.enqueue(JobKind.SCRUB_SLICE, SCRUB_KEY, now=now, payload=payload)

    def scrub_slice_done(self, slice_index: int) -> None:
        """Advance the rolling scrub after slice ``slice_index`` completed."""
        start, cursor = self.scrub_position()
        if slice_index != cursor:
            return  # stale completion (e.g. replayed after a crash)
        cursor += 1
        with self.store.tx() as c:
            if cursor >= self.settings.scrub_slices:
                cycle = timedelta(days=self.settings.scrub_cycle_days)
                self.store.set_meta(_SCRUB_START, ts(max(start + cycle, self.clock.now())), c)
                cursor = 0
            self.store.set_meta(_SCRUB_CURSOR, str(cursor), c)

    # --- retention-window alerts ---------------------------------------------------------------

    def retention_window(self, spec: SourceSpec) -> timedelta | None:
        windows = self.settings.retention_windows
        dur = windows.get(spec.adapter or "") or windows.get(spec.id)
        return dur.approx() if dur is not None else None

    def check_retention_windows(self, now: datetime | None = None) -> list[str]:
        """Raise (or resolve) an alert per source whose cycle is overdue by half its upstream window."""
        now = now or self.clock.now()
        raised: list[str] = []
        for spec in self.active_sources():
            window = self.retention_window(spec)
            if window is None or spec.partitioned:  # no orchestrator cycles for partitioned sources
                continue
            key = f"retention-window:{spec.id}"
            health = self.store.source_health(spec.id)
            # Never completed a cycle: it has been due since the scheduler first saw the source.
            due_at = self.source_cadence(spec).next_due(health.last_cycle_ok)
            if due_at is None:
                due_at = self._first_seen(spec.id, now)
            deadline = due_at + window
            if now >= due_at + window / 2:
                if self.alerter is not None and self.alerter.raise_(
                    Alert(
                        key=key,
                        severity=Severity.CRITICAL,
                        message=(
                            f"{spec.id}: cycle due {due_at:%Y-%m-%d} has not completed; upstream retention"
                            f" ({window.days} days) may drop the release around {deadline:%Y-%m-%d}"
                        ),
                        data={
                            "source_id": spec.id,
                            "due_at": due_at.isoformat(),
                            "deadline": deadline.isoformat(),
                        },
                    )
                ):
                    raised.append(key)
            elif self.alerter is not None:
                self.alerter.resolve(key)
        return raised

    def _first_seen(self, source_id: str, now: datetime) -> datetime:
        key = f"first_seen:{source_id}"
        seen = parse_ts(self.store.get_meta(key))
        if seen is None:
            self.store.set_meta(key, ts(now))
            return now
        return seen
