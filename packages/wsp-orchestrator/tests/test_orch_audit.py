"""Regression tests for the 2026-10-11 orchestrator audit (docs/audits/2026-10-11/orchestrator.md).

Each test started as the auditor's repro of a finding and now asserts the fixed behaviour.
"""

from __future__ import annotations

import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest

from wsp_core.model import (
    FetchPlan,
    FetchResult,
    Generation,
    ReleaseState,
    SourceSpec,
    Verdict,
    VerdictStatus,
)
from wsp_core.ports import Ctx
from wsp_orchestrator.errors import ResourceBusyError, ResourceExhaustedError
from wsp_orchestrator.isolation import ChildExit, IsolatedRunner
from wsp_orchestrator.pipeline import (
    IllegalTransitionError,
    Outcome,
    RefineResult,
    RetryPolicy,
    pause_source,
)
from wsp_orchestrator.store import ConcurrentUpdateError, StateStore
from wsp_orchestrator.testing import (
    FakeLauncher,
    PipelineHarness,
    ScreeningAdapter,
    SimulatedCrash,
    fetch_result,
)

S = ReleaseState
FILES = {"a.zim": b"alpha" * 100}


def _verified_fetch(h: PipelineHarness) -> None:
    """Fetch like ``HttpFetcher`` when the issuer lists sha256: every upstream digest matched."""
    ad = h.adapter

    def fetch(ctx: Ctx, plan: FetchPlan) -> list[FetchResult]:
        out: list[FetchResult] = []
        for remote in plan.files:
            dest = ctx.workdir / remote.name
            dest.write_bytes(ad.contents[(plan.release.version, remote.name)])
            out.append(fetch_result(dest, verified=remote.checksums))
        return out

    ad.fetch = fetch  # type: ignore[method-assign,assignment]


# --- H3: the document-policy screen always runs ------------------------------------------------------


def test_document_policy_runs_when_the_fetcher_verified_digests(tmp_path: Path) -> None:
    h = PipelineHarness(tmp_path)
    h.adapter.policy["restricted.pdf"] = Verdict(VerdictStatus.REJECTED, "policy deny: export-controlled")
    _verified_fetch(h)
    h.pipeline.register(h.adapter.add_release("2026-10-01", {"ok.txt": b"fine", "restricted.pdf": b"secret"}))
    result = h.pipeline.run("src", "2026-10-01")
    assert result.outcome is Outcome.PROMOTED
    current = h.vault.current("src")
    assert current is not None
    assert sorted(str(a.relpath) for a in current.artifacts) == ["upstream/ok.txt"]
    assert h.adapter.calls["verify"] == 2, "no screen of its own: verify (and its policy) runs every time"
    assert not list((tmp_path / "vault").rglob("restricted.pdf"))
    assert not list((tmp_path / "work").rglob("restricted.pdf")), "rejected bytes are deleted"


def test_a_separate_screen_runs_and_saves_only_the_rehash(tmp_path: Path) -> None:
    h = PipelineHarness(tmp_path, adapter_type=ScreeningAdapter)
    h.adapter.policy["restricted.pdf"] = Verdict(VerdictStatus.REJECTED, "policy deny")
    h.adapter.policy["review.pdf"] = Verdict(VerdictStatus.HELD, "policy review")
    _verified_fetch(h)
    files = {"ok.txt": b"fine", "restricted.pdf": b"secret", "review.pdf": b"maybe"}
    h.pipeline.register(h.adapter.add_release("2026-10-01", files))
    assert h.pipeline.run("src", "2026-10-01").outcome is Outcome.PROMOTED
    assert h.adapter.calls["screen"] == 3
    assert h.adapter.calls["verify"] == 0
    current = h.vault.current("src")
    assert current is not None
    assert [str(a.relpath) for a in current.artifacts] == ["upstream/ok.txt"]


def test_the_wired_screen_hook_is_called_and_none_falls_back_to_verify(tmp_path: Path) -> None:
    seen: list[str] = []

    def screen(spec: object, ctx: Ctx, remote: object, local: Path) -> Verdict | None:
        seen.append(local.name)
        if local.name == "restricted.pdf":
            return Verdict(VerdictStatus.REJECTED, "deny")
        return None  # unknown policy: the adapter's verify must run

    h = PipelineHarness(tmp_path, document_screen=screen)  # type: ignore[arg-type]
    _verified_fetch(h)
    h.pipeline.register(h.adapter.add_release("2026-10-01", {"ok.txt": b"fine", "restricted.pdf": b"x"}))
    assert h.pipeline.run("src", "2026-10-01").outcome is Outcome.PROMOTED
    assert sorted(seen) == ["ok.txt", "restricted.pdf"]
    assert h.adapter.calls["verify"] == 1  # ok.txt only: restricted.pdf was withheld by the screen


# --- C1: crash accounting ---------------------------------------------------------------------------


class _Killer:
    """A refine that kills the daemon (SimulatedCrash) a number of times, then works."""

    def __init__(self, h: PipelineHarness, times: int) -> None:
        self.h = h
        self.times = times
        self.calls = 0

    def __call__(self, source: object, generation: object, generation_dir: Path) -> RefineResult:
        self.calls += 1
        if self.calls <= self.times:
            raise SimulatedCrash("OOM kill")
        return RefineResult(1000)


def _crash_loop(h: PipelineHarness, killer: _Killer) -> None:
    """Run until the daemon "dies", then restart it like systemd would."""
    try:
        h.pipeline.run("src", "2026-10-01")
    except SimulatedCrash:
        h.restart()
        h.pipeline.refine = killer


def test_crashes_are_counted_and_back_off_without_quarantine(tmp_path: Path) -> None:
    h = PipelineHarness(tmp_path, retry=RetryPolicy(max_attempts=3, base_delay=timedelta(minutes=10)))
    killer = _Killer(h, times=1000)
    h.pipeline.refine = killer
    h.pipeline.register(h.adapter.add_release("2026-10-01", FILES))

    _crash_loop(h, killer)  # first crash: retried at once
    rec = h.store.get_release("src", "2026-10-01")
    assert rec is not None
    assert (rec.state, rec.crash_count, rec.crash_state, rec.attempts) == (S.SEALED, 1, S.SEALED, 0)
    assert rec.step_token is None
    assert "crashed:src/2026-10-01" not in h.alerter.open_keys()

    _crash_loop(h, killer)  # second crash in the same state: FAILED with backoff and an alert
    rec = h.store.get_release("src", "2026-10-01")
    assert rec is not None
    assert (rec.state, rec.crash_count, rec.attempts) == (S.FAILED, 2, 0)
    assert "probably OOM" in (rec.last_error or "")
    assert rec.next_attempt_at == h.clock.now() + timedelta(minutes=10)
    assert "crashed:src/2026-10-01" in h.alerter.open_keys()
    alert = next(a for a in h.sink.delivered if a.key == "crashed:src/2026-10-01")
    assert "crashed in sealed 2 times, probably OOM" in alert.message

    for _ in range(10):  # a crash loop never quarantines; backoff grows
        h.clock.advance(timedelta(days=2))
        _crash_loop(h, killer)
    rec = h.store.get_release("src", "2026-10-01")
    assert rec is not None
    assert rec.state is not S.QUARANTINED
    assert rec.attempts == 0
    assert rec.crash_count >= 10
    crash_events = h.ledger.of_kind("release.crash", "src/2026-10-01")
    assert crash_events
    assert crash_events[0].data["state"] == "sealed"

    killer.times = 0  # memory was raised: the step succeeds, the crash record is cleared
    h.clock.advance(timedelta(days=2))
    assert h.pipeline.run("src", "2026-10-01").outcome is Outcome.PROMOTED
    rec = h.store.get_release("src", "2026-10-01")
    assert rec is not None
    assert (rec.crash_count, rec.crash_state) == (0, None)
    assert "crashed:src/2026-10-01" not in h.alerter.open_keys()


def test_crash_between_side_effect_and_transition_resumes(tmp_path: Path) -> None:
    """Fault injection: the refine finished, the process died before REFINED was committed."""
    h = PipelineHarness(tmp_path)

    def crash_after_refine(point: str) -> None:
        if point == "refine:done":
            raise SimulatedCrash(point)

    h.pipeline.checkpoint = crash_after_refine
    h.pipeline.register(h.adapter.add_release("2026-10-01", FILES))
    with pytest.raises(SimulatedCrash):
        h.pipeline.run("src", "2026-10-01")
    rec = h.store.get_release("src", "2026-10-01")
    assert rec is not None
    assert (rec.state, rec.step_state) == (S.SEALED, S.SEALED)
    assert rec.step_token is not None, "the step token was committed before the side effect"
    h.restart()
    rec = h.store.get_release("src", "2026-10-01")
    assert rec is not None
    assert (rec.crash_count, rec.step_token, rec.next_attempt_at) == (1, None, None)
    assert h.pipeline.run("src", "2026-10-01").outcome is Outcome.PROMOTED
    assert h.refinery.refined == [("src", "2026-10-01"), ("src", "2026-10-01")]


def test_an_oom_killed_step_unit_counts_as_a_crash_not_an_attempt(tmp_path: Path) -> None:
    oom = ChildExit("oom-kill", None, "KILL", "6.0G")
    launcher = FakeLauncher(exits=[oom, oom])
    runner = IsolatedRunner(tmp_path / "steps", launcher=launcher, cwd=tmp_path)
    h = PipelineHarness(tmp_path, retry=RetryPolicy(max_attempts=2, base_delay=timedelta(minutes=10)))

    def refine(source: SourceSpec, generation: Generation, generation_dir: Path) -> RefineResult:
        value = runner.call("refine", "wsp_orchestrator.testing:child_echo", {"value": 7}, key="src/x")
        return RefineResult(int(value))

    h.pipeline.refine = refine
    h.pipeline.register(h.adapter.add_release("2026-10-01", FILES))
    first = h.pipeline.run("src", "2026-10-01")
    assert first.outcome is Outcome.BACKOFF  # retried once at once, the second OOM backs off
    assert (first.record.attempts, first.record.crash_count) == (0, 2)
    assert "OOM-killed" in (first.record.last_error or "")
    assert "crashed:src/2026-10-01" in h.alerter.open_keys()
    h.clock.advance(timedelta(hours=1))
    assert h.pipeline.run("src", "2026-10-01").outcome is Outcome.PROMOTED
    assert h.store.get_release("src", "2026-10-01").doc_count == 7  # type: ignore[union-attr]


def test_a_step_interrupted_by_shutdown_is_not_a_crash(tmp_path: Path) -> None:
    h = PipelineHarness(tmp_path)
    stopping = [False]

    def refine(source: SourceSpec, generation: Generation, generation_dir: Path) -> RefineResult:
        stopping[0] = True
        raise RuntimeError("child unit stopped for shutdown")

    h.pipeline.refine = refine
    h.pipeline.register(h.adapter.add_release("2026-10-01", FILES))
    result = h.pipeline.run("src", "2026-10-01", cancel=lambda: stopping[0])
    assert result.outcome is Outcome.CANCELLED
    rec = h.store.get_release("src", "2026-10-01")
    assert rec is not None
    assert (rec.state, rec.step_token, rec.attempts, rec.crash_count) == (S.SEALED, None, 0, 0)
    h.restart()
    rec = h.store.get_release("src", "2026-10-01")
    assert rec is not None
    assert rec.crash_count == 0


# --- H1 / M1: lock contention and the failure taxonomy ----------------------------------------------


def test_lock_contention_defers_and_never_quarantines(tmp_path: Path) -> None:
    h = PipelineHarness(tmp_path, retry=RetryPolicy())  # production defaults: 5 attempts
    busy = [True]
    calls: list[object] = []

    def index(source: SourceSpec, generation: Generation) -> None:  # `wsp index build` holds the lock
        calls.append(h.clock.now())
        if busy[0]:
            raise ValueError("Failed to acquire Lockfile: LockBusy")

    h.pipeline.index = index
    h.pipeline.register(h.adapter.add_release("2026-10-01", FILES))
    for _ in range(200):  # 200 x 5 min of contention
        r = h.pipeline.run("src", "2026-10-01")
        assert r.outcome in (Outcome.DEFERRED,), r.outcome
        h.clock.advance(timedelta(minutes=5))
    rec = h.store.get_release("src", "2026-10-01")
    assert rec is not None
    assert (rec.state, rec.attempts) == (S.REFINED, 0)
    assert "deferred (busy)" in (rec.last_error or "")
    assert len(h.refinery.refined) == 1, "refine is never redone while the index lock is busy"
    assert len(calls) == 100  # every 10 minutes (defer_delay), not every loop
    busy[0] = False
    h.clock.advance(timedelta(minutes=10))
    assert h.pipeline.run("src", "2026-10-01").outcome is Outcome.PROMOTED


def test_a_lost_nas_defers_instead_of_failing(tmp_path: Path) -> None:
    mounted = [False]
    h = PipelineHarness(tmp_path, storage_available=lambda: mounted[0])
    h.adapter.failures["plan"] = [OSError("Host is down")]
    h.pipeline.register(h.adapter.add_release("2026-10-01", FILES))
    r = h.pipeline.run("src", "2026-10-01")
    assert r.outcome is Outcome.DEFERRED
    assert (r.record.state, r.record.attempts) == (S.DISCOVERED, 0)
    mounted[0] = True
    h.clock.advance(timedelta(minutes=10))
    assert h.pipeline.run("src", "2026-10-01").outcome is Outcome.PROMOTED


def test_owner_can_retry_a_quarantined_release(tmp_path: Path) -> None:
    h = PipelineHarness(tmp_path, retry=RetryPolicy(max_attempts=1))
    h.refinery.failures.append(RuntimeError("adapter bug"))
    h.pipeline.register(h.adapter.add_release("2026-10-01", FILES))
    assert h.pipeline.run("src", "2026-10-01").outcome is Outcome.QUARANTINED
    rec = h.pipeline.retry_release("src", "2026-10-01", note="adapter fixed")
    assert (rec.state, rec.attempts) == (S.FAILED, 0)
    assert "quarantined:src/2026-10-01" not in h.alerter.open_keys()
    assert h.ledger.of_kind("release.retry")[-1].data["from"] == "quarantined"
    assert h.pipeline.run("src", "2026-10-01").outcome is Outcome.PROMOTED
    assert h.adapter.calls["fetch"] == 1, "retry resumes: the sealed generation is reused"
    with pytest.raises(IllegalTransitionError):
        h.pipeline.retry_release("src", "2026-10-01")


# --- H2: promotion gate, then vault, then index ------------------------------------------------------


def test_held_generation_is_never_indexed_and_promote_precedes_index(tmp_path: Path) -> None:
    h = PipelineHarness(tmp_path)
    order: list[str] = []
    real_promote, real_index = h.vault.promote, h.refinery.index

    def promote(source_id: str, generation_id: str) -> None:
        order.append(f"promote {generation_id}")
        real_promote(source_id, generation_id)

    def index(source: SourceSpec, generation: Generation) -> None:
        order.append(f"index {generation.generation_id}")
        real_index(source, generation)

    h.vault.promote = promote  # type: ignore[method-assign]
    h.pipeline.index = index
    h.pipeline.register(h.adapter.add_release("2026-09-01", FILES))
    assert h.pipeline.run("src", "2026-09-01").outcome is Outcome.PROMOTED
    assert order == ["promote 2026-09-01", "index 2026-09-01"]
    h.refinery.doc_counts["2026-10-01"] = 10  # 99 % drop: held for review
    h.pipeline.register(h.adapter.add_release("2026-10-01", FILES))
    r = h.pipeline.run("src", "2026-10-01")
    assert r.outcome is Outcome.HELD
    assert r.record.state is S.REFINED
    assert ("src", "2026-10-01") not in h.refinery.indexed
    assert h.vault.current("src").generation_id == "2026-09-01"  # type: ignore[union-attr]
    assert r.record.local_files
    assert not any(Path(p).exists() for p in r.record.local_files), "held: no download copies (M3)"


# --- H4: optimistic concurrency ---------------------------------------------------------------------


def test_failure_after_withhold_keeps_the_narrowed_plan(tmp_path: Path) -> None:
    h = PipelineHarness(tmp_path)
    h.adapter.policy["restricted.pdf"] = Verdict(VerdictStatus.REJECTED, "restricted")
    h.pipeline.register(h.adapter.add_release("2026-10-01", {"ok.txt": b"fine", "restricted.pdf": b"secret"}))

    class Boom:
        def check_fetch(self, *a: object, **k: object) -> object:
            raise RuntimeError("checksum channel crashed")

    h.pipeline.integrity = Boom()  # type: ignore[assignment] - fails after _withhold committed
    h.pipeline.run("src", "2026-10-01")
    rec = h.store.get_release("src", "2026-10-01")
    assert rec is not None
    assert rec.plan is not None
    assert rec.state is S.FAILED
    assert [f["name"] for f in rec.plan["files"]] == ["ok.txt"]
    assert [w["name"] for w in rec.plan["withheld"]] == ["restricted.pdf"]
    h.pipeline.integrity = None
    h.clock.advance(timedelta(days=1))
    h.adapter.policy.clear()  # even if the policy verdict were flaky, the file never comes back
    assert h.pipeline.run("src", "2026-10-01").outcome is Outcome.PROMOTED
    current = h.vault.current("src")
    assert current is not None
    assert [str(a.relpath) for a in current.artifacts] == ["upstream/ok.txt"]


def test_a_ledger_hiccup_after_commit_is_not_a_failure(tmp_path: Path) -> None:
    h = PipelineHarness(tmp_path)
    h.pipeline.register(h.adapter.add_release("2026-10-01", FILES))
    real_append, armed = h.ledger.append, list[int]()

    def checkpoint(point: str) -> None:
        if point == "commit:refined":
            armed.append(1)

    def append(*a: object, **k: object) -> object:
        if len(armed) == 1:
            armed.append(2)
            raise OSError("NAS: Host is down")
        return real_append(*a, **k)  # type: ignore[arg-type]

    h.pipeline.checkpoint = checkpoint
    h.ledger.append = append  # type: ignore[method-assign,assignment]
    assert h.pipeline.run("src", "2026-10-01").outcome is Outcome.PROMOTED
    rec = h.store.get_release("src", "2026-10-01")
    assert rec is not None
    assert (rec.attempts, rec.doc_count) == (0, 1000)
    assert h.store.outbox() == []
    assert [e.data["to"] for e in h.ledger.of_kind("release.state")][-3:] == [
        "refined",
        "indexed",
        "promoted",
    ]


def test_owner_reject_during_a_running_step_wins(tmp_path: Path) -> None:
    h = PipelineHarness(tmp_path)
    h.pipeline.register(h.adapter.add_release("2026-10-01", FILES))
    real = h.refinery.refine

    def refine(source: SourceSpec, generation: Generation, generation_dir: Path) -> RefineResult:
        h.pipeline.reject("src", "2026-10-01", reason="bad dump")  # the owner, from the CLI
        return real(source, generation, generation_dir)

    h.pipeline.refine = refine
    r = h.pipeline.run("src", "2026-10-01")
    assert r.outcome is Outcome.QUARANTINED
    assert h.vault.current("src") is None
    assert "rejected: bad dump" in (r.record.last_error or "")


def test_a_concurrent_pause_is_not_lost(tmp_path: Path) -> None:
    h = PipelineHarness(tmp_path)
    h.adapter.corrupt.add(("2026-10-01", "a.zim"))
    real_verify = h.adapter.verify

    def verify(ctx: Ctx, remote: object, local: Path) -> Verdict:
        # another thread pauses the source while this step is about to record a checksum failure
        pause_source(h.store, h.pipeline.ledger_outbox_writer, "src", "license-changed: CC-BY-NC")
        return real_verify(ctx, remote, local)  # type: ignore[arg-type]

    h.adapter.verify = verify  # type: ignore[method-assign]
    h.pipeline.register(h.adapter.add_release("2026-10-01", FILES))
    for _ in range(4):
        h.pipeline.step("src", "2026-10-01")
    health = h.store.source_health("src")
    assert health.paused
    assert health.pause_reasons == ("license-changed: CC-BY-NC",)
    assert health.checksum_failures == 1


def test_stale_record_updates_are_refused(tmp_path: Path) -> None:
    h = PipelineHarness(tmp_path)
    h.pipeline.register(h.adapter.add_release("2026-10-01", FILES))
    stale = h.store.get_release("src", "2026-10-01")
    assert stale is not None
    h.pipeline.step("src", "2026-10-01")
    with pytest.raises(ConcurrentUpdateError), h.store.tx() as c:
        h.store.update_release(stale, h.clock.now(), c)
    assert h.store.get_release("src", "2026-10-01").state is S.PLANNED  # type: ignore[union-attr]


# --- H5: never backwards -------------------------------------------------------------------------------


def test_approving_a_stale_hold_needs_force_and_newer_promotions_supersede_holds(tmp_path: Path) -> None:
    h = PipelineHarness(tmp_path)
    h.pipeline.register(h.adapter.add_release("2026-08-01", FILES))
    assert h.pipeline.run("src", "2026-08-01").outcome is Outcome.PROMOTED
    h.refinery.doc_counts["2026-09-01"] = 10
    h.pipeline.register(h.adapter.add_release("2026-09-01", FILES))
    assert h.pipeline.run("src", "2026-09-01").outcome is Outcome.HELD
    h.pipeline.register(h.adapter.add_release("2026-10-01", FILES))
    assert h.pipeline.run("src", "2026-10-01").outcome is Outcome.PROMOTED
    old = h.store.get_release("src", "2026-09-01")
    assert old is not None
    assert old.state is S.QUARANTINED
    assert "superseded" in (old.last_error or "")
    assert "held:src/2026-09-01" not in h.alerter.open_keys()
    with pytest.raises(IllegalTransitionError):
        h.pipeline.approve("src", "2026-09-01", note="looked fine")
    assert h.vault.current("src").generation_id == "2026-10-01"  # type: ignore[union-attr]

    h.pipeline.approve("src", "2026-09-01", note="roll back on purpose", force=True)
    assert h.pipeline.run("src", "2026-09-01").outcome is Outcome.PROMOTED
    assert h.vault.current("src").generation_id == "2026-09-01"  # type: ignore[union-attr]


def test_an_older_release_never_moves_current_backwards(tmp_path: Path) -> None:
    h = PipelineHarness(tmp_path)
    h.vault.add_sealed("src", "2026-11-01", current=True)  # e.g. `wsp ingest` promoted it meanwhile
    h.pipeline.register(h.adapter.add_release("2026-10-01", FILES))
    r = h.pipeline.run("src", "2026-10-01")
    assert r.outcome is Outcome.QUARANTINED
    assert "superseded" in (r.record.last_error or "")
    assert h.vault.current("src").generation_id == "2026-11-01"  # type: ignore[union-attr]
    assert "quarantined:src/2026-10-01" not in h.alerter.open_keys()


def test_promoting_a_newer_release_supersedes_an_older_hold(tmp_path: Path) -> None:
    h = PipelineHarness(tmp_path)
    h.refinery.doc_counts["2026-09-01"] = 1000
    h.pipeline.register(h.adapter.add_release("2026-09-01", FILES))
    h.refinery.doc_counts["2026-10-01"] = 1000
    h.pipeline.register(h.adapter.add_release("2026-10-01", FILES))
    for _ in range(6):  # 09 stops in REFINED (held by hand below), 10 gets promoted
        h.pipeline.step("src", "2026-09-01")
    rec = h.store.get_release("src", "2026-09-01")
    assert rec is not None
    assert rec.state is S.REFINED
    h.pipeline._hold(rec, "manual check")  # pyright: ignore[reportPrivateUsage]
    assert h.pipeline.run("src", "2026-10-01").outcome is Outcome.PROMOTED
    assert h.store.get_release("src", "2026-09-01").state is S.QUARANTINED  # type: ignore[union-attr]


# --- M3: a failed COMMIT does not wedge the store -------------------------------------------------


def test_commit_failure_rolls_back(tmp_path: Path) -> None:
    s = StateStore(tmp_path / "s.sqlite3", check_local=False)
    s.query("CREATE TABLE p(id INTEGER PRIMARY KEY)")
    s.query("CREATE TABLE c(pid INTEGER REFERENCES p(id) DEFERRABLE INITIALLY DEFERRED)")
    with pytest.raises(sqlite3.IntegrityError), s.tx() as c:
        c.execute("INSERT INTO c VALUES (42)")  # COMMIT fails on the deferred constraint
    with s.tx() as c:  # the next transaction works
        c.execute("INSERT INTO p VALUES (1)")
    assert s.query("SELECT COUNT(*) AS n FROM c")[0]["n"] == 0
    s.close()


def test_startup_sweep_removes_download_copies_of_finished_releases(tmp_path: Path) -> None:
    h = PipelineHarness(tmp_path)
    h.pipeline.register(h.adapter.add_release("2026-10-01", FILES))
    for _ in range(5):
        h.pipeline.step("src", "2026-10-01")  # ... SEALED
    rec = h.store.get_release("src", "2026-10-01")
    assert rec is not None
    assert rec.state is S.SEALED
    leftover = Path(rec.local_files[0])
    leftover.write_bytes(b"left by an older daemon")
    assert h.pipeline.sweep_workdir() == 1
    assert not leftover.exists()


# --- crash-resource helpers -------------------------------------------------------------------------


def test_classified_errors_from_hooks(tmp_path: Path) -> None:
    h = PipelineHarness(tmp_path)
    h.refinery.failures += [ResourceBusyError("heavy lock"), ResourceExhaustedError("oom")]
    h.pipeline.register(h.adapter.add_release("2026-10-01", FILES))
    first = h.pipeline.run("src", "2026-10-01")
    assert first.outcome is Outcome.DEFERRED
    h.clock.advance(timedelta(minutes=10))
    assert h.pipeline.run("src", "2026-10-01").outcome is Outcome.PROMOTED  # oom once: retried at once
    rec = h.store.get_release("src", "2026-10-01")
    assert rec is not None
    assert (rec.attempts, rec.crash_count) == (0, 0)


def test_a_step_stopped_at_its_time_limit_is_not_called_out_of_memory(tmp_path: Path) -> None:
    # 2026-10-25: a 204 GB ZIM profile timed out three times and was reported "probably OOM".
    h = PipelineHarness(tmp_path, retry=RetryPolicy(max_attempts=3, base_delay=timedelta(minutes=10)))
    timeout = ResourceExhaustedError("refine (whisipedia-step-refine-x) stopped: timeout after 86400s")
    h.refinery.failures += [timeout, timeout]
    h.pipeline.register(h.adapter.add_release("2026-10-01", FILES))
    for _ in range(2):
        h.pipeline.run("src", "2026-10-01")
    rec = h.store.get_release("src", "2026-10-01")
    assert rec is not None
    assert rec.state is S.FAILED
    assert "stopped at its time limit" in (rec.last_error or "")
    assert "OOM" not in (rec.last_error or "")


# --- adapters C1 follow-up: an unchanged release plans no files -----------------------------------------


def test_a_release_with_nothing_new_is_superseded_without_an_attempt_or_alert(tmp_path: Path) -> None:
    h = PipelineHarness(tmp_path)
    ad = h.adapter

    def plan(ctx: Ctx, have: Generation | None, want: object) -> FetchPlan:
        return FetchPlan(release=want, files=(), previous_generation=None)  # type: ignore[arg-type]

    ad.plan = plan  # type: ignore[method-assign,assignment]
    h.pipeline.register(ad.add_release("2026-10-01", FILES))
    result = h.pipeline.run("src", "2026-10-01")
    assert result.record.state is S.QUARANTINED
    assert "superseded" in (result.record.last_error or "")
    assert result.record.attempts == 0
    assert not [k for k in h.alerter.open_keys() if "src/2026-10-01" in k]


def test_a_destination_locked_by_another_download_is_busy_not_a_failure() -> None:
    from wsp_fetch.errors import FetchLockedError
    from wsp_orchestrator.errors import FailureKind, classify

    assert classify(FetchLockedError("another process is fetching x")) is FailureKind.BUSY


def test_a_plan_sees_the_files_carried_from_earlier_generations(tmp_path: Path) -> None:
    """2026-10-26 review: bundles keep only changed files in a new generation and read the rest from
    earlier ones; a plan against the current generation alone downloaded the unchanged files again."""
    from pathlib import PurePosixPath

    from wsp_core.model import Artifact, Generation, GenerationStatus

    def art(gen: str, name: str) -> Artifact:
        return Artifact("src", gen, PurePosixPath("upstream") / name, 1, "0" * 64, "")

    h = PipelineHarness(tmp_path)
    spec = h.sources[0]
    vault = h.vault
    vault._gens[("src", "2026-08")] = Generation(  # pyright: ignore[reportPrivateUsage]
        "src", "2026-08", GenerationStatus.SEALED, (art("2026-08", "a.zim"), art("2026-08", "b.zim"))
    )
    vault._gens[("src", "2026-09")] = Generation(  # pyright: ignore[reportPrivateUsage]
        "src",
        "2026-09",
        GenerationStatus.SEALED,
        (art("2026-09", "b.zim"),),  # b changed; a carried
    )
    vault._gens[("src", "2026-10")] = Generation("src", "2026-10", GenerationStatus.STAGING)  # pyright: ignore[reportPrivateUsage]
    vault._current["src"] = "2026-09"  # pyright: ignore[reportPrivateUsage]
    held = h.pipeline._held(spec)  # pyright: ignore[reportPrivateUsage]
    assert held is not None
    assert held.generation_id == "2026-09"
    assert sorted((a.relpath.name, a.version) for a in held.artifacts) == [
        ("a.zim", "2026-08"),
        ("b.zim", "2026-09"),
    ]
