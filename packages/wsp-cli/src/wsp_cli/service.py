"""``wsp service install|status|uninstall``: the systemd **user** units in ``deploy/systemd/``.

The committed units are templates: ``@REPO@`` (this checkout) and ``@CONFIG_DIR@`` are filled in when
they are rendered into ``$XDG_CONFIG_HOME/systemd/user/`` (default ``~/.config/systemd/user``); ``%h``
is left for systemd. Installing enables the units (``--now`` also starts them); nothing is started
behind the owner's back.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

UNITS = (
    "whisipedia-orchestrator.service",
    "whisipedia-api.service",
    "whisipedia-api-health.service",  # restarts an API that stops answering (tools/wsp-api-health)
    "whisipedia-api-health.timer",
    "whisipedia-index.service",
    "whisipedia-index.timer",
    "whisipedia-background.slice",  # shared memory/CPU/IO budget of the background units
)
ENABLE = (
    "whisipedia-orchestrator.service",
    "whisipedia-api.service",
    "whisipedia-api-health.timer",
    "whisipedia-index.timer",
)
MARKER = "# Rendered by `wsp service install` from deploy/systemd/"

Runner = Callable[[Sequence[str]], tuple[int, str]]


SYSTEMCTL_TIMEOUT = 60.0  # seconds; a hung user manager must not hang `wsp service`


def systemctl(args: Sequence[str]) -> tuple[int, str]:
    exe = shutil.which("systemctl")
    if exe is None:
        return 127, "systemctl not found"
    try:
        done = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [exe, "--user", *args], capture_output=True, text=True, check=False, timeout=SYSTEMCTL_TIMEOUT
        )
    except subprocess.TimeoutExpired:
        return 124, f"systemctl --user {' '.join(args)} timed out after {SYSTEMCTL_TIMEOUT:.0f}s"
    return done.returncode, (done.stdout + done.stderr).strip()


def unit_dir(environ: Mapping[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    base = env.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "systemd" / "user"


def render(template: str, *, repo: Path, config_dir: Path) -> str:
    text = template.replace("@REPO@", str(repo)).replace("@CONFIG_DIR@", str(config_dir))
    return f"{MARKER}\n{text}"


@dataclass(frozen=True, slots=True)
class ServiceManager:
    repo: Path
    config_dir: Path
    target: Path
    run: Runner = systemctl

    @property
    def templates(self) -> Path:
        return self.repo / "deploy" / "systemd"

    def rendered(self) -> dict[str, str]:
        return {
            name: render(
                (self.templates / name).read_text(encoding="utf-8"),
                repo=self.repo,
                config_dir=self.config_dir,
            )
            for name in UNITS
        }

    def install(self, out: TextIO, *, enable: bool = True, now: bool = False, dry_run: bool = False) -> int:
        if not (self.repo / ".venv" / "bin" / "wsp").exists():
            out.write(f"warning: {self.repo}/.venv/bin/wsp not found; run `uv sync --all-packages` first\n")
        for name, text in self.rendered().items():
            dest = self.target / name
            existing = dest.read_text(encoding="utf-8") if dest.exists() else None
            if existing is not None and not existing.startswith(MARKER):
                out.write(f"  skip {dest}: exists and was not written by `wsp service install`\n")
                continue
            out.write(f"  {'would write' if dry_run else 'write'} {dest}\n")
            if not dry_run:
                dest.parent.mkdir(parents=True, exist_ok=True)
                tmp = dest.with_name(dest.name + ".partial")
                tmp.write_text(text, encoding="utf-8")
                tmp.replace(dest)
        if dry_run:
            return 0
        steps: list[list[str]] = [["daemon-reload"]]
        if enable:
            steps.append(["enable", *(["--now"] if now else []), *ENABLE])
        for step in steps:
            code, msg = self.run(step)
            out.write(f"  systemctl --user {' '.join(step)}: {'ok' if code == 0 else msg}\n")
            if code != 0:
                return 1
        if enable and not now:
            out.write(
                "  enabled; start with: systemctl --user start whisipedia-orchestrator whisipedia-api\n"
            )
        out.write('  keep services running without a login session: loginctl enable-linger "$USER"\n')
        return 0

    def status(self, out: TextIO) -> int:
        worst = 0
        for name in UNITS:
            installed = (self.target / name).exists()
            _, enabled = self.run(["is-enabled", name])
            _, active = self.run(["is-active", name])
            where = "installed" if installed else "missing"
            out.write(f"  {name:<34} {where:<10} {enabled or '-':<10} {active or '-'}\n")
            if not installed:
                worst = 1
        return worst

    def uninstall(self, out: TextIO, *, dry_run: bool = False) -> int:
        present = [n for n in UNITS if (self.target / n).exists()]
        ours: list[str] = []
        for name in present:
            dest = self.target / name
            if dest.read_text(encoding="utf-8").startswith(MARKER):
                ours.append(name)
            else:  # the owner's own unit: neither disabled nor removed
                out.write(f"  keep {dest}: not written by `wsp service install`\n")
        if not dry_run and ours:
            code, msg = self.run(["disable", "--now", *ours])
            out.write(f"  systemctl --user disable --now: {'ok' if code == 0 else msg}\n")
        for name in ours:
            dest = self.target / name
            out.write(f"  {'would remove' if dry_run else 'remove'} {dest}\n")
            if not dry_run:
                dest.unlink()
        if not dry_run:
            self.run(["daemon-reload"])
        return 0
