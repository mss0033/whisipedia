"""Phase 5 operator surface: proposals, services, doctor, System Monitor data, the wiring."""

from __future__ import annotations

import io
import json
import os
import shutil
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from wsp_cli import bootstrap
from wsp_cli.doctor import Doctor, Level, exit_code, render
from wsp_cli.main import main
from wsp_cli.monitor import LibraryMonitor, download_lanes, source_slo
from wsp_cli.service import MARKER, UNITS, ServiceManager
from wsp_orchestrator.curator import ProposalFiles, ProposalRecord
from wsp_orchestrator.curator.registry_edit import load_overlay, upsert_overlay
from wsp_orchestrator.policy import Candidate, CandidateKind, GateConfig, evaluate_candidate
from wsp_orchestrator.store import StateStore
from wsp_orchestrator.testing import make_spec

ROOT = Path(__file__).resolve().parents[3]
NOW = datetime(2026, 10, 10, 12, tzinfo=UTC)


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(ROOT)
    monkeypatch.delenv("WSP_CONFIG_DIR", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    (tmp_path / "vault" / "library").mkdir(parents=True)
    (tmp_path / "index").mkdir()
    registry = tmp_path / "sources.yaml"
    shutil.copy(ROOT / "registry" / "sources.yaml", registry)
    for key, value in {
        "VAULT__ROOT": tmp_path / "vault",
        "INDEX__ROOT": tmp_path / "index",
        "ORCHESTRATOR__STATE_DB": tmp_path / "state" / "orchestrator.sqlite3",
        "ORCHESTRATOR__WORKDIR": tmp_path / "work",
        "REGISTRY__PATH": registry,
        "CURATOR__OVERLAY": tmp_path / "registry.local.yaml",
    }.items():
        monkeypatch.setenv(f"WSP__{key}", str(value))
    return tmp_path


def run(*argv: str) -> tuple[int, str]:
    out = io.StringIO()
    return main(list(argv), out=out), out.getvalue()


def _record(pid: int, key: str, entry: dict[str, object], status: str = "open") -> ProposalRecord:
    cand = Candidate(
        key=key,
        name=key,
        kind=CandidateKind.KIWIX_TITLE,
        license="CC-BY-SA-4.0",
        domain=("education",),
        found_by="kiwix-opds",
        registry_entry=entry,
    )
    return ProposalRecord(pid, status, NOW, evaluate_candidate(cand, GateConfig(), []))


ENTRY: dict[str, object] = {
    "id": "kiwix-wikivoyage-en-all",
    "name": "Wikivoyage (Kiwix ZIM)",
    "domain": ["geography"],
    "tier": "T2",
    "status": "active",
    "license": "CC-BY-SA-4.0",
    "legal_basis": "open-license",
    "access": {"adapter": "kiwix-opds", "selector": {"name": "wikivoyage_en_all"}},
}


def test_proposals_list_accept_reject(env: Path) -> None:
    files = ProposalFiles(env / "proposals")
    files.write(_record(1, "kiwix:wikivoyage_en_all", ENTRY))
    files.write(_record(2, "gap:domain:nature.com", {}))
    upsert_overlay(env / "registry.local.yaml", ENTRY)

    code, text = run("proposals", "list")
    assert code == 0
    assert "kiwix" in text
    code, text = run("proposals", "accept", "1", "--note", "looks good")
    assert code == 0, text
    assert "git diff" in text
    registry = yaml.safe_load((env / "sources.yaml").read_text())
    assert any(s["id"] == "kiwix-wikivoyage-en-all" for s in registry["sources"])
    assert load_overlay(env / "registry.local.yaml") == []
    assert run("proposals", "accept", "1")[0] == 2
    assert run("proposals", "reject", "2")[0] == 0
    statuses = {d["id"]: d["status"] for d in files.list()}
    assert statuses == {1: "accepted", 2: "rejected"}
    assert run("proposals", "show", "9")[0] == 2


def test_accept_refuses_an_invalid_entry(env: Path) -> None:
    ProposalFiles(env / "proposals").write(_record(3, "bad", {"id": "x", "name": "missing fields"}))
    before = (env / "sources.yaml").read_text()
    assert run("proposals", "accept", "3")[0] == 1
    assert (env / "sources.yaml").read_text() == before


class FakeSystemctl:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, args: Sequence[str]) -> tuple[int, str]:
        self.calls.append(list(args))
        if args and args[0] == "is-active":
            return 3, "inactive"
        if args and args[0] == "is-enabled":
            return 0, "enabled"
        return 0, ""


def test_service_install_status_uninstall(tmp_path: Path) -> None:
    fake = FakeSystemctl()
    mgr = ServiceManager(repo=ROOT, config_dir=ROOT / "config", target=tmp_path / "units", run=fake)
    out = io.StringIO()
    assert mgr.install(out) == 0
    for name in UNITS:
        text = (tmp_path / "units" / name).read_text()
        assert text.startswith(MARKER)
        assert "@REPO@" not in text
        assert "@CONFIG_DIR@" not in text
    assert "%h/.config/whisipedia/whisipedia.env" in (tmp_path / "units" / UNITS[0]).read_text()
    assert ["daemon-reload"] in fake.calls
    assert any(c[0] == "enable" and "--now" not in c for c in fake.calls)
    assert mgr.status(out) == 0
    (tmp_path / "units" / "foreign.service").write_text("[Unit]\n")
    assert mgr.uninstall(out) == 0
    assert sorted(p.name for p in (tmp_path / "units").iterdir()) == ["foreign.service"]


def test_doctor_report(env: Path) -> None:
    lay = bootstrap.layout()
    cfg = bootstrap.load(lay)

    def which(exe: str, args: Sequence[str]) -> str | None:
        return None if exe == "par2" else f"{exe} 1.0"

    checks = Doctor(cfg, lay, run=FakeSystemctl(), which=which).checks()
    by = {c.name: c.level for c in checks}
    assert by["config"] is Level.OK
    assert by["vault"] is Level.OK
    assert by["par2"] is Level.FAIL
    assert by["key"] is Level.FAIL
    assert by["services"] is Level.WARN
    out = io.StringIO()
    render(checks, out)
    assert "S Y S T E M   D O C T O R" in out.getvalue()
    assert exit_code(checks) == 1
    assert exit_code(checks, ["vault", "config"]) == 0
    assert run("doctor", "--quiet", "--require", "vault")[0] == 0


def test_monitor_status_from_local_state(tmp_path: Path) -> None:
    state = tmp_path / "orchestrator.sqlite3"
    store = StateStore(state, check_local=False)
    store.put_offers(
        "wikidata",
        [{"generation_id": "2026-10-01", "version": "2026-10-01", "published": "2026-09-01T00:00:00+00:00"}],
        NOW,
    )
    store.open_alert("drill-failed", "critical", "drill failed", {}, NOW)
    store.set_meta("bot:kiwix-opds", json.dumps({"at": NOW.isoformat(), "ok": True, "candidates": 2}))
    with store.tx() as c:
        c.execute(
            "INSERT INTO jobs(kind, key, payload_json, status, run_after, created_at, updated_at)"
            " VALUES ('parity', 'backfill', '{}', 'pending', 'x', 'x', 'x')"
        )
    store.close()
    lanes = tmp_path / "bootstrap"
    lanes.mkdir()
    (lanes / "kiwix.log").write_text(
        "2026 [NOTICE] Download complete: /x/a.zim\n2026 [NOTICE] Download complete: /x/b.zim\n"
    )
    (lanes / "kiwix.console").write_text("Download Results:\n(OK):download completed.\n")
    old = (NOW - timedelta(hours=5)).timestamp()
    for p in lanes.iterdir():
        os.utime(p, (old, old))
    files = ProposalFiles(tmp_path / "proposals")
    files.write(_record(1, "kiwix:a", ENTRY))
    mon = LibraryMonitor(
        sources=[make_spec("wikidata", adapter="wikimedia-dumps")],
        vault=None,
        state_db=state,
        scrub_db=None,
        proposals=files.list,
        downloads_dir=lanes,
        disks={"state": tmp_path},
        background=False,
    )
    ops = mon.status(NOW)
    assert ops.orchestrator_state
    (slo,) = ops.slo
    assert slo.slo_class == "wikimedia"
    assert slo.lag_days == 39.5  # nothing promoted; upstream has offered a release since Sep 1
    assert slo.within_slo is True  # Wikimedia: one cycle + 14 days
    assert [a.key for a in ops.alerts] == ["drill-failed"]
    assert [(q.kind, q.count) for q in ops.queue] == [("parity", 1)]
    assert ops.bots[0].candidates == 2
    assert ops.proposals.open == 1
    (lane,) = ops.lanes
    assert lane.completed == 2
    assert lane.last_completed == "b.zim"
    assert ops.disk[0].name == "state"
    assert mon.proposals("open")[0].key == "kiwix:a"


def test_slo_lag_and_lanes_tolerate_absence(tmp_path: Path) -> None:
    spec = make_spec("rfc")
    offers = [{"generation_id": "2026-10-01", "published": None}]
    assert source_slo(spec, "2026-10-01", offers, NOW, NOW, 30.5).lag_days == 0
    assert source_slo(spec, None, None, None, NOW, 30.5).within_slo is None
    assert download_lanes(tmp_path / "missing", NOW) == []


def test_build_wiring_wires_bots_parity_and_proposals(env: Path) -> None:
    from wsp_cli.wiring import DEFAULT_BOTS, build_wiring

    cfg = bootstrap.load(bootstrap.layout())
    wiring = build_wiring(cfg)
    assert [b.name for b in wiring.bots] == list(DEFAULT_BOTS)
    assert wiring.parity is not None
    assert wiring.parity(lambda s, g: True)["pending"] == 0
    assert wiring.promoter is not None
    assert wiring.gate_provider is not None
    assert "CC-BY-SA-4.0" in wiring.gate_provider().license_allowlist
