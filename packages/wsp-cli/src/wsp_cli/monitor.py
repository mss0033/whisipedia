"""System Monitor data (``/v1/system`` ``ops``, ``/v1/proposals``): the API side of operations.

Everything here only *reads*: the orchestrator's state DB (opened read-only), the scrub state, the
proposal files, the vault's manifests and the bulk-download logs. Any part that is missing (no
orchestrator yet, no downloads, no par2) is reported as empty instead of failing. Slow parts (walking
every generation's manifest on the NAS for parity coverage and domain budgets) are refreshed in a
background thread and served from a cache, so the endpoint stays fast.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import closing, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

from wsp_api.schemas import (
    AlertInfo,
    BotRun,
    DiskUsage,
    DomainBudget,
    DownloadLane,
    HeldRelease,
    IntegrityStatus,
    OpsStatus,
    ParityStatus,
    ProposalInfo,
    ProposalSummary,
    QueueDepth,
    ScrubStatus,
    SourceIntegrity,
    SourceSlo,
)
from wsp_core.model import GenerationStatus, SourceSpec, SourceStatus
from wsp_core.ports import Vault

_PROGRESS = re.compile(r"\[#\w+ [^\]]*\]")
_COMPLETE = "Download complete: "
_ACTIVE_WINDOW = timedelta(minutes=3)


# --- SLOs (PLAN §6.6) -------------------------------------------------------------------------------


def _days(iso: str | None, default: float) -> float:
    if not iso:
        return default
    m = re.fullmatch(r"P(\d+)([YMWD])", iso)
    if not m:
        return default
    return int(m[1]) * {"Y": 365.0, "M": 30.5, "W": 7.0, "D": 1.0}[m[2]]


def slo_for(spec: SourceSpec, cycle_days: float) -> tuple[str, float]:
    """``(class, allowed lag in days)`` per PLAN §6.6."""
    cadence = _days(spec.cadence, cycle_days)
    if spec.adapter == "wikimedia-dumps":
        return "wikimedia", cycle_days + 14
    if spec.adapter == "kiwix-opds":
        return "zim", 45.0
    if "science" in spec.domain:
        return "scholarly", cycle_days + 7
    if "law" in spec.domain:
        return "law", cycle_days + 7
    return "default", cadence + 7


def source_slo(
    spec: SourceSpec,
    current: str | None,
    offers: Sequence[Mapping[str, Any]] | None,
    observed: datetime | None,
    now: datetime,
    cycle_days: float,
    paused: tuple[bool, Sequence[str]] = (False, ()),
) -> SourceSlo:
    klass, target = slo_for(spec, cycle_days)
    lag: float | None = None
    newest: str | None = None
    if offers:
        newest = max(str(o.get("generation_id")) for o in offers)
        newer = sorted(
            (o for o in offers if current is None or str(o.get("generation_id")) > current),
            key=lambda o: str(o.get("generation_id")),
        )
        if not newer:
            lag = 0.0
        else:
            since: datetime | None = None
            with suppress(TypeError, ValueError):
                since = datetime.fromisoformat(str(newer[0].get("published")))
            since = since or observed or now
            if since.tzinfo is None:
                since = since.replace(tzinfo=UTC)
            lag = max(0.0, (now - since).total_seconds() / 86400)
    return SourceSlo(
        source_id=spec.id,
        slo_class=klass,  # pyright: ignore[reportArgumentType]
        target_days=round(target, 1),
        lag_days=None if lag is None else round(lag, 2),
        within_slo=None if lag is None else lag <= target,
        upstream_newest=newest,
        paused=paused[0],
        pause_reasons=list(paused[1]),
    )


# --- download lanes ------------------------------------------------------------------------------


def _tail(path: Path, nbytes: int = 16384) -> str:
    try:
        with path.open("rb") as fh:
            fh.seek(max(0, path.stat().st_size - nbytes))
            return fh.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def _running_sessions() -> set[str]:
    """Session file names of running aria2c processes (from /proc; empty elsewhere)."""
    found: set[str] = set()
    proc = Path("/proc")
    if not proc.is_dir():
        return found
    for pid in proc.iterdir():
        if not pid.name.isdigit():
            continue
        try:
            argv = (pid / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        if not argv or not argv[0].endswith(b"aria2c"):
            continue
        for arg in argv:
            text = arg.decode("utf-8", errors="replace")
            if "save-session" in text or text.endswith(".session"):
                found.add(Path(text.split("=", 1)[-1]).name)
    return found


def download_lanes(directory: Path | None, now: datetime) -> list[DownloadLane]:
    """Lanes of the bootstrap bulk downloads: ``<lane>.log`` / ``.console`` / ``.session`` (aria2)."""
    if directory is None or not directory.is_dir():
        return []
    running = _running_sessions()
    lanes: list[DownloadLane] = []
    for log in sorted(directory.glob("*.log")):
        name = log.stem
        console = log.with_suffix(".console")
        mtimes = [p.stat().st_mtime for p in (log, console) if p.exists()]
        last = datetime.fromtimestamp(max(mtimes), UTC) if mtimes else None
        log_tail = _tail(log, 65536)
        done = [ln.split(_COMPLETE, 1)[1].strip() for ln in log_tail.splitlines() if _COMPLETE in ln]
        con_tail = _tail(console)
        progress = next(iter(reversed(_PROGRESS.findall(con_tail))), None)
        if f"{name}.session" in running or (last is not None and now - last < _ACTIVE_WINDOW):
            state = "active"
        elif "Download Results" in con_tail:
            state = "finished"
        else:
            state = "idle" if last is not None else "unknown"
        try:
            completed = sum(1 for ln in log.open(encoding="utf-8", errors="replace") if _COMPLETE in ln)
        except OSError:
            completed = len(done)
        lanes.append(
            DownloadLane(
                name=name,
                state=state,  # pyright: ignore[reportArgumentType]
                last_activity=last,
                completed=completed,
                last_completed=Path(done[-1]).name if done else None,
                progress=progress if state == "active" else None,
            )
        )
    return lanes


# --- proposals -----------------------------------------------------------------------------------


def _dt(value: object) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


class CachedProposals:
    """The proposal files as dicts, each parsed once per change (audit #4).

    ``/v1/system`` and ``/v1/proposals`` need every proposal. Parsing 75 YAML files with the pure-Python
    loader took ~0.5 s per call; files are re-parsed only when their ``(mtime, size)`` changes, with
    libyaml's ``CSafeLoader`` when PyYAML has it. ``files`` lists the proposal files (local disk).
    """

    def __init__(self, files: Callable[[], Sequence[Path]]) -> None:
        self._files = files
        self._cache: dict[Path, tuple[tuple[int, int], dict[str, Any]]] = {}
        self._lock = threading.Lock()
        self.parsed = 0  # files parsed so far (tests)

    def __call__(self) -> list[dict[str, Any]]:
        import yaml

        loader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
        out: list[dict[str, Any]] = []
        seen: set[Path] = set()
        for path in self._files():
            try:
                st = path.stat()
            except OSError:
                continue
            stamp = (st.st_mtime_ns, st.st_size)
            seen.add(path)
            with self._lock:
                hit = self._cache.get(path)
            if hit is not None and hit[0] == stamp:
                out.append(hit[1])
                continue
            try:
                doc: object = yaml.load(path.read_text(encoding="utf-8"), Loader=loader)  # noqa: S506 - safe loader
            except (OSError, yaml.YAMLError):
                continue
            if not isinstance(doc, dict):
                continue
            parsed = cast(dict[str, Any], doc)
            with self._lock:
                self._cache[path] = (stamp, parsed)
                self.parsed += 1
            out.append(parsed)
        with self._lock:
            for gone in set(self._cache) - seen:
                del self._cache[gone]
        return out


def proposal_info(doc: Mapping[str, Any]) -> ProposalInfo | None:
    try:
        cand = cast(Mapping[str, Any], doc.get("candidate") or {})
        return ProposalInfo(
            id=int(doc["id"]),
            key=str(cand.get("key", "")),
            title=str(doc.get("title", "")),
            status=str(doc.get("status", "open")),  # pyright: ignore[reportArgumentType]
            decision=str(doc.get("decision", "review")),  # pyright: ignore[reportArgumentType]
            score=float(doc["score"]) if doc.get("score") is not None else None,
            kind=str(cand.get("kind", "")),
            found_by=str(cand.get("found_by", "")),
            created_at=_dt(doc.get("created_at")),
            decided_at=_dt(doc.get("decided_at")),
            body=str(doc.get("body", "")),
        )
    except (KeyError, ValueError, TypeError):
        return None


# --- the provider ----------------------------------------------------------------------------------


@dataclass
class _Slow:
    parity: ParityStatus | None = None
    budgets: list[DomainBudget] = field(default_factory=list[DomainBudget])
    provenance: Any = None  # wsp_cli.integrity.Coverage
    at: float = 0.0


class LibraryMonitor:
    """Implements :class:`wsp_api.seams.OpsProvider` from local state files and the vault."""

    def __init__(
        self,
        *,
        sources: Sequence[SourceSpec],
        vault: Vault | None,
        state_db: Path | None,
        scrub_db: Path | None,
        proposals: Callable[[], list[dict[str, Any]]],
        downloads_dir: Path | None,
        disks: Mapping[str, Path | None],
        cycle_days: float = 30.5,
        scrub_cycle_days: int = 30,
        parity_scope: str = "all",
        parity_available: Callable[[], bool] = lambda: shutil.which("par2") is not None,
        coverage: Callable[[Sequence[tuple[str, str]]], Any] | None = None,
        domain_usage: Callable[[], Mapping[str, int]] | None = None,
        budgets: Mapping[str, int] | None = None,
        provenance_coverage: Callable[[], Any] | None = None,
        refresh_seconds: float = 600.0,
        background: bool = True,
    ) -> None:
        self.sources = list(sources)
        self.vault = vault
        self.state_db = state_db
        self.scrub_db = scrub_db
        self._proposals = proposals
        self.downloads_dir = downloads_dir
        self.disks = dict(disks)
        self.cycle_days = cycle_days
        self.scrub_cycle_days = scrub_cycle_days
        self.parity_scope = parity_scope
        self.parity_available = parity_available
        self.coverage = coverage
        self.domain_usage = domain_usage
        self.budgets = dict(budgets or {})
        self.provenance_coverage = provenance_coverage
        self.refresh_seconds = refresh_seconds
        self.background = background
        self._slow = _Slow()
        self._lock = threading.Lock()
        self._refreshing = False

    # --- state DB ---

    def _query(self, sql: str, params: Sequence[object] = ()) -> list[sqlite3.Row]:
        if self.state_db is None or not self.state_db.is_file():
            return []
        try:
            with closing(sqlite3.connect(f"file:{self.state_db}?mode=ro", uri=True, timeout=2)) as db:
                db.row_factory = sqlite3.Row
                return list(db.execute(sql, params).fetchall())
        except sqlite3.Error:
            return []

    def _meta(self, prefix: str) -> dict[str, Any]:
        rows = self._query("SELECT key, value FROM meta WHERE key LIKE ?", (f"{prefix}%",))
        out: dict[str, Any] = {}
        for r in rows:
            with suppress(ValueError):
                out[str(r["key"])[len(prefix) :]] = json.loads(str(r["value"]))
        return out

    # --- slow parts ---

    def _targets(self) -> list[tuple[str, str]]:
        if self.vault is None:
            return []
        by_id = {s.id: s for s in self.sources}
        out: list[tuple[str, str]] = []
        for spec in by_id.values():
            if self.parity_scope != "all" and not spec.irreplaceable:
                continue
            for g in self.vault.generations(spec.id):
                if g.status is GenerationStatus.SEALED:
                    out.append((spec.id, g.generation_id))
        return out

    def refresh_slow(self) -> None:
        try:
            parity: ParityStatus | None = None
            if self.coverage is not None:
                cov = self.coverage(self._targets())
                last = self._meta("parity:").get("last")
                parity = ParityStatus(
                    scope=self.parity_scope,
                    available=self.parity_available(),
                    generations=int(cov.generations),
                    covered=int(cov.covered),
                    bytes_total=int(cov.bytes_total),
                    bytes_covered=int(cov.bytes_covered),
                    fraction=round(float(cov.fraction), 4),
                    states=dict(cov.states),
                    last_run=cast(dict[str, Any], last) if isinstance(last, dict) else None,
                )
            used = dict(self.domain_usage()) if self.domain_usage is not None else {}
            budgets = [
                DomainBudget(domain=d, used_bytes=used.get(d, 0), budget_bytes=self.budgets.get(d))
                for d in sorted(set(used) | set(self.budgets))
            ]
            provenance = self.provenance_coverage() if self.provenance_coverage is not None else None
            with self._lock:
                self._slow = _Slow(parity, budgets, provenance, time.monotonic())
        finally:
            self._refreshing = False

    def _slow_parts(self) -> _Slow:
        with self._lock:
            slow = self._slow
            stale = time.monotonic() - slow.at > self.refresh_seconds or slow.at == 0.0
            start = stale and not self._refreshing
            if start:
                self._refreshing = True
        if start:
            if self.background:
                threading.Thread(target=self.refresh_slow, name="wsp-monitor", daemon=True).start()
            else:
                self.refresh_slow()
                with self._lock:
                    slow = self._slow
        return slow

    # --- OpsProvider ---

    def proposals(self, status: str | None = None) -> list[ProposalInfo]:
        out = [p for p in (proposal_info(d) for d in self._proposals()) if p is not None]
        return [p for p in out if status is None or p.status == status]

    def status(self, now: datetime) -> OpsStatus:
        slow = self._slow_parts()
        offers = {str(r["source_id"]): r for r in self._query("SELECT * FROM upstream_offers")}
        health = {str(r["source_id"]): r for r in self._query("SELECT * FROM sources")}
        slo: list[SourceSlo] = []
        for spec in sorted(self.sources, key=lambda s: s.id):
            if spec.status is not SourceStatus.ACTIVE and spec.id not in health:
                continue
            current = None
            if self.vault is not None:
                with suppress(Exception):
                    cur = self.vault.current(spec.id)
                    current = cur.generation_id if cur else None
            row = offers.get(spec.id)
            seen = cast(list[dict[str, Any]], json.loads(row["offers_json"])) if row is not None else None
            observed = _dt(row["observed_at"].replace("Z", "+00:00")) if row is not None else None
            h = health.get(spec.id)
            reasons: list[str] = json.loads(h["pause_reasons_json"]) if h is not None else []
            paused = (h is not None and bool(h["paused"]), reasons)
            slo.append(source_slo(spec, current, seen, observed, now, self.cycle_days, paused))
        scrub = None
        if self.scrub_db is not None:
            from wsp_vault.scrub import scrub_progress

            prog = scrub_progress(self.scrub_db, now=now, cycle=timedelta(days=self.scrub_cycle_days))
            if prog is not None:
                scrub = ScrubStatus(
                    files_known=prog.files_known,
                    verified_in_cycle=prog.verified_in_cycle,
                    failing=prog.failing,
                    fraction=round(prog.fraction, 4),
                    cycle_days=self.scrub_cycle_days,
                    oldest_check=prog.oldest_check,
                )
        disks: list[DiskUsage] = []
        for name, path in self.disks.items():
            if path is None:
                continue
            with suppress(OSError):
                du = shutil.disk_usage(path)
                disks.append(
                    DiskUsage(
                        name=name,  # pyright: ignore[reportArgumentType]
                        total_bytes=du.total,
                        free_bytes=du.free,
                        used_bytes=du.used,
                    )
                )
        queue = [
            QueueDepth(
                kind=str(r["kind"]),
                status=str(r["status"]),  # pyright: ignore[reportArgumentType]
                count=int(r["n"]),
            )
            for r in self._query(
                "SELECT kind, status, COUNT(*) AS n FROM jobs WHERE status IN ('pending','running')"
                " GROUP BY kind, status ORDER BY kind, status"
            )
        ]
        alerts = [
            AlertInfo(
                key=str(r["key"]),
                severity=str(r["severity"]),
                message=str(r["message"]),
                raised_at=_dt(str(r["raised_at"]).replace("Z", "+00:00")) or now,
            )
            for r in self._query("SELECT * FROM alerts WHERE resolved_at IS NULL ORDER BY raised_at DESC")
        ]
        bots = [_bot_run(name, run) for name, run in sorted(self._meta("bot:").items())]
        props = self.proposals()
        counts = {
            s: sum(1 for p in props if p.status == s) for s in ("open", "applied", "accepted", "rejected")
        }
        latest = sorted((p for p in props if p.status == "open"), key=lambda p: p.id, reverse=True)[:5]
        return OpsStatus(
            slo=slo,
            scrub=scrub,
            parity=slow.parity,
            disk=disks,
            budgets=slow.budgets,
            lanes=download_lanes(self.downloads_dir, now),
            queue=queue,
            alerts=alerts,
            bots=bots,
            proposals=ProposalSummary(**counts, latest=latest),
            orchestrator_state=self.state_db is not None and self.state_db.is_file(),
            integrity=self._integrity(slow, now),
        )

    def _integrity(self, slow: _Slow, now: datetime) -> IntegrityStatus | None:
        """Source integrity (ADR-0009) from the state DB and the cached provenance coverage."""
        from wsp_cli.integrity import channel_summary, read_integrity_state

        if self.state_db is None or not self.state_db.is_file():
            return None
        state = read_integrity_state(self.state_db)
        active = sorted(s.id for s in self.sources if s.status is SourceStatus.ACTIVE)
        summary = channel_summary(state, active)
        channels = [
            SourceIntegrity(
                source_id=sid,
                channel=summary[sid],
                version=str(state.channels.get(sid, {}).get("version") or "") or None,
                verdict=str(state.channels.get(sid, {}).get("verdict") or "") or None,
                checked_at=_dt(state.channels.get(sid, {}).get("at")),
            )
            for sid in active
        ]
        cov = slow.provenance
        held = [_held(h) for h in state.held]
        quarantined = [_held(q) for q in state.quarantined]
        alerts = [
            AlertInfo(
                key=str(a["key"]),
                severity=str(a["severity"]),
                message=str(a["message"]),
                raised_at=_dt(str(a["raised_at"]).replace("Z", "+00:00")) or now,
            )
            for a in state.drift_alerts
        ]
        return IntegrityStatus(
            provenance_generations=int(cov.generations) if cov is not None else 0,
            provenance_covered=int(cov.with_provenance) if cov is not None else 0,
            provenance_fraction=round(float(cov.fraction), 4) if cov is not None else 0.0,
            provenance_invalid=list(cov.invalid) if cov is not None else [],
            channels=channels,
            held=held,
            quarantined=quarantined,
            drift_alerts=alerts,
            pinned_hosts=state.pinned_hosts,
        )


def _held(row: Mapping[str, Any]) -> HeldRelease:
    reason = row.get("reason")
    return HeldRelease(
        source_id=str(row["source_id"]),
        version=str(row["version"]),
        state=str(row["state"]),
        reason=str(reason) if reason else None,
    )


def _bot_run(name: str, raw: object) -> BotRun:
    run: Mapping[str, object] = cast(Mapping[str, object], raw) if isinstance(raw, dict) else {}
    ok = run.get("ok")
    count = run.get("candidates")
    return BotRun(
        name=name,
        at=_dt(run.get("at")),
        ok=ok if isinstance(ok, bool) else None,
        candidates=count if isinstance(count, int) else 0,
        error=str(run["error"]) if run.get("error") else None,
    )


def default_downloads_dir(environ: Mapping[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    base = env.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "whisipedia" / "bootstrap"
