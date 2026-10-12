"""Release pipeline: drives one release DISCOVERED → … → PROMOTED (PLAN §6.3).

Guarantees:

* Only transitions allowed by :func:`wsp_core.model.allowed_transitions` are taken.
* Each transition is committed to the local state store together with an *outbox* row in one SQLite
  transaction, then appended to the ledger with ``idempotency_key=event_id``. A crash between the
  two is repaired on the next run by appending again: the ledger hands back the event it already
  holds - so every transition produces exactly one ledger event. A ledger append that fails after
  the commit (the NAS hiccups) is logged and retried later; it never fails the release.
* **Optimistic concurrency** (audit H4): every update of a release is guarded by its ``row_version``.
  Failures are recorded on the *current* row, never a stale copy; when the owner (``wsp reject``) or
  another thread changed the release during a step, the step's result is dropped instead of
  overwriting theirs. Source health is changed inside the same transaction it is read in.
* Fetch digests (``FetchResult``) are stored with the release: the vault's read-back must equal them.
* Each step is idempotent, so a crash anywhere just repeats the interrupted step: adapters resume
  partial downloads, ``Vault.stage`` reopens, a generation that is already sealed is reused, and
  ``Vault.promote`` re-points ``CURRENT`` at the same generation.
* **Crash accounting** (audit C1): a step token is committed before the step's side effects. A token
  found at startup (:meth:`ReleasePipeline.recover_crashes`) means the daemon died inside that step;
  an OOM kill of a step's child unit counts the same. The first crash retries at once; the second in
  the same state goes to FAILED with backoff and raises ``crashed:<release>`` ("probably OOM").
  Crashes never count as attempts and never quarantine.
* **Failure taxonomy** (:mod:`wsp_orchestrator.errors`, audit M1/H1): a busy lock or an unreachable NAS
  *defers* the step (same state, no attempt, retried after ``defer_delay``); out-of-memory is a
  crash; only content failures count attempts, and ``max_attempts`` of them quarantine the release
  with an alert. A failed release retries from PLANNED; every step up to the failed one finds its work
  done (the sealed generation, the refined output) and passes straight through.
* **Ordering** (audit H2): SEALED → REFINED refines; REFINED → INDEXED runs the promotion gate first
  (sanity checks, PLAN §6.4, and the integrity content profile), then ``Vault.promote``, then
  indexes. A held generation therefore stays REFINED and is never searchable; INDEXED → PROMOTED is
  bookkeeping (pins, statistics, work directory). A generation older than the current one is never
  promoted (it is superseded) unless the owner approves it with ``force``; promoting a generation
  supersedes older held releases of the source (audit H5).
* With an :class:`~wsp_orchestrator.integrity.IntegrityGuard` (ADR-0009): after the download the
  provenance is recorded, mirrors and an independent checksum channel are cross-checked and hosts
  are compared with the source's pins (a disagreement quarantines the release, moves the downloaded
  files to ``<workdir>/_quarantine/<source>/<generation>/evidence/`` and pauses the source; an unknown
  origin domain holds it in FETCHED); new pins are staged and committed only on promotion (audit M5);
  the signed ``PROVENANCE.json`` is written after sealing; the content profile and the signed
  ``PROMOTION.json`` are part of the promotion gate.
* **Document policy** (ADR-0010, audit H3): every fetched file passes the policy screen, whatever the
  fetcher verified. An adapter with a separate ``screen()`` (:class:`DocumentScreen`) or the wired
  ``document_screen`` hook is always called; otherwise the adapter's ``verify`` (where adapters keep
  their policy) is always called. A fetcher-verified digest only saves the *checksum* re-hash, and only
  when a screen covered the file. A withheld file (``rejected``: restricted or out of scope; ``held``:
  needs an owner decision) is dropped from the release and the rest goes on: the narrowed plan is
  committed first (ledger ``release.withheld``), then a rejected file is deleted and a held one moved
  to ``<workdir>/_quarantine/<source>/<generation>/`` (never the vault); one alert per release and the
  ``on_withheld`` hook report them. A release whose every file is withheld ends QUARANTINED.
* Partitioned sources (ADR-0010): the promotion sanity checks and the backwards-promotion guard
  compare a generation with earlier generations of the same partition only.
* Local copies of downloaded files are removed once the generation is sealed (held and failed releases
  no longer keep gigabytes in the work directory, audit M3).
"""

from __future__ import annotations

import contextlib
import logging
import re
import threading
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol, cast, runtime_checkable

from wsp_core.model import (
    FetchPlan,
    FetchResult,
    Generation,
    GenerationStatus,
    ReleaseRef,
    ReleaseState,
    RemoteFile,
    SourceSpec,
    Verdict,
    VerdictStatus,
    allowed_transitions,
    searchable,
)
from wsp_core.partitions import partition_of, same_partition
from wsp_core.ports import Ctx, Fetcher, Ledger, SourceAdapter, SupportsGuardedPromote, Vault
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
from wsp_orchestrator.errors import FailureKind, classify
from wsp_orchestrator.integrity.findings import Action
from wsp_orchestrator.integrity.guard import IntegrityGuard, sanity_checks
from wsp_orchestrator.policy import SanityThresholds, check_promotion
from wsp_orchestrator.store import (
    ConcurrentUpdateError,
    GenerationStats,
    ReleaseRecord,
    SourceHealth,
    StateStore,
)
from wsp_orchestrator.versions import is_newer, newest

log = logging.getLogger("wsp.orchestrator.pipeline")

LEDGER_KIND_STATE = "release.state"
LEDGER_KIND_HOLD = "release.hold"
LEDGER_KIND_WITHHELD = "release.withheld"
LEDGER_KIND_CRASH = "release.crash"
LEDGER_KIND_RETRY = "release.retry"
QUARANTINE_DIR = "_quarantine"
_UNSAFE_GEN_CHARS = re.compile(r"[^A-Za-z0-9._-]+")
_TERMINAL = (ReleaseState.PROMOTED, ReleaseState.QUARANTINED)
_SEALED_STATES = (ReleaseState.SEALED, ReleaseState.REFINED, ReleaseState.INDEXED, ReleaseState.PROMOTED)


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


@runtime_checkable
class DocumentScreen(Protocol):
    """Adapter capability: the document-policy screen, separate from checksum verification.

    Returns ``ok`` (allowed), ``rejected``/``held`` (withheld, ADR-0010) or ``mismatch`` (the file is
    not what it claims, e.g. an HTML page instead of a PDF). Called for every fetched file.
    """

    def screen(self, ctx: Ctx, remote: RemoteFile, local: Path) -> Verdict: ...


# The composition root's screen for adapters without ``screen()``: a verdict, or ``None`` when it does
# not know the adapter's policy (then the adapter's own ``verify`` always runs: fail closed).
DocumentScreenHook = Callable[[SourceSpec, Ctx, RemoteFile, Path], Verdict | None]

AdapterResolver = Callable[[SourceSpec], SourceAdapter]


@dataclass(frozen=True, slots=True)
class Withheld:
    """A fetched file that document policy keeps out of the vault (ADR-0010)."""

    name: str
    url: str
    status: str  # VerdictStatus.REJECTED or VerdictStatus.HELD value
    detail: str
    quarantined: str = ""  # where a held file waits for the owner (local work directory)

    def to_json(self) -> dict[str, str]:
        out = {"name": self.name, "url": self.url, "status": self.status, "detail": self.detail}
        if self.quarantined:
            out["quarantined"] = self.quarantined
        return out

    @staticmethod
    def from_json(d: Mapping[str, Any]) -> Withheld:
        return Withheld(
            str(d["name"]),
            str(d.get("url", "")),
            str(d["status"]),
            str(d.get("detail", "")),
            str(d.get("quarantined", "")),
        )


# Called once per release that withheld files: (source, version, withheld files).
WithheldHook = Callable[[SourceSpec, str, Sequence[Withheld]], None]


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
    HELD = "held"  # sanity or integrity check failed; waiting for review
    BACKOFF = "backoff"  # FAILED, waiting for next_attempt_at
    DEFERRED = "deferred"  # a lock was busy or storage unavailable; same state, retried later
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


HealthChange = Callable[[SourceHealth], SourceHealth]


@dataclass(frozen=True, slots=True)
class _Step:
    """The step a thread is running: the release, the state it started in and its token."""

    source_id: str
    version: str
    state: ReleaseState
    token: str | None


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
        on_withheld: WithheldHook | None = None,
        document_screen: DocumentScreenHook | None = None,
        storage_available: Callable[[], bool] | None = None,
        defer_delay: timedelta = timedelta(minutes=10),
        crashes_to_fail: int = 2,
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
        self.on_withheld = on_withheld
        self.document_screen = document_screen
        self.storage_available = storage_available
        self.defer_delay = defer_delay
        self.crashes_to_fail = max(1, crashes_to_fail)
        self._flush_lock = threading.Lock()

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
                self.store.modify_source_health(
                    ref.source_id, lambda h: replace(h, last_new_release_at=now), c
                )
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
        """Perform at most one transition (or record a failure, deferral or crash).

        An exception raised while ``cancel()`` is true (a download, hash or heavy child stopped by
        shutdown) is not a failure: the step is simply repeated on the next start.
        """
        rec = self._get(source_id, version)
        if self._stop_reason(rec) is not None:
            return rec
        self.checkpoint(f"step:{rec.state.value}:begin")
        if rec.state is ReleaseState.FAILED:  # the retry transition itself: no side effects
            try:
                return self._advance(rec)
            except ConcurrentUpdateError:
                return self._get(source_id, version)
        try:
            rec = self._begin_step(rec)
        except ConcurrentUpdateError:
            return self._get(source_id, version)
        step = _Step(source_id, version, rec.state, rec.step_token)
        try:
            return self._advance(rec)
        except ConcurrentUpdateError as exc:
            log.info(
                "release changed during its step; the step's result is dropped",
                extra={"fields": {"release": rec.subject, "state": step.state.value, "error": str(exc)}},
            )
            return self._get(source_id, version)
        except Exception as exc:
            fields = {"release": rec.subject, "state": step.state.value, "error": repr(exc)}
            if cancel():
                log.info("release step interrupted by shutdown", extra={"fields": fields})
                return self._end_step(step)
            kind = classify(exc, storage_available=self.storage_available)
            log.warning("release step failed", extra={"fields": {**fields, "kind": kind.value}})
            if kind in (FailureKind.BUSY, FailureKind.UNAVAILABLE):
                return self._defer(step, exc, kind)
            if kind is FailureKind.RESOURCE:
                return self._crashed(step, f"{type(exc).__name__}: {exc}")
            return self._fail(step, exc)

    def recover_crashes(self) -> list[ReleaseRecord]:
        """At daemon start: every release whose step token is still set crashed in that step.

        Must only be called while no step can be running (the daemon holds the state DB lock).
        """
        out: list[ReleaseRecord] = []
        for rec in self.store.releases():
            if rec.step_token is None:
                continue
            step = _Step(rec.source_id, rec.version, rec.state, rec.step_token)
            if rec.state in _TERMINAL or rec.step_state is not rec.state:
                self._end_step(step)
                continue
            log.warning(
                "release step did not finish: the daemon crashed or was killed",
                extra={"fields": {"release": rec.subject, "state": rec.state.value}},
            )
            out.append(self._crashed(step, "the daemon stopped during this step (crash or kill)"))
        return out

    def sweep_workdir(self) -> int:
        """Remove local download copies that are no longer needed (sealed or finished releases).

        Kept: files of releases still being fetched or verified, and the evidence of an integrity
        quarantine. Returns how many releases were cleaned.
        """
        n = 0
        for rec in self.store.releases():
            integrity = rec.state is ReleaseState.QUARANTINED and (rec.last_error or "").startswith(
                "integrity:"
            )
            if (rec.state in _SEALED_STATES or rec.state is ReleaseState.QUARANTINED) and not integrity:
                n += self._cleanup_workdir(rec) > 0
        return n

    def approve(self, source_id: str, version: str, *, note: str = "", force: bool = False) -> ReleaseRecord:
        """Owner override: promote a held release despite the sanity or integrity check.

        A release held earlier (an unknown origin domain, held in FETCHED) is released by trusting
        the new hosts (they are pinned); it then continues through every other check. A release whose
        generation is older than the current one is refused without ``force`` (audit H5): promoting
        it would move ``CURRENT`` backwards.
        """
        rec = self._get(source_id, version)
        if rec.state is ReleaseState.QUARANTINED and force:  # e.g. superseded: start again, forced
            rec = self.retry_release(source_id, version, note=f"approve --force: {note}")
            self._persist(
                replace(rec, approved=True, force_older=True),
                event=(LEDGER_KIND_HOLD, {"action": "approved", "note": note, "force": True}),
            )
            return self._get(source_id, version)
        if rec.state in (ReleaseState.REFINED, ReleaseState.INDEXED) and rec.hold_reason is not None:
            newer = self._newer_current(rec)
            if newer is not None and not force:
                raise IllegalTransitionError(
                    f"{rec.subject}: generation {newer} is current and newer; approving would move"
                    " CURRENT backwards (use --force to do it anyway, or reject this release)"
                )
            self._persist(
                replace(rec, hold_reason=None, approved=True, force_older=force),
                event=(
                    LEDGER_KIND_HOLD,
                    {"action": "approved", "note": note, "reason": rec.hold_reason, "force": force},
                ),
            )
        elif rec.hold_reason is not None and rec.state not in _TERMINAL:
            trusted = self.integrity.trust(source_id, version) if self.integrity is not None else []
            self._persist(
                replace(rec, hold_reason=None),
                event=(
                    LEDGER_KIND_HOLD,
                    {"action": "approved", "note": note, "reason": rec.hold_reason, "trusted_hosts": trusted},
                ),
            )
        else:
            raise IllegalTransitionError(
                f"{rec.subject} is {rec.state.value} and not held; nothing to approve"
            )
        if self.alerter is not None:
            self.alerter.resolve(f"held:{rec.subject}")
        return self._get(source_id, version)

    def reject(self, source_id: str, version: str, *, reason: str) -> ReleaseRecord:
        """Owner decision: a held (or otherwise stuck) release is bad. It ends QUARANTINED."""
        rec = self._get(source_id, version)
        if rec.state in _TERMINAL:
            raise IllegalTransitionError(f"{rec.subject} is already {rec.state.value}")
        if rec.state is not ReleaseState.FAILED:
            rec = self._transition(
                rec, ReleaseState.FAILED, attempts=rec.attempts + 1, last_error=f"rejected: {reason}"
            )
        rec = self._transition(rec, ReleaseState.QUARANTINED, extra={"reason": f"rejected: {reason}"})
        self._cleanup_workdir(rec)
        if self.alerter is not None:
            self.alerter.resolve(f"held:{rec.subject}")
        return rec

    def retry_release(self, source_id: str, version: str, *, note: str = "") -> ReleaseRecord:
        """Owner action (``wsp retry``): give a quarantined or backing-off release a fresh start.

        A FAILED release is retried now. A QUARANTINED one (not a terminal judgement on the data any
        more: lock contention used to quarantine good releases, audit H1) returns to FAILED with no
        attempts, crashes or holds, recorded as one ``release.retry`` ledger event. A promoted release
        cannot be retried.
        """
        rec = self._get(source_id, version)
        if rec.state is ReleaseState.PROMOTED:
            raise IllegalTransitionError(f"{rec.subject} is promoted; nothing to retry")
        if rec.state is not ReleaseState.QUARANTINED and rec.state is not ReleaseState.FAILED:
            if rec.next_attempt_at is None:
                raise IllegalTransitionError(f"{rec.subject} is {rec.state.value} and not waiting")
            self._persist(
                replace(rec, next_attempt_at=None),
                event=(LEDGER_KIND_RETRY, {"from": rec.state.value, "note": note}),
            )
            return self._get(source_id, version)
        fresh = replace(
            rec,
            state=ReleaseState.FAILED,
            attempts=0,
            next_attempt_at=None,
            hold_reason=None,
            approved=False,
            force_older=False,
            crash_count=0,
            crash_state=None,
            step_token=None,
            step_state=None,
            step_started_at=None,
        )
        self._persist(fresh, event=(LEDGER_KIND_RETRY, {"from": rec.state.value, "note": note}))
        if self.alerter is not None:
            for key in (f"quarantined:{rec.subject}", f"crashed:{rec.subject}", f"stuck:{rec.subject}"):
                self.alerter.resolve(key)
        return self._get(source_id, version)

    def flush_ledger(self) -> int:
        """Deliver pending outbox events to the ledger exactly once (idempotent appends keyed by
        ``event_id``). Returns how many outbox rows were flushed.

        A ledger that cannot be written (the NAS is away) is logged, never raised: the events stay
        in the outbox and are delivered by the next flush, in order.
        """
        flushed = 0
        with self._flush_lock:
            for e in self.store.outbox():
                try:
                    ev = self.ledger.append(e.kind, e.subject, e.data, idempotency_key=e.event_id)
                except Exception:
                    log.warning(
                        "ledger append failed; the event stays in the outbox",
                        extra={"fields": {"event": e.event_id, "kind": e.kind}},
                        exc_info=True,
                    )
                    break
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
        if retry_at is not None and retry_at > self.clock.now():
            return Outcome.BACKOFF if rec.state is ReleaseState.FAILED else Outcome.DEFERRED
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
                rec = self._withhold(spec, rec, self._verify(spec, rec))
                self._apply_withheld(rec)
                if not self._plan(rec).files:
                    if not self._withheld_of(rec):
                        # Nothing new upstream: every file is already held by an earlier generation
                        # (content sets carry it). Not a fault and not a hold: no attempt, no alert.
                        return self._supersede(rec, "nothing new: earlier generations hold every file")
                    return self._nothing_left(rec)
                if self.integrity is not None and self._sealed_generation(rec) is None:
                    verdict = self.integrity.check_fetch(
                        spec, rec.version, self._plan(rec).files, rec.fetched
                    )
                    self.checkpoint("integrity:checked")
                    if verdict.action is Action.QUARANTINE:
                        return self._quarantine_integrity(rec, verdict.reason)
                    if verdict.action is Action.HOLD:
                        return self._hold(rec, f"integrity: {verdict.reason}")
                return self._transition(rec, ReleaseState.VERIFIED, health=_reset_checksum_failures)
            case ReleaseState.VERIFIED:
                self._apply_withheld(rec)  # idempotent: a crash may have come between commit and action
                gen = self._seal(rec)
                if self.integrity is not None:
                    self.integrity.attest_provenance(rec.source_id, rec.version, rec.generation_id)
                    self.checkpoint("integrity:provenance")
                size = sum(a.size for a in gen.artifacts)
                sealed = self._transition(rec, ReleaseState.SEALED, size_bytes=size)
                self._cleanup_workdir(sealed)  # the vault has the bytes now
                return sealed
            case ReleaseState.SEALED:
                gen = self._require_sealed(rec)
                if not searchable(spec):  # served as stored (ZIM, raw data): nothing to refine
                    return self._transition(rec, ReleaseState.REFINED)
                result = self.refine(spec, gen, self.vault.generation_path(rec.source_id, rec.generation_id))
                self.checkpoint("refine:done")
                return self._transition(rec, ReleaseState.REFINED, doc_count=result.doc_count)
            case ReleaseState.REFINED:
                gen = self._require_sealed(rec)
                if not self._is_current(rec):
                    stopped = self._promotion_gate(spec, rec)
                    if stopped is None:
                        stopped = self._promote(rec)
                    if stopped is not None:
                        return stopped
                    self.checkpoint("vault:promoted")
                if searchable(spec):
                    self.index(spec, gen)
                    self.checkpoint("index:done")
                return self._transition(rec, ReleaseState.INDEXED)
            case ReleaseState.INDEXED:
                if not self._is_current(rec):  # indexed by an older daemon before its gate, or legacy
                    stopped = self._promotion_gate(spec, rec)
                    if stopped is None:
                        stopped = self._promote(rec)
                    if stopped is not None:
                        return stopped
                    self.checkpoint("vault:promoted")
                return self._finish(spec, rec)
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

    def _screen(
        self, spec: SourceSpec, adapter: SourceAdapter, ctx: Ctx, remote: RemoteFile, local: Path
    ) -> Verdict | None:
        """The document-policy verdict, or ``None`` when no screen separate from ``verify`` exists."""
        if isinstance(adapter, DocumentScreen):
            return adapter.screen(ctx, remote, local)
        if self.document_screen is not None:
            return self.document_screen(spec, ctx, remote, local)
        return None

    def _verify(self, spec: SourceSpec, rec: ReleaseRecord) -> list[Withheld]:
        """Screen and check every planned file; returns the files document policy withholds.

        Raises for integrity problems (missing, mismatch). The policy screen always runs (audit H3):
        a fetcher that matched every upstream digest saves only the checksum re-hash, and only when
        a screen separate from ``verify`` has judged the file.
        """
        if self._sealed_generation(rec) is not None:
            return []  # screened, verified and read back by the vault when it was sealed
        adapter, ctx = self.resolve_adapter(spec), self.ctx(spec)
        fetched = self._fetched(rec)
        problems: list[str] = []
        withheld: list[Withheld] = []
        mismatch = False

        def judge(remote: RemoteFile, verdict: Verdict) -> bool:
            """Record a withheld or failed verdict; True if the file is fine."""
            nonlocal mismatch
            if verdict.withheld:
                withheld.append(Withheld(remote.name, remote.url, verdict.status.value, verdict.detail))
                return False
            if not verdict.ok:
                mismatch = mismatch or verdict.status is VerdictStatus.MISMATCH
                problems.append(f"{remote.name}: {verdict.status.value} {verdict.detail}".strip())
                return False
            return True

        for remote in self._plan(rec).files:
            local = self._local_for(rec, remote.name)
            if not local.is_file():
                problems.append(f"{remote.name}: missing locally")
                continue
            screened = self._screen(spec, adapter, ctx, remote, local)
            if screened is not None and not judge(remote, screened):
                continue
            result = fetched.get(remote.name)
            digests_matched = (
                result is not None
                and result.verdict.status is VerdictStatus.OK
                and local.stat().st_size == result.size
            )
            if screened is not None and digests_matched:
                continue  # policy judged above; the fetcher matched every upstream digest: no re-hash
            judge(remote, adapter.verify(ctx, remote, local))
        self.checkpoint("adapter:verified")
        if problems:
            err = ChecksumMismatchError if mismatch else StepError
            raise err("; ".join(problems))
        return withheld

    # --- document policy (ADR-0010) -------------------------------------------------------------

    def quarantine_dir(self, rec: ReleaseRecord) -> Path:
        """``<workdir>/_quarantine/<source>/<generation>/``: per release, so a later release of the
        source never overwrites what an earlier one set aside (audit M3)."""
        return self.workdir / QUARANTINE_DIR / rec.source_id / rec.generation_id

    @staticmethod
    def _withheld_of(rec: ReleaseRecord) -> list[Withheld]:
        raw = rec.plan.get("withheld") if rec.plan is not None else None
        return [Withheld.from_json(d) for d in cast(list[Mapping[str, Any]], raw or [])]

    def _withhold(self, spec: SourceSpec, rec: ReleaseRecord, withheld: Sequence[Withheld]) -> ReleaseRecord:
        """Commit the plan without the withheld files (before touching them), then report them."""
        if not withheld:
            return rec
        names = {w.name for w in withheld}
        items = [
            replace(w, quarantined=str(self.quarantine_dir(rec) / w.name))
            if w.status == VerdictStatus.HELD.value
            else w
            for w in withheld
        ]
        plan = self._plan(rec)
        narrowed = dict(
            plan_to_json(replace(plan, files=tuple(f for f in plan.files if f.name not in names)))
        )
        narrowed["withheld"] = [w.to_json() for w in [*self._withheld_of(rec), *items]]
        rejected = [w for w in items if w.status == VerdictStatus.REJECTED.value]
        held = [w for w in items if w.status == VerdictStatus.HELD.value]
        self._persist(
            replace(rec, plan=narrowed),
            event=(
                LEDGER_KIND_WITHHELD,
                {"rejected": [w.to_json() for w in rejected], "held": [w.to_json() for w in held]},
            ),
        )
        rec = self._get(rec.source_id, rec.version)
        self._apply_withheld(rec)
        if self.alerter is not None:
            self.alerter.raise_(
                Alert(
                    key=f"withheld:{rec.subject}",
                    severity=Severity.WARNING,
                    message=(
                        f"{rec.subject}: document policy withheld {len(rejected)} rejected file(s) "
                        f"(deleted) and {len(held)} held file(s) (quarantined for review); the release "
                        "goes on without them"
                    ),
                    data={
                        "source_id": rec.source_id,
                        "version": rec.version,
                        "rejected": [f"{w.name}: {w.detail}" for w in rejected][:50],
                        "held": [f"{w.name}: {w.detail}" for w in held][:50],
                    },
                )
            )
        if self.on_withheld is not None:
            try:
                self.on_withheld(spec, rec.version, items)
            except Exception:  # reporting must never fail the release; the ledger has the list
                log.exception("on_withheld hook failed", extra={"fields": {"release": rec.subject}})
        return rec

    def _apply_withheld(self, rec: ReleaseRecord) -> None:
        """Delete rejected files and move held ones out of the work directory (idempotent)."""
        for w in self._withheld_of(rec):
            try:
                local = self._local_for(rec, w.name)
            except StepError:
                continue
            if not local.is_file():
                continue
            if w.status == VerdictStatus.HELD.value and w.quarantined:
                target = Path(w.quarantined)
                target.parent.mkdir(parents=True, exist_ok=True)
                local.replace(target)
            else:
                local.unlink(missing_ok=True)  # restricted bytes are not kept anywhere

    def _nothing_left(self, rec: ReleaseRecord) -> ReleaseRecord:
        """Every file was withheld: there is no generation to make. Not a fault of the source."""
        reason = "every fetched file was withheld by document policy"
        failed = self._transition(
            rec,
            ReleaseState.FAILED,
            attempts=rec.attempts + 1,
            last_error=reason,
            next_attempt_at=None,
            extra={"error": reason, "failed_in": rec.state.value},
        )
        return self._transition(failed, ReleaseState.QUARANTINED, extra={"reason": reason})

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

    # --- promotion (gate -> vault -> index -> bookkeeping) -------------------------------------

    def _current_ids(self, source_id: str) -> list[str]:
        return [g.generation_id for g in self.vault.current_set(source_id)]

    def _is_current(self, rec: ReleaseRecord) -> bool:
        return rec.generation_id in self._current_ids(rec.source_id)

    def _newer_current(self, rec: ReleaseRecord) -> str | None:
        """A current generation (of the same partition) newer than the release's, if any."""
        spec = self.sources.get(rec.source_id)
        current = self._current_ids(rec.source_id)
        if spec is not None and spec.partitioned:
            current = [g for g in current if partition_of(g) == partition_of(rec.generation_id)]
        return newest(g for g in current if is_newer(g, rec.generation_id))

    def _promote(self, rec: ReleaseRecord) -> ReleaseRecord | None:
        """Make the generation current; ``None`` on success, else the superseded record.

        The gate's "is a newer generation current?" is checked again *inside* the vault's pointer lock
        (:class:`~wsp_core.ports.SupportsGuardedPromote`): a CLI ingest of the same source may have
        promoted a newer generation since the gate ran (audit 2026-10-12, M3). An owner-forced older
        release (``force_older``) is promoted as asked.
        """
        if rec.force_older or not isinstance(self.vault, SupportsGuardedPromote):
            self.vault.promote(rec.source_id, rec.generation_id)
            return None
        if self.vault.promote_if_newer(rec.source_id, rec.generation_id):
            return None
        newer = self._newer_current(rec) or "a newer generation"
        return self._supersede(rec, f"generation {newer} is current and newer")

    def _promotion_gate(self, spec: SourceSpec, rec: ReleaseRecord) -> ReleaseRecord | None:
        """``None`` if the generation may become current; else the held or superseded record."""
        newer = self._newer_current(rec)
        if newer is not None and not rec.force_older:
            return self._supersede(rec, f"generation {newer} is current and newer")
        history = self.store.generation_stats(spec.id, limit=max(self.sanity.trend_window, 1))
        history = [h for h in history if h.generation_id != rec.generation_id]
        if spec.partitioned:  # ADR-0010: compare a partition with itself, never with another partition
            same = set(same_partition([h.generation_id for h in history], rec.generation_id))
            history = [h for h in history if h.generation_id in same]
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
            if spec.partitioned:
                promoted = same_partition(promoted, rec.generation_id)
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
        return None

    def _finish(self, spec: SourceSpec, rec: ReleaseRecord) -> ReleaseRecord:
        """INDEXED → PROMOTED: the generation is current and searchable; record it."""
        if self.integrity is not None:
            self.integrity.commit_pins(rec.source_id, rec.version)
        now = self.clock.now()
        rec = self._transition(
            rec,
            ReleaseState.PROMOTED,
            health=lambda h: replace(h, last_cycle_ok=now, checksum_failures=0),
            stats=GenerationStats(rec.source_id, rec.generation_id, rec.doc_count, rec.size_bytes, now),
        )
        self._cleanup_workdir(rec)
        self._supersede_older_holds(spec, rec)
        return rec

    def _supersede(self, rec: ReleaseRecord, why: str) -> ReleaseRecord:
        """End a release that a newer current generation made obsolete (no attempt, no alert)."""
        reason = f"superseded: {why}"
        if rec.state is not ReleaseState.FAILED:
            rec = self._transition(
                rec,
                ReleaseState.FAILED,
                last_error=reason,
                next_attempt_at=None,
                extra={"error": reason, "failed_in": rec.state.value, "superseded": True},
            )
        rec = self._transition(rec, ReleaseState.QUARANTINED, extra={"reason": reason})
        self._cleanup_workdir(rec)
        if self.alerter is not None:
            self.alerter.resolve(f"held:{rec.subject}")
        log.info("release superseded", extra={"fields": {"release": rec.subject, "reason": why}})
        return rec

    def _supersede_older_holds(self, spec: SourceSpec, promoted: ReleaseRecord) -> None:
        for other in self.store.releases(source_id=spec.id):
            if other.version == promoted.version or other.state in _TERMINAL or other.hold_reason is None:
                continue
            if spec.partitioned and partition_of(other.generation_id) != partition_of(promoted.generation_id):
                continue
            if not is_newer(promoted.generation_id, other.generation_id):
                continue
            try:
                self._supersede(other, f"{promoted.version} was promoted")
            except (ConcurrentUpdateError, IllegalTransitionError):
                log.warning(
                    "could not supersede a held release", extra={"fields": {"release": other.subject}}
                )

    def _hold(self, rec: ReleaseRecord, reason: str) -> ReleaseRecord:
        self._persist(
            replace(rec, hold_reason=reason, step_token=None, step_state=None, step_started_at=None),
            event=(LEDGER_KIND_HOLD, {"action": "held", "reason": reason}),
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
        """A possible compromise: never promote, pause the source, alert. The downloaded files are
        moved to ``<quarantine_dir>/evidence/`` (see the runbook "Investigating a held release")."""
        evidence = self.quarantine_dir(rec) / "evidence"
        kept: list[str] = []
        for p in rec.local_files:
            path = Path(p)
            target = evidence / path.name
            if path.is_file():
                target.parent.mkdir(parents=True, exist_ok=True)
                path.replace(target)
            kept.append(str(target))
        failed = self._transition(
            rec,
            ReleaseState.FAILED,
            attempts=rec.attempts + 1,
            last_error=f"integrity: {reason}",
            next_attempt_at=None,
            local_files=tuple(kept),
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

    def _cleanup_workdir(self, rec: ReleaseRecord) -> int:
        """Delete the release's local download copies under ``<workdir>/<source>/``. Returns how many."""
        root = (self.workdir / rec.source_id).resolve()
        n = 0
        for p in rec.local_files:
            path = Path(p).resolve()
            if root in path.parents and path.is_file():
                path.unlink(missing_ok=True)
                n += 1
        return n

    # --- failures, deferrals, crashes -----------------------------------------------------------

    def _begin_step(self, rec: ReleaseRecord) -> ReleaseRecord:
        """Commit the step token before any side effect of the step (crash accounting, C1)."""
        token = uuid.uuid4().hex
        self._persist(
            replace(rec, step_token=token, step_state=rec.state, step_started_at=self.clock.now()),
            event=None,
        )
        self.checkpoint(f"step:{rec.state.value}:token")
        return self._get(rec.source_id, rec.version)

    def _owned(self, step: _Step) -> ReleaseRecord | None:
        """The current row if it is still in the step's state under its token; else ``None``.

        Failures are recorded on this fresh row (never on the record the step started from), so
        whatever was committed meanwhile - a narrowed plan, an owner's reject - is kept (audit H4).
        """
        fresh = self._get(step.source_id, step.version)
        if fresh.state is step.state and fresh.step_token == step.token:
            return fresh
        return None

    def _end_step(self, step: _Step) -> ReleaseRecord:
        fresh = self._owned(step)
        if fresh is not None:
            with contextlib.suppress(ConcurrentUpdateError):
                self._persist(
                    replace(fresh, step_token=None, step_state=None, step_started_at=None), event=None
                )
        return self._get(step.source_id, step.version)

    def _defer(self, step: _Step, exc: Exception, kind: FailureKind) -> ReleaseRecord:
        """Busy lock or unreachable storage: same state, no attempt, try again after ``defer_delay``."""
        fresh = self._owned(step)
        if fresh is None:
            return self._get(step.source_id, step.version)
        until = self.clock.now() + self.defer_delay
        self._persist(
            replace(
                fresh,
                step_token=None,
                step_state=None,
                step_started_at=None,
                next_attempt_at=until,
                last_error=f"deferred ({kind.value}): {type(exc).__name__}: {exc}",
            ),
            event=None,
        )
        return self._get(fresh.source_id, fresh.version)

    def _crashed(self, step: _Step, reason: str) -> ReleaseRecord:
        """The step crashed (the daemon died, or its child was OOM-killed). Never an attempt.

        The first crash in a state retries at once; from the ``crashes_to_fail``-th on the release
        goes to FAILED with growing backoff and a ``crashed:<release>`` alert.
        """
        fresh = self._owned(step)
        if fresh is None:
            return self._get(step.source_id, step.version)
        state = step.state
        count = fresh.crash_count + 1 if fresh.crash_state is state else 1
        base = replace(
            fresh,
            step_token=None,
            step_state=None,
            step_started_at=None,
            crash_count=count,
            crash_state=state,
        )
        data: dict[str, object] = {"state": state.value, "crashes": count, "reason": reason[:500]}
        if count < self.crashes_to_fail:
            self._persist(
                replace(base, last_error=f"crashed in {state.value}: {reason}", next_attempt_at=None),
                event=(LEDGER_KIND_CRASH, data),
            )
            return self._get(fresh.source_id, fresh.version)
        delay = self.retry.delay(count - self.crashes_to_fail + 1)
        until = self.clock.now() + delay
        message = f"crashed in {state.value} {count} times, probably OOM (out of memory): {reason}"
        failed = self._transition(
            base,
            ReleaseState.FAILED,
            last_error=message,
            next_attempt_at=until,
            extra={"error": message[:500], "failed_in": state.value, "crash": True, "crashes": count},
        )
        if self.alerter is not None:
            self.alerter.raise_(
                Alert(
                    key=f"crashed:{fresh.subject}",
                    severity=Severity.CRITICAL,
                    message=(
                        f"{fresh.subject} crashed in {state.value} {count} times, probably OOM; "
                        f"backing off until {until:%Y-%m-%d %H:%M} UTC ({reason})"
                    ),
                    data={"source_id": fresh.source_id, "version": fresh.version, **data},
                )
            )
        return failed

    def _fail(self, step: _Step, exc: Exception) -> ReleaseRecord:
        fresh = self._owned(step)
        if fresh is None:  # someone else moved the release meanwhile: their state wins
            return self._get(step.source_id, step.version)
        state = step.state
        attempts = fresh.attempts + 1
        now = self.clock.now()
        checksum = isinstance(exc, ChecksumMismatchError)
        quarantine = attempts >= self.retry.max_attempts
        failed = self._transition(
            fresh,
            ReleaseState.FAILED,
            attempts=attempts,
            last_error=f"{type(exc).__name__}: {exc}",
            next_attempt_at=None if quarantine else now + self.retry.delay(attempts),
            health=_bump_checksum_failures if checksum else None,
            extra={"error": f"{type(exc).__name__}: {exc}", "failed_in": state.value},
        )
        if checksum:
            failures = self.store.source_health(fresh.source_id).checksum_failures
            if failures >= self.checksum_failures_to_pause:
                self._pause_source(
                    fresh.source_id, f"checksum-failures: {failures} consecutive checksum failures"
                )
        if not quarantine:
            return failed
        quarantined = self._transition(failed, ReleaseState.QUARANTINED, extra={"reason": "max attempts"})
        self._cleanup_workdir(quarantined)
        if self.alerter is not None:
            self.alerter.raise_(
                Alert(
                    key=f"quarantined:{fresh.subject}",
                    severity=Severity.CRITICAL,
                    message=f"{fresh.subject} quarantined after {attempts} failed attempts: {exc}",
                    data={"source_id": fresh.source_id, "version": fresh.version, "attempts": attempts},
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
        health: HealthChange | None = None,
        stats: GenerationStats | None = None,
        extra: Mapping[str, object] | None = None,
        **changes: Any,
    ) -> ReleaseRecord:
        if to not in allowed_transitions(rec.state):
            raise IllegalTransitionError(f"{rec.subject}: {rec.state.value} -> {to.value} is not allowed")
        changes.setdefault("next_attempt_at", None)  # a transition ends any deferral
        survived_crash = rec.crash_state is rec.state and to is not ReleaseState.FAILED
        if survived_crash:
            changes.update(crash_count=0, crash_state=None)
        new = replace(rec, state=to, step_token=None, step_state=None, step_started_at=None, **changes)
        data: dict[str, object] = {"from": rec.state.value, "to": to.value, "attempt": new.attempts}
        if extra:
            data.update(extra)
        self._persist(new, event=(LEDGER_KIND_STATE, data), health=health, stats=stats)
        log.info(
            "release transition",
            extra={"fields": {"release": rec.subject, "from": rec.state.value, "to": to.value}},
        )
        if survived_crash and self.alerter is not None:
            self.alerter.resolve(f"crashed:{rec.subject}")
        return self._get(rec.source_id, rec.version)

    def _persist(
        self,
        rec: ReleaseRecord,
        *,
        event: tuple[str, Mapping[str, object]] | None,
        health: HealthChange | None = None,
        stats: GenerationStats | None = None,
    ) -> None:
        """Write ``rec`` (guarded by its ``row_version``) with its ledger event, in one transaction.

        The ledger flush after the commit never raises: a delivery failure leaves the event in the
        outbox (audit H4b: a NAS hiccup used to turn a committed REFINED into FAILED).
        """
        now: datetime = self.clock.now()
        event_id = uuid.uuid4().hex
        with self.store.tx() as c:
            self.store.update_release(rec, now, c)
            if event is not None:
                kind, data = event
                self.store.add_outbox(c, event_id, kind, rec.subject, {"event_id": event_id, **data})
            if health is not None:
                self.store.modify_source_health(rec.source_id, health, c)
            if stats is not None:
                self.store.add_generation_stats(stats, c)
        if event is None:
            return
        self.checkpoint(f"commit:{event[1].get('to') or event[1].get('action') or event[0]}")
        self.flush_ledger()


def _reset_checksum_failures(h: SourceHealth) -> SourceHealth:
    return replace(h, checksum_failures=0) if h.checksum_failures else h


def _bump_checksum_failures(h: SourceHealth) -> SourceHealth:
    return replace(h, checksum_failures=h.checksum_failures + 1)


def pause_source(
    store: StateStore,
    write_event: Callable[[str, str, Mapping[str, object]], None],
    source_id: str,
    reason: str,
    *,
    alerter: Alerter | None = None,
) -> bool:
    """Auto-pause a source (PLAN §5.4): one alert, the last good generation stays current.

    ``reason`` is ``"<code>: <detail>"``; a reason code already recorded is not added again. The
    read and the write share one transaction, so a concurrent pause or failure is never lost.
    Returns True if the source was newly paused (or gained a new reason code).
    """
    code = reason.split(":", 1)[0]
    added = False

    def change(h: SourceHealth) -> SourceHealth:
        nonlocal added
        if any(r.split(":", 1)[0] == code for r in h.pause_reasons):
            return h
        added = True
        return replace(h, paused=True, pause_reasons=(*h.pause_reasons, reason))

    store.modify_source_health(source_id, change)
    if not added:
        return False
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
    previous: tuple[str, ...] = ()

    def change(h: SourceHealth) -> SourceHealth:
        nonlocal previous
        previous = h.pause_reasons
        return replace(h, paused=False, pause_reasons=(), checksum_failures=0, observed_format=None)

    store.modify_source_health(source_id, change)
    write_event("source.resumed", source_id, {"previous_reasons": list(previous)})
    if alerter is not None:
        alerter.resolve(f"paused:{source_id}")
