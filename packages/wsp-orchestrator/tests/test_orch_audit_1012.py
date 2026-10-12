"""Regressions of the vault, orchestrator and lanes audit (docs/audits/2026-10-12/)."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from wsp_core.ports import SupportsGuardedPromote
from wsp_orchestrator.errors import ResourceBusyError
from wsp_orchestrator.isolation import IsolatedRunner, KnowsUnits, SystemdRunLauncher, unit_name
from wsp_orchestrator.pipeline import Outcome
from wsp_orchestrator.testing import FakeLauncher, PipelineHarness

FILES = {"a.zim": b"alpha" * 100}


# --- M3: the "newer is current" check and the promotion are one step ----------------------------------


def test_a_newer_generation_promoted_after_the_gate_supersedes_the_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A CLI ingest promotes a newer generation of the same source between the pipeline's gate and its
    promotion: the release must not move CURRENT backwards."""
    h = PipelineHarness(tmp_path)
    assert isinstance(h.vault, SupportsGuardedPromote)
    real = h.vault.promote_if_newer

    def ingest_wins_the_race(source_id: str, generation_id: str) -> bool:
        h.vault.add_sealed("src", "2026-11-01", current=True)  # `wsp ingest`, meanwhile
        return real(source_id, generation_id)

    monkeypatch.setattr(h.vault, "promote_if_newer", ingest_wins_the_race)
    h.pipeline.register(h.adapter.add_release("2026-10-01", FILES))
    r = h.pipeline.run("src", "2026-10-01")
    assert r.outcome is Outcome.QUARANTINED
    assert "superseded" in (r.record.last_error or "")
    assert h.vault.current("src").generation_id == "2026-11-01"  # type: ignore[union-attr]


def test_releases_are_ordered_naturally_by_the_guarded_promotion(tmp_path: Path) -> None:
    h = PipelineHarness(tmp_path)
    h.vault.add_sealed("src", "2026-10-12-r4", current=True)
    h.pipeline.register(h.adapter.add_release("2026-10-12-r10", FILES))
    assert h.pipeline.run("src", "2026-10-12-r10").outcome is Outcome.PROMOTED
    assert h.vault.current("src").generation_id == "2026-10-12-r10"  # type: ignore[union-attr]


# --- L5: a restarted daemon never clears the box of a step unit that still runs -----------------------


@dataclass
class _UnitsLauncher(FakeLauncher):
    running_units: set[str] = field(default_factory=set[str])

    def unit_running(self, unit: str) -> bool:
        return unit in self.running_units


def test_a_box_of_a_step_still_running_from_an_earlier_daemon_is_left_alone(tmp_path: Path) -> None:
    launcher = _UnitsLauncher()
    assert isinstance(launcher, KnowsUnits)
    runner = IsolatedRunner(tmp_path / "work", launcher=launcher, cwd=tmp_path)
    unit = unit_name("refine", "s/1")
    box = tmp_path / "work" / "_steps" / unit
    box.mkdir(parents=True)
    (box / "request.json").write_text("{}")  # the orphan child's request: it is still working on it
    launcher.running_units.add(unit)
    with pytest.raises(ResourceBusyError, match="still running"):
        runner.call("refine", "wsp_orchestrator.testing:child_echo", {"value": 1}, key="s/1")
    assert (box / "request.json").is_file()
    assert launcher.started == []
    launcher.running_units.clear()  # it ended: the leftover box is cleared and the step runs
    assert runner.call("refine", "wsp_orchestrator.testing:child_echo", {"value": 1}, key="s/1") == 1
    assert not box.exists()


@pytest.mark.parametrize(
    ("returncode", "state", "running"),
    [
        (0, "active\n", True),
        (0, "deactivating\n", True),
        (0, "inactive\n", False),
        (0, "failed\n", False),
        (1, "", True),  # systemctl could not answer: assume running (deferring is the safe side)
    ],
)
def test_systemd_launcher_reads_the_unit_state(
    monkeypatch: pytest.MonkeyPatch, returncode: int, state: str, running: bool
) -> None:
    def fake_run(argv: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
        assert argv[1:] == ["--user", "show", "-p", "ActiveState", "--value", "whisipedia-step-x"]
        return subprocess.CompletedProcess(argv, returncode, state, "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    launcher = SystemdRunLauncher(systemd_run="/bin/true", systemctl="/bin/systemctl")
    assert launcher.unit_running("whisipedia-step-x") is running
