"""The orchestrator: wires ports to the pipeline, scheduler and job handlers, and dispatches jobs.

Real components plug in through :class:`Wiring` - every field is a wsp-core port or a plain callable,
so the vault, ledger, fetcher, adapters, refinery, index, warden and curator can be developed and
swapped independently (PLAN §12.2).
"""

from __future__ import annotations

import logging
import threading
import time
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from urllib.parse import urlparse

from wsp_core.model import Generation, ReleaseRef, ReleaseState, SourceSpec
from wsp_core.ports import Fetcher, Ledger, SourceAdapter, Vault
from wsp_orchestrator.alerts import Alert, Alerter, AlertSink, Severity
from wsp_orchestrator.clock import Clock, SystemClock
from wsp_orchestrator.codec import release_from_json
from wsp_orchestrator.jobs import Job, JobKind, JobQueue
from wsp_orchestrator.metrics import Metrics
from wsp_orchestrator.pipeline import (
    IndexStep,
    Outcome,
    RefineStep,
    ReleasePipeline,
    StepError,
    adapter_resolver,
    generation_id_for,
    pause_source,
    resume_source,
)
from wsp_orchestrator.policy import (
    Candidate,
    GateConfig,
    Observation,
    Proposal,
    auto_pause_findings,
    evaluate_candidate,
)
from wsp_orchestrator.retention import GenerationInfo, GfsPolicy, plan_retention
from wsp_orchestrator.scheduler import Scheduler
from wsp_orchestrator.settings import OrchestratorSettings
from wsp_orchestrator.store import StateStore

log = logging.getLogger("wsp.orchestrator")

ScrubHook = Callable[[int, int], None]  # (slice_index, slice_count) - Warden rolling scrub (PLAN §8.2)
DrillHook = Callable[[], Mapping[str, object]]  # monthly chaos drill; must return {"ok": bool, ...}
DiscoveryHook = Callable[[], Iterable[Candidate]]  # Curator discovery bots (PLAN §5.3)
PrunerHook = Callable[[str, str], None]  # delete one generation (source_id, generation_id)
LastCopyHook = Callable[[str, Generation], bool]  # True if the generation holds the last copy of something


@dataclass
class Wiring:
    """Everything the orchestrator drives. Built by the lead's composition root."""

    sources: Sequence[SourceSpec]
    adapters: Mapping[str, SourceAdapter]  # keyed by registry ``access.adapter`` id (or source id)
    fetcher: Fetcher
    vault: Vault
    ledger: Ledger
    refine: RefineStep
    index: IndexStep
    alert_sinks: Sequence[AlertSink] = ()
    scrub: ScrubHook | None = None
    drill: DrillHook | None = None
    discovery_bots: DiscoveryHook | None = None
    gate: GateConfig = field(default_factory=GateConfig)
    proposal_sink: Callable[[Proposal], None] | None = None
    pruner: PrunerHook | None = None
    last_copy: LastCopyHook | None = None


class Handled(StrEnum):
    DONE = "done"
    DEFER = "defer"
    CANCEL = "cancel"


@dataclass(frozen=True, slots=True)
class JobResult:
    status: Handled = Handled.DONE
    run_after: datetime | None = None
    note: str | None = None


def _no_checkpoint(_point: str) -> None:
    return None


def _host(ref: ReleaseRef) -> str | None:
    for f in ref.files:
        host = urlparse(f.url).hostname
        if host:
            return host
    return None


class Orchestrator:
    def __init__(
        self,
        settings: OrchestratorSettings,
        wiring: Wiring,
        *,
        clock: Clock | None = None,
        metrics: Metrics | None = None,
        check_local: bool = True,
        checkpoint: Callable[[str], None] | None = None,
    ) -> None:
        self.settings = settings
        self.wiring = wiring
        self.clock = clock or SystemClock()
        self.metrics = metrics or Metrics()
        roots = [settings.vault_root] if settings.vault_root else []
        self.store = StateStore(settings.state_db, forbidden_roots=roots, check_local=check_local)
        self.queue = JobQueue(self.store)
        self.alerter = Alerter(self.store, self.clock, wiring.alert_sinks)
        self.sources = {s.id: s for s in wiring.sources}
        self.pipeline = ReleasePipeline(
            store=self.store,
            sources=wiring.sources,
            adapters=adapter_resolver(wiring.adapters),
            fetcher=wiring.fetcher,
            vault=wiring.vault,
            ledger=wiring.ledger,
            refine=wiring.refine,
            index=wiring.index,
            clock=self.clock,
            workdir=settings.workdir,
            user_agent=settings.user_agent,
            alerter=self.alerter,
            retry=settings.retry,
            checkpoint=checkpoint or _no_checkpoint,
        )
        self.scheduler = Scheduler(
            store=self.store,
            queue=self.queue,
            clock=self.clock,
            settings=settings,
            sources=wiring.sources,
            alerter=self.alerter,
        )
        self.handlers: dict[JobKind, Callable[[Job], JobResult]] = {
            JobKind.DISCOVER: self._discover,
            JobKind.FETCH_RELEASE: self._fetch_release,
            JobKind.HEALTH_PROBE: self._health_probe,
            JobKind.PRUNE: self._prune,
            JobKind.SCRUB_SLICE: self._scrub_slice,
            JobKind.DRILL: self._drill,
            JobKind.DISCOVERY_BOTS: self._discovery_bots,
        }
        self.stopping = threading.Event()
        self._lock = threading.Lock()
        self._running_kind: Counter[JobKind] = Counter()
        self._running_host: Counter[str] = Counter()
        self._executor: ThreadPoolExecutor | None = None
        self._futures: set[Future[None]] = set()

    # --- lifecycle -----------------------------------------------------------------------------

    def start(self) -> list[Job]:
        """Crash recovery and catch-up. Returns jobs enqueued by the first scheduler tick."""
        now = self.clock.now()
        recovered = self.queue.recover(now, all_running=True)
        flushed = self.pipeline.flush_ledger()
        resumed = 0
        for rec in self.store.releases():
            if rec.state not in (ReleaseState.PROMOTED, ReleaseState.QUARANTINED):
                resumed += self._enqueue_fetch(rec.source_id, rec.version, now) is not None
        log.info(
            "orchestrator started",
            extra={
                "fields": {
                    "recovered_jobs": recovered,
                    "ledger_flushed": flushed,
                    "resumed_releases": resumed,
                }
            },
        )
        return self.scheduler.tick()

    def close(self) -> None:
        self.store.close()

    # --- dispatch ------------------------------------------------------------------------------

    def _limit(self, kind: JobKind) -> int:
        return self.settings.job_limits.get(kind, 1)

    def _accept(self, job: Job) -> bool:
        if sum(self._running_kind.values()) >= self.settings.max_workers:
            return False
        if self._running_kind[job.kind] >= self._limit(job.kind):
            return False
        return job.host is None or self._running_host[job.host] < self.settings.max_jobs_per_host

    def claim_ready(self) -> list[Job]:
        """Claim due jobs that fit the global, per-type and per-host limits, reserving capacity."""
        claimed: list[Job] = []
        with self._lock:
            while True:
                jobs = self.queue.claim(self.clock.now(), lease=self.settings.lease, accept=self._accept)
                if not jobs:
                    return claimed
                job = jobs[0]
                self._running_kind[job.kind] += 1
                if job.host:
                    self._running_host[job.host] += 1
                claimed.append(job)

    def _release_capacity(self, job: Job) -> None:
        with self._lock:
            self._running_kind[job.kind] -= 1
            if job.host:
                self._running_host[job.host] -= 1

    def run_pending(self, *, max_jobs: int = 10_000) -> int:
        """Synchronously run due jobs until none are left (tests, one-shot CLI). Returns jobs run."""
        done = 0
        while done < max_jobs and not self.stopping.is_set():
            self.scheduler.tick()
            jobs = self.claim_ready()
            if not jobs:
                break
            for job in jobs:
                self.execute(job)
                done += 1
        self.refresh_metrics()
        return done

    def dispatch(self) -> int:
        """Threaded mode: submit claimed jobs to the worker pool. Returns how many were submitted."""
        if self.stopping.is_set():
            return 0
        if self._executor is None:
            self._executor = ThreadPoolExecutor(self.settings.max_workers, thread_name_prefix="wsp-job")
        jobs = self.claim_ready()
        for job in jobs:
            fut = self._executor.submit(self.execute, job)
            self._futures.add(fut)
            fut.add_done_callback(self._futures.discard)
        return len(jobs)

    def shutdown(self, grace_seconds: float | None = None) -> bool:
        """Stop claiming, ask pipelines to stop between steps, wait for workers. True if all finished."""
        self.stopping.set()
        grace = self.settings.shutdown_grace_seconds if grace_seconds is None else grace_seconds
        deadline = time.monotonic() + grace
        while self._futures and time.monotonic() < deadline:
            time.sleep(0.05)
        finished = not self._futures
        if self._executor is not None:
            self._executor.shutdown(wait=finished, cancel_futures=True)
            self._executor = None
        if not finished:
            log.warning("shutdown grace expired; unfinished jobs resume on next start")
        self.refresh_metrics()
        return finished

    def execute(self, job: Job) -> None:
        started = time.monotonic()
        outcome = "error"
        try:
            result = self.handlers[job.kind](job)
            now = self.clock.now()
            match result.status:
                case Handled.DONE:
                    self.queue.complete(job, now)
                case Handled.DEFER:
                    self.queue.defer(job, now, result.run_after or now, note=result.note)
                case Handled.CANCEL:
                    self.queue.cancel(job, now, result.note or "cancelled")
            outcome = result.status.value
        except Exception as exc:
            now = self.clock.now()
            retry = self.settings.retry
            retry_at = now + retry.delay(job.attempts) if job.attempts < retry.max_attempts else None
            self.queue.fail(job, now, f"{type(exc).__name__}: {exc}", retry_at=retry_at)
            log.exception("job failed", extra={"fields": {"kind": job.kind.value, "key": job.key}})
            if retry_at is None:
                self.alerter.raise_(
                    Alert(
                        key=f"job-dead:{job.kind.value}:{job.key}",
                        severity=Severity.WARNING,
                        message=f"{job.kind.value} {job.key} gave up after {job.attempts} attempts: {exc}",
                        data={"kind": job.kind.value, "key": job.key},
                    )
                )
        finally:
            self._release_capacity(job)
            labels = {"kind": job.kind.value}
            self.metrics.inc(
                "jobs_total", "Jobs finished by type and outcome", {**labels, "outcome": outcome}
            )
            self.metrics.inc(
                "job_seconds_total", "Wall time spent in jobs", labels, time.monotonic() - started
            )
        if outcome == "done":
            self.alerter.resolve(f"job-dead:{job.kind.value}:{job.key}")

    # --- owner actions -------------------------------------------------------------------------

    def approve_release(self, source_id: str, version: str, *, note: str = "") -> None:
        self.pipeline.approve(source_id, version, note=note)
        self._enqueue_fetch(source_id, version, self.clock.now())

    def reject_release(self, source_id: str, version: str, *, reason: str) -> None:
        self.pipeline.reject(source_id, version, reason=reason)

    def resume_source(self, source_id: str) -> None:
        resume_source(self.store, self.pipeline.ledger_outbox_writer, source_id, alerter=self.alerter)
        self.pipeline.flush_ledger()
        self.queue.enqueue(
            JobKind.DISCOVER, source_id, now=self.clock.now(), payload={"source_id": source_id}
        )

    # --- handlers ------------------------------------------------------------------------------

    def _spec(self, job: Job) -> SourceSpec | None:
        return self.sources.get(str(job.payload.get("source_id", "")))

    def _enqueue_fetch(self, source_id: str, version: str, now: datetime) -> Job | None:
        rec = self.store.get_release(source_id, version)
        host = None
        if rec is not None:
            host = _host(release_from_json(rec.ref))
        return self.queue.enqueue(
            JobKind.FETCH_RELEASE,
            f"{source_id}/{version}",
            now=now,
            payload={"source_id": source_id, "version": version},
            host=host,
        )

    def _upstream(self, spec: SourceSpec) -> list[ReleaseRef]:
        adapter = self.pipeline.resolve_adapter(spec)
        refs = list(adapter.discover(self.pipeline.ctx(spec)))
        if not refs:
            raise StepError(f"{spec.id}: upstream offers no releases")
        return refs

    def _observe(self, spec: SourceSpec, newest: ReleaseRef) -> bool:
        """Apply the auto-pause rules to what upstream shows. Returns True if the source is now paused."""
        now = self.clock.now()
        health = self.store.source_health(spec.id)
        obs = Observation(
            license=newest.license,
            format=newest.format,
            newest_published=newest.published,
        )
        findings = auto_pause_findings(
            spec,
            observation=obs,
            known_format=health.observed_format,
            checksum_failures=health.checksum_failures,
            last_new_release_at=health.last_new_release_at,
            cadence_period=self.scheduler.source_cadence(spec).approx_period(),
            now=now,
        )
        if not findings:
            updated = replace(
                health,
                observed_format=obs.format or health.observed_format,
                observed_license=obs.license or health.observed_license,
            )
            if updated != health:
                self.store.put_source_health(updated)
        for f in findings:
            pause_source(
                self.store,
                self.pipeline.ledger_outbox_writer,
                spec.id,
                f"{f.reason.value}: {f.detail}",
                alerter=self.alerter,
            )
        self.pipeline.flush_ledger()
        return bool(findings) or self.store.source_health(spec.id).paused

    def _mark_cycle_ok(self, source_id: str) -> None:
        health = self.store.source_health(source_id)
        self.store.put_source_health(replace(health, last_cycle_ok=self.clock.now()))
        self.alerter.resolve(f"retention-window:{source_id}")

    def _discover(self, job: Job) -> JobResult:
        spec = self._spec(job)
        if spec is None:
            return JobResult(Handled.CANCEL, note="unknown source")
        if self.store.source_health(spec.id).paused:
            return JobResult(Handled.CANCEL, note="source paused")
        newest = max(self._upstream(spec), key=lambda r: r.version)
        if self._observe(spec, newest):
            return JobResult(Handled.CANCEL, note="source paused by policy")
        now = self.clock.now()
        rec = self.store.get_release(spec.id, newest.version)
        current = self.wiring.vault.current(spec.id)
        if rec is None:
            if current is not None and current.generation_id >= generation_id_for(newest):
                self._mark_cycle_ok(spec.id)
                return JobResult()
            self.pipeline.register(newest)
            self._enqueue_fetch(spec.id, newest.version, now)
        elif rec.state is ReleaseState.PROMOTED:
            self._mark_cycle_ok(spec.id)
        elif rec.state is not ReleaseState.QUARANTINED:
            self._enqueue_fetch(spec.id, newest.version, now)
        return JobResult()

    def _fetch_release(self, job: Job) -> JobResult:
        source_id, version = str(job.payload["source_id"]), str(job.payload["version"])
        result = self.pipeline.run(source_id, version, cancel=self.stopping.is_set)
        now = self.clock.now()
        match result.outcome:
            case Outcome.PROMOTED:
                self._mark_cycle_ok(source_id)
                self.queue.enqueue(JobKind.PRUNE, source_id, now=now, payload={"source_id": source_id})
                return JobResult()
            case Outcome.BACKOFF:
                return JobResult(Handled.DEFER, run_after=result.record.next_attempt_at, note="backoff")
            case Outcome.CANCELLED:
                return JobResult(Handled.DEFER, run_after=now, note="shutdown")
            case Outcome.PAUSED:
                return JobResult(Handled.CANCEL, note="source paused")
            case Outcome.HELD | Outcome.QUARANTINED:
                return JobResult(note=result.outcome.value)

    def _health_probe(self, job: Job) -> JobResult:
        spec = self._spec(job)
        if spec is None:
            return JobResult(Handled.CANCEL, note="unknown source")
        newest = max(self._upstream(spec), key=lambda r: r.version)
        key = f"no-checksums:{spec.id}"
        if not newest.files or any(not f.checksums for f in newest.files):
            self.alerter.raise_(
                Alert(
                    key=key,
                    severity=Severity.WARNING,
                    message=f"{spec.id} {newest.version}: upstream publishes files without checksums",
                    data={"source_id": spec.id, "version": newest.version},
                )
            )
        else:
            self.alerter.resolve(key)
        self._observe(spec, newest)
        return JobResult()

    def _prune(self, job: Job) -> JobResult:
        spec = self._spec(job)
        if spec is None:
            return JobResult(Handled.CANCEL, note="unknown source")
        retention = spec.raw.get("retention")
        policy = GfsPolicy.from_registry(retention if isinstance(retention, Mapping) else None)  # pyright: ignore[reportUnknownArgumentType]
        if policy is None:
            return JobResult(note="non-GFS retention: nothing to prune")
        vault, last_copy = self.wiring.vault, self.wiring.last_copy
        current = vault.current(spec.id)
        # Without a last-copy oracle we cannot prove a generation is redundant: keep everything.
        infos = [
            GenerationInfo.from_generation(
                g, holds_last_copy=True if last_copy is None else last_copy(spec.id, g)
            )
            for g in vault.generations(spec.id)
        ]
        plan = plan_retention(infos, policy, current=current.generation_id if current else None)
        dry_run = self.wiring.pruner is None or last_copy is None
        self.pipeline.ledger_outbox_writer(
            "retention.plan",
            spec.id,
            {"keep": list(plan.keep), "prune": list(plan.prune), "dry_run": dry_run},
        )
        if not dry_run and self.wiring.pruner is not None:
            for gid in plan.prune:
                self.wiring.pruner(spec.id, gid)
                self.pipeline.ledger_outbox_writer(
                    "retention.pruned", f"{spec.id}/{gid}", {"generation_id": gid}
                )
        self.pipeline.flush_ledger()
        return JobResult()

    def _scrub_slice(self, job: Job) -> JobResult:
        index, count = int(job.payload["slice"]), int(job.payload["slices"])
        if self.wiring.scrub is not None:
            self.wiring.scrub(index, count)
        self.scheduler.scrub_slice_done(index)
        return JobResult()

    def _drill(self, job: Job) -> JobResult:
        if self.wiring.drill is None:
            return JobResult(note="no drill hook wired")
        result = dict(self.wiring.drill())
        self.pipeline.ledger_outbox_writer("drill.result", "warden", result)
        self.pipeline.flush_ledger()
        if result.get("ok") is False:
            self.alerter.raise_(
                Alert("drill-failed", Severity.CRITICAL, "monthly integrity drill failed", {"result": result})
            )
        else:
            self.alerter.resolve("drill-failed")
        return JobResult()

    def _discovery_bots(self, job: Job) -> JobResult:
        if self.wiring.discovery_bots is None:
            return JobResult(note="no discovery hook wired")
        open_keys = {str(r["candidate_key"]) for r in self.store.proposals()}
        now = self.clock.now()
        for candidate in self.wiring.discovery_bots():
            if candidate.key in open_keys:
                continue
            proposal = evaluate_candidate(candidate, self.wiring.gate, self.wiring.sources)
            self.store.add_proposal(candidate.key, proposal.decision.value, proposal.to_json(), now)
            self.pipeline.ledger_outbox_writer(
                "policy.decision",
                candidate.key,
                {"decision": proposal.decision.value, "score": proposal.score, "title": proposal.title},
            )
            if self.wiring.proposal_sink is not None:
                self.wiring.proposal_sink(proposal)
            open_keys.add(candidate.key)
        self.pipeline.flush_ledger()
        return JobResult()

    # --- metrics -------------------------------------------------------------------------------

    def refresh_metrics(self) -> None:
        m = self.metrics
        m.reset_gauge("jobs")
        for (kind, status), n in self.queue.depth().items():
            if status in ("pending", "running"):
                m.set("jobs", "Active jobs by type and status", n, {"kind": kind, "status": status})
        m.reset_gauge("releases")
        counts = Counter(r.state.value for r in self.store.releases())
        for state, n in counts.items():
            m.set("releases", "Releases by state", n, {"state": state})
        m.set("alerts_open", "Open alerts", len(self.store.open_alerts()))
        paused = sum(self.store.source_health(s).paused for s in self.sources)
        m.set("sources_paused", "Paused sources", paused)
        m.set("last_refresh_timestamp_seconds", "Last metrics refresh", self.clock.now().timestamp())
        if self.settings.metrics_path is not None:
            m.write(self.settings.metrics_path)

    def sleep_seconds(self) -> float:
        """How long the main loop may sleep before something could become due."""
        now = self.clock.now()
        candidates = [self.settings.poll_interval_seconds]
        for when in (self.queue.next_run_after(), self.scheduler.next_wakeup()):
            if when is not None:
                candidates.append((when - now).total_seconds())
        return max(0.05, min(candidates))
