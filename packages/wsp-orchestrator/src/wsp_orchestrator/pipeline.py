"""Release pipeline: drives one release DISCOVERED → … → PROMOTED (PLAN §6.3).

Guarantees:

* Only transitions allowed by :func:`wsp_core.model.allowed_transitions` are taken.
* Each transition is committed to the local state store together with an *outbox* row in one SQLite
  transaction, then appended to the ledger with ``idempotency_key=event_id``. A crash between the
  two is repaired on the next run by appending again: the ledger hands back the event it already
  holds - so every transition produces exactly one ledger event.
* Fetch digests (``FetchResult``) are stored with the release: verification trusts a download whose
  upstream digests all matched (no local re-hash), and the vault's read-back must equal them.
* Each step is idempotent, so a crash anywhere just repeats the interrupted step: adapters resume
  partial downloads, ``Vault.stage`` reopens, a generation that is already sealed is reused, and
  ``Vault.promote`` re-points ``CURRENT`` at the same generation.
* Failures go to FAILED with exponential backoff; after ``max_attempts`` failures the release is
  QUARANTINED and an alert is raised.
* Promotion is gated by sanity checks (PLAN §6.4); a suspicious generation is *held* in INDEXED
  until :meth:`ReleasePipeline.approve` or :meth:`ReleasePipeline.reject`.
* With an :class:`~wsp_orchestrator.integrity.IntegrityGuard` (ADR-0009): after the download the
  provenance is recorded, mirrors and an independent checksum channel are cross-checked and hosts
  are compared with the source's pins (a disagreement quarantines the release and pauses the
  source; an unknown origin domain holds it in FETCHED); the signed ``PROVENANCE.json`` is written
  after sealing; before promotion a content profile is compared with the last promoted generation
  and the signed ``PROMOTION.json`` is written (out-of-range checks hold the release).
"""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from wsp_core.model import (
    FetchPlan,
    FetchResult,
    Generation,
    GenerationStatus,
    ReleaseRef,
    ReleaseState,
    SourceSpec,
    VerdictStatus,
    allowed_transitions,
)
from wsp_core.ports import Ctx, Fetcher, Ledger, SourceAdapter, Vault
from wsp_orchestrator.alerts import Alert, Alerter, Severity
from wsp_orchestrator.clock import Clock
from wsp_orchestrator.codec import (
    fetch_result_from_json,
    fetch_result_to_json,
    plan_from_json,
    plan_to_json,
    release_from_json,
    release_to_json,
)
from wsp_orchestrator.integrity.findings import Action
from wsp_orchestrator.integrity.guard import IntegrityGuard, sanity_checks
from wsp_orchestrator.policy import SanityThresholds, check_promotion
from wsp_orchestrator.store import GenerationStats, ReleaseRecord, SourceHealth, StateStore

log = logging.getLogger("wsp.orchestrator.pipeline")

LEDGER_KIND_STATE = "release.state"
LEDGER_KIND_HOLD = "release.hold"
_UNSAFE_GEN_CHARS = re.compile(r"[^A-Za-z0-9._-]+")


class IllegalTransitionError(RuntimeError):
    pass


class StepError(RuntimeError):
    """A step failed in a way the pipeline understands (as opposed to an unexpected exception)."""


class ChecksumMismatchError(StepError):
    pass


class UnknownReleaseError(KeyError):
    pass


@dataclass(frozen=True, slots=True)
class RefineResult:
    doc_count: int | None = None


class RefineStep(Protocol):
    """Refinery hook: normalize documents of a sealed generation (PLAN §9.1)."""

    def __call__(self, source: SourceSpec, generation: Generation, generation_dir: Path) -> RefineResult: ...


class IndexStep(Protocol):
    """Index hook: upsert the generation's change set into local indexes (PLAN §9.3)."""

    def __call__(self, source: SourceSpec, generation: Generation) -> None: ...


AdapterResolver = Callable[[SourceSpec], SourceAdapter]


def adapter_resolver(adapters: Mapping[str, SourceAdapter]) -> AdapterResolver:
    """Resolve by the registry ``access.adapter`` id, falling back to the source id."""

    def resolve(spec: SourceSpec) -> SourceAdapter:
        for key in (spec.adapter, spec.id):
            if key is not None and key in adapters:
                return adapters[key]
        raise StepError(f"no adapter registered for source {spec.id!r} (adapter {spec.adapter!r})")

    return resolve


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    max_attempts: int = 5
    base_delay: timedelta = timedelta(minutes=10)
    factor: float = 4.0
    max_delay: timedelta = timedelta(hours=24)

    def delay(self, attempt: int) -> timedelta:
        """Backoff before retry number ``attempt`` (1-based)."""
        seconds = self.base_delay.total_seconds() * self.factor ** max(attempt - 1, 0)
        return min(timedelta(seconds=seconds), self.max_delay)


class Outcome(StrEnum):
    PROMOTED = "promoted"
    QUARANTINED = "quarantined"
    HELD = "held"  # sanity check failed; waiting for review
    BACKOFF = "backoff"  # FAILED, waiting for next_attempt_at
    PAUSED = "paused"  # source auto-paused or paused by the owner
    CANCELLED = "cancelled"  # shutdown requested between steps


@dataclass(frozen=True, slots=True)
class RunResult:
    record: ReleaseRecord
    outcome: Outcome


def generation_id_for(ref: ReleaseRef) -> str:
    gid = _UNSAFE_GEN_CHARS.sub("-", ref.version).strip("-.")
    if not gid:
        raise StepError(f"cannot derive a generation id from version {ref.version!r}")
    return gid


def _noop(_point: str) -> None:
    return None


class ReleasePipeline:
    def __init__(
        self,
        *,
        store: StateStore,
        sources: Sequence[SourceSpec],
        adapters: AdapterResolver,
        fetcher: Fetcher,
        vault: Vault,
        ledger: Ledger,
        refine: RefineStep,
        index: IndexStep,
        clock: Clock,
        workdir: Path,
        user_agent: str,
        alerter: Alerter | None = None,
        retry: RetryPolicy = RetryPolicy(),  # noqa: B008 - immutable
        sanity: SanityThresholds = SanityThresholds(),  # noqa: B008 - immutable
        checksum_failures_to_pause: int = 2,
        checkpoint: Callable[[str], None] = _noop,
        integrity: IntegrityGuard | None = None,
    ) -> None:
        self.store = store
        self.sources = {s.id: s for s in sources}
        self.resolve_adapter = adapters
        self.fetcher = fetcher
        self.vault = vault
        self.ledger = ledger
        self.refine = refine
        self.index = index
        self.clock = clock
        self.workdir = workdir
        self.user_agent = user_agent
        self.alerter = alerter
        self.retry = retry
        self.sanity = sanity
        self.checksum_failures_to_pause = checksum_failures_to_pause
        self.checkpoint = checkpoint  # test hook: called at every crash-relevant point
        self.integrity = integrity

    # --- public API ---------------------------------------------------------------------------

    def register(self, ref: ReleaseRef) -> ReleaseRecord:
        """Record a discovered release (idempotent). Returns the stored record."""
        if ref.source_id not in self.sources:
            raise StepError(f"unknown source {ref.source_id!r}")
        now = self.clock.now()
        rec = ReleaseRecord(
            source_id=ref.source_id,
            version=ref.version,
            state=ReleaseState.DISCOVERED,
            attempts=0,
            ref=release_to_json(ref),
            generation_id=generation_id_for(ref),
        )
        with self.store.tx() as c:
            if self.store.insert_release(rec, now, c):
                event_id = uuid.uuid4().hex
                self.store.add_outbox(
                    c,
                    event_id,
                    LEDGER_KIND_STATE,
                    rec.subject,
                    {"event_id": event_id, "from": None, "to": ReleaseState.DISCOVERED.value, "attempt": 0},
                )
                health = self.store.source_health(ref.source_id)
                self.store.put_source_health(replace(health, last_new_release_at=now), c)
        self.checkpoint("register:committed")
        self.flush_ledger()
        return self._get(ref.source_id, ref.version)

    def get(self, source_id: str, version: str) -> ReleaseRecord | None:
        return self.store.get_release(source_id, version)

    def run(self, source_id: str, version: str, *, cancel: Callable[[], bool] = lambda: False) -> RunResult:
        """Advance the release as far as possible. Never raises for step failures."""
        self.flush_ledger()
        while True:
            rec = self._get(source_id, version)
            outcome = self._stop_reason(rec)
            if outcome is not None:
                return RunResult(rec, outcome)
            if cancel():
                return RunResult(rec, Outcome.CANCELLED)
            self.step(source_id, version, cancel=cancel)

    def step(
        self, source_id: str, version: str, *, cancel: Callable[[], bool] = lambda: False
    ) -> ReleaseRecord:
        """Perform at most one transition (or record a failure).

        An exception raised while ``cancel()`` is true (a download or hash interrupted by shutdown)
        is not a failure: the step is simply repeated on the next start.
        """
        rec = self._get(source_id, version)
        if self._stop_reason(rec) is not None:
            return rec
        self.checkpoint(f"step:{rec.state.value}:begin")
        try:
            return self._advance(rec)
        except Exception as exc:
            if rec.state in (ReleaseState.FAILED, ReleaseState.QUARANTINED, ReleaseState.PROMOTED):
                raise
            if cancel():
                log.info(
                    "release step interrupted by shutdown",
                    extra={"fields": {"release": rec.subject, "state": rec.state.value, "error": repr(exc)}},
                )
                return rec
            log.warning(
                "release step failed",
                extra={"fields": {"release": rec.subject, "state": rec.state.value, "error": repr(exc)}},
            )
            return self._fail(rec, exc)

    def approve(self, source_id: str, version: str, *, note: str = "") -> ReleaseRecord:
        """Owner override: promote a held release despite the sanity check.

        A release held earlier (an unknown origin domain, held in FETCHED) is released by trusting
        the new hosts (they are pinned); it then continues through every other check.
        """
        rec = self._get(source_id, version)
        if rec.state is not ReleaseState.INDEXED:
            if rec.hold_reason is None:
                raise IllegalTransitionError(
                    f"{rec.subject} is {rec.state.value} and not held; nothing to approve"
                )
            trusted = self.integrity.trust(source_id, version) if self.integrity is not None else []
            self._persist(
                replace(rec, hold_reason=None),
                event=(
                    LEDGER_KIND_HOLD,
                    {"action": "approved", "note": note, "reason": rec.hold_reason, "trusted_hosts": trusted},
                ),
            )
            if self.alerter is not None:
                self.alerter.resolve(f"held:{rec.subject}")
            return self._get(source_id, version)
        self._persist(
            replace(rec, hold_reason=None, approved=True),
            event=(LEDGER_KIND_HOLD, {"action": "approved", "note": note, "reason": rec.hold_reason}),
        )
        if self.alerter is not None:
            self.alerter.resolve(f"held:{rec.subject}")
        return self._get(source_id, version)

    def reject(self, source_id: str, version: str, *, reason: str) -> ReleaseRecord:
        """Owner decision: a held (or otherwise stuck) release is bad. It ends QUARANTINED."""
        rec = self._get(source_id, version)
        if rec.state is not ReleaseState.FAILED:
            rec = self._transition(
                rec, ReleaseState.FAILED, attempts=rec.attempts + 1, last_error=f"rejected: {reason}"
            )
        rec = self._transition(rec, ReleaseState.QUARANTINED, extra={"reason": f"rejected: {reason}"})
        if self.alerter is not None:
            self.alerter.resolve(f"held:{rec.subject}")
        return rec

    def flush_ledger(self) -> int:
        """Deliver pending outbox events to the ledger exactly once (idempotent appends keyed by
        ``event_id``). Returns how many outbox rows were flushed."""
        flushed = 0
        for e in self.store.outbox():
            ev = self.ledger.append(e.kind, e.subject, e.data, idempotency_key=e.event_id)
            self.checkpoint(f"ledger:{e.data.get('to') or e.data.get('action')}:appended")
            self.store.remove_outbox(e.event_id, ev.seq)
            flushed += 1
        return flushed

    # --- state machine ------------------------------------------------------------------------

    def _stop_reason(self, rec: ReleaseRecord) -> Outcome | None:
        if rec.state is ReleaseState.PROMOTED:
            return Outcome.PROMOTED
        if rec.state is ReleaseState.QUARANTINED:
            return Outcome.QUARANTINED
        if self.store.source_health(rec.source_id).paused:
            return Outcome.PAUSED
        retry_at = rec.next_attempt_at
        if rec.state is ReleaseState.FAILED and retry_at is not None and retry_at > self.clock.now():
            return Outcome.BACKOFF
        if rec.hold_reason is not None and rec.state is not ReleaseState.FAILED:
            return Outcome.HELD
        return None

    def _advance(self, rec: ReleaseRecord) -> ReleaseRecord:
        spec = self.sources.get(rec.source_id)
        if spec is None:
            raise StepError(f"source {rec.source_id!r} is no longer in the registry")
        match rec.state:
            case ReleaseState.FAILED:
                return self._transition(
                    rec, ReleaseState.PLANNED, next_attempt_at=None, extra={"retry": True}
                )
            case ReleaseState.DISCOVERED:
                return self._transition(rec, ReleaseState.PLANNED, plan=self._make_plan(spec, rec))
            case ReleaseState.PLANNED:
                plan = rec.plan if rec.plan is not None else self._make_plan(spec, rec)
                if self._sealed_generation(rec) is None:
                    self.vault.stage(rec.source_id, rec.generation_id)
                    self.checkpoint("vault:staged")
                return self._transition(rec, ReleaseState.FETCHING, plan=plan)
            case ReleaseState.FETCHING:
                results = self._fetch(spec, rec)
                return self._transition(
                    rec,
                    ReleaseState.FETCHED,
                    local_files=tuple(str(r.path) for r in results),
                    fetched={r.path.name: fetch_result_to_json(r) for r in results},
                )
            case ReleaseState.FETCHED:
                self._verify(spec, rec)
                if self.integrity is not None and self._sealed_generation(rec) is None:
                    verdict = self.integrity.check_fetch(
                        spec, rec.version, self._plan(rec).files, rec.fetched
                    )
                    self.checkpoint("integrity:checked")
                    if verdict.action is Action.QUARANTINE:
                        return self._quarantine_integrity(rec, verdict.reason)
                    if verdict.action is Action.HOLD:
                        return self._hold(rec, f"integrity: {verdict.reason}")
                return self._transition(rec, ReleaseState.VERIFIED, health=self._reset_checksum_failures(rec))
            case ReleaseState.VERIFIED:
                gen = self._seal(rec)
                if self.integrity is not None:
                    self.integrity.attest_provenance(rec.source_id, rec.version, rec.generation_id)
                    self.checkpoint("integrity:provenance")
                size = sum(a.size for a in gen.artifacts)
                return self._transition(rec, ReleaseState.SEALED, size_bytes=size)
            case ReleaseState.SEALED:
                gen = self._require_sealed(rec)
                result = self.refine(spec, gen, self.vault.generation_path(rec.source_id, rec.generation_id))
                self.checkpoint("refine:done")
                return self._transition(rec, ReleaseState.REFINED, doc_count=result.doc_count)
            case ReleaseState.REFINED:
                self.index(spec, self._require_sealed(rec))
                self.checkpoint("index:done")
                return self._transition(rec, ReleaseState.INDEXED)
            case ReleaseState.INDEXED:
                return self._promote(spec, rec)
            case ReleaseState.PROMOTED | ReleaseState.QUARANTINED:
                return rec

    def ctx(self, spec: SourceSpec) -> Ctx:
        workdir = self.workdir / spec.id
        workdir.mkdir(parents=True, exist_ok=True)
        return Ctx(source=spec, fetcher=self.fetcher, workdir=workdir, user_agent=self.user_agent)

    def _make_plan(self, spec: SourceSpec, rec: ReleaseRecord) -> dict[str, Any]:
        ref = release_from_json(rec.ref)
        have = self.vault.current(spec.id)
        plan = self.resolve_adapter(spec).plan(self.ctx(spec), have, ref)
        self.checkpoint("adapter:planned")
        return plan_to_json(plan)

    def _plan(self, rec: ReleaseRecord) -> FetchPlan:
        if rec.plan is None:
            raise StepError(f"{rec.subject}: no fetch plan recorded")
        return plan_from_json(rec.plan)

    def _fetch(self, spec: SourceSpec, rec: ReleaseRecord) -> tuple[FetchResult, ...]:
        if self._sealed_generation(rec) is not None:
            return tuple(self._fetched(rec).values())  # retry after sealing: nothing to download again
        results = tuple(self.resolve_adapter(spec).fetch(self.ctx(spec), self._plan(rec)))
        self.checkpoint("adapter:fetched")
        return results

    @staticmethod
    def _fetched(rec: ReleaseRecord) -> dict[str, FetchResult]:
        return {name: fetch_result_from_json(d) for name, d in rec.fetched.items()}

    def _local_for(self, rec: ReleaseRecord, name: str) -> Path:
        for p in rec.local_files:
            if Path(p).name == name:
                return Path(p)
        raise StepError(f"{rec.subject}: fetched files do not include {name!r}")

    def _verify(self, spec: SourceSpec, rec: ReleaseRecord) -> None:
        if self._sealed_generation(rec) is not None:
            return  # verified and read back by the vault when it was sealed
        adapter, ctx = self.resolve_adapter(spec), self.ctx(spec)
        fetched = self._fetched(rec)
        problems: list[str] = []
        mismatch = False
        for remote in self._plan(rec).files:
            local = self._local_for(rec, remote.name)
            if not local.is_file():
                problems.append(f"{remote.name}: missing locally")
                continue
            result = fetched.get(remote.name)
            if (
                result is not None
                and result.verdict.status is VerdictStatus.OK
                and local.stat().st_size == result.size
            ):
                continue  # the fetcher matched every upstream digest while downloading: no re-hash
            verdict = adapter.verify(ctx, remote, local)
            if not verdict.ok:
                mismatch = mismatch or verdict.status is VerdictStatus.MISMATCH
                problems.append(f"{remote.name}: {verdict.status.value} {verdict.detail}".strip())
        self.checkpoint("adapter:verified")
        if problems:
            err = ChecksumMismatchError if mismatch else StepError
            raise err("; ".join(problems))

    def _sealed_generation(self, rec: ReleaseRecord) -> Generation | None:
        for g in self.vault.generations(rec.source_id):
            if g.generation_id == rec.generation_id and g.status is GenerationStatus.SEALED:
                return g
        return None

    def _require_sealed(self, rec: ReleaseRecord) -> Generation:
        gen = self._sealed_generation(rec)
        if gen is None:
            raise StepError(f"{rec.subject}: generation {rec.generation_id} is not sealed in the vault")
        return gen

    def _seal(self, rec: ReleaseRecord) -> Generation:
        existing = self._sealed_generation(rec)
        if existing is not None:
            return existing
        self.vault.stage(rec.source_id, rec.generation_id)
        fetched = self._fetched(rec)
        for remote in self._plan(rec).files:
            local = self._local_for(rec, remote.name)
            self.vault.ingest(
                rec.source_id,
                rec.generation_id,
                local,
                f"upstream/{remote.name}",
                upstream=remote.checksums,
                expected=fetched.get(remote.name),
            )
            self.checkpoint("vault:ingested")
        gen = self.vault.seal(rec.source_id, rec.generation_id)
        self.checkpoint("vault:sealed")
        return gen

    def _promote(self, spec: SourceSpec, rec: ReleaseRecord) -> ReleaseRecord:
        history = self.store.generation_stats(spec.id, limit=max(self.sanity.trend_window, 1))
        history = [h for h in history if h.generation_id != rec.generation_id]
        result = check_promotion(
            [h.doc_count for h in history if h.doc_count is not None],
            [h.size_bytes for h in history if h.size_bytes is not None],
            doc_count=rec.doc_count,
            size_bytes=rec.size_bytes,
            thresholds=self.sanity,
        )
        if self.integrity is not None:
            gen = self._require_sealed(rec)
            promoted = [h.generation_id for h in self.store.generation_stats(spec.id, limit=50)]
            report = self.integrity.promotion_report(
                spec,
                gen,
                self.vault.generation_path(rec.source_id, rec.generation_id),
                sanity=sanity_checks(result.reasons, result.ok),
                promoted=promoted,
            )
            if not report.ok and rec.approved:
                report = replace(report, decision="approved by the owner")
            self.integrity.write_report(report, rec.version)
            self.checkpoint("integrity:report")
            if not report.ok and not rec.approved:
                return self._hold(rec, report.reasons())
        elif not rec.approved and not result.ok:
            return self._hold(rec, "; ".join(result.reasons))
        self.vault.promote(rec.source_id, rec.generation_id)
        self.checkpoint("vault:promoted")
        now = self.clock.now()
        health = self.store.source_health(rec.source_id)
        rec = self._transition(
            rec,
            ReleaseState.PROMOTED,
            health=replace(health, last_cycle_ok=now, checksum_failures=0),
            stats=GenerationStats(rec.source_id, rec.generation_id, rec.doc_count, rec.size_bytes, now),
        )
        self._cleanup_workdir(rec)
        return rec

    def _hold(self, rec: ReleaseRecord, reason: str) -> ReleaseRecord:
        self._persist(
            replace(rec, hold_reason=reason), event=(LEDGER_KIND_HOLD, {"action": "held", "reason": reason})
        )
        if self.alerter is not None:
            self.alerter.raise_(
                Alert(
                    key=f"held:{rec.subject}",
                    severity=Severity.WARNING,
                    message=f"{rec.subject} held for review before promotion: {reason}",
                    data={"source_id": rec.source_id, "version": rec.version, "reason": reason},
                )
            )
        return self._get(rec.source_id, rec.version)

    def _quarantine_integrity(self, rec: ReleaseRecord, reason: str) -> ReleaseRecord:
        """A possible compromise: never promote, pause the source, alert. The downloaded files stay
        in the work directory as evidence (see the runbook "Investigating a held release")."""
        failed = self._transition(
            rec,
            ReleaseState.FAILED,
            attempts=rec.attempts + 1,
            last_error=f"integrity: {reason}",
            next_attempt_at=None,
            extra={"error": f"integrity: {reason}", "failed_in": rec.state.value, "integrity": True},
        )
        quarantined = self._transition(
            failed, ReleaseState.QUARANTINED, extra={"reason": f"integrity: {reason}"}
        )
        self._pause_source(rec.source_id, f"integrity: {rec.version}: {reason[:300]}")
        if self.alerter is not None:
            self.alerter.raise_(
                Alert(
                    key=f"integrity:{rec.subject}",
                    severity=Severity.CRITICAL,
                    message=f"{rec.subject} quarantined, possible upstream compromise: {reason}",
                    data={"source_id": rec.source_id, "version": rec.version, "reason": reason},
                )
            )
        return quarantined

    def _cleanup_workdir(self, rec: ReleaseRecord) -> None:
        root = (self.workdir / rec.source_id).resolve()
        for p in rec.local_files:
            path = Path(p).resolve()
            if root in path.parents and path.is_file():
                path.unlink(missing_ok=True)

    def _reset_checksum_failures(self, rec: ReleaseRecord) -> SourceHealth | None:
        health = self.store.source_health(rec.source_id)
        return replace(health, checksum_failures=0) if health.checksum_failures else None

    def _fail(self, rec: ReleaseRecord, exc: Exception) -> ReleaseRecord:
        attempts = rec.attempts + 1
        now = self.clock.now()
        health: SourceHealth | None = None
        if isinstance(exc, ChecksumMismatchError):
            h = self.store.source_health(rec.source_id)
            health = replace(h, checksum_failures=h.checksum_failures + 1)
        quarantine = attempts >= self.retry.max_attempts
        failed = self._transition(
            rec,
            ReleaseState.FAILED,
            attempts=attempts,
            last_error=f"{type(exc).__name__}: {exc}",
            next_attempt_at=None if quarantine else now + self.retry.delay(attempts),
            health=health,
            extra={"error": f"{type(exc).__name__}: {exc}", "failed_in": rec.state.value},
        )
        if health is not None and health.checksum_failures >= self.checksum_failures_to_pause:
            self._pause_source(
                rec.source_id, f"checksum-failures: {health.checksum_failures} consecutive checksum failures"
            )
        if not quarantine:
            return failed
        quarantined = self._transition(failed, ReleaseState.QUARANTINED, extra={"reason": "max attempts"})
        if self.alerter is not None:
            self.alerter.raise_(
                Alert(
                    key=f"quarantined:{rec.subject}",
                    severity=Severity.CRITICAL,
                    message=f"{rec.subject} quarantined after {attempts} failed attempts: {exc}",
                    data={"source_id": rec.source_id, "version": rec.version, "attempts": attempts},
                )
            )
        return quarantined

    def _pause_source(self, source_id: str, reason: str) -> None:
        pause_source(self.store, self.ledger_outbox_writer, source_id, reason, alerter=self.alerter)
        self.flush_ledger()

    def ledger_outbox_writer(self, kind: str, subject: str, data: Mapping[str, object]) -> None:
        """Queue a ledger event through the outbox (used by helpers sharing this pipeline's ledger)."""
        event_id = uuid.uuid4().hex
        with self.store.tx() as c:
            self.store.add_outbox(c, event_id, kind, subject, {"event_id": event_id, **data})

    # --- persistence --------------------------------------------------------------------------

    def _get(self, source_id: str, version: str) -> ReleaseRecord:
        rec = self.store.get_release(source_id, version)
        if rec is None:
            raise UnknownReleaseError(f"{source_id}/{version}")
        return rec

    def _transition(
        self,
        rec: ReleaseRecord,
        to: ReleaseState,
        *,
        health: SourceHealth | None = None,
        stats: GenerationStats | None = None,
        extra: Mapping[str, object] | None = None,
        **changes: Any,
    ) -> ReleaseRecord:
        if to not in allowed_transitions(rec.state):
            raise IllegalTransitionError(f"{rec.subject}: {rec.state.value} -> {to.value} is not allowed")
        new = replace(rec, state=to, **changes)
        data: dict[str, object] = {"from": rec.state.value, "to": to.value, "attempt": new.attempts}
        if extra:
            data.update(extra)
        self._persist(new, event=(LEDGER_KIND_STATE, data), health=health, stats=stats)
        log.info(
            "release transition",
            extra={"fields": {"release": rec.subject, "from": rec.state.value, "to": to.value}},
        )
        return self._get(rec.source_id, rec.version)

    def _persist(
        self,
        rec: ReleaseRecord,
        *,
        event: tuple[str, Mapping[str, object]],
        health: SourceHealth | None = None,
        stats: GenerationStats | None = None,
    ) -> None:
        now: datetime = self.clock.now()
        event_id = uuid.uuid4().hex
        kind, data = event
        with self.store.tx() as c:
            self.store.update_release(rec, now, c)
            self.store.add_outbox(c, event_id, kind, rec.subject, {"event_id": event_id, **data})
            if health is not None:
                self.store.put_source_health(health, c)
            if stats is not None:
                self.store.add_generation_stats(stats, c)
        self.checkpoint(f"commit:{data.get('to') or data.get('action')}")
        self.flush_ledger()


def pause_source(
    store: StateStore,
    write_event: Callable[[str, str, Mapping[str, object]], None],
    source_id: str,
    reason: str,
    *,
    alerter: Alerter | None = None,
) -> bool:
    """Auto-pause a source (PLAN §5.4): one alert, the last good generation stays current.

    ``reason`` is ``"<code>: <detail>"``; a reason code already recorded is not added again.
    Returns True if the source was newly paused (or gained a new reason code).
    """
    health = store.source_health(source_id)
    code = reason.split(":", 1)[0]
    if any(r.split(":", 1)[0] == code for r in health.pause_reasons):
        return False
    store.put_source_health(replace(health, paused=True, pause_reasons=(*health.pause_reasons, reason)))
    write_event("source.paused", source_id, {"reason": reason})
    if alerter is not None:
        alerter.raise_(
            Alert(
                key=f"paused:{source_id}",
                severity=Severity.CRITICAL,
                message=f"source {source_id} auto-paused: {reason}; the current generation keeps serving",
                data={"source_id": source_id, "reason": reason},
            )
        )
    return True


def resume_source(
    store: StateStore,
    write_event: Callable[[str, str, Mapping[str, object]], None],
    source_id: str,
    *,
    alerter: Alerter | None = None,
) -> None:
    """Owner action: clear a pause, reset the checksum failure counter and accept the current format."""
    health = store.source_health(source_id)
    store.put_source_health(
        replace(health, paused=False, pause_reasons=(), checksum_failures=0, observed_format=None)
    )
    write_event("source.resumed", source_id, {"previous_reasons": list(health.pause_reasons)})
    if alerter is not None:
        alerter.resolve(f"paused:{source_id}")
