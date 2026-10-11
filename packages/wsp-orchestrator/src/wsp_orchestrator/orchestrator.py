"""The orchestrator: wires ports to the pipeline, scheduler and job handlers, and dispatches jobs.

Real components plug in through :class:`Wiring` - every field is a wsp-core port or a plain callable,
so the vault, ledger, fetcher, adapters, refinery, index, warden and curator can be developed and
swapped independently (PLAN §12.2).
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from typing import cast
from urllib.parse import urlparse

from wsp_core.model import Generation, ReleaseRef, ReleaseState, SourceSpec
from wsp_core.ports import Fetcher, Ledger, SourceAdapter, Vault
from wsp_orchestrator.alerts import Alert, Alerter, AlertSink, Severity
from wsp_orchestrator.bots import Bot, BotContext, BotResult
from wsp_orchestrator.clock import Clock, SystemClock, ensure_utc
from wsp_orchestrator.codec import release_from_json
from wsp_orchestrator.curator.proposals import ProposalRecord
from wsp_orchestrator.integrity.guard import IntegrityGuard, IntegrityHooks
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
    Decision,
    GateConfig,
    Observation,
    Proposal,
    auto_pause_findings,
    evaluate_candidate,
)
from wsp_orchestrator.retention import GenerationInfo, GfsPolicy, plan_retention
from wsp_orchestrator.scheduler import GLOBAL_KEY, PARITY_KEY, Scheduler
from wsp_orchestrator.settings import OrchestratorSettings
from wsp_orchestrator.store import StateStore

log = logging.getLogger("wsp.orchestrator")

ScrubHook = Callable[[int, int], None]  # (slice_index, slice_count) - Warden rolling scrub (PLAN §8.2)
DrillHook = Callable[[], Mapping[str, object]]  # monthly chaos drill; must return {"ok": bool, ...}
DiscoveryHook = Callable[[], Iterable[Candidate]]  # Curator discovery bots (PLAN §5.3)
PrunerHook = Callable[[str, str], None]  # retire one generation (source_id, generation_id): moved aside
LastCopyHook = Callable[[str, Generation], bool]  # True if the generation holds the last copy of something
# One bounded slice of parity work (create/backfill). Gets the last-copy oracle for scope decisions;
# returns a JSON-safe summary with at least {"pending": int}.
ParityHook = Callable[[LastCopyHook], Mapping[str, object]]


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
    last_copy: LastCopyHook | None = None  # override; default: what upstream served lately (store)
    bots: Sequence[Bot] = ()  # Curator bots, scheduled per bot (PLAN §5.3, §5.5)
    gate_provider: Callable[[], GateConfig] | None = None  # live budgets; falls back to ``gate``
    promoter: Callable[[Proposal], None] | None = None  # applies AUTO_PROMOTE (registry overlay)
    proposal_recorder: Callable[[ProposalRecord], object] | None = None  # proposal files
    parity: ParityHook | None = None
    integrity: IntegrityHooks | None = None  # source integrity guard (ADR-0009); None = off
    # Called once on shutdown (SIGTERM): cancel downloads, stop scrub/parity, kill par2. The
    # fetcher's own ``cancel()`` is called too when it has one.
    on_shutdown: Sequence[Callable[[], None]] = ()


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
        self.integrity: IntegrityGuard | None = None
        if wiring.integrity is not None and wiring.integrity.settings.enabled:
            self.integrity = IntegrityGuard(
                self.store,
                wiring.fetcher,
                wiring.integrity,
                clock=self.clock,
                alerter=self.alerter,
                profiles_dir=settings.state_db.parent / "profiles",
            )
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
            integrity=self.integrity,
        )
        self.scheduler = Scheduler(
            store=self.store,
            queue=self.queue,
            clock=self.clock,
            settings=settings,
            sources=wiring.sources,
            alerter=self.alerter,
            bots={b.name: b.cadence for b in wiring.bots},
            parity=wiring.parity is not None,
        )
        self.bots = {b.name: b for b in wiring.bots}
        self.handlers: dict[JobKind, Callable[[Job], JobResult]] = {
            JobKind.DISCOVER: self._discover,
            JobKind.FETCH_RELEASE: self._fetch_release,
            JobKind.HEALTH_PROBE: self._health_probe,
            JobKind.PRUNE: self._prune,
            JobKind.SCRUB_SLICE: self._scrub_slice,
            JobKind.DRILL: self._drill,
            JobKind.DISCOVERY_BOTS: self._discovery_bots,
            JobKind.PARITY: self._parity,
        }
        self.stopping = threading.Event()
        self._lock = threading.Lock()
        self._running_kind: Counter[JobKind] = Counter()
        self._running_host: Counter[str] = Counter()
        # Job threads are daemon threads: a job that ignores cancellation can never keep the
        # process alive past shutdown (systemd's TimeoutStopSec); its work resumes on next start.
        self._threads: set[threading.Thread] = set()

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
        """Threaded mode: start claimed jobs on daemon worker threads. Returns how many started.

        Concurrency is bounded by :meth:`claim_ready` (``max_workers`` and per-type/host limits).
        """
        if self.stopping.is_set():
            return 0
        jobs = self.claim_ready()
        for job in jobs:
            t = threading.Thread(
                target=self._run_job, args=(job,), name=f"wsp-job-{job.kind.value}", daemon=True
            )
            with self._lock:
                self._threads.add(t)
            t.start()
        return len(jobs)

    def _run_job(self, job: Job) -> None:
        try:
            self.execute(job)
        except Exception:
            if not self.stopping.is_set():
                raise
            # A job abandoned at shutdown finished after the state DB was closed: its outcome is
            # not recorded and it runs again on the next start (it is still "running" there).
            log.info(
                "job finished after shutdown", extra={"fields": {"kind": job.kind.value, "key": job.key}}
            )
        finally:
            with self._lock:
                self._threads.discard(threading.current_thread())

    def running_jobs(self) -> int:
        with self._lock:
            return sum(1 for t in self._threads if t.is_alive())

    def _cancel_work(self) -> None:
        """Ask running work to stop now: wired hooks, then the fetcher's ``cancel()``."""
        hooks: list[Callable[[], None]] = list(self.wiring.on_shutdown)
        cancel = getattr(self.wiring.fetcher, "cancel", None)
        if callable(cancel):
            hooks.append(cast(Callable[[], None], cancel))
        for hook in hooks:
            try:
                hook()
            except Exception:
                log.exception("shutdown hook failed")

    def shutdown(self, grace_seconds: float | None = None) -> bool:
        """Stop claiming, cancel running work, wait up to the grace period. True if all finished.

        Downloads, scrub and parity stop within about a second (cooperative cancellation); a job
        still running after the grace period is abandoned (its thread is a daemon) and resumes on
        the next start, so the process always exits promptly.
        """
        self.stopping.set()
        self._cancel_work()
        grace = self.settings.shutdown_grace_seconds if grace_seconds is None else grace_seconds
        deadline = time.monotonic() + grace
        while self.running_jobs() and time.monotonic() < deadline:
            time.sleep(0.05)
        finished = not self.running_jobs()
        if not finished:
            log.warning(
                "shutdown grace expired; unfinished jobs resume on next start",
                extra={"fields": {"running": self.running_jobs()}},
            )
        try:
            self.refresh_metrics()
        except Exception:  # a job abandoned mid-write must not turn a clean stop into a crash
            log.exception("final metrics refresh failed")
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
        for row in self.store.open_alerts():  # the owner investigated the quarantined release(s)
            if str(row["key"]).startswith(f"integrity:{source_id}/"):
                self.alerter.resolve(str(row["key"]))
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
        offers: list[Mapping[str, object]] = []
        for r in refs:
            try:
                gid = generation_id_for(r)
            except StepError:
                continue
            published = r.published.isoformat() if r.published else None
            offers.append({"generation_id": gid, "version": r.version, "published": published})
        self.store.put_offers(spec.id, offers, self.clock.now())
        return refs

    def holds_last_copy(self, source_id: str, generation: Generation) -> bool:
        """True unless upstream was recently seen serving this generation's version (PLAN §6.4).

        A generation upstream no longer serves is the last copy of that version and is never
        pruned; without a recent observation nothing can be proven redundant, so it is kept too.
        """
        if self.wiring.last_copy is not None:
            return self.wiring.last_copy(source_id, generation)
        seen = self.store.offers(source_id)
        if seen is None:
            return True
        offers, observed = seen
        if self.clock.now() - ensure_utc(observed) > self.settings.offer_max_age:
            return True
        return generation.generation_id not in {str(o.get("generation_id")) for o in offers}

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
                if self.wiring.parity is not None:  # protect the new generation soon (PLAN §8.1)
                    self.queue.enqueue(JobKind.PARITY, PARITY_KEY, now=now, payload={"backfill": True})
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
        vault = self.wiring.vault
        current = vault.current(spec.id)
        promoted = {
            g.generation_id: g.promoted_at for g in self.store.generation_stats(spec.id, limit=10_000)
        }
        infos = [
            GenerationInfo.from_generation(
                g,
                holds_last_copy=self.holds_last_copy(spec.id, g),
                known_good=g.generation_id in promoted,
                settled=self._settled(spec.id, g.generation_id, promoted.get(g.generation_id)),
            )
            for g in vault.generations(spec.id)
        ]
        plan = plan_retention(infos, policy, current=current.generation_id if current else None)
        dry_run = self.wiring.pruner is None or self.settings.prune_dry_run
        last_copies = [i.generation_id for i in infos if i.holds_last_copy]
        self.pipeline.ledger_outbox_writer(
            "retention.plan",
            spec.id,
            {
                "keep": list(plan.keep),
                "prune": list(plan.prune),
                "dry_run": dry_run,
                "last_copy": last_copies,
            },
        )
        failed: list[str] = []
        if not dry_run and self.wiring.pruner is not None:
            for gid in plan.prune:
                try:
                    self.wiring.pruner(spec.id, gid)  # moves aside under pruned/; never deletes
                except Exception as exc:  # e.g. the vault refuses: keep going, report it
                    failed.append(f"{gid}: {exc}")
                    continue
                self.pipeline.ledger_outbox_writer(
                    "retention.pruned", f"{spec.id}/{gid}", {"generation_id": gid}
                )
        self.pipeline.flush_ledger()
        if failed:
            raise StepError(f"{spec.id}: could not prune {'; '.join(failed)}")
        return JobResult()

    def _settled(self, source_id: str, generation_id: str, promoted_at: datetime | None) -> bool:
        """A promoted generation is the known-good anchor once it survived ``settle_days`` and a scrub."""
        if self.integrity is not None:
            return self.integrity.settled(source_id, generation_id, promoted_at)
        return promoted_at is not None  # without the guard: any promoted generation (old behaviour)

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
        if job.key == GLOBAL_KEY:
            if self.wiring.discovery_bots is None:
                return JobResult(note="no discovery hook wired")
            self.handle_candidates(list(self.wiring.discovery_bots()))
            return JobResult()
        bot = self.bots.get(str(job.payload.get("bot", job.key)))
        if bot is None:
            return JobResult(Handled.CANCEL, note="unknown bot")
        meta_key = f"bot:{bot.name}"
        previous: object = json.loads(self.store.get_meta(meta_key) or "{}")
        workdir = self.settings.workdir / "bots" / bot.name
        workdir.mkdir(parents=True, exist_ok=True)
        ctx = BotContext(
            fetcher=self.wiring.fetcher,
            sources=self.wiring.sources,
            now=self.clock.now(),
            user_agent=self.settings.user_agent,
            workdir=workdir,
            gate=self._gate(),
            adapter_for=self.pipeline.resolve_adapter,
            ctx_for=self.pipeline.ctx,
            previous=_notes(previous),
        )
        try:
            result = bot.run(ctx)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            self._record_bot(meta_key, {"at": ctx.now.isoformat(), "ok": False, "error": error})
            raise
        self._apply_bot_result(bot, result, meta_key)
        return JobResult()

    def _apply_bot_result(self, bot: Bot, result: BotResult, meta_key: str) -> None:
        for alert in result.alerts:
            self.alerter.raise_(alert)
        raised = {a.key for a in result.alerts}
        for key in result.resolved:
            if key not in raised:
                self.alerter.resolve(key)
        decided = self.handle_candidates(list(result.candidates))
        summary: dict[str, object] = {
            "at": self.clock.now().isoformat(),
            "ok": True,
            "candidates": len(result.candidates),
            "proposals": decided,
            "alerts": sorted(raised),
            "notes": dict(result.notes),
        }
        self._record_bot(meta_key, summary)
        self.pipeline.ledger_outbox_writer(
            "curator.bot",
            bot.name,
            {"candidates": len(result.candidates), "proposals": decided, "alerts": sorted(raised)},
        )
        self.pipeline.flush_ledger()

    def _record_bot(self, meta_key: str, summary: Mapping[str, object]) -> None:
        self.store.set_meta(meta_key, json.dumps(summary, sort_keys=True, default=str))

    def bot_runs(self) -> dict[str, dict[str, object]]:
        """Last summary of every bot (System Monitor)."""
        out: dict[str, dict[str, object]] = {}
        for name in sorted(self.bots):
            raw = self.store.get_meta(f"bot:{name}")
            out[name] = json.loads(raw) if raw else {}
        return out

    def _gate(self) -> GateConfig:
        return self.wiring.gate_provider() if self.wiring.gate_provider is not None else self.wiring.gate

    def handle_candidates(self, candidates: Sequence[Candidate]) -> list[int]:
        """Run candidates through the policy gate (PLAN §5.4). Returns the new proposal ids.

        A candidate key is proposed once, whatever the owner decides later. AUTO_PROMOTE is applied
        through ``Wiring.promoter`` (the local registry overlay) when one is wired; REJECT is final.
        """
        known = self.store.proposal_keys()
        gate = self._gate()
        ids: list[int] = []
        for candidate in candidates:
            if candidate.key in known:
                continue
            known.add(candidate.key)
            now = self.clock.now()
            proposal = evaluate_candidate(candidate, gate, self.wiring.sources)
            status = "open"
            if proposal.decision is Decision.REJECT:
                status = "rejected"
            elif proposal.decision is Decision.AUTO_PROMOTE and self.wiring.promoter is not None:
                self.wiring.promoter(proposal)
                status = "applied"
            pid = self.store.add_proposal(candidate.key, proposal.decision.value, proposal.to_json(), now)
            if status != "open":
                self.store.set_proposal_status(pid, status)
            self.pipeline.ledger_outbox_writer(
                "policy.decision",
                candidate.key,
                {
                    "decision": proposal.decision.value,
                    "score": proposal.score,
                    "title": proposal.title,
                    "proposal_id": pid,
                    "status": status,
                },
            )
            if self.wiring.proposal_recorder is not None:
                self.wiring.proposal_recorder(ProposalRecord(pid, status, now, proposal))
            if self.wiring.proposal_sink is not None:
                self.wiring.proposal_sink(proposal)
            ids.append(pid)
        self.pipeline.flush_ledger()
        return ids

    def _parity(self, job: Job) -> JobResult:
        if self.wiring.parity is None:
            return JobResult(note="no parity hook wired")
        summary = dict(self.wiring.parity(self.holds_last_copy))
        now = self.clock.now()
        self.store.set_meta("parity:last", json.dumps({"at": now.isoformat(), **summary}, default=str))
        if summary.get("created") or summary.get("skipped") or summary.get("sets_done"):
            self.pipeline.ledger_outbox_writer("parity.run", "warden", summary)
            self.pipeline.flush_ledger()
        skipped = summary.get("skipped")
        if isinstance(skipped, list) and skipped:
            self.alerter.raise_(
                Alert(
                    "parity-skipped",
                    Severity.WARNING,
                    "parity could not be created for some generations; see the parity.run ledger event",
                    {"skipped": cast(list[object], skipped)},
                )
            )
        else:
            self.alerter.resolve("parity-skipped")
        pending = summary.get("pending")
        if isinstance(pending, int) and pending > 0 and summary.get("in_progress"):
            # Budget reached with work left: continue soon (the hook paces itself).
            return JobResult(Handled.DEFER, run_after=now + self.settings.parity_continue, note="more to do")
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


def _notes(summary: object) -> Mapping[str, object]:
    """The ``notes`` of a stored bot summary (handed to the bot's next run as ``previous``)."""
    if not isinstance(summary, dict):
        return {}
    notes = cast(dict[str, object], summary).get("notes")
    return cast(dict[str, object], notes) if isinstance(notes, dict) else {}
