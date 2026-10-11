"""Orchestrator settings, read from the layered config (``wsp_core.config.load_config``).

Keys (all optional except the paths in production)::

    [acquirer]
    cycle = "monthly"                  # or "quarterly" (PLAN §6.7)
    user_agent = "..."
    health_probe_interval = "P1W"

    [vault]
    root = "..."                       # the state DB is refused if it lives under here
    scrub_cycle_days = 30

    [orchestrator]
    state_db = "/path/to/local/state/orchestrator.sqlite3"   # LOCAL disk (ADR-0002)
    workdir = "/path/to/local/work"
    metrics_path = "/path/to/local/metrics/orchestrator.prom"
    anchor_day = 15                    # day of month the calendar cycle is due
    scrub_slices = 720                 # slices per scrub cycle (hourly at 30 days)
    drill_interval = "P1M"
    max_workers = 4
    max_jobs_per_host = 1
    poll_interval_seconds = 30
    shutdown_grace_seconds = 10       # running work is cancelled first; must stay well under
                                       # systemd's TimeoutStopSec
    job_retry_gap = "PT1H"
    log_level = "INFO"
    prune_dry_run = false              # true: retention records its plan but moves nothing
    offer_max_age = "P14D"             # upstream observations older than this cannot prove redundancy
    parity_interval = "PT6H"           # how often the parity backfill wakes up
    parity_continue = "PT2M"           # pause between bounded parity runs while work remains
    wiring = "package.module:factory"  # builds the Wiring (adapters, vault, ledger...)
    [orchestrator.job_limits]          # per job type concurrency
    fetch_release = 2
    [orchestrator.retry]
    max_attempts = 5
    base_delay = "PT10M"
    max_delay = "P1D"
    [orchestrator.retention_windows]   # upstream retention per adapter (PLAN §6.5)
    wikimedia-dumps = "P60D"
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any, cast

from wsp_orchestrator.durations import IsoDuration, cadence_for_cycle
from wsp_orchestrator.jobs import JobKind
from wsp_orchestrator.pipeline import RetryPolicy

DEFAULT_JOB_LIMITS: Mapping[JobKind, int] = {
    JobKind.DISCOVER: 4,
    JobKind.FETCH_RELEASE: 2,
    JobKind.SCRUB_SLICE: 1,
    JobKind.DRILL: 1,
    JobKind.HEALTH_PROBE: 4,
    JobKind.PRUNE: 1,
    JobKind.DISCOVERY_BOTS: 1,
    JobKind.PARITY: 1,
}
DEFAULT_RETENTION_WINDOWS: Mapping[str, str] = {"wikimedia-dumps": "P60D", "wikidata": "P60D"}
DEFAULT_USER_AGENT = "Whisipedia/0.0 (personal offline archive)"


class SettingsError(ValueError):
    pass


def _section(cfg: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    value = cfg.get(name, {})
    if not isinstance(value, Mapping):
        raise SettingsError(f"[{name}] must be a table")
    return cast(Mapping[str, Any], value)


def _path(value: object) -> Path | None:
    return Path(str(value)).expanduser() if value not in (None, "") else None


@dataclass(frozen=True, slots=True)
class OrchestratorSettings:
    state_db: Path
    workdir: Path
    metrics_path: Path | None = None
    vault_root: Path | None = None
    cycle: str = "monthly"
    anchor_day: int = 15
    user_agent: str = DEFAULT_USER_AGENT
    health_probe_interval: IsoDuration = field(default_factory=lambda: IsoDuration.parse("P1W"))
    scrub_cycle_days: int = 30
    scrub_slices: int = 720
    drill_interval: IsoDuration = field(default_factory=lambda: IsoDuration.parse("P1M"))
    max_workers: int = 4
    job_limits: Mapping[JobKind, int] = field(default_factory=lambda: dict(DEFAULT_JOB_LIMITS))
    max_jobs_per_host: int = 1
    poll_interval_seconds: float = 30.0
    shutdown_grace_seconds: float = 10.0
    job_retry_gap: timedelta = timedelta(hours=1)
    lease: timedelta = timedelta(hours=24)
    retry: RetryPolicy = field(default_factory=RetryPolicy)
    retention_windows: Mapping[str, IsoDuration] = field(
        default_factory=lambda: {k: IsoDuration.parse(v) for k, v in DEFAULT_RETENTION_WINDOWS.items()}
    )
    log_level: str = "INFO"
    wiring: str | None = None
    prune_dry_run: bool = False  # True: retention only records its plan (never moves anything)
    offer_max_age: timedelta = timedelta(days=14)  # older upstream observations prove nothing
    parity_interval: IsoDuration = field(default_factory=lambda: IsoDuration.parse("PT6H"))
    parity_continue: timedelta = timedelta(minutes=2)  # pause between bounded parity runs

    def __post_init__(self) -> None:
        cadence_for_cycle(self.cycle, anchor_day=self.anchor_day)  # validates both
        if self.scrub_slices < 1 or self.scrub_cycle_days < 1:
            raise SettingsError("scrub_slices and scrub_cycle_days must be positive")
        if self.max_workers < 1 or self.max_jobs_per_host < 1:
            raise SettingsError("max_workers and max_jobs_per_host must be positive")

    @property
    def scrub_slice_interval(self) -> timedelta:
        return timedelta(days=self.scrub_cycle_days) / self.scrub_slices

    @classmethod
    def from_config(cls, cfg: Mapping[str, Any]) -> OrchestratorSettings:
        acq, vault, orch = _section(cfg, "acquirer"), _section(cfg, "vault"), _section(cfg, "orchestrator")
        state_db, workdir = _path(orch.get("state_db")), _path(orch.get("workdir"))
        if state_db is None or workdir is None:
            raise SettingsError("orchestrator.state_db and orchestrator.workdir must be set (local disk)")
        limits = dict(DEFAULT_JOB_LIMITS)
        for kind, n in _section(orch, "job_limits").items():
            limits[JobKind(kind)] = int(n)
        retry_cfg = _section(orch, "retry")
        default_retry = RetryPolicy()
        retry = RetryPolicy(
            max_attempts=int(retry_cfg.get("max_attempts", default_retry.max_attempts)),
            base_delay=IsoDuration.parse(str(retry_cfg["base_delay"])).approx()
            if "base_delay" in retry_cfg
            else default_retry.base_delay,
            factor=float(retry_cfg.get("factor", default_retry.factor)),
            max_delay=IsoDuration.parse(str(retry_cfg["max_delay"])).approx()
            if "max_delay" in retry_cfg
            else default_retry.max_delay,
        )
        windows = dict(DEFAULT_RETENTION_WINDOWS)
        windows.update({str(k): str(v) for k, v in _section(orch, "retention_windows").items()})
        return cls(
            state_db=state_db,
            workdir=workdir,
            metrics_path=_path(orch.get("metrics_path")),
            vault_root=_path(vault.get("root")),
            cycle=str(acq.get("cycle", "monthly")),
            anchor_day=int(orch.get("anchor_day", 15)),
            user_agent=str(acq.get("user_agent", DEFAULT_USER_AGENT)),
            health_probe_interval=IsoDuration.parse(str(acq.get("health_probe_interval", "P1W"))),
            scrub_cycle_days=int(vault.get("scrub_cycle_days", 30)),
            scrub_slices=int(orch.get("scrub_slices", 720)),
            drill_interval=IsoDuration.parse(str(orch.get("drill_interval", "P1M"))),
            max_workers=int(orch.get("max_workers", 4)),
            job_limits=limits,
            max_jobs_per_host=int(orch.get("max_jobs_per_host", 1)),
            poll_interval_seconds=float(orch.get("poll_interval_seconds", 30)),
            shutdown_grace_seconds=float(orch.get("shutdown_grace_seconds", 10)),
            job_retry_gap=IsoDuration.parse(str(orch.get("job_retry_gap", "PT1H"))).approx(),
            retry=retry,
            retention_windows={k: IsoDuration.parse(v) for k, v in windows.items()},
            log_level=str(orch.get("log_level", "INFO")),
            wiring=str(orch["wiring"]) if orch.get("wiring") else None,
            prune_dry_run=bool(orch.get("prune_dry_run", False)),
            offer_max_age=IsoDuration.parse(str(orch.get("offer_max_age", "P14D"))).approx(),
            parity_interval=IsoDuration.parse(str(orch.get("parity_interval", "PT6H"))),
            parity_continue=IsoDuration.parse(str(orch.get("parity_continue", "PT2M"))).approx(),
        )
