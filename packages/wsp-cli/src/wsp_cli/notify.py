"""Alerts that reach the owner: Windows notifications from WSL.

Until 2026-10-14 alerts were only stored: 41 were open and nothing told the owner. Two channels now:

* critical alerts show as a Windows notification when they are raised (:class:`WindowsToastSink`,
  at most a few an hour, so a burst cannot flood the screen);
* every morning ``wsp alerts digest --notify`` (whisipedia-digest.timer) sums up what was raised in
  the last day, the trawler's findings included, in one notification.

Notifications go through Windows PowerShell's own app identity, so nothing is installed or
registered on Windows. Off WSL (no ``powershell.exe``) both are quiet no-ops.
"""

from __future__ import annotations

import argparse
import base64
import logging
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, TextIO
from xml.sax.saxutils import escape

from wsp_cli import bootstrap
from wsp_orchestrator.alerts import Alert, AlertSink, Severity

log = logging.getLogger("wsp.cli.notify")

POWERSHELL = Path("/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe")
# Windows PowerShell's registered app id: a toast needs one, and this one exists on every Windows.
APP_ID = r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"
RANK = {Severity.INFO: 0, Severity.WARNING: 1, Severity.CRITICAL: 2}
TOAST_TIMEOUT = 60.0
GROUP_OVER = 3  # more alerts of one kind than this are one digest line


def toast_script(title: str, body: str) -> str:
    """The PowerShell that shows one notification (text escaped for XML, then for a '…' string)."""
    xml = (
        '<toast><visual><binding template="ToastGeneric">'
        f"<text>{escape(title[:120])}</text><text>{escape(body[:600])}</text>"
        "</binding></visual></toast>"
    ).replace("'", "''")
    return (
        "$ErrorActionPreference = 'Stop'\n"
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, "
        "ContentType = WindowsRuntime] > $null\n"
        "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, "
        "ContentType = WindowsRuntime] > $null\n"
        "$xml = New-Object Windows.Data.Xml.Dom.XmlDocument\n"
        f"$xml.LoadXml('{xml}')\n"
        f"[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{APP_ID}')"
        ".Show([Windows.UI.Notifications.ToastNotification]::new($xml))\n"
    )


def show_toast(
    title: str,
    body: str,
    *,
    powershell: Path = POWERSHELL,
    run: Callable[..., Any] = subprocess.run,
) -> bool:
    """Show a Windows notification; False (logged) when it cannot. Never raises."""
    if not powershell.exists():
        return False
    encoded = base64.b64encode(toast_script(title, body).encode("utf-16-le")).decode("ascii")
    try:
        done = run(
            [str(powershell), "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
            capture_output=True,
            timeout=TOAST_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("notification failed: %s", exc)
        return False
    if done.returncode != 0:
        log.warning("notification failed: exit %s", done.returncode)
        return False
    return True


class WindowsToastSink:
    """An :class:`~wsp_orchestrator.alerts.AlertSink`: alerts of ``min_severity`` or worse as Windows
    notifications, at most ``max_per_hour``, shown from a thread so the daemon never waits on them."""

    def __init__(
        self,
        *,
        min_severity: Severity = Severity.CRITICAL,
        max_per_hour: int = 4,
        show: Callable[[str, str], bool] = show_toast,
        monotonic: Callable[[], float] = time.monotonic,
        background: bool = True,
    ) -> None:
        self.min_severity = min_severity
        self.max_per_hour = max_per_hour
        self._show = show
        self._monotonic = monotonic
        self._background = background
        self._sent: deque[float] = deque()
        self._lock = threading.Lock()

    def deliver(self, alert: Alert) -> None:
        if RANK[alert.severity] < RANK[self.min_severity]:
            return
        now = self._monotonic()
        with self._lock:
            while self._sent and now - self._sent[0] > 3600:
                self._sent.popleft()
            if len(self._sent) >= self.max_per_hour:
                return  # the morning digest still lists it
            self._sent.append(now)
        title = f"Whisipedia: {alert.severity.value} alert"
        if self._background:
            threading.Thread(target=self._show, args=(title, alert.message), daemon=True).start()
        else:
            self._show(title, alert.message)


def alert_sinks(cfg: Mapping[str, Any]) -> list[AlertSink]:
    """The configured sinks (``[alerts]``): a Windows notification sink unless turned off."""
    section = bootstrap.section(cfg, "alerts")
    if not bool(section.get("windows_toast", True)) or not POWERSHELL.exists():
        return []
    return [
        WindowsToastSink(
            min_severity=Severity(str(section.get("toast_min_severity", "critical"))),
            max_per_hour=int(section.get("toast_max_per_hour", 4)),
        )
    ]


def digest_lines(
    alerts: Sequence[Mapping[str, Any]], now: datetime, *, since: timedelta = timedelta(days=1)
) -> tuple[str, list[str]]:
    """(one-line summary, detail lines) of the alerts raised since ``now - since`` and still open."""
    cutoff = now - since
    fresh: list[Mapping[str, Any]] = []
    for a in alerts:
        try:
            raised = datetime.fromisoformat(str(a["raised_at"]).replace("Z", "+00:00"))
        except (KeyError, ValueError):
            continue
        if raised >= cutoff and a.get("resolved_at") is None:
            fresh.append(a)
    by = {s: sum(1 for a in fresh if a["severity"] == s.value) for s in Severity}
    open_total = sum(1 for a in alerts if a.get("resolved_at") is None)
    parts = [f"{n} {s.value}" for s, n in sorted(by.items(), key=lambda kv: -RANK[kv[0]]) if n]
    summary = (
        f"{len(fresh)} new alert(s) in the last day ({', '.join(parts)}); {open_total} open in all."
        if fresh
        else f"No new alerts in the last day; {open_total} open in all."
    )
    fresh.sort(key=lambda a: (-RANK[Severity(a["severity"])], str(a["key"])))
    # Many alerts of one kind (36 "dormant upstream" after a discovery pass) read as one line each
    # would bury the rest: more than GROUP_OVER of a kind become one line naming a few.
    kinds: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    for a in fresh:
        kinds.setdefault((str(a["severity"]), str(a["key"]).split(":", 1)[0]), []).append(a)
    lines: list[str] = []
    for (sev, kind), group in kinds.items():
        if len(group) > GROUP_OVER:
            names = ", ".join(str(a["key"]).split(":", 1)[-1] for a in group[:3])
            lines.append(f"[{sev}] {len(group)} alerts of kind {kind}: {names}, …")
        else:
            lines.extend(f"[{sev}] {a['message']}" for a in group)
    return summary, lines


def cmd_alerts_digest(args: argparse.Namespace, out: TextIO) -> int:
    from wsp_cli.trawl import recent_failures, summary_path
    from wsp_orchestrator.store import StateStore

    lay = bootstrap.layout(args.config_dir)
    cfg = bootstrap.load(lay)
    state_db = Path(str(bootstrap.section(cfg, "orchestrator").get("state_db") or ""))
    rows: list[Mapping[str, Any]] = []
    if state_db.is_file():
        store = StateStore(state_db, check_local=False)
        try:
            rows = [dict(r) for r in store.query("SELECT * FROM alerts")]
        finally:
            store.close()
    now = datetime.now(UTC)
    summary, lines = digest_lines(rows, now)
    trawl = ""
    path = summary_path(cfg)
    if path.is_file():
        import json

        data = json.loads(path.read_text(encoding="utf-8"))
        bad = recent_failures(data, now, timedelta(days=1))
        trawl = f"Trawler: {int(data.get('pages', 0)):,} pages checked; " + (
            f"{sum(f.count for f in bad)} failed in the last day." if bad else "none failed in the last day."
        )
    out.write(summary + "\n")
    if trawl:
        out.write(trawl + "\n")
    for line in lines[:50]:
        out.write(f"  {line}\n")
    if args.notify:
        body = summary + (f"\n{trawl}" if trawl else "") + "\nDetails: wsp://system or `wsp alerts digest`."
        show_toast("Whisipedia: morning report", body)
    return 0


def _alerter(cfg: Mapping[str, Any]) -> Any:
    """An :class:`~wsp_orchestrator.alerts.Alerter` on the orchestrator's state DB with the configured
    sinks (so a critical alert raised from a script shows as a notification too), or None."""
    from wsp_orchestrator.alerts import Alerter
    from wsp_orchestrator.clock import SystemClock
    from wsp_orchestrator.store import StateStore

    state_db = Path(str(bootstrap.section(cfg, "orchestrator").get("state_db") or ""))
    if not state_db.is_file():
        return None
    store = StateStore(state_db, check_local=False)
    return Alerter(store, SystemClock(), [*alert_sinks(cfg)]), store


def cmd_alerts_raise(args: argparse.Namespace, out: TextIO) -> int:
    """``wsp alerts raise``: open an alert from a script (the collection lanes). Open already: no-op."""
    cfg = bootstrap.load(bootstrap.layout(args.config_dir))
    pair = _alerter(cfg)
    if pair is None:
        out.write("alerts: no orchestrator state DB; nothing raised\n")
        return 0
    alerter, store = pair
    try:
        # A sink shows its notification from a thread; wait for it: this process exits right after.
        for sink in alerter.sinks:
            if isinstance(sink, WindowsToastSink):
                sink._background = False  # pyright: ignore[reportPrivateUsage]
        raised = alerter.raise_(Alert(args.key, Severity(args.severity), args.message))
    finally:
        store.close()
    out.write(f"{args.key}: {'raised' if raised else 'already open'}\n")
    return 0


def cmd_alerts_resolve(args: argparse.Namespace, out: TextIO) -> int:
    """``wsp alerts resolve``: close an alert from a script once its cause is gone."""
    cfg = bootstrap.load(bootstrap.layout(args.config_dir))
    pair = _alerter(cfg)
    if pair is None:
        return 0
    alerter, store = pair
    try:
        resolved = alerter.resolve(args.key)
    finally:
        store.close()
    if resolved:
        out.write(f"{args.key}: resolved\n")
    return 0
