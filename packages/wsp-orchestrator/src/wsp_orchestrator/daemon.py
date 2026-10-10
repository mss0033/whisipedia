"""Daemon entry point: ``wsp_orchestrator.daemon:run(config)`` (PLAN §6.5).

The main loop ticks the scheduler (catch-up on start), dispatches jobs to a bounded worker pool,
writes Prometheus-text metrics and sleeps until something is due. SIGTERM/SIGINT trigger a graceful
shutdown: no new jobs are claimed, running pipelines stop between steps, and anything unfinished
resumes on the next start.
"""

from __future__ import annotations

import argparse
import importlib
import logging
import signal
import threading
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from types import FrameType
from typing import Any, cast

from wsp_core.config import load_config
from wsp_orchestrator.clock import Clock
from wsp_orchestrator.jsonlog import configure_logging
from wsp_orchestrator.orchestrator import Orchestrator, Wiring
from wsp_orchestrator.settings import OrchestratorSettings, SettingsError

log = logging.getLogger("wsp.orchestrator.daemon")

WiringFactory = Callable[[Mapping[str, Any]], object]  # checked at runtime


def load_wiring(spec: str | None, config: Mapping[str, Any]) -> Wiring:
    """Import ``package.module:factory`` and call it with the config to build the :class:`Wiring`."""
    if not spec or ":" not in spec:
        raise SettingsError("orchestrator.wiring must be 'package.module:factory'")
    module_name, _, attr = spec.partition(":")
    factory = cast(WiringFactory, getattr(importlib.import_module(module_name), attr))
    wiring = factory(config)
    if not isinstance(wiring, Wiring):
        raise SettingsError(f"{spec} returned {type(wiring).__name__}, expected Wiring")
    return wiring


def _install_signals(stop: threading.Event) -> dict[int, Any]:
    def handler(signum: int, _frame: FrameType | None) -> None:
        log.info("shutdown requested", extra={"fields": {"signal": signal.Signals(signum).name}})
        stop.set()

    previous: dict[int, Any] = {}
    for sig in (signal.SIGTERM, signal.SIGINT):
        previous[sig] = signal.signal(sig, handler)
    return previous


def run(
    config: Mapping[str, Any],
    *,
    wiring: Wiring | None = None,
    clock: Clock | None = None,
    stop: threading.Event | None = None,
    install_signals: bool = True,
    check_local: bool = True,
) -> int:
    """Run the orchestrator until ``stop`` is set or SIGTERM/SIGINT arrives. Returns an exit code."""
    settings = OrchestratorSettings.from_config(config)
    configure_logging(settings.log_level)
    wiring = wiring or load_wiring(settings.wiring, config)
    stop = stop or threading.Event()
    previous = _install_signals(stop) if install_signals else {}
    orch = Orchestrator(settings, wiring, clock=clock, check_local=check_local)
    try:
        orch.start()
        while not stop.is_set():
            orch.scheduler.tick()
            orch.dispatch()
            orch.refresh_metrics()
            stop.wait(orch.sleep_seconds())
    finally:
        clean = orch.shutdown()
        orch.close()
        for sig, old in previous.items():
            signal.signal(sig, old)
        log.info("orchestrator stopped", extra={"fields": {"clean": clean}})
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="wsp-orchestrator", description="Whisipedia orchestrator daemon")
    parser.add_argument("--config-dir", type=Path, default=Path("config"), help="directory with default.toml")
    args = parser.parse_args(argv)
    return run(load_config(cast(Path, args.config_dir)))


if __name__ == "__main__":
    raise SystemExit(main())
