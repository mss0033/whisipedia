"""A dormant upstream (nothing new for 3x the cadence) warns and keeps being watched; it is not paused."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from wsp_orchestrator.clock import ManualClock
from wsp_orchestrator.orchestrator import Orchestrator
from wsp_orchestrator.pipeline import pause_source
from wsp_orchestrator.testing import FakeWorld, make_settings, make_spec

T0 = datetime(2026, 10, 15, 6, tzinfo=UTC)
FILES = {"a.zim": b"a" * 1000}


def build(tmp_path: Path, clock: ManualClock) -> tuple[Orchestrator, FakeWorld]:
    world = FakeWorld.build(tmp_path, clock, [make_spec("weekly", cadence="P1W")])
    orch = Orchestrator(
        make_settings(tmp_path, scrub_slices=1), world.wiring(), clock=clock, check_local=False
    )
    return orch, world


def alerts(orch: Orchestrator) -> dict[str, str]:
    return {str(r["key"]): str(r["severity"]) for r in orch.store.open_alerts()}


def test_a_dormant_upstream_warns_keeps_discovering_and_clears_when_it_revives(tmp_path: Path) -> None:
    clock = ManualClock(T0)
    orch, world = build(tmp_path, clock)
    world.adapters["weekly"].add_release("2026-10-01", FILES)
    orch.start()
    orch.run_pending()
    assert alerts(orch) == {}
    # Five weeks without a release: over 3x the weekly cadence.
    clock.set(T0 + timedelta(weeks=5))
    orch.run_pending()
    health = orch.store.source_health("weekly")
    assert not health.paused
    assert alerts(orch) == {"dormant:weekly": "warning"}
    # Discovery still runs at cadence, so a revived upstream is seen and the warning goes away.
    clock.set(T0 + timedelta(weeks=6))
    world.adapters["weekly"].add_release("2026-11-25", FILES)
    orch.run_pending()
    current = world.vault.current("weekly")
    assert current is not None
    assert current.generation_id == "2026-11-25"
    assert alerts(orch) == {}


def test_stale_pauses_from_before_are_cleared_at_start(tmp_path: Path) -> None:
    clock = ManualClock(T0)
    orch, world = build(tmp_path, clock)
    world.adapters["weekly"].add_release("2026-10-01", FILES)
    pause_source(
        orch.store,
        orch.pipeline.ledger_outbox_writer,
        "weekly",
        "stale: newest upstream release is 158 days old, over 3x cadence (7 days)",
        alerter=orch.alerter,
    )
    assert orch.store.source_health("weekly").paused
    assert "paused:weekly" in alerts(orch)
    orch.start()
    health = orch.store.source_health("weekly")
    assert not health.paused
    assert health.pause_reasons == ()
    assert "paused:weekly" not in alerts(orch)
    # Other reasons are kept: only the stale one is a thing of the past.
    pause_source(orch.store, orch.pipeline.ledger_outbox_writer, "weekly", "license-changed: CC-BY-NC")
    pause_source(orch.store, orch.pipeline.ledger_outbox_writer, "weekly", "stale: again")
    from wsp_orchestrator.pipeline import drop_pause_reason

    assert drop_pause_reason(orch.store, orch.pipeline.ledger_outbox_writer, "weekly", "stale")
    assert orch.store.source_health("weekly").pause_reasons == ("license-changed: CC-BY-NC",)
    assert orch.store.source_health("weekly").paused
    assert not drop_pause_reason(orch.store, orch.pipeline.ledger_outbox_writer, "weekly", "stale")
