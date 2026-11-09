"""tools/wsp-stall: a job that stops working, a lock held for hours, a rate-limited lane and a silent API
are each written once when they start and once when they end (2026-10-19: a 10.5-hour lock and a lane
stopped on HTTP 429 were found by hand, hours late)."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
from test_cli_autonomy import ROOT

CG = "/whisipedia.slice/whisipedia-job-lane.service"
T0 = 1_800_000_000


def _world(tmp: Path, *, usage: int, failed: bool = False, run: str = "r1") -> dict[str, str]:
    fakebin = tmp / "bin"
    fakebin.mkdir(exist_ok=True)
    rows = "whisipedia-job-lane.service loaded active running lane\n"
    rows += "whisipedia-api.service loaded active running api\n"
    if failed:
        rows += "whisipedia-job-maps.service loaded failed failed maps\n"
    (tmp / "units").write_text(rows)
    (fakebin / "systemctl").write_text(
        f'#!/bin/sh\ncase "$*" in *list-units*) cat {tmp}/units ;; *ControlGroup*) echo {CG} ;;'
        f" *MainPID*) echo 99 ;; *InvocationID*) echo {run} ;; esac\n"
    )
    (fakebin / "systemctl").chmod(0o755)
    (fakebin / "journalctl").write_text('#!/bin/sh\necho "the journal of $3"\n')
    (fakebin / "journalctl").chmod(0o755)
    # `wsp alerts raise|resolve`: recorded; it fails while <tmp>/alert-fail exists.
    (fakebin / "wsp").write_text(f'#!/bin/sh\necho "$*" >> {tmp}/alerts\n[ ! -e {tmp}/alert-fail ]\n')
    (fakebin / "wsp").chmod(0o755)
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
        "WSP_STALL_ALERT_CMD": str(fakebin / "wsp"),  # never the checkout's: tests raise no real alert
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


def test_a_slice_stalled_on_memory_is_raised_until_its_pressure_falls(tmp_path: Path) -> None:
    """2026-10-20: eleven lanes over the download slice's 1G were stalled 81% of the time, unnoticed."""
    env = _world(tmp_path, usage=10**9)
    cg = tmp_path / "cgroup" / CG.strip("/")
    (cg / "memory.current").write_text(f"{1176 * 2**20}\n")
    (cg / "memory.high").write_text(f"{2**30}\n")
    (cg / "memory.pressure").write_text(
        "some avg10=85.66 avg60=87.70 avg300=81.33 total=1\n"
        "full avg10=85.13 avg60=87.15 avg300=80.85 total=1\n"
    )
    raised = [e for e in _run(env, 0) if "mem-stall:" in e]
    assert len(raised) == 3  # the fake systemctl gives every unit the same cgroup
    assert "RAISED mem-stall:whisipedia-background-fetch.slice" in "\n".join(raised)
    assert "stalled on memory 81% of the last 5 minutes (1,176 MB of memory.high 1,024 MB)" in raised[0]
    assert not [e for e in _run(env, 0.2) if "mem-stall:" in e]  # still open: not written again
    (cg / "memory.pressure").write_text(
        "some avg10=1 avg60=1 avg300=2 total=1\nfull avg10=0 avg60=0 avg300=1.5 total=1\n"
    )
    assert sum("CLEARED mem-stall:" in e for e in _run(env, 0.4)) == 3


def test_jobs_owed_a_start_and_listed_jobs_not_running_are_raised(tmp_path: Path) -> None:
    """2026-10-22: the memory guard stopped every lane, nothing started them again, and nothing said
    so for 10 hours."""
    env = _world(tmp_path, usage=10**9)
    jobs = tmp_path / ".local" / "state" / "whisipedia"
    (jobs / "jobs").mkdir(parents=True)
    owed = jobs / "jobs" / "yield-stopped"
    owed.write_text("whisipedia-job-collections-std.service\nwhisipedia-job-era5.service\n")
    os.utime(owed, (T0, T0))
    (jobs / "jobs.conf").write_text("# resumed after a reboot\nlane  # running\nera5\n")
    first = " ".join(_run(env, 0.2))
    assert "jobs-owed" not in first  # 12 minutes: the guard waits for 10 calm minutes first
    assert "job-down" not in first  # era5 seen down just now
    later = " ".join(_run(env, 1.5))
    assert "RAISED jobs-owed: 2 jobs stopped for memory 1.5 h ago are not started again" in later
    assert "(collections-std, era5)" in later
    assert "RAISED job-down:era5: whisipedia-job-era5 not running for 1.3 h" in later
    assert "job-down:lane" not in later  # it runs
    owed.unlink()
    (jobs / "jobs.conf").write_text("lane\n")  # era5 done: taken out
    cleared = " ".join(_run(env, 2))
    assert "CLEARED jobs-owed" in cleared
    assert "CLEARED job-down:era5" in cleared


def test_a_watcher_idle_by_design_is_not_a_stalled_job(tmp_path: Path) -> None:
    """The API memory watcher samples every 30 minutes and used no CPU in 3 hours (2026-10-23)."""
    env = _world(tmp_path, usage=5_000_000)
    rows = (
        (tmp_path / "units")
        .read_text()
        .replace("whisipedia-job-lane.service", "whisipedia-job-api-memory-watch.service")
    )
    (tmp_path / "units").write_text(rows)
    _run(env, 0)
    assert not [e for e in _run(env, 3.5) if "job-idle" in e]


def test_a_job_restarted_by_a_reboot_starts_a_new_baseline(tmp_path: Path) -> None:
    """After a reboot the unit's CPU counter starts again from zero: 12 minutes into the boot, six
    jobs compared with a sample from before it were "idle for 12.1 h" (2026-10-24)."""
    env = _world(tmp_path, usage=1_000_000)  # the last boot's run, 1 s in
    _run(env, 0)
    env = _world(tmp_path, usage=2_000_000, run="r2")  # rebooted: 2 s of CPU in its first minutes
    assert not [e for e in _run(env, 12) if "job-idle" in e]
    env = _world(tmp_path, usage=3_000_000, run="r2")  # 1 s in the 3.5 h since: idle for real
    assert [e for e in _run(env, 15.5) if "RAISED job-idle" in e]


def test_a_job_waiting_its_turn_in_the_ingest_queue_is_not_idle(tmp_path: Path) -> None:
    """2026-10-25: NARA, third in the ingest queue behind two hour-long ingests, was raised as idle
    every 3 hours. A ticket of a living process is a turn being waited for; after 12 hours it is
    reported, with its place."""
    env = _world(tmp_path, usage=5_000_000)
    queue = tmp_path / "locks" / "ingest.queue"
    queue.mkdir()
    (tmp_path / "proc" / "76").mkdir(parents=True)  # the lane's ingest, alive
    (queue / f"{(T0 - 600) * 10**9:020d}-31337").touch()  # ahead of it: a dead waiter's ticket
    (queue / f"{T0 * 10**9:020d}-76").touch()
    os.utime(queue / f"{T0 * 10**9:020d}-76", (T0, T0))  # aged by mtime, not by its name
    _run(env, 0)
    assert not [e for e in _run(env, 3.5) if "job-idle" in e]
    raised = [e for e in _run(env, 12.5) if "RAISED job-idle" in e]
    assert raised
    assert "number 1 in the ingest lock's queue, waiting 12.5 h" in raised[0]


def test_a_lane_sleeping_between_rounds_is_not_idle(tmp_path: Path) -> None:
    """2026-10-29: most job-idle alerts were lanes in their hour's pause between rounds (a shell and
    its `sleep`). A process of another kind, idle as long, is still raised."""
    env = _world(tmp_path, usage=5_000_000)
    for pid, comm in (("76", "lane-run.sh"), ("77", "sleep")):  # a script is named after itself
        (tmp_path / "proc" / pid).mkdir(parents=True, exist_ok=True)
        (tmp_path / "proc" / pid / "comm").write_text(comm + "\n")
    _run(env, 0)
    assert not [e for e in _run(env, 3.5) if "job-idle" in e]
    (tmp_path / "proc" / "77" / "comm").write_text("python3\n")  # a worker, not a pause
    assert not [e for e in _run(env, 3.6) if "job-idle" in e]  # just out of its pause
    assert [e for e in _run(env, 6.6) if "RAISED job-idle" in e]  # idle as long on its own


def test_a_job_just_out_of_the_queue_is_not_idle_and_an_open_alert_clears_once_it_queues(
    tmp_path: Path,
) -> None:
    """2026-11-08/09: 43 "job-idle ... it may be waiting on the ingest lock" alerts, most of them lanes
    a few minutes out of the ingest queue (3 hours in it), starting their next fetch. A job seen with
    a ticket is not idle until IDLE_H after it was last seen so; one quiet that long on its own is."""
    env = _world(tmp_path, usage=5_000_000)
    queue = tmp_path / "locks" / "ingest.queue"
    queue.mkdir()
    (tmp_path / "proc" / "76").mkdir(parents=True)  # the lane's ingest, alive
    ticket = queue / f"{T0 * 10**9:020d}-76"
    _run(env, 0)
    assert [e for e in _run(env, 3.5) if "RAISED job-idle" in e]  # no ticket, no wait: a hang
    ticket.touch()  # now queued for the ingest lock: the open alert ends
    os.utime(ticket, (T0 + 3.5 * 3600, T0 + 3.5 * 3600))
    assert [e for e in _run(env, 3.6) if "CLEARED job-idle:whisipedia-job-lane.service" in e]
    assert not [e for e in _run(env, 6.5) if "job-idle" in e]  # 3 hours in the queue
    ticket.unlink()  # its turn came and went; the lane starts its next fetch
    assert not [e for e in _run(env, 6.6) if "job-idle" in e]
    assert not [e for e in _run(env, 9.4) if "job-idle" in e]  # under 3 h since it was seen waiting
    raised = [e for e in _run(env, 9.6) if "RAISED job-idle" in e]  # 3.1 h quiet on its own
    assert raised
    assert "queue" not in raised[0]


def test_a_lane_waiting_for_another_lanes_source_lock_is_not_idle(tmp_path: Path) -> None:
    """tools/lanes/lane-run.sh waits up to 6 hours in `flock --wait` for a source another lane holds:
    a wait, not a hang. A process blocked on a lock of another kind still is reported."""
    env = _world(tmp_path, usage=5_000_000)
    lane_locks = tmp_path / "lane-locks"
    lane_locks.mkdir()
    src = lane_locks / "dla-assist.lock"
    src.touch()
    st = src.stat()
    key = f"{os.major(st.st_dev):02x}:{os.minor(st.st_dev):02x}:{st.st_ino}"
    proc_locks = tmp_path / "proc" / "locks"
    base = proc_locks.read_text()
    proc_locks.write_text(
        base + f"7: FLOCK  ADVISORY  WRITE 5 {key} 0 EOF\n7: -> FLOCK  ADVISORY  WRITE 76 {key} 0 EOF\n"
    )
    env["WSP_STALL_LANE_LOCKS"] = str(lane_locks)
    waiter = tmp_path / "proc" / "76"
    waiter.mkdir(parents=True)
    (waiter / "cmdline").write_bytes(b"flock\0--wait\x0021600\x008\0")

    def started(hours_ago: float) -> None:  # uptime is 90,000 s; ticks of 1/100 s
        ticks = int((90_000 - hours_ago * 3600) * 100)
        (waiter / "stat").write_text("76 (flock) S " + " ".join(["0"] * 18) + f" {ticks} 0 0\n")

    started(1)
    _run(env, 0)
    assert not [e for e in _run(env, 3.5) if "job-idle" in e]
    proc_locks.write_text(base)  # it got the source; quiet for 3 hours more on its own: a hang
    assert not [e for e in _run(env, 3.6) if "job-idle" in e]
    assert [e for e in _run(env, 6.6) if "RAISED job-idle" in e]


def test_a_source_lock_waited_for_over_twelve_hours_is_reported(tmp_path: Path) -> None:
    env = _world(tmp_path, usage=5_000_000)
    lane_locks = tmp_path / "lane-locks"
    lane_locks.mkdir()
    src = lane_locks / "dla-assist.lock"
    src.touch()
    st = src.stat()
    key = f"{os.major(st.st_dev):02x}:{os.minor(st.st_dev):02x}:{st.st_ino}"
    proc_locks = tmp_path / "proc" / "locks"
    proc_locks.write_text(proc_locks.read_text() + f"7: -> FLOCK  ADVISORY  WRITE 76 {key} 0 EOF\n")
    env["WSP_STALL_LANE_LOCKS"] = str(lane_locks)
    waiter = tmp_path / "proc" / "76"
    waiter.mkdir(parents=True)
    (waiter / "cmdline").write_bytes(b"flock\0--wait\0")
    (waiter / "stat").write_text(
        "76 (flock) S " + " ".join(["0"] * 18) + f" {(90_000 - 13 * 3600) * 100} 0 0\n"
    )
    _run(env, 0)
    raised = [e for e in _run(env, 3.5) if "RAISED job-idle" in e]
    assert raised
    assert "it waits for another lane's dla-assist source lock (13.0 h)" in raised[0]


def test_a_job_that_just_took_the_lock_after_its_wait_is_not_idle(tmp_path: Path) -> None:
    """2026-10-26: NARA waited 3 hours in the queue, took the ingest lock, and was raised as idle in
    that minute: its ticket was gone and the waiting hours counted as idle."""
    env = _world(tmp_path, usage=5_000_000)
    locks, proc = tmp_path / "locks", tmp_path / "proc"
    ingest = (locks / "ingest.lock").stat().st_ino
    (proc / "76").mkdir(parents=True, exist_ok=True)
    (proc / "76" / "cmdline").write_bytes(b"wsp\0ingest\0--source\0nara-catalog\0")
    (proc / "76" / "stat").write_text(
        "76 (wsp) S " + " ".join(["0"] * 18) + " 8999900 0 0\n"
    )  # started a second ago
    (proc / "locks").write_text(f"1: FLOCK  ADVISORY  WRITE 76 08:30:{ingest} 0 EOF\n")
    _run(env, 0)
    assert not [e for e in _run(env, 3.5) if "job-idle" in e]


def test_a_slow_systemd_stays_within_the_budget_and_clears_nothing_it_could_not_check(
    tmp_path: Path,
) -> None:
    """2026-11-04 trawl: each systemctl call could wait 60 s, ~26 a run when memory is short, and the
    unit kills the run at 5 minutes before it writes anything. Now each call waits at most
    WSP_STALL_SYSTEMCTL_S, the run at most WSP_STALL_BUDGET_S, and what went unchecked stays open."""
    env = _world(tmp_path, usage=5_000_000, failed=True)
    first = " ".join(_run(env, 0))
    assert "RAISED unit-failed:whisipedia-job-maps.service" in first
    assert "RAISED files-high:whisipedia-api.service" in first
    (tmp_path / "bin" / "systemctl").write_text("#!/bin/sh\nexec sleep 30\n")  # systemd under pressure
    slow = {**env, "WSP_STALL_SYSTEMCTL_S": "1", "WSP_STALL_BUDGET_S": "3"}
    done = subprocess.run(
        [str(ROOT / "tools" / "wsp-stall")],
        env={**slow, "WSP_STALL_NOW": str(T0 + 3600)},
        capture_output=True,
        text=True,
        check=True,
        timeout=20,  # the run is bounded, whatever systemd does
    )
    lines = done.stdout.splitlines()
    assert not [line for line in lines if " CLEARED " in line], lines
    assert any("RAISED systemd-slow" in line for line in lines), lines
    status = {line.split()[0] for line in _run(env, 1, "--status")}
    assert {"unit-failed:whisipedia-job-maps.service", "files-high:whisipedia-api.service"} <= status
    assert "api-down" in status
    state = json.loads((tmp_path / "state" / "watch" / "state.json").read_text())
    assert "whisipedia-job-lane.service" in state["cpu"]  # its CPU samples are kept for the next run


def _alerts(tmp: Path) -> list[str]:
    path = tmp / "alerts"
    return path.read_text().splitlines() if path.exists() else []


def test_a_long_running_service_left_stopped_is_raised_and_reaches_the_owner(tmp_path: Path) -> None:
    """2026-11-05 deep dive: a halt stopped the memory guard, the library was started again without a
    reboot, and the guard stayed stopped for 6 days; only the API and the orchestrator were watched,
    and what was watched reached only a Claude session."""
    env = _world(tmp_path, usage=10**9)
    with (tmp_path / "units").open("a") as fh:
        fh.write("whisipedia-yield.service loaded inactive dead yield\n")
        fh.write("whisipedia-trawler.service loaded inactive dead trawler\n")
    jobs = tmp_path / ".local" / "state" / "whisipedia" / "jobs"
    jobs.mkdir(parents=True)
    (jobs / "yield-stopped").write_text("whisipedia-trawler.service\n")  # owed by the guard: jobs-owed
    first = " ".join(_run(env, 0))
    assert "unit-failed:whisipedia-yield" not in first  # a restart is not an outage
    assert "raise --key watch:api-down --severity critical" in " ".join(_alerts(tmp_path))
    later = [line for line in _run(env, 0.5) if "unit-failed:" in line]
    assert len(later) == 1, later
    assert (
        "RAISED unit-failed:whisipedia-yield.service: whisipedia-yield.service is inactive (dead)" in later[0]
    )
    assert "systemctl --user start whisipedia-yield.service" in later[0]
    saved = later[0].split("its journal then: ", 1)[1]
    assert Path(saved).read_text() == "the journal of whisipedia-yield.service\n"
    assert any(
        a.startswith("alerts raise --key watch:unit-failed:whisipedia-yield.service --severity critical")
        for a in _alerts(tmp_path)
    )
    _run(env, 0.7)
    assert sum("watch:unit-failed:whisipedia-yield" in a for a in _alerts(tmp_path)) == 1  # raised once
    rows = (tmp_path / "units").read_text().replace("inactive dead yield", "active running yield")
    (tmp_path / "units").write_text(rows)
    assert any("CLEARED unit-failed:whisipedia-yield.service" in line for line in _run(env, 1))
    assert _alerts(tmp_path)[-1] == "alerts resolve --key watch:unit-failed:whisipedia-yield.service"
    state = json.loads((tmp_path / "state" / "watch" / "state.json").read_text())
    assert state["saved"] == {}


def test_a_halt_stops_the_services_without_an_alert_until_one_of_them_runs_again(tmp_path: Path) -> None:
    """Halts for a game are routine (tools/wsp-shutdown): the stopped services and the silent API are
    the halt's. On 2026-10-30 all but the memory guard were started again by hand: then it counts."""
    env = _world(tmp_path, usage=10**9)
    rows = "".join(
        f"whisipedia-{name}.service loaded inactive dead {name}\n"
        for name in ("api", "orchestrator", "trawler", "yield")
    )
    (tmp_path / "units").write_text(rows)
    halted = tmp_path / ".local" / "state" / "whisipedia" / "halted"
    halted.parent.mkdir(parents=True)
    halted.touch()
    first = " ".join(_run(env, 0))
    assert "RAISED halted" in first
    assert "RAISED api-down" in first  # in the log, as always
    assert "unit-failed" not in " ".join(_run(env, 2))
    assert _alerts(tmp_path) == []  # nothing for the owner: the owner halted it
    (tmp_path / "units").write_text(rows.replace("inactive dead api", "active running api"))
    resumed = " ".join(_run(env, 2.1))
    assert "CLEARED halted" in resumed
    assert "unit-failed" not in resumed  # down from now on, not from the halt
    later = " ".join(_run(env, 2.5))
    assert "RAISED unit-failed:whisipedia-yield.service" in later
    assert "watch:unit-failed:whisipedia-yield.service --severity critical" in " ".join(_alerts(tmp_path))


def test_an_alert_the_cli_could_not_raise_is_tried_again_and_lock_waits_stay_in_the_log(
    tmp_path: Path,
) -> None:
    env = _world(tmp_path, usage=10**9, failed=True)
    (tmp_path / "alert-fail").touch()
    _run(env, 0)
    assert "watch:unit-failed:whisipedia-job-maps.service --severity warning" in " ".join(_alerts(tmp_path))
    (tmp_path / "alert-fail").unlink()
    (tmp_path / "alerts").unlink()
    _run(env, 0.2)
    keys = sorted(a.split()[3] for a in _alerts(tmp_path))
    assert keys == [
        "watch:api-down",
        "watch:files-high:whisipedia-api.service",
        "watch:unit-failed:whisipedia-job-maps.service",
    ]
    _run(env, 9)  # lock-held (ingest, heavy) is open now: a log condition, never an alert
    assert not [a for a in _alerts(tmp_path) if "lock-held" in a]


def _index_build(tmp: Path, *, quiet_h: float) -> Path:
    """pid 43 runs `wsp index build`, holding index and heavy; its run log was last written quiet_h
    before T0."""
    locks, proc = tmp / "locks", tmp / "proc"
    (locks / "index.lock").touch()
    index, heavy = (locks / "index.lock").stat().st_ino, (locks / "heavy.lock").stat().st_ino
    (proc / "43").mkdir(parents=True, exist_ok=True)
    (proc / "43" / "cmdline").write_bytes(
        b"/r/.venv/bin/python3\0/r/.venv/bin/wsp\0--config-dir\0c\0index\0build\0"
    )
    (proc / "43" / "stat").write_text("43 (wsp) S " + " ".join(["0"] * 18) + " 100 0 0\n")
    (proc / "locks").write_text(
        f"1: FLOCK  ADVISORY  WRITE 43 08:30:{heavy} 0 EOF\n"
        f"2: FLOCK  ADVISORY  WRITE 43 08:30:{index} 0 EOF\n"
    )
    log = tmp / "state" / "logs" / "index-build" / "20261102T093000Z-43.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text('{"msg": "run start"}\n{"msg": "rank: 67,109,080/348,642,755 documents scanned"}\n')
    os.utime(log, (T0 - quiet_h * 3600, T0 - quiet_h * 3600))
    return log


def test_the_nightly_build_holds_its_locks_without_noise_and_a_silent_one_is_raised(tmp_path: Path) -> None:
    """2026-11-05 deep dive: lock-held:index and lock-held:heavy were raised as a pair by most nightly
    builds (8 to 10 hours), while the builds that hung on 11-02 and 11-03 looked the same."""
    env = _world(tmp_path, usage=10**9)
    log = _index_build(tmp_path, quiet_h=0)
    _run(env, 0)
    busy = " ".join(_run(env, 9))
    assert "lock-held:index" not in busy  # a normal night
    assert "index-silent" in busy  # but 9 h without a line is not
    assert f"has written nothing to {log} for 9.0 h" in busy
    assert "rank: 67,109,080/348,642,755 documents scanned" in busy
    os.utime(log, (T0 + 14 * 3600, T0 + 14 * 3600))  # writing again
    held = [line for line in _run(env, 14.5) if "lock-held" in line or "index-silent" in line]
    assert [line.split()[1:3] for line in held] == [
        ["RAISED", "lock-held:index:"],
        ["CLEARED", "index-silent"],
    ]
    assert "index and heavy locks held 14.5 h by pid 43: wsp --config-dir c index build" in held[0]


def test_a_frozen_index_build_is_not_silent(tmp_path: Path) -> None:
    env = _world(tmp_path, usage=10**9)
    _index_build(tmp_path, quiet_h=7)
    (tmp_path / "proc" / "43" / "cgroup").write_text("0::/bg.slice/whisipedia-index.service\n")
    (tmp_path / "cgroup" / "bg.slice").mkdir(parents=True)
    (tmp_path / "cgroup" / "bg.slice" / "cgroup.freeze").write_text("1\n")
    assert not [line for line in _run(env, 0) if "index-silent" in line]
    (tmp_path / "cgroup" / "bg.slice" / "cgroup.freeze").write_text("0\n")
    assert [line for line in _run(env, 0.2) if "RAISED index-silent" in line]


def test_a_lanes_own_retries_are_noted_weekly_and_ingest_pending_is_not_a_problem(tmp_path: Path) -> None:
    """2026-11-05 deep dive: 70% of the notes were a lane's retries of a known outage, every day, and
    fetch exit 5 (an earlier fetch waits for its ingest, which the lane runs next) counted as one."""
    env = _world(tmp_path, usage=1)
    logs = tmp_path / "logs"
    logs.mkdir()
    lane = logs / "lane-govinfo.log"
    lane.write_text("")
    _run(env, 0)

    def say(hours: float, *lines: str) -> list[str]:
        with lane.open("a") as fh:
            fh.write("".join(f"{line}\n" for line in lines))
        return [line for line in _run(env, hours) if " NOTE " in line]

    notes = say(0.1, "fetch exit 5", "fetch exit 6", "Traceback (most recent call last):")
    assert len(notes) == 2
    assert not [n for n in notes if "fetch exit 5" in n]
    notes = say(25, "fetch exit 6", "b0002 left for a later run", "Traceback (most recent call last):")
    assert [n.split("lane-govinfo.log: ", 1)[1] for n in notes] == [
        "b0002 left for a later run",  # first seen: noted
        "Traceback (most recent call last):",  # a day later: noted again
    ]
    assert not say(50, "fetch exit 6", "b0003 left for a later run")  # a routine retry, within the week
    assert len(say(7 * 24 + 1, "fetch exit 6")) == 1  # a week on, still waiting: noted again


def test_the_watched_services_are_the_long_running_ones_service_install_enables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sys
    from importlib.machinery import SourceFileLoader
    from importlib.util import module_from_spec, spec_from_loader

    from wsp_cli.service import ENABLE

    loader = SourceFileLoader("wsp_stall", str(ROOT / "tools" / "wsp-stall"))
    spec = spec_from_loader("wsp_stall", loader)
    assert spec is not None
    module = module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "wsp_stall", module)  # its dataclass looks itself up there
    loader.exec_module(module)
    assert sorted(module.RUNNING) == sorted(u for u in ENABLE if u.endswith(".service"))
    assert module.push_severity("unit-failed:whisipedia-yield.service") == "critical"
    assert module.push_severity("unit-failed:whisipedia-weather.service") == "warning"
    assert module.push_severity("lock-held:index") is None
    assert module.push_severity("job-idle:whisipedia-job-x.service") is None
