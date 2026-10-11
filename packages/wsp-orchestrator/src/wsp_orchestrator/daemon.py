"""Daemon entry point: ``wsp_orchestrator.daemon:run(config)`` (PLAN §6.5).

The main loop ticks the scheduler (catch-up on start), dispatches jobs to a bounded worker pool,
writes Prometheus-text metrics and sleeps until something is due. SIGTERM/SIGINT trigger a prompt
shutdown (well inside systemd's ``TimeoutStopSec``): no new jobs are claimed, downloads, hashing,
scrub and parity are cancelled, pipelines stop between steps, the daemon waits at most
``shutdown_grace_seconds`` (10 s) and exits; anything unfinished resumes on the next start. Job
threads are daemon threads, and ``main`` leaves with ``os._exit`` if a library left a non-daemon
thread behind, so nothing can hold the process open. A second signal exits immediately.
"""

from __future__ import annotations

import argparse
import contextlib
import importlib
import logging
import os
import signal
import sys
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
        name = signal.Signals(signum).name
        if stop.is_set():  # second signal: the owner wants it gone now
            log.warning("second signal, exiting immediately", extra={"fields": {"signal": name}})
            _flush_logs()
            os._exit(128 + signum)
        log.info("shutdown requested", extra={"fields": {"signal": name}})
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


def _flush_logs() -> None:
    for handler in logging.getLogger().handlers + logging.getLogger("wsp").handlers:
        with contextlib.suppress(Exception):  # best effort while exiting
            handler.flush()
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(Exception):
            stream.flush()


def lingering_threads() -> list[threading.Thread]:
    """Non-daemon threads (other than main) that would make the interpreter wait at exit."""
    main_thread = threading.main_thread()
    return [t for t in threading.enumerate() if t is not main_thread and not t.daemon and t.is_alive()]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="wsp-orchestrator", description="Whisipedia orchestrator daemon")
    parser.add_argument("--config-dir", type=Path, default=Path("config"), help="directory with default.toml")
    args = parser.parse_args(argv)
    code = run(load_config(cast(Path, args.config_dir)))
    left = lingering_threads()
    if left:
        # e.g. an executor inside an abandoned job: the interpreter would join it at exit and the
        # service would outlive TimeoutStopSec. All state is durable (SQLite, atomic vault writes).
        log.warning(
            "exiting with non-daemon threads still running",
            extra={"fields": {"threads": [t.name for t in left]}},
        )
        _flush_logs()
        os._exit(code)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
