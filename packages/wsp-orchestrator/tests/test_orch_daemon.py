import io
import json
import logging
import os
import signal
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from wsp_orchestrator.daemon import load_wiring, main, run
from wsp_orchestrator.durations import IsoDuration
from wsp_orchestrator.jobs import JobKind
from wsp_orchestrator.jsonlog import configure_logging
from wsp_orchestrator.metrics import Metrics
from wsp_orchestrator.settings import OrchestratorSettings, SettingsError


@pytest.fixture(autouse=True)
def _restore_wsp_logger() -> Iterator[None]:
    logger = logging.getLogger("wsp")
    handlers, level, propagate = list(logger.handlers), logger.level, logger.propagate
    yield
    logger.handlers[:] = handlers
    logger.setLevel(level)
    logger.propagate = propagate


def config_for(tmp_path: Path, **orch: Any) -> dict[str, Any]:
    return {
        "acquirer": {"cycle": "quarterly", "user_agent": "Whisipedia-test/0", "health_probe_interval": "P2W"},
        "vault": {"root": str(tmp_path / "vault"), "scrub_cycle_days": 30},
        "orchestrator": {
            "state_db": str(tmp_path / "state" / "o.sqlite3"),
            "workdir": str(tmp_path / "work"),
            "metrics_path": str(tmp_path / "metrics" / "o.prom"),
            "poll_interval_seconds": 0.05,
            "scrub_slices": 1,
            "wiring": "wsp_orchestrator.testing:demo_wiring",
            **orch,
        },
    }


def test_settings_from_config(tmp_path: Path) -> None:
    cfg = config_for(
        tmp_path,
        job_limits={"fetch_release": 3},
        retry={"max_attempts": 7, "base_delay": "PT1M", "max_delay": "PT1H"},
        retention_windows={"osm-replication": "P10Y"},
    )
    s = OrchestratorSettings.from_config(cfg)
    assert s.cycle == "quarterly"
    assert s.health_probe_interval == IsoDuration.parse("P2W")
    assert s.job_limits[JobKind.FETCH_RELEASE] == 3
    assert s.job_limits[JobKind.SCRUB_SLICE] == 1
    assert s.retry.max_attempts == 7
    assert s.retry.max_delay.total_seconds() == 3600
    assert set(s.retention_windows) == {"wikimedia-dumps", "osm-replication"}
    assert s.vault_root == tmp_path / "vault"
    with pytest.raises(SettingsError, match="state_db"):
        OrchestratorSettings.from_config({"orchestrator": {}})
    with pytest.raises(ValueError, match="cycle"):
        OrchestratorSettings.from_config(config_for(tmp_path) | {"acquirer": {"cycle": "weekly"}})


def test_repository_default_config_needs_only_local_paths(tmp_path: Path) -> None:
    from wsp_core.config import load_config

    root = Path(__file__).resolve().parents[3]
    cfg = load_config(root / "config", environ={})
    cfg.setdefault("orchestrator", {}).update(
        {"state_db": str(tmp_path / "s.sqlite3"), "workdir": str(tmp_path / "w")}
    )
    s = OrchestratorSettings.from_config(cfg)
    assert s.cycle == "monthly"
    assert s.scrub_cycle_days == 30


def test_load_wiring_errors() -> None:
    with pytest.raises(SettingsError):
        load_wiring(None, {})
    with pytest.raises(SettingsError, match="expected Wiring"):
        load_wiring("builtins:dict", {})


def test_run_until_stopped_promotes_and_writes_metrics(tmp_path: Path) -> None:
    stop = threading.Event()
    timer = threading.Timer(1.5, stop.set)
    timer.start()
    try:
        assert run(config_for(tmp_path), stop=stop, install_signals=False, check_local=False) == 0
    finally:
        timer.cancel()
    prom = (tmp_path / "metrics" / "o.prom").read_text()
    assert 'wsp_orchestrator_releases{state="promoted"} 1' in prom
    assert (tmp_path / "vault" / "demo" / "2026-10-01" / "upstream" / "demo.txt").is_file()


def test_main_stops_gracefully_on_sigterm(tmp_path: Path) -> None:
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    cfg = config_for(tmp_path)
    lines = ['[acquirer]\ncycle = "monthly"\n', "[orchestrator]\n"]
    lines += [f"{k} = {json.dumps(v)}\n" for k, v in cfg["orchestrator"].items()]
    (cfg_dir / "default.toml").write_text("".join(lines))
    before = signal.getsignal(signal.SIGTERM)
    killer = threading.Timer(1.0, lambda: os.kill(os.getpid(), signal.SIGTERM))
    killer.start()
    try:
        assert main(["--config-dir", str(cfg_dir)]) == 0
    finally:
        killer.cancel()
    assert signal.getsignal(signal.SIGTERM) == before, "signal handlers restored"


def test_json_logging() -> None:
    stream = io.StringIO()
    configure_logging("DEBUG", stream)
    configure_logging("DEBUG", stream)  # idempotent: one handler
    logging.getLogger("wsp.orchestrator.test").info("hello", extra={"fields": {"source": "x", "n": 2}})
    [line] = stream.getvalue().splitlines()
    rec = json.loads(line)
    assert rec["msg"] == "hello"
    assert rec["source"] == "x"
    assert rec["n"] == 2
    assert rec["level"] == "info"
    assert rec["logger"] == "wsp.orchestrator.test"


def test_metrics_render_and_atomic_write(tmp_path: Path) -> None:
    m = Metrics()
    m.inc("jobs_total", "Jobs", {"kind": "drill", "outcome": "done"})
    m.inc("jobs_total", "Jobs", {"kind": "drill", "outcome": "done"}, by=2)
    m.set("alerts_open", "Open alerts", 1.5)
    m.set("weird", "Escaping", 1, {"label": 'a"b\\c'})
    text = m.render()
    assert "# TYPE wsp_orchestrator_jobs_total counter" in text
    assert 'wsp_orchestrator_jobs_total{kind="drill",outcome="done"} 3' in text
    assert "wsp_orchestrator_alerts_open 1.5" in text
    assert r'label="a\"b\\c"' in text
    with pytest.raises(ValueError, match="already declared"):
        m.set("jobs_total", "Jobs", 1)
    m.write(tmp_path / "m" / "o.prom")
    assert (tmp_path / "m" / "o.prom").read_text() == text
    assert list((tmp_path / "m").iterdir()) == [tmp_path / "m" / "o.prom"]
