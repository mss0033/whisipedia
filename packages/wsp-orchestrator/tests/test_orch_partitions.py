"""Partitioned sources and document-policy withholding in the orchestrator (ADR-0010)."""

from collections.abc import Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import cast
from unittest.mock import ANY

from wsp_core.model import Generation, ReleaseState, SourceSpec, Verdict, VerdictStatus
from wsp_orchestrator.clock import ManualClock
from wsp_orchestrator.jobs import JobKind
from wsp_orchestrator.orchestrator import Orchestrator
from wsp_orchestrator.pipeline import Outcome, Withheld
from wsp_orchestrator.retention import GenerationInfo, GfsPolicy, plan_partitioned_retention, plan_retention
from wsp_orchestrator.testing import FakeWorld, PipelineHarness, make_settings, make_spec

T0 = datetime(2026, 10, 15, 6, tzinfo=UTC)
FILES = {"a.pdf": b"%PDF-a" * 50, "b.pdf": b"%PDF-b" * 50, "c.pdf": b"%PDF-c" * 50}
PARTITIONED = {"access": {"adapter": "parts", "partitioned": True}}


def info(gid: str, *, needed: bool = False) -> GenerationInfo:
    y, m, d = (int(x) for x in gid.split(".")[0].split("-"))
    return GenerationInfo(gid, date(y, m, d), needed=needed)


# --- retention planning ------------------------------------------------------------------------------


def test_partitions_are_planned_on_their_own() -> None:
    gens = [
        info("2025-01-01.ak"),
        info("2025-06-01.ak"),
        info("2026-01-01.ak"),
        info("2026-02-01.ca"),
        info("2026-03-01.hi"),
    ]
    policy = GfsPolicy(last=1, monthly=0, yearly=0)
    current = ["2026-01-01.ak", "2026-02-01.ca", "2026-03-01.hi"]
    flat = plan_retention(gens, policy, current="2026-03-01.hi")
    assert "2026-02-01.ca" in flat.prune  # the bug: another partition looks like an older version
    plan = plan_partitioned_retention(gens, policy, current=current)
    assert plan.prune == ("2025-01-01.ak", "2025-06-01.ak")
    assert set(plan.keep) == set(current)
    assert "current" in plan.reasons["2026-02-01.ca"]


def test_a_generation_read_by_a_current_one_is_never_pruned() -> None:
    gens = [info("2025-01-01.ak", needed=True), info("2025-06-01.ak"), info("2026-01-01.ak")]
    plan = plan_partitioned_retention(gens, GfsPolicy(last=1, monthly=0, yearly=0), current=["2026-01-01.ak"])
    assert plan.prune == ("2025-06-01.ak",)
    assert plan.reasons["2025-01-01.ak"] == ("read by a current generation",)
    flat = plan_retention(gens, GfsPolicy(last=1, monthly=0, yearly=0), current="2026-01-01.ak")
    assert "2025-01-01.ak" in flat.keep


# --- orchestrator: prune, schedule ---------------------------------------------------------------------


def build(
    tmp_path: Path, sources: list[SourceSpec], world: FakeWorld | None = None
) -> tuple[Orchestrator, FakeWorld]:
    clock = ManualClock(T0)
    world = world or FakeWorld.build(tmp_path, clock, sources)

    def never_last_copy(source_id: str, g: Generation) -> bool:
        return False

    wiring = world.wiring(last_copy=never_last_copy, pruner=world.vault.prune)
    orch = Orchestrator(make_settings(tmp_path, scrub_slices=1), wiring, clock=clock, check_local=False)
    return orch, world


def partitioned_spec() -> SourceSpec:
    return make_spec(
        "topo",
        adapter="parts",
        raw={**PARTITIONED, "retention": {"gfs": {"last": 1, "monthly": 0, "yearly": 0}}},
    )


def test_prune_keeps_every_partition_and_what_current_ones_read(tmp_path: Path) -> None:
    spec = partitioned_spec()
    assert spec.partitioned
    orch, world = build(tmp_path, [spec])
    for gid in ("2025-01-01.ak", "2025-06-01.ak", "2026-01-01.ak", "2026-02-01.ca", "2026-03-01.hi"):
        world.vault.add_sealed("topo", gid, current=gid.startswith("2026"))
    world.adapters["parts"].dependencies = {"2026-01-01.ak": frozenset({"2025-01-01.ak"})}
    assert [g.generation_id for g in world.vault.current_set("topo")] == [
        "2026-01-01.ak",
        "2026-02-01.ca",
        "2026-03-01.hi",
    ]
    orch.queue.enqueue(JobKind.PRUNE, "topo", now=T0, payload={"source_id": "topo"})
    orch.run_pending()
    assert world.vault.pruned == [("topo", "2025-06-01.ak")]
    (plan,) = world.ledger.of_kind("retention.plan", "topo")
    assert plan.data["prune"] == ["2025-06-01.ak"]


def test_prune_refuses_to_guess_when_dependencies_cannot_be_read(tmp_path: Path) -> None:
    spec = partitioned_spec()
    orch, world = build(tmp_path, [spec])
    world.adapters["parts"].dependencies = None  # depends_on raises
    world.vault.add_sealed("topo", "2025-01-01.ak")
    world.vault.add_sealed("topo", "2026-01-01.ak", current=True)
    orch.queue.enqueue(JobKind.PRUNE, "topo", now=T0, payload={"source_id": "topo"})
    orch.run_pending()
    assert world.vault.pruned == []


def test_partitioned_sources_are_not_discovered_by_the_scheduler(tmp_path: Path) -> None:
    orch, _ = build(tmp_path, [partitioned_spec(), make_spec("plain")])
    entries = {(e.kind, e.key) for e in orch.scheduler.entries()}
    assert (JobKind.PRUNE, "topo") in entries
    assert (JobKind.DISCOVER, "topo") not in entries
    assert (JobKind.HEALTH_PROBE, "topo") not in entries
    assert (JobKind.DISCOVER, "plain") in entries


# --- pipeline: document policy -------------------------------------------------------------------------


def test_withheld_documents_are_dropped_and_the_release_goes_on(tmp_path: Path) -> None:
    calls: list[tuple[str, str, Sequence[Withheld]]] = []

    def hook(spec: SourceSpec, version: str, items: Sequence[Withheld]) -> None:
        calls.append((spec.id, version, items))

    h = PipelineHarness(tmp_path, on_withheld=hook)
    h.adapter.policy = {
        "a.pdf": Verdict(VerdictStatus.REJECTED, "policy deny:marking=distribution-C"),
        "b.pdf": Verdict(VerdictStatus.HELD, "policy review:marking=missing-A"),
    }
    h.pipeline.register(h.adapter.add_release("2026-10-01", FILES))
    result = h.pipeline.run("src", "2026-10-01")

    assert result.outcome is Outcome.PROMOTED
    current = h.vault.current("src")
    assert current is not None
    assert [str(a.relpath) for a in current.artifacts] == ["upstream/c.pdf"]
    work = tmp_path / "work"
    assert not (work / "src" / "a.pdf").exists(), "restricted bytes are deleted"
    held = work / "_quarantine" / "src" / "b.pdf"
    assert held.read_bytes() == FILES["b.pdf"], "held bytes wait outside the vault"
    assert not list((tmp_path / "vault").rglob("a.pdf"))
    assert not list((tmp_path / "vault").rglob("b.pdf"))
    health = h.store.source_health("src")
    assert not health.paused
    assert health.checksum_failures == 0
    assert "withheld:src/2026-10-01" in h.sink.alert_keys()
    (event,) = h.ledger.of_kind("release.withheld", "src/2026-10-01")
    assert event.data["rejected"] == [
        {"name": "a.pdf", "url": ANY, "status": "rejected", "detail": "policy deny:marking=distribution-C"}
    ]
    assert [cast(dict[str, str], w)["name"] for w in cast(list[object], event.data["held"])] == ["b.pdf"]
    ((sid, version, items),) = calls
    assert (sid, version) == ("src", "2026-10-01")
    assert {(w.name, w.status) for w in items} == {("a.pdf", "rejected"), ("b.pdf", "held")}
    assert next(w for w in items if w.name == "b.pdf").quarantined == str(held)


def test_withholding_survives_a_restart_between_commit_and_action(tmp_path: Path) -> None:
    h = PipelineHarness(tmp_path)
    h.adapter.policy = {"a.pdf": Verdict(VerdictStatus.REJECTED, "policy deny:safety=x")}
    h.pipeline.register(h.adapter.add_release("2026-10-01", FILES))
    while (rec := h.pipeline.step("src", "2026-10-01")).state is not ReleaseState.FETCHED:
        pass
    local = tmp_path / "work" / "src" / "a.pdf"
    assert local.is_file()
    rec = h.pipeline._withhold(  # pyright: ignore[reportPrivateUsage]  # commit only, as if we crashed after it
        h.sources[0], rec, [Withheld("a.pdf", "u", "rejected", "policy deny:safety=x")]
    )
    local.write_bytes(b"still here after a crash")  # the action did not happen
    h.restart()
    assert h.pipeline.run("src", "2026-10-01").outcome is Outcome.PROMOTED
    assert not local.exists()
    current = h.vault.current("src")
    assert current is not None
    assert sorted(str(a.relpath) for a in current.artifacts) == ["upstream/b.pdf", "upstream/c.pdf"]


def test_a_release_with_every_file_withheld_is_quarantined_without_pausing(tmp_path: Path) -> None:
    h = PipelineHarness(tmp_path)
    h.adapter.policy = {name: Verdict(VerdictStatus.REJECTED, "policy deny:x") for name in FILES}
    h.pipeline.register(h.adapter.add_release("2026-10-01", FILES))
    result = h.pipeline.run("src", "2026-10-01")
    assert result.outcome is Outcome.QUARANTINED
    assert "withheld" in (result.record.last_error or "")
    assert not h.store.source_health("src").paused
    assert h.vault.current("src") is None


def test_partition_sanity_compares_a_partition_only_with_itself(tmp_path: Path) -> None:
    spec = make_spec("topo", adapter="parts", raw=PARTITIONED)
    h = PipelineHarness(tmp_path, sources=[spec])
    h.refinery.doc_counts = {"2026-10-01.ca": 10_000, "2026-10-02.hi": 40, "2026-10-03.hi": 30}
    for version in ("2026-10-01.ca", "2026-10-02.hi"):
        h.pipeline.register(h.adapter.add_release(version, {"x.pdf": b"%PDF" + version.encode()}))
        assert h.pipeline.run("topo", version).outcome is Outcome.PROMOTED  # a small state is no drop
    h.pipeline.register(h.adapter.add_release("2026-10-03.hi", {"x.pdf": b"%PDF-3"}))
    held = h.pipeline.run("topo", "2026-10-03.hi")
    assert held.outcome is Outcome.HELD  # -25 % against the same partition
    assert [g.generation_id for g in h.vault.current_set("topo")] == ["2026-10-01.ca", "2026-10-02.hi"]
