from datetime import timedelta
from pathlib import Path

import pytest

from wsp_core.model import FetchPlan, FetchResult, ReleaseState, allowed_transitions
from wsp_core.ports import Ctx
from wsp_orchestrator.pipeline import (
    IllegalTransitionError,
    Outcome,
    RetryPolicy,
    StepError,
    UnknownReleaseError,
    resume_source,
)
from wsp_orchestrator.testing import PipelineHarness, ScreeningAdapter, SimulatedCrash, fetch_result

S = ReleaseState
HAPPY = [
    (None, S.DISCOVERED),
    (S.DISCOVERED, S.PLANNED),
    (S.PLANNED, S.FETCHING),
    (S.FETCHING, S.FETCHED),
    (S.FETCHED, S.VERIFIED),
    (S.VERIFIED, S.SEALED),
    (S.SEALED, S.REFINED),
    (S.REFINED, S.INDEXED),
    (S.INDEXED, S.PROMOTED),
]
FILES = {"a.zim": b"alpha" * 100, "b.txt": b"beta"}


def transitions(h: PipelineHarness, subject: str) -> list[tuple[S | None, S]]:
    out: list[tuple[S | None, S]] = []
    for e in h.ledger.of_kind("release.state", subject):
        frm = e.data["from"]
        out.append((S(str(frm)) if frm is not None else None, S(str(e.data["to"]))))
    return out


def test_happy_path_promotes_and_logs_every_transition(tmp_path: Path) -> None:
    h = PipelineHarness(tmp_path)
    ref = h.adapter.add_release("2026-10-01", FILES)
    h.pipeline.register(ref)
    result = h.pipeline.run("src", "2026-10-01")

    assert result.outcome is Outcome.PROMOTED
    assert transitions(h, "src/2026-10-01") == HAPPY
    assert h.ledger.verify()
    current = h.vault.current("src")
    assert current is not None
    assert current.generation_id == "2026-10-01"
    assert sorted(str(a.relpath) for a in current.artifacts) == ["upstream/a.zim", "upstream/b.txt"]
    assert result.record.size_bytes == sum(len(v) for v in FILES.values())
    assert result.record.doc_count == 1000
    assert h.refinery.indexed == [("src", "2026-10-01")]
    assert not list((tmp_path / "work" / "src").iterdir()), "workdir cleaned after promotion"
    assert h.store.outbox() == []
    assert h.store.source_health("src").last_cycle_ok == h.clock.now()
    assert [g.generation_id for g in h.store.generation_stats("src")] == ["2026-10-01"]


def test_every_logged_transition_is_allowed(tmp_path: Path) -> None:
    h = PipelineHarness(tmp_path)
    h.adapter.failures["fetch"] = [OSError("reset")]
    h.pipeline.register(h.adapter.add_release("2026-10-01", FILES))
    h.pipeline.run("src", "2026-10-01")
    h.clock.advance(timedelta(days=1))
    assert h.pipeline.run("src", "2026-10-01").outcome is Outcome.PROMOTED
    for frm, to in transitions(h, "src/2026-10-01")[1:]:
        assert frm is not None
        assert to in allowed_transitions(frm)


def test_register_is_idempotent(tmp_path: Path) -> None:
    h = PipelineHarness(tmp_path)
    ref = h.adapter.add_release("2026-10-01", FILES)
    h.pipeline.register(ref)
    h.pipeline.register(ref)
    assert len(h.store.releases()) == 1
    assert len(h.ledger.of_kind("release.state")) == 1


def test_unknown_release_and_source(tmp_path: Path) -> None:
    h = PipelineHarness(tmp_path)
    with pytest.raises(UnknownReleaseError):
        h.pipeline.run("src", "nope")
    other = h.adapter.add_release("2026-10-01", FILES)
    with pytest.raises(StepError):
        h.pipeline.register(type(other)("ghost", "1", None, ()))


def test_failure_backs_off_then_retries_from_planned(tmp_path: Path) -> None:
    h = PipelineHarness(tmp_path, retry=RetryPolicy(max_attempts=3, base_delay=timedelta(minutes=10)))
    h.adapter.failures["fetch"] = [OSError("connection reset")]
    h.pipeline.register(h.adapter.add_release("2026-10-01", FILES))

    first = h.pipeline.run("src", "2026-10-01")
    assert first.outcome is Outcome.BACKOFF
    assert first.record.state is S.FAILED
    assert first.record.attempts == 1
    assert first.record.next_attempt_at == h.clock.now() + timedelta(minutes=10)
    assert "connection reset" in (first.record.last_error or "")

    h.clock.advance(timedelta(minutes=5))
    assert h.pipeline.run("src", "2026-10-01").outcome is Outcome.BACKOFF
    h.clock.advance(timedelta(minutes=5))
    assert h.pipeline.run("src", "2026-10-01").outcome is Outcome.PROMOTED
    assert (S.FAILED, S.PLANNED) in transitions(h, "src/2026-10-01")


def test_backoff_grows_and_is_capped() -> None:
    p = RetryPolicy(base_delay=timedelta(minutes=10), factor=4, max_delay=timedelta(hours=2))
    assert [p.delay(i) for i in (1, 2, 3, 4)] == [
        timedelta(minutes=10),
        timedelta(minutes=40),
        timedelta(minutes=120),
        timedelta(hours=2),
    ]


def test_quarantine_after_max_attempts(tmp_path: Path) -> None:
    h = PipelineHarness(tmp_path, retry=RetryPolicy(max_attempts=3, base_delay=timedelta(minutes=1)))
    h.adapter.failures["plan"] = [RuntimeError(f"boom {i}") for i in range(5)]
    h.pipeline.register(h.adapter.add_release("2026-10-01", FILES))
    outcomes: list[Outcome] = []
    for _ in range(5):
        outcomes.append(h.pipeline.run("src", "2026-10-01").outcome)
        h.clock.advance(timedelta(days=1))
    assert outcomes[:2] == [Outcome.BACKOFF, Outcome.BACKOFF]
    assert outcomes[2:] == [Outcome.QUARANTINED] * 3
    rec = h.pipeline.get("src", "2026-10-01")
    assert rec is not None
    assert rec.state is S.QUARANTINED
    assert rec.attempts == 3
    assert "quarantined:src/2026-10-01" in h.sink.alert_keys()
    assert transitions(h, "src/2026-10-01")[-2:] == [(S.PLANNED, S.FAILED), (S.FAILED, S.QUARANTINED)]
    assert h.vault.current("src") is None


def test_two_consecutive_checksum_failures_pause_the_source(tmp_path: Path) -> None:
    h = PipelineHarness(tmp_path, retry=RetryPolicy(max_attempts=5, base_delay=timedelta(minutes=1)))
    h.vault.add_sealed("src", "2026-09-01", current=True)
    h.pipeline.register(h.adapter.add_release("2026-10-01", FILES))
    h.adapter.corrupt.add(("2026-10-01", "a.zim"))

    assert h.pipeline.run("src", "2026-10-01").outcome is Outcome.BACKOFF
    assert h.store.source_health("src").checksum_failures == 1
    assert not h.store.source_health("src").paused
    h.clock.advance(timedelta(hours=1))
    assert h.pipeline.run("src", "2026-10-01").outcome is Outcome.PAUSED
    health = h.store.source_health("src")
    assert health.paused
    assert health.checksum_failures == 2
    assert health.pause_reasons[0].startswith("checksum-failures")
    assert h.sink.alert_keys().count("paused:src") == 1
    assert [e.subject for e in h.ledger.of_kind("source.paused")] == ["src"]
    current = h.vault.current("src")
    assert current is not None
    assert current.generation_id == "2026-09-01", "last good generation stays current"

    # Owner fixes upstream and resumes: the release completes and the counter resets.
    h.adapter.corrupt.clear()
    resume_source(h.store, h.pipeline.ledger_outbox_writer, "src", alerter=h.alerter)
    h.clock.advance(timedelta(hours=1))
    assert h.pipeline.run("src", "2026-10-01").outcome is Outcome.PROMOTED
    assert h.store.source_health("src").checksum_failures == 0
    assert "paused:src" not in h.alerter.open_keys()


def test_retry_after_seal_does_not_refetch_or_reseal(tmp_path: Path) -> None:
    h = PipelineHarness(tmp_path)
    h.refinery.failures.append(RuntimeError("refinery down"))
    h.pipeline.register(h.adapter.add_release("2026-10-01", FILES))
    assert h.pipeline.run("src", "2026-10-01").outcome is Outcome.BACKOFF
    h.clock.advance(timedelta(days=1))
    assert h.pipeline.run("src", "2026-10-01").outcome is Outcome.PROMOTED
    assert h.adapter.calls["fetch"] == 1
    assert h.vault.calls["seal"] == 1


def test_doc_count_drop_holds_for_review_then_approve(tmp_path: Path) -> None:
    h = PipelineHarness(tmp_path)
    h.refinery.doc_counts = {"2026-09-01": 1000, "2026-10-01": 979}  # -2.1 %
    h.pipeline.register(h.adapter.add_release("2026-09-01", FILES))
    assert h.pipeline.run("src", "2026-09-01").outcome is Outcome.PROMOTED
    h.pipeline.register(h.adapter.add_release("2026-10-01", FILES))
    held = h.pipeline.run("src", "2026-10-01")
    assert held.outcome is Outcome.HELD
    assert held.record.state is S.REFINED  # the gate runs before the index (audit H2)
    assert ("src", "2026-10-01") not in h.refinery.indexed
    assert "document count dropped" in (held.record.hold_reason or "")
    assert "held:src/2026-10-01" in h.sink.alert_keys()
    current = h.vault.current("src")
    assert current is not None
    assert current.generation_id == "2026-09-01"
    assert h.pipeline.run("src", "2026-10-01").outcome is Outcome.HELD  # stays held

    h.pipeline.approve("src", "2026-10-01", note="upstream purge of spam pages")
    assert h.pipeline.run("src", "2026-10-01").outcome is Outcome.PROMOTED
    assert [e.data["action"] for e in h.ledger.of_kind("release.hold")] == ["held", "approved"]
    assert "held:src/2026-10-01" not in h.alerter.open_keys()


def test_small_doc_count_drop_is_fine(tmp_path: Path) -> None:
    h = PipelineHarness(tmp_path)
    h.refinery.doc_counts = {"2026-09-01": 1000, "2026-10-01": 981}  # -1.9 %
    for v in ("2026-09-01", "2026-10-01"):
        h.pipeline.register(h.adapter.add_release(v, FILES))
        assert h.pipeline.run("src", v).outcome is Outcome.PROMOTED


def test_size_outside_trend_holds_and_reject_quarantines(tmp_path: Path) -> None:
    h = PipelineHarness(tmp_path)
    for v in ("2026-07-01", "2026-08-01", "2026-09-01"):
        h.pipeline.register(h.adapter.add_release(v, {"a.zim": b"x" * 1000}))
        assert h.pipeline.run("src", v).outcome is Outcome.PROMOTED
    h.pipeline.register(h.adapter.add_release("2026-10-01", {"a.zim": b"x" * 1200}))
    held = h.pipeline.run("src", "2026-10-01")
    assert held.outcome is Outcome.HELD
    assert "from trend" in (held.record.hold_reason or "")

    rejected = h.pipeline.reject("src", "2026-10-01", reason="truncated upstream dump")
    assert rejected.state is S.QUARANTINED
    current = h.vault.current("src")
    assert current is not None
    assert current.generation_id == "2026-09-01"


def test_approve_requires_indexed(tmp_path: Path) -> None:
    h = PipelineHarness(tmp_path)
    h.pipeline.register(h.adapter.add_release("2026-10-01", FILES))
    with pytest.raises(IllegalTransitionError):
        h.pipeline.approve("src", "2026-10-01")


def _crash_points(tmp_path: Path, fail_fetch_once: bool) -> int:
    h = PipelineHarness(tmp_path / "probe")
    if fail_fetch_once:
        h.adapter.failures["fetch"] = [OSError("reset")]
    h.pipeline.register(h.adapter.add_release("2026-10-01", FILES))
    h.pipeline.run("src", "2026-10-01")
    h.clock.advance(timedelta(days=1))
    h.pipeline.run("src", "2026-10-01")
    return len(h.injector.points)


@pytest.mark.parametrize("fail_fetch_once", [False, True])
def test_crash_at_every_checkpoint_resumes_exactly_once(tmp_path: Path, fail_fetch_once: bool) -> None:
    total = _crash_points(tmp_path, fail_fetch_once)
    assert total > 20
    for k in range(1, total + 1):
        h = PipelineHarness(tmp_path / f"crash{k}", crash_at=k)
        if fail_fetch_once:
            h.adapter.failures["fetch"] = [OSError("reset")]
        ref = h.adapter.add_release("2026-10-01", FILES)
        crashed_at: str | None = None
        try:
            h.pipeline.register(ref)
            h.pipeline.run("src", "2026-10-01")
            h.clock.advance(timedelta(days=1))
            h.pipeline.run("src", "2026-10-01")
        except SimulatedCrash as crash:
            crashed_at = str(crash)
        assert crashed_at is not None, f"checkpoint {k} never reached"

        h.restart()
        h.pipeline.register(ref)  # discovery runs again after a restart
        result = h.pipeline.run("src", "2026-10-01")
        if result.outcome is Outcome.BACKOFF:
            h.clock.advance(timedelta(days=1))
            result = h.pipeline.run("src", "2026-10-01")

        assert result.outcome is Outcome.PROMOTED, f"crash at {k} ({crashed_at})"
        got = transitions(h, "src/2026-10-01")
        expected = HAPPY
        if fail_fetch_once:
            expected = [
                *HAPPY[:3],
                (S.FETCHING, S.FAILED),
                (S.FAILED, S.PLANNED),
                (S.PLANNED, S.FETCHING),
                *HAPPY[3:],
            ]
        assert got == expected, f"crash at {k} ({crashed_at}): ledger {got}"
        assert h.ledger.verify()
        assert h.vault.calls["seal"] == 1, f"crash at {k} ({crashed_at}) resealed"
        assert h.store.outbox() == []
        h.store.close()


def test_fetch_digests_are_persisted_and_forwarded_to_the_vault(tmp_path: Path) -> None:
    h = PipelineHarness(tmp_path)
    ref = h.adapter.add_release("2026-10-01", FILES)
    h.pipeline.register(ref)
    result = h.pipeline.run("src", "2026-10-01")
    assert result.outcome is Outcome.PROMOTED
    assert set(result.record.fetched) == set(FILES)
    assert result.record.fetched["b.txt"]["size"] == len(FILES["b.txt"])
    current = h.vault.current("src")
    assert current is not None
    for art in current.artifacts:
        assert art.upstream == next(f.checksums for f in ref.files if f.name == art.relpath.name)


def test_verified_fetch_skips_the_adapter_rehash(tmp_path: Path) -> None:
    """With a policy screen separate from ``verify``, a fetcher-verified file is not re-hashed."""
    h = PipelineHarness(tmp_path, adapter_type=ScreeningAdapter)
    ref = h.adapter.add_release("2026-10-01", FILES)
    original = h.adapter.fetch

    def verified_fetch(ctx: Ctx, plan: FetchPlan) -> list[FetchResult]:
        out = list(original(ctx, plan))
        sums = {f.name: f.checksums for f in plan.files}
        return [fetch_result(r.path, sums[r.path.name]) for r in out]

    h.adapter.fetch = verified_fetch  # type: ignore[method-assign]
    h.pipeline.register(ref)
    assert h.pipeline.run("src", "2026-10-01").outcome is Outcome.PROMOTED
    assert h.adapter.calls["verify"] == 0
    assert h.adapter.calls["screen"] == len(FILES)  # the policy screen always runs (audit H3)


def test_ledger_flush_is_idempotent_by_event_id(tmp_path: Path) -> None:
    h = PipelineHarness(tmp_path)
    ref = h.adapter.add_release("2026-10-01", FILES)
    h.pipeline.register(ref)
    first = h.ledger.of_kind("release.state")[0]
    assert first.data["idempotency_key"] == first.data["event_id"]
    again = h.ledger.append(
        first.kind, first.subject, first.data, idempotency_key=str(first.data["event_id"])
    )
    assert again == first
