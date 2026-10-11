"""``wsp doctor``: is this machine ready to run the library? A 90s-style system check report.

Only reads: it never writes to the vault (the signing key may be *looked at*, never created).
Each check is ``OK``, ``WARN`` or ``FAIL``. Exit codes: 1 when a check fails; with ``--require NAME``
(repeatable), 1 when one of the named checks is not ``OK`` (a warning counts: the caller asked for that
check to pass), and 2 when a name is not a check at all (a typo must not pass silently).
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
from collections.abc import Callable, Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, TextIO, cast

from wsp_cli import bootstrap
from wsp_cli.service import UNITS, Runner, systemctl

CHECK_NAMES = (
    "config",
    "registry",
    "vault",
    "index",
    "disk",
    "aria2",
    "par2",
    "services",
    "key",
    "ledger",
    "orchestrator",
    "integrity",
)  # in report order; each is a method of Doctor

PLACEHOLDER = "/path/to/"
GB = 10**9


class Level(StrEnum):
    OK = "OK"
    WARN = "WARN"
    FAIL = "FAIL"


@dataclass(frozen=True, slots=True)
class Check:
    name: str  # stable id for --require: config, registry, vault, index, disk, aria2, par2, ...
    title: str
    level: Level
    detail: str


def _which_version(exe: str, args: Sequence[str]) -> str | None:
    path = shutil.which(exe)
    if path is None:
        return None
    try:
        out = subprocess.run([path, *args], capture_output=True, text=True, timeout=10, check=False)  # noqa: S603
    except (OSError, subprocess.TimeoutExpired):
        return path
    first = (out.stdout or out.stderr).strip().splitlines()
    return first[0] if first else path


def _free(path: Path) -> tuple[int, int] | None:
    try:
        du = shutil.disk_usage(path)
    except OSError:
        return None
    return du.free, du.total


class Doctor:
    def __init__(
        self,
        cfg: Mapping[str, Any],
        lay: bootstrap.Layout,
        *,
        run: Runner = systemctl,
        which: Callable[[str, Sequence[str]], str | None] = _which_version,
    ) -> None:
        self.cfg = cfg
        self.lay = lay
        self.run = run
        self.which = which
        self.paths = bootstrap.paths(cfg)

    def checks(self, only: Sequence[str] | None = None) -> list[Check]:
        steps = self.steps()
        known = {name for name, _ in steps}
        out: list[Check] = [
            Check(name, name, Level.FAIL, f"no such check; known: {', '.join(n for n, _ in steps)}")
            for name in dict.fromkeys(only or ())
            if name not in known
        ]
        for name, step in steps:
            if only and name not in only:
                continue
            try:
                out.append(step())
            except Exception as exc:  # a broken check is a finding, not a crash
                out.append(Check(name, name.title(), Level.FAIL, f"{type(exc).__name__}: {exc}"))
        return out

    def steps(self) -> list[tuple[str, Callable[[], Check]]]:
        return [(name, cast(Callable[[], Check], getattr(self, name))) for name in CHECK_NAMES]

    # --- checks ---

    def config(self) -> Check:
        local = self.lay.config_dir / "local.toml"
        root = str(bootstrap.section(self.cfg, "vault").get("root", ""))
        if root.startswith(PLACEHOLDER) or not root:
            return Check("config", "Configuration", Level.FAIL, f"vault.root not set ({local})")
        where = "config/local.toml" if local.is_file() else "defaults + environment (no local.toml)"
        return Check("config", "Configuration", Level.OK, f"loaded from {where}")

    def registry(self) -> Check:
        specs = bootstrap.load_sources(self.cfg, self.lay)
        active = sum(1 for s in specs if s.status.value == "active")
        overlay = bootstrap.overlay_path(self.cfg, self.lay)
        extra = " + local overlay" if overlay.is_file() else ""
        return Check("registry", "Source registry", Level.OK, f"{len(specs)} sources, {active} active{extra}")

    def vault(self) -> Check:
        root = self.paths.vault_root
        if root is None or not root.is_dir():
            return Check(
                "vault", "Vault (NAS)", Level.FAIL, f"{root} is not reachable (is the share mounted?)"
            )
        if not (root / "library").is_dir():
            return Check("vault", "Vault (NAS)", Level.WARN, f"{root} has no library/ yet")
        writable = os.access(root, os.W_OK)
        sources = sum(1 for d in (root / "library").iterdir() if d.is_dir() for _ in d.iterdir())
        level = Level.OK if writable else Level.WARN
        return Check(
            "vault", "Vault (NAS)", level, f"{sources} source shelves{'' if writable else ', READ-ONLY'}"
        )

    def index(self) -> Check:
        root = self.paths.index_root
        if root is None or not root.is_dir():
            return Check("index", "Search index", Level.FAIL, f"{root} does not exist; run `wsp index build`")
        refined = (
            len([p for p in (root / "refinery").iterdir() if p.is_dir()])
            if (root / "refinery").is_dir()
            else 0
        )
        catalog = len(list((root / "catalog").glob("*.json"))) if (root / "catalog").is_dir() else 0
        lexical = (root / "lexical").is_dir()
        level = Level.OK if lexical and refined else Level.WARN
        return Check(
            "index",
            "Search index",
            level,
            f"{refined} refined sources, catalog for {catalog}, lexical {'ok' if lexical else 'missing'}",
        )

    def disk(self) -> Check:
        parts: list[str] = []
        level = Level.OK
        for label, path, minimum in (
            ("vault", self.paths.vault_root, 500 * GB),
            ("index", self.paths.index_root, 50 * GB),
        ):
            if path is None:
                continue
            free = _free(path)
            if free is None:
                continue
            parts.append(f"{label} {free[0] / 1e12:.2f} TB free of {free[1] / 1e12:.1f}")
            if free[0] < minimum or free[0] < 0.05 * free[1]:
                level = Level.WARN
        return Check("disk", "Disk space", level, "; ".join(parts) or "no paths configured")

    def aria2(self) -> Check:
        found = self.which("aria2c", ["--version"])
        if found is None:
            return Check(
                "aria2", "aria2c", Level.WARN, "not on PATH (only needed for acquirer.fetcher = 'aria2')"
            )
        return Check("aria2", "aria2c", Level.OK, found)

    def par2(self) -> Check:
        found = self.which("par2", ["--version"])
        if found is None:
            return Check("par2", "par2 (parity)", Level.FAIL, "not on PATH: no bit-rot repair without it")
        scope = bootstrap.section(self.cfg, "vault").get("parity_scope", "all")
        return Check("par2", "par2 (parity)", Level.OK, f"{found}; scope {scope}")

    def services(self) -> Check:
        states: list[str] = []
        level = Level.OK
        for unit in UNITS:
            if unit.endswith("index.service"):
                continue
            code, msg = self.run(["is-active", unit])
            if code == 127:
                return Check("services", "Services", Level.WARN, "systemctl not available")
            states.append(f"{unit.removeprefix('whisipedia-')}={msg or 'unknown'}")
            if msg not in ("active", "activating"):
                level = Level.WARN
        return Check("services", "Services", level, ", ".join(states))

    def key(self) -> Check:
        vault = bootstrap.section(self.cfg, "vault")
        configured = str(vault.get("signing_key") or "")
        key = (
            Path(configured).expanduser()
            if configured
            else bootstrap.config_home() / bootstrap.SIGNING_KEY_NAME
        )
        if not key.is_file():
            return Check("key", "Signing key", Level.FAIL, f"{key} missing (manifests cannot be verified)")
        mode = key.stat().st_mode & 0o077
        if mode:
            return Check("key", "Signing key", Level.WARN, f"{key.name} is readable by others (chmod 600)")
        published = 0
        if self.paths.vault_root is not None and (self.paths.vault_root / "keys").is_dir():
            published = len(list((self.paths.vault_root / "keys").glob("*.pub")))
        return Check("key", "Signing key", Level.OK, f"{key.name} (0600); {published} public key(s) in vault")

    def ledger(self) -> Check:
        if self.paths.vault_root is None or not (self.paths.vault_root / "ledger").is_dir():
            return Check("ledger", "Ledger", Level.WARN, "no ledger yet")
        from wsp_vault.ledger import JsonlLedger

        signer = bootstrap.load_signer(self.cfg, self.lay, create=False)
        ledger = JsonlLedger(self.paths.vault_root / "ledger", signer)
        problem = ledger.verify_detail(recent_days=2)
        days = len(ledger.day_files())
        if problem:
            return Check("ledger", "Ledger (fast verify)", Level.FAIL, problem)
        return Check(
            "ledger",
            "Ledger (fast verify)",
            Level.OK,
            f"newest {min(2, days)} of {days} day file(s) verified",
        )

    def orchestrator(self) -> Check:
        raw = str(bootstrap.section(self.cfg, "orchestrator").get("state_db") or "")
        state = Path(raw).expanduser() if raw else None
        if state is None or not state.is_file():
            return Check(
                "orchestrator", "Orchestrator state", Level.WARN, "no state DB yet (daemon never ran)"
            )
        with closing(sqlite3.connect(f"file:{state}?mode=ro", uri=True)) as db:
            alerts = db.execute(
                "SELECT severity, COUNT(*) FROM alerts WHERE resolved_at IS NULL GROUP BY severity"
            ).fetchall()
            jobs = db.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('pending','running')").fetchone()[0]
        by = {str(s): int(n) for s, n in alerts}
        level = Level.FAIL if by.get("critical") else Level.WARN if by else Level.OK
        summary = ", ".join(f"{n} {s}" for s, n in sorted(by.items())) or "no open alerts"
        return Check("orchestrator", "Orchestrator state", level, f"{summary}; {jobs} queued job(s)")

    def integrity(self) -> Check:
        """Source integrity (ADR-0009): provenance coverage, checksum channels, holds, drift."""
        from wsp_cli.integrity import channel_summary, read_integrity_state

        raw = str(bootstrap.section(self.cfg, "orchestrator").get("state_db") or "")
        state = read_integrity_state(Path(raw).expanduser() if raw else None)
        specs = [s for s in bootstrap.load_sources(self.cfg, self.lay) if s.status.value == "active"]
        channels = channel_summary(state, [s.id for s in specs])
        counts: dict[str, int] = {}
        for status in channels.values():
            counts[status] = counts.get(status, 0) + 1
        sealed = with_prov = 0
        root = self.paths.vault_root
        if root is not None and (root / "library").is_dir():
            for manifest in (root / "library").glob("*/*/*/MANIFEST.json"):
                sealed += 1
                with_prov += (manifest.parent / "PROVENANCE.json").is_file()
        parts = [
            f"provenance {with_prov}/{sealed} generation(s)",
            "channels " + (", ".join(f"{n} {k}" for k, n in sorted(counts.items())) or "none"),
            f"{state.pinned_hosts} pinned host(s)",
        ]
        critical = [a for a in state.drift_alerts if a.get("severity") == "critical"]
        drift = [a for a in state.drift_alerts if str(a.get("key", "")).startswith("origin-drift:")]
        if state.held:
            parts.append(
                f"{len(state.held)} held: "
                + ", ".join(f"{h['source_id']}/{h['version']}" for h in state.held)
            )
        if drift:
            parts.append(f"{len(drift)} origin drift alert(s)")
        if critical:
            parts.append(f"{len(critical)} possible compromise(s): runbook held-release")
        level = (
            Level.FAIL
            if critical
            else Level.WARN
            if (state.held or drift or counts.get("mismatch"))
            else Level.OK
        )
        return Check("integrity", "Source integrity", level, "; ".join(parts))


_ICON = {Level.OK: "[ OK ]", Level.WARN: "[WARN]", Level.FAIL: "[FAIL]"}


def render(checks: Sequence[Check], out: TextIO) -> None:
    width = 74
    bar = "+" + "=" * (width - 2) + "+"
    out.write(f"{bar}\n|{'W H I S I P E D I A   S Y S T E M   D O C T O R':^{width - 2}}|\n")
    out.write(f"|{'(c) 1996-style diagnostics -- best viewed at 80x25':^{width - 2}}|\n{bar}\n")
    for c in checks:
        label = f" {c.title} ".ljust(24, ".")
        out.write(f" {_ICON[c.level]} {label} {c.detail}\n")
    fails = sum(c.level is Level.FAIL for c in checks)
    warns = sum(c.level is Level.WARN for c in checks)
    verdict = "ALL SYSTEMS GO" if not fails and not warns else f"{fails} FAILURE(S), {warns} WARNING(S)"
    out.write(f"{bar}\n|{verdict:^{width - 2}}|\n{bar}\n")


def exit_code(checks: Sequence[Check], require: Sequence[str] | None = None) -> int:
    """0 all good; 1 a check failed (with ``require``: a required check is not OK); 2 unknown names."""
    if require:
        if any(name not in CHECK_NAMES for name in require):
            return 2
        return 0 if all(c.level is Level.OK for c in checks if c.name in require) else 1
    return 1 if any(c.level is Level.FAIL for c in checks) else 0
