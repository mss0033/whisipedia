from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from wsp_core.model import Generation, ReleaseState, SourceSpec
from wsp_orchestrator.clock import ManualClock
from wsp_orchestrator.jobs import JobKind, JobStatus
from wsp_orchestrator.orchestrator import Orchestrator
from wsp_orchestrator.policy import Candidate, CandidateKind, Decision, GateConfig, Proposal
from wsp_orchestrator.testing import FakeWorld, SimulatedCrash, make_settings, make_spec, record_promotions

T0 = datetime(2026, 10, 15, 6, tzinfo=UTC)
FILES = {"a.zim": b"a" * 1000}


def build(
    tmp_path: Path,
    sources: list[SourceSpec],
    *,
    clock: ManualClock | None = None,
    world: FakeWorld | None = None,
    wiring_extra: Mapping[str, Any] | None = None,
    checkpoint: Any = None,
    **kw: Any,
) -> tuple[Orchestrator, FakeWorld, ManualClock]:
    clock = clock or ManualClock(T0)
    world = world or FakeWorld.build(tmp_path, clock, sources)
    kw.setdefault("scrub_slices", 1)
    orch = Orchestrator(
        make_settings(tmp_path, **kw),
        world.wiring(**(wiring_extra or {})),
        clock=clock,
        check_local=False,
        checkpoint=checkpoint,
    )
    return orch, world, clock


def test_end_to_end_two_sources_with_dry_run_prune(tmp_path: Path) -> None:
    orch, world, _ = build(tmp_path, [make_spec("a"), make_spec("b")])
    world.adapters["a"].add_release("2026-10-01", FILES)
    world.adapters["b"].add_release("2026-09-30", FILES)
    orch.start()
    orch.run_pending()
    for src, ver in (("a", "2026-10-01"), ("b", "2026-09-30")):
        current = world.vault.current(src)
        assert current is not None
        assert current.generation_id == ver
    plans = world.ledger.of_kind("retention.plan")
    assert {e.subject for e in plans} == {"a", "b"}
    assert all(e.data["dry_run"] is True for e in plans)
    assert world.ledger.verify()
    assert orch.metrics.get("releases", {"state": "promoted"}) == 2
    prom = (tmp_path / "metrics" / "orchestrator.prom").read_text()
    assert 'wsp_orchestrator_releases{state="promoted"} 2' in prom
    assert 'wsp_orchestrator_jobs_total{kind="fetch_release",outcome="done"} 2' in prom


def test_per_host_and_per_kind_concurrency_limits(tmp_path: Path) -> None:
    sources = [make_spec(f"s{i}") for i in range(3)]
    orch, world, clock = build(tmp_path, sources, max_jobs_per_host=1, max_workers=8)
    for s in sources:
        world.adapters[s.id].add_release("2026-10-01", FILES)
        orch.pipeline.register(world.adapters[s.id].releases[0])
    for s in sources:
        orch.queue.enqueue(
            JobKind.FETCH_RELEASE,
            f"{s.id}/2026-10-01",
            now=clock.now(),
            payload={"source_id": s.id, "version": "2026-10-01"},
            host="upstream.example",
        )
    claimed = orch.claim_ready()
    fetches = [j for j in claimed if j.kind is JobKind.FETCH_RELEASE]
    assert len(fetches) == 1, "one fetch per host at a time"
    assert len(orch.queue.jobs(status=JobStatus.PENDING, kind=JobKind.FETCH_RELEASE)) == 2
    for j in claimed:
        orch.execute(j)
    nxt = orch.claim_ready()
    assert [j.key for j in nxt if j.kind is JobKind.FETCH_RELEASE] == ["s1/2026-10-01"]
    assert [j.key for j in nxt if j.kind is JobKind.PRUNE] == ["s0"], "promotion triggers a prune"


def test_job_type_limit(tmp_path: Path) -> None:
    sources = [make_spec(f"s{i}") for i in range(4)]
    orch, _, clock = build(tmp_path, sources, job_limits={JobKind.HEALTH_PROBE: 2})
    for s in sources:
        orch.queue.enqueue(JobKind.HEALTH_PROBE, s.id, now=clock.now(), payload={"source_id": s.id})
    assert len(orch.claim_ready()) == 2


def test_license_change_auto_pauses_and_resume(tmp_path: Path) -> None:
    spec = make_spec("src", license="CC-BY-SA-4.0")
    orch, world, clock = build(tmp_path, [spec])
    adapter = world.adapters["src"]
    adapter.add_release("2026-10-01", FILES, license="CC-BY-SA-4.0", format="zim6")
    orch.start()
    orch.run_pending()
    assert orch.store.source_health("src").observed_format == "zim6"

    adapter.add_release("2026-11-01", FILES, license="CC-BY-NC-4.0", format="zim6")
    clock.set(datetime(2026, 11, 15, tzinfo=UTC))
    orch.run_pending()
    health = orch.store.source_health("src")
    assert health.paused
    assert health.pause_reasons[0].startswith("license-changed")
    assert world.sink.alert_keys().count("paused:src") == 1
    assert orch.store.get_release("src", "2026-11-01") is None, "nothing fetched while paused"
    current = world.vault.current("src")
    assert current is not None
    assert current.generation_id == "2026-10-01"
    clock.advance(timedelta(days=40))
    assert ("discover", "src") not in {(d.entry.kind.value, d.entry.key) for d in orch.scheduler.due()}

    # Owner accepts (registry updated) and resumes.
    spec2 = make_spec("src", license="CC-BY-NC-4.0")
    orch.close()
    world.sources[:] = [spec2]
    orch, _, _ = build(tmp_path, [spec2], clock=clock, world=world)
    orch.resume_source("src")
    orch.run_pending()
    current = world.vault.current("src")
    assert current is not None
    assert current.generation_id == "2026-11-01"
    assert [e.kind for e in world.ledger.events() if e.kind.startswith("source.")] == [
        "source.paused",
        "source.resumed",
    ]


def test_format_change_pauses(tmp_path: Path) -> None:
    orch, world, clock = build(tmp_path, [make_spec("src")])
    adapter = world.adapters["src"]
    adapter.add_release("2026-10-01", FILES, format="zim6")
    orch.start()
    orch.run_pending()
    adapter.add_release("2026-11-01", FILES, format="zim7")
    clock.set(datetime(2026, 11, 15, tzinfo=UTC))
    orch.run_pending()
    assert orch.store.source_health("src").pause_reasons[0].startswith("format-changed")


def test_held_release_and_owner_approval(tmp_path: Path) -> None:
    orch, world, clock = build(tmp_path, [make_spec("src")])
    adapter = world.adapters["src"]
    world.refinery.doc_counts = {"2026-10-01": 1000, "2026-11-01": 500}
    adapter.add_release("2026-10-01", FILES)
    orch.start()
    orch.run_pending()
    adapter.add_release("2026-11-01", FILES)
    clock.set(datetime(2026, 11, 15, tzinfo=UTC))
    orch.run_pending()
    rec = orch.store.get_release("src", "2026-11-01")
    assert rec is not None
    assert rec.state is ReleaseState.REFINED  # held before indexing (audit H2)
    assert ("src", "2026-11-01") not in world.refinery.indexed
    assert rec.hold_reason is not None
    assert "held:src/2026-11-01" in world.sink.alert_keys()

    orch.approve_release("src", "2026-11-01", note="checked by owner")
    orch.run_pending()
    current = world.vault.current("src")
    assert current is not None
    assert current.generation_id == "2026-11-01"


def test_crash_mid_fetch_resumes_after_restart(tmp_path: Path) -> None:
    def crash(point: str) -> None:
        if point == "vault:sealed":
            raise SimulatedCrash(point)

    orch, world, clock = build(tmp_path, [make_spec("src")], checkpoint=crash)
    world.adapters["src"].add_release("2026-10-01", FILES)
    orch.start()
    with pytest.raises(SimulatedCrash):
        orch.run_pending()
    assert orch.queue.jobs(status=JobStatus.RUNNING, kind=JobKind.FETCH_RELEASE)
    orch.close()

    orch, _, _ = build(tmp_path, [make_spec("src")], clock=clock, world=world)
    orch.start()
    orch.run_pending()
    current = world.vault.current("src")
    assert current is not None
    assert current.generation_id == "2026-10-01"
    assert world.vault.calls["seal"] == 1
    assert not orch.queue.jobs(status=JobStatus.RUNNING)


def test_backoff_defers_fetch_job(tmp_path: Path) -> None:
    orch, world, clock = build(tmp_path, [make_spec("src")])
    world.adapters["src"].add_release("2026-10-01", FILES)
    world.adapters["src"].failures["fetch"] = [OSError("reset")]
    orch.start()
    orch.run_pending()
    [job] = orch.queue.jobs(status=JobStatus.PENDING, kind=JobKind.FETCH_RELEASE)
    assert job.run_after == clock.now() + timedelta(minutes=10)
    clock.advance(timedelta(minutes=10))
    orch.run_pending()
    assert world.vault.current("src") is not None


def test_discovery_bots_produce_deduplicated_proposals(tmp_path: Path) -> None:
    proposals: list[Proposal] = []
    candidates = [
        Candidate(
            key="kiwix:wikipedia_de_all_maxi",
            name="Wikipedia DE maxi",
            kind=CandidateKind.KIWIX_TITLE,
            license="CC-BY-SA-4.0",
            domain=("encyclopedia",),
            found_by="kiwix-opds",
            scores=dict.fromkeys(
                ("authority", "density", "uniqueness", "license", "bulk", "health", "longevity", "cost"), 4
            ),
            size_bytes=10,
            category="wikipedia",
        ),
        Candidate(
            key="hf:some-dataset",
            name="Some dataset",
            kind=CandidateKind.NEW_SOURCE,
            license="CC-BY-4.0",
            domain=("data",),
            found_by="hf",
        ),
    ]

    def bots() -> Iterable[Candidate]:
        return candidates

    gate = GateConfig(
        license_allowlist=frozenset({"CC-BY-SA-4.0"}),
        kiwix_categories=frozenset({"wikipedia"}),
        domain_budget_bytes={"encyclopedia": 100},
    )
    orch, world, clock = build(
        tmp_path,
        [],
        wiring_extra={"discovery_bots": bots, "gate": gate, "proposal_sink": proposals.append},
    )
    orch.start()
    orch.run_pending()
    assert [p.decision for p in proposals] == [Decision.AUTO_PROMOTE, Decision.REVIEW]
    assert len(orch.store.proposals()) == 2
    assert len(world.ledger.of_kind("policy.decision")) == 2
    clock.set(datetime(2026, 11, 15, tzinfo=UTC))
    orch.run_pending()
    assert len(proposals) == 2, "open proposals are not re-raised"


def test_drill_failure_alerts(tmp_path: Path) -> None:
    results = [{"ok": False, "detail": "bit flip not detected"}, {"ok": True}]

    def drill() -> Mapping[str, object]:
        return results.pop(0)

    orch, world, clock = build(tmp_path, [], wiring_extra={"drill": drill})
    orch.start()
    orch.run_pending()
    assert "drill-failed" in orch.alerter.open_keys()
    clock.set(datetime(2026, 11, 16, tzinfo=UTC))
    orch.run_pending()
    assert "drill-failed" not in orch.alerter.open_keys()
    assert [e.data["ok"] for e in world.ledger.of_kind("drill.result")] == [False, True]


def test_prune_with_pruner_and_last_copy_oracle(tmp_path: Path) -> None:
    keep_forever = {"2025-01-01"}

    def last_copy(source_id: str, g: Generation) -> bool:
        return g.generation_id in keep_forever

    spec = make_spec("src", raw={"retention": {"gfs": {"last": 2, "monthly": 0, "yearly": 0}}})
    orch, world, _ = build(tmp_path, [spec], wiring_extra={"last_copy": last_copy})
    for gid in ("2025-01-01", "2025-06-01", "2026-01-01", "2026-06-01"):
        world.vault.add_sealed("src", gid)
    world.vault.add_sealed("src", "2026-09-01", current=True)
    orch.close()
    orch, _, _ = build(
        tmp_path,
        [spec],
        world=world,
        wiring_extra={"last_copy": last_copy, "pruner": world.vault.prune},
    )
    record_promotions(orch.store, "src", ["2026-09-01"], T0)
    orch.queue.enqueue(JobKind.PRUNE, "src", now=T0, payload={"source_id": "src"})
    orch.run_pending()
    assert sorted(world.vault.pruned) == [("src", "2025-06-01"), ("src", "2026-01-01")]
    pruned_events = {e.subject for e in world.ledger.of_kind("retention.pruned")}
    assert pruned_events == {"src/2025-06-01", "src/2026-01-01"}


def test_prune_skips_non_gfs_sources(tmp_path: Path) -> None:
    spec = make_spec("src", raw={"retention": {"base": "yearly", "segments": "P2Y"}})
    orch, world, _ = build(tmp_path, [spec])
    orch.queue.enqueue(JobKind.PRUNE, "src", now=T0, payload={"source_id": "src"})
    claimed = orch.claim_ready()
    for j in claimed:
        orch.execute(j)
    assert world.ledger.of_kind("retention.plan") == []


def test_health_probe_flags_missing_checksums(tmp_path: Path) -> None:
    orch, world, _ = build(tmp_path, [make_spec("src")])
    ref = world.adapters["src"].add_release("2026-10-01", FILES)
    no_sums = type(ref)(
        ref.source_id, ref.version, ref.published, tuple(type(f)(f.url, f.name, f.size) for f in ref.files)
    )
    world.adapters["src"].releases[:] = [no_sums]
    orch.queue.enqueue(JobKind.HEALTH_PROBE, "src", now=T0, payload={"source_id": "src"})
    for j in orch.claim_ready():
        orch.execute(j)
    assert "no-checksums:src" in world.sink.alert_keys()


def test_unknown_source_jobs_are_cancelled(tmp_path: Path) -> None:
    orch, _, _ = build(tmp_path, [])
    for kind in (JobKind.DISCOVER, JobKind.HEALTH_PROBE, JobKind.PRUNE):
        orch.queue.enqueue(kind, "ghost", now=T0, payload={"source_id": "ghost"})
    for j in orch.claim_ready():
        orch.execute(j)
    assert len(orch.queue.jobs(status=JobStatus.CANCELLED)) == 3


def test_resuming_a_source_requeues_its_releases_still_in_flight(tmp_path: Path) -> None:
    """2026-10-26 review #57: a release whose fetch job the pause cancelled sat without a job after
    `wsp resume` until the next restart."""
    from wsp_orchestrator.pipeline import pause_source

    spec = make_spec("src")
    orch, world, clock = build(tmp_path, [spec])
    world.adapters["src"].add_release("2026-10-01", FILES)
    orch.start()
    orch.run_pending()
    orch.pipeline.register(world.adapters["src"].add_release("2026-11-01", FILES))
    pause_source(orch.store, orch.pipeline.ledger_outbox_writer, "src", "owner: check the dump")
    for job in orch.queue.jobs():  # the pause cancelled its jobs
        if job.key == "src/2026-11-01":
            orch.queue.cancel(job, clock.now(), "paused")
    assert orch.queue.active(JobKind.FETCH_RELEASE, "src/2026-11-01") is None
    orch.resume_source("src")
    assert orch.queue.active(JobKind.FETCH_RELEASE, "src/2026-11-01") is not None
