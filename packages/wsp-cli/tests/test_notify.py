import base64
import re
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from wsp_cli.notify import WindowsToastSink, digest_lines, show_toast, toast_script
from wsp_orchestrator.alerts import Alert, Severity

NOW = datetime(2026, 10, 14, 9, 0, tzinfo=UTC)


def test_the_script_escapes_xml_and_carries_any_quote_safely() -> None:
    """2026-10-26 review: PowerShell ends a '...' string at a curly quote too; the text now travels
    base64-encoded, so no quote of any kind reaches the script."""
    s = toast_script("A <b> & 'c'", "the owner\u2019s \u2018note\u2019, it's <done>")
    (packed,) = re.findall(r"FromBase64String\('([A-Za-z0-9+/=]+)'\)", s)
    xml = base64.b64decode(packed).decode("utf-8")
    assert "<text>A &lt;b&gt; &amp; 'c'</text>" in xml
    assert "<text>the owner\u2019s \u2018note\u2019, it's &lt;done&gt;</text>" in xml
    assert not any(q in s for q in ("\u2018", "\u2019", "it's"))


def test_show_toast_runs_powershell_with_an_encoded_command(tmp_path: Path) -> None:
    ps = tmp_path / "powershell.exe"
    ps.write_text("")
    calls: list[list[str]] = []

    def run(cmd: list[str], **_: Any) -> subprocess.CompletedProcess[bytes]:
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0)

    assert show_toast("T", "B", powershell=ps, run=run)
    script = base64.b64decode(calls[0][-1]).decode("utf-16-le")
    (packed,) = re.findall(r"FromBase64String\('([A-Za-z0-9+/=]+)'\)", script)
    assert "<text>T</text><text>B</text>" in base64.b64decode(packed).decode("utf-8")
    assert not show_toast("T", "B", powershell=tmp_path / "missing.exe", run=run)


def test_the_sink_shows_only_serious_alerts_and_caps_the_rate() -> None:
    shown: list[tuple[str, str]] = []
    clock = [0.0]
    sink = WindowsToastSink(
        max_per_hour=2,
        show=lambda t, b: shown.append((t, b)) or True,
        monotonic=lambda: clock[0],
        background=False,
    )
    sink.deliver(Alert("w", Severity.WARNING, "dormant upstream"))
    for i in range(3):
        sink.deliver(Alert(f"c{i}", Severity.CRITICAL, f"quarantined {i}"))
    assert [b for _, b in shown] == ["quarantined 0", "quarantined 1"]  # the third waits for the digest
    clock[0] = 3601.0
    sink.deliver(Alert("c9", Severity.CRITICAL, "later"))
    assert shown[-1] == ("Whisipedia: critical alert", "later")


def test_the_digest_counts_what_is_new_and_still_open() -> None:
    def row(key: str, sev: str, hours: float, resolved: bool = False) -> dict[str, Any]:
        return {
            "key": key,
            "severity": sev,
            "message": f"msg {key}",
            "raised_at": (NOW - timedelta(hours=hours)).isoformat(),
            "resolved_at": "x" if resolved else None,
        }

    summary, lines = digest_lines(
        [
            row("a", "warning", 2),
            row("b", "critical", 5),
            row("c", "warning", 30),
            row("d", "warning", 1, True),
        ],
        NOW,
    )
    assert summary == "2 new alert(s) in the last day (1 critical, 1 warning); 3 open in all."
    assert lines == ["[critical] msg b", "[warning] msg a"]
    assert digest_lines([], NOW)[0] == "No new alerts in the last day; 0 open in all."
    many = [row(f"dormant:s{i}", "warning", 1) for i in range(5)]
    assert digest_lines(many, NOW)[1] == ["[warning] 5 alerts of kind dormant: s0, s1, s2, …"]
