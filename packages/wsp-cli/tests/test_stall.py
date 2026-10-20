"""tools/wsp-stall: a job that stops working, a lock held for hours, a rate-limited lane and a silent API
are each written once when they start and once when they end (2026-10-19: a 10.5-hour lock and a lane
stopped on HTTP 429 were found by hand, hours late)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from test_cli_autonomy import ROOT

CG = "/whisipedia.slice/whisipedia-job-lane.service"
T0 = 1_800_000_000


def _world(tmp: Path, *, usage: int, failed: bool = False) -> dict[str, str]:
    fakebin = tmp / "bin"
    fakebin.mkdir(exist_ok=True)
    rows = "whisipedia-job-lane.service loaded active running lane\n"
    rows += "whisipedia-api.service loaded active running api\n"
    if failed:
        rows += "whisipedia-job-maps.service loaded failed failed maps\n"
    (tmp / "units").write_text(rows)
    (fakebin / "systemctl").write_text(
        f'#!/bin/sh\ncase "$*" in *list-units*) cat {tmp}/units ;; *ControlGroup*) echo {CG} ;;'
        " *MainPID*) echo 99 ;; esac\n"
    )
    (fakebin / "systemctl").chmod(0o755)
    cg = tmp / "cgroup" / CG.strip("/")
    cg.mkdir(parents=True, exist_ok=True)
    (cg / "cpu.stat").write_text(f"usage_usec {usage}\nuser_usec 1\n")
    (cg / "cgroup.procs").write_text("76\n77\n")
    locks, proc = tmp / "locks", tmp / "proc"
    locks.mkdir(exist_ok=True)
    (locks / "ingest.lock").touch()
    (locks / "scrub.lock").touch()
    (locks / "heavy.lock").touch()
    (locks / "profile.lock").touch()
    (proc / "42").mkdir(parents=True, exist_ok=True)
    (proc / "42" / "cmdline").write_bytes(b"/r/.venv/bin/python3\0.venv/bin/wsp\0ingest\0--source\0big\0")
    (proc / "42" / "stat").write_text("42 (wsp) S " + " ".join(["0"] * 18) + " 100 0 0\n")  # started 1 s in
    (proc / "uptime").write_text("90000.00 1.00\n")  # the holder is 25 h old
    (proc / "99" / "fd").mkdir(parents=True, exist_ok=True)  # the API: 9 of 10 files it may open
    for n in range(9):
        (proc / "99" / "fd" / str(n)).touch()
    (proc / "99" / "limits").write_text(
        "Max open files            10                   65536                files\n"
    )
    ingest, scrub = (locks / "ingest.lock").stat().st_ino, (locks / "scrub.lock").stat().st_ino
    heavy = (locks / "heavy.lock").stat().st_ino
    profile = (locks / "profile.lock").stat().st_ino
    (proc / "locks").write_text(
        f"1: FLOCK  ADVISORY  WRITE 42 08:30:{ingest} 0 EOF\n"
        f"3: FLOCK  ADVISORY  WRITE 42 08:30:{heavy} 0 EOF\n"
        f"3: -> FLOCK  ADVISORY  WRITE 77 08:30:{heavy} 0 EOF\n"  # the lane's process waits for it
        f"4: FLOCK  ADVISORY  WRITE 42 08:30:{profile} 0 EOF\n"  # a paced profile: hours by design
        f"2: FLOCK  ADVISORY  WRITE 42 08:30:{scrub} 0 EOF\n"
    )
    return {
        "PATH": f"{fakebin}:/usr/bin:/bin",
        "WSP_STALL_STATE": str(tmp / "state"),
        "WSP_STALL_LOCKS": str(locks),
        "WSP_STALL_PROC": str(proc),
        "WSP_STALL_CGROUP_ROOT": str(tmp / "cgroup"),
        "WSP_STALL_LOGS": str(tmp / "logs" / "*.log"),
        "WSP_STALL_HEALTH_URL": "http://127.0.0.1:9/v1/health",  # nothing answers there
        "WSP_STALL_MIN_FREE_GB": "0",
        "HOME": str(tmp),
    }


def _run(env: dict[str, str], hours: float, *args: str) -> list[str]:
    done = subprocess.run(
        [str(ROOT / "tools" / "wsp-stall"), *args],
        env={**env, "WSP_STALL_NOW": str(T0 + int(hours * 3600))},
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout.splitlines()


def test_an_idle_job_a_long_held_lock_and_a_silent_api_are_raised_once_then_cleared(tmp_path: Path) -> None:
    env = _world(tmp_path, usage=5_000_000)
    first = _run(env, 0)
    assert sorted(line.split()[2].rstrip(":") for line in first) == [
        "api-down",
        "files-high:whisipedia-api.service",  # nothing else is known yet
    ]
    assert _run(env, 1) == []  # raised once, not every run
    env = _world(tmp_path, usage=6_000_000, failed=True)  # 1 s of CPU in 3.5 h: idle
    later = " ".join(_run(env, 3.5))
    assert "RAISED job-idle:whisipedia-job-lane.service" in later
    # The lock its process waits for (heavy), not the likeliest held one (ingest); held from first sight.
    assert "it waits for the heavy lock, held (3.5 h) by wsp ingest --source big" in later
    assert "RAISED unit-failed:whisipedia-job-maps.service" in later
    assert "lock-held" not in later
    held = " ".join(_run(env, 8.5))
    assert "RAISED lock-held:ingest: ingest lock held 8.5 h by pid 42" in held
    assert "scrub" not in held  # the orchestrator's for its whole life
    assert "lock-held:profile" not in held  # its limit is 24 h
    env = _world(tmp_path, usage=900_000_000)  # working again; the failed unit is gone
    cleared = " ".join(_run(env, 9))
    assert "CLEARED job-idle:whisipedia-job-lane.service" in cleared
    assert "CLEARED unit-failed:whisipedia-job-maps.service" in cleared
    status = _run(env, 9, "--status")
    assert {line.split()[0] for line in status} == {
        "api-down",
        "lock-held:ingest",
        "lock-held:heavy",
        "files-high:whisipedia-api.service",
    }
    events = (tmp_path / "state" / "watch" / "events.log").read_text()
    assert events.count("RAISED api-down") == 1


def test_new_problem_lines_in_lane_logs_are_noted_once_a_day_without_the_backlog(tmp_path: Path) -> None:
    env = _world(tmp_path, usage=1)
    logs = tmp_path / "logs"
    logs.mkdir()
    lane = logs / "bulk-lane-chronicling.log"
    lane.write_text("old: GET x failed after 6 attempt(s): HTTP 429\n")
    assert not any("NOTE" in line for line in _run(env, 0))  # the backlog is not news
    with lane.open("a") as fh:
        fh.write("2026-10-19T05:13:01Z wyu: GET ocr.json failed after 6 attempt(s): HTTP 429\n")
        fh.write("2026-10-19T05:14:01Z wv: GET ocr.json failed after 6 attempt(s): HTTP 429\n")
        fh.write("error: another 'ingest' is running (pid 695565); lock: ingest.lock\n")
        fh.write("2026-10-19T05:15:01Z fetched wv\n")
        fh.write("2026-10-19T05:16:01Z fetch exit 7\n")  # complete, with alerts: not a problem
    notes = [line for line in _run(env, 0.2) if " NOTE " in line]
    assert len(notes) == 1  # one problem; the lock wait is not one
    assert "bulk-lane-chronicling.log: 2026-10-19T05:14:01Z wv: GET ocr.json" in notes[0]
    assert notes[0].endswith("HTTP 429 (x2)")
    with lane.open("a") as fh:
        fh.write("2026-10-19T06:00:00Z ak: GET ocr.json failed after 6 attempt(s): HTTP 429\n")
    assert not [line for line in _run(env, 1) if " NOTE " in line]  # the same kind, the same day
    with lane.open("a") as fh:
        fh.write("2026-10-20T06:00:00Z ak: GET ocr.json failed after 6 attempt(s): HTTP 429\n")
    assert [line for line in _run(env, 25.5) if " NOTE " in line]  # a day later: still happening
    state = json.loads((tmp_path / "state" / "watch" / "state.json").read_text())
    assert state["offsets"][str(lane)] == lane.stat().st_size
