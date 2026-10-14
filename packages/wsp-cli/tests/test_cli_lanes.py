"""tools/lanes/lane-run.sh against a scripted fake ``wsp`` (audit 2026-10-12 C1, H2, M5, M6).

The fake answers each ``collections fetch`` and ``ingest`` with the next exit code of its script and
records every call, so each test states which fetch and ingest outcomes make an item done.
"""

from __future__ import annotations

import fcntl
import os
import signal
import subprocess
import time
from pathlib import Path

import pytest
from test_cli_autonomy import ROOT

LANE = ROOT / "tools" / "lanes" / "lane-run.sh"

FAKE_WSP = """#!/bin/sh
# fake wsp: pops the next exit code for this command from $FAKE/<command>.codes
echo "$*" >> "$FAKE/calls"
case $1 in collections) kind=fetch ;; ingest) kind=ingest ;; *) exit 99 ;; esac
code=$(head -n 1 "$FAKE/$kind.codes")
tail -n +2 "$FAKE/$kind.codes" > "$FAKE/$kind.rest" && mv "$FAKE/$kind.rest" "$FAKE/$kind.codes"
if [ "$code" = hang ]; then
  trap 'echo "$kind got TERM" >> "$FAKE/calls"; exit 143' TERM
  echo started > "$FAKE/hanging"
  while :; do sleep 0.1; done
fi
case $code in *:*) echo "${code#*:}"; code=${code%%:*} ;; esac  # "CODE:LINE" also prints LINE
exit "${code:-98}"
"""


FAKE_ALERTS = """#!/bin/sh
# fake alert command: records `alerts raise|resolve ...` in $FAKE/alerts
printf '%s\\n' "$*" >> "$FAKE/alerts"
"""


class Lane:
    def __init__(self, tmp_path: Path) -> None:
        self.fake = tmp_path / "fake"
        self.fake.mkdir()
        self.state = tmp_path / "state"
        wsp = self.fake / "wsp"
        wsp.write_text(FAKE_WSP)
        wsp.chmod(0o755)
        alerts = self.fake / "alerts-cmd"
        alerts.write_text(FAKE_ALERTS)
        alerts.chmod(0o755)
        self.env = {
            "PATH": "/usr/bin:/bin",
            "HOME": str(tmp_path),
            "FAKE": str(self.fake),
            "WSP": str(wsp),
            "LANE_ALERT_CMD": str(alerts),
            "WSP_LANE_STATE": str(self.state),
            "LANE_LISTING_WAIT": "0",
            "LANE_PARTIAL_WAIT": "0",
            "LANE_BUSY_WAIT": "0",
        }

    def script(self, fetch: list[int | str], ingest: list[int | str]) -> None:
        (self.fake / "fetch.codes").write_text("".join(f"{c}\n" for c in fetch))
        (self.fake / "ingest.codes").write_text("".join(f"{c}\n" for c in ingest))

    def run(self, *items: str, source: str = "-", **env: str) -> int:
        argv = [str(LANE), "lane-t", source, *items]
        done = subprocess.run(argv, env={**self.env, **env}, capture_output=True, text=True, timeout=60)
        return done.returncode

    def calls(self) -> list[str]:
        path = self.fake / "calls"
        return path.read_text().splitlines() if path.exists() else []

    def alerts(self) -> list[str]:
        path = self.fake / "alerts"
        return path.read_text().splitlines() if path.exists() else []

    def done(self) -> list[str]:
        return (self.state / "lane-t.done").read_text().split()

    def cycle(self, item: str) -> str:
        lines = [ln.split() for ln in (self.state / "lane-t.cycles").read_text().splitlines()]
        return [c for i, c in lines if i == item][-1]


@pytest.fixture
def lane(tmp_path: Path) -> Lane:
    return Lane(tmp_path)


def test_complete_fetch_and_ingest_of_this_cycle_is_done(lane: Lane) -> None:
    lane.script(fetch=[0], ingest=[0])
    assert lane.run("neets") == 0
    assert lane.done() == ["neets"]
    cyc = lane.cycle("neets")
    assert lane.calls() == [
        f"collections fetch --source neets --version {cyc}",
        f"ingest --source neets --version {cyc}",  # this cycle only, never the whole source
    ]
    lane.script(fetch=[], ingest=[])
    assert lane.run("neets") == 0  # done items are skipped
    assert len(lane.calls()) == 2


@pytest.mark.parametrize("code", [1, 2, 137, 143])
def test_a_crashed_refused_or_killed_fetch_is_never_done_nor_ingested(lane: Lane, code: int) -> None:
    lane.script(fetch=[code], ingest=[0])
    assert lane.run("borden-institute") == 1
    assert lane.done() == []
    assert [c.split()[0] for c in lane.calls()] == ["collections"], "nothing ingested"


def test_nothing_new_is_done_when_nothing_was_planned(lane: Lane) -> None:
    lane.script(fetch=[8], ingest=[8])
    assert lane.run("neets") == 0
    assert lane.done() == ["neets"]


def test_a_complete_fetch_whose_ingest_finds_no_plan_is_not_done(lane: Lane) -> None:
    lane.script(fetch=[0], ingest=[8])  # the fetch said it wrote a plan: where is it?
    assert lane.run("neets") == 1
    assert lane.done() == []


def test_a_partial_fetch_is_ingested_then_fetched_again_in_a_new_cycle(lane: Lane) -> None:
    lane.script(fetch=[6, 0], ingest=[0, 0])
    assert lane.run("b0001", source="cia-reading-room") == 0
    first = lane.calls()[0].split("--version ")[1]
    assert lane.calls() == [
        f"collections fetch --source cia-reading-room --partition b0001 --version {first}",
        f"ingest --source cia-reading-room --partition b0001 --version {first}",
        f"collections fetch --source cia-reading-room --partition b0001 --version {first}r2",
        f"ingest --source cia-reading-room --partition b0001 --version {first}r2",
    ]
    assert lane.done() == ["b0001"]
    assert lane.cycle("b0001") == f"{first}r2"


def test_a_sealed_cycle_is_followed_to_the_one_the_fetch_used(lane: Lane) -> None:
    # The fetch moves off a sealed cycle by itself and says so; the ingest must name the version fetched.
    lane.script(
        fetch=["0:cia-reading-room: 2026-10-12.b0001 is already sealed; fetching as 2026-10-12r2.b0001"],
        ingest=[0],
    )
    assert lane.run("b0001", source="cia-reading-room") == 0
    assert lane.calls()[-1].endswith("--partition b0001 --version 2026-10-12r2")
    assert lane.cycle("b0001") == "2026-10-12r2"
    assert lane.done() == ["b0001"]


def test_an_earlier_fetch_awaiting_ingest_is_ingested_by_name_first(lane: Lane) -> None:
    waiting = (
        "cia-reading-room: fetched but not ingested yet: 2026-10-01.b0001; "
        "run `wsp ingest --source cia-reading-room` first (nothing fetched)"
    )
    lane.script(fetch=[f"5:{waiting}", 0], ingest=[0, 0])
    assert lane.run("b0001", source="cia-reading-room") == 0
    calls = lane.calls()
    assert calls[1] == "ingest --source cia-reading-room --partition b0001 --version 2026-10-01"
    assert calls[2].startswith("collections fetch --source cia-reading-room --partition b0001")
    assert lane.done() == ["b0001"]


def test_downloads_that_keep_failing_leave_the_item_for_later(lane: Lane) -> None:
    lane.script(fetch=[6, 6, 6], ingest=[0, 8, 8])
    assert lane.run("uk-resilience-guidance", LANE_PARTIAL_TRIES="3") == 1
    assert lane.done() == []
    cycles = [c.split("--version ")[1] for c in lane.calls() if c.startswith("collections")]
    base = cycles[0]
    assert cycles == [base, f"{base}r2", f"{base}r2"], "a new cycle only after one was sealed"


def test_an_incomplete_listing_is_retried_in_the_same_cycle(lane: Lane) -> None:
    lane.script(fetch=[4, 4, 0], ingest=[0])
    assert lane.run("osti-doe-reports") == 0
    cycles = {c.split("--version ")[1] for c in lane.calls()}
    assert len(cycles) == 1


def test_an_interrupted_ingest_resumes_without_fetching_again(lane: Lane) -> None:
    lane.script(fetch=[0], ingest=[3])  # another ingest holds the lock past the lane's patience
    assert lane.run("faa-handbooks", LANE_BUSY_TRIES="1") == 1
    assert lane.done() == []
    lane.script(fetch=[], ingest=[0])
    assert lane.run("faa-handbooks") == 0
    assert [c.split()[0] for c in lane.calls()] == ["collections", "ingest", "ingest"]
    assert lane.done() == ["faa-handbooks"]


def test_a_failed_ingest_fetches_the_cycle_again_next_time(lane: Lane) -> None:
    lane.script(fetch=[0], ingest=[1])
    assert lane.run("who-iris") == 1
    lane.script(fetch=[0], ingest=[0])
    assert lane.run("who-iris") == 0
    kinds = [c.split()[0] for c in lane.calls()]
    assert kinds == ["collections", "ingest", "collections", "ingest"]
    assert len({c.split("--version ")[1] for c in lane.calls()}) == 1, "same cycle: re-staging is safe (H2)"


def test_two_lanes_never_work_on_one_source_at_once(lane: Lane) -> None:
    lane.script(fetch=[0], ingest=[0])
    locks = lane.state / "locks"
    locks.mkdir(parents=True)
    fd = os.open(locks / "fema-building-science.lock", os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)  # the other lane
        assert lane.run("fema-building-science") == 1
        assert lane.calls() == []
    finally:
        os.close(fd)
    assert lane.run("fema-building-science") == 0


def test_sigterm_reaches_the_running_fetch_and_stops_the_lane(lane: Lane) -> None:
    lane.script(fetch=["hang", 0], ingest=[0, 0])
    proc = subprocess.Popen([str(LANE), "lane-t", "-", "neets", "cia-csi"], env=lane.env)
    deadline = time.monotonic() + 30
    while not (lane.fake / "hanging").exists():
        assert time.monotonic() < deadline, "the fake fetch never started"
        time.sleep(0.05)
    proc.send_signal(signal.SIGTERM)  # what systemd sends the unit's main process
    assert proc.wait(timeout=30) == 143
    assert "fetch got TERM" in lane.calls()
    assert lane.done() == []
    assert not any("cia-csi" in c for c in lane.calls()), "the lane stopped instead of going on"


# --- tools/lanes/rebuild-resume.sh (audit M2) ------------------------------------------------------------

FAKE_INDEX_WSP = """#!/bin/sh
# fake wsp for the rebuild: exit code from $FAKE/codes/<source, rank or spell> (default 0)
echo "$*" >> "$FAKE/calls"
case $2 in build) key=${6:-} ;; *) key=$2 ;; esac
code=$(cat "$FAKE/codes/$key" 2>/dev/null)
exit "${code:-0}"
"""


def _rebuild(tmp_path: Path, codes: dict[str, int], *flags: str) -> tuple[int, str, list[str]]:
    fake = tmp_path / "fake"
    (fake / "codes").mkdir(parents=True, exist_ok=True)
    for path in (fake / "codes").iterdir():
        path.unlink()
    for key, code in codes.items():
        (fake / "codes" / key).write_text(str(code))
    wsp = fake / "wsp"
    wsp.write_text(FAKE_INDEX_WSP)
    wsp.chmod(0o755)
    systemctl = fake / "systemctl"  # never the real one: --enable-timer would enable the live timer
    systemctl.write_text('#!/bin/sh\necho "systemctl $*" >> "$FAKE/calls"\n')
    systemctl.chmod(0o755)
    env = {
        "PATH": f"{fake}:/usr/bin:/bin",
        "FAKE": str(fake),
        "WSP": str(wsp),
        "REBUILD_BUSY_WAIT": "0",
        "REBUILD_BUSY_TRIES": "3",
    }
    script = ROOT / "tools" / "lanes" / "rebuild-resume.sh"
    argv = [str(script), *flags, str(tmp_path / "rebuild.done"), "mdwiki", "courtlistener", "pubmed"]
    done = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=60)
    calls = (fake / "calls").read_text().splitlines() if (fake / "calls").exists() else []
    (fake / "calls").unlink(missing_ok=True)
    return done.returncode, done.stdout, calls


def test_a_failed_build_step_stops_rank_and_spell(tmp_path: Path) -> None:
    code, out, calls = _rebuild(tmp_path, {"courtlistener": 137})  # OOM-killed
    assert code == 1
    assert "all done" not in out
    assert "build failed for: courtlistener" in out
    assert [c.split()[-1] for c in calls] == ["mdwiki", "courtlistener", "pubmed"], "no rank, no spell"
    done = (tmp_path / "rebuild.done").read_text().split()
    assert done == ["build:mdwiki", "build:pubmed"]
    code, out, calls = _rebuild(tmp_path, {})  # fixed: only the missing build, then rank and spell
    assert code == 0
    assert "all done" in out
    assert calls == [
        "index build --no-rank --no-spell --source courtlistener",
        "index rank",
        "index spell",
    ]


def test_a_busy_lock_is_waited_for_but_not_forever(tmp_path: Path) -> None:
    code, out, calls = _rebuild(tmp_path, {"rank": 3})
    assert code == 1
    assert "NOT done: rank failed" in out
    assert "all done" not in out
    assert calls.count("index rank") == 3, "retried while busy, then given up"
    assert "index spell" not in calls


# --- tools/lanes/resume-after-reboot.sh (audit M6) -------------------------------------------------------


def _resume(tmp_path: Path, mount: str, lanes: str) -> tuple[int, str, list[str]]:
    fakebin = tmp_path / "bin"
    fakebin.mkdir(exist_ok=True)
    started = tmp_path / "started"
    fake = fakebin / "systemd-run"
    fake.write_text(f'#!/bin/sh\necho "$*" >> {started}\n')
    fake.chmod(0o755)
    (tmp_path / "state").mkdir(exist_ok=True)
    (tmp_path / "state" / "lanes.conf").write_text(lanes)
    env = {
        "PATH": f"{fakebin}:/usr/bin:/bin",
        "HOME": str(tmp_path),
        "WSP_STATE": str(tmp_path / "state"),
        "WSP_NAS_MOUNT": mount,
        "WSP_NAS_WAIT": "2",
        "WSP_NAS_POLL": "1",
        "WSP_RESUME_SETTLE": "0",
        "WSP_BG_SLICE_HIGH": "7G",
    }
    script = ROOT / "tools" / "lanes" / "resume-after-reboot.sh"
    done = subprocess.run([str(script)], env=env, capture_output=True, text=True, timeout=60)
    lines = started.read_text().splitlines() if started.exists() else []
    return done.returncode, done.stderr, lines


def test_resume_waits_for_the_nas_and_fails_loudly_if_it_never_mounts(tmp_path: Path) -> None:
    code, err, started = _resume(tmp_path, str(tmp_path / "not-a-mount"), "u lane - neets\n")
    assert code == 1
    assert "<2>CRITICAL" in err
    assert "not mounted" in err
    assert started == [], "nothing resumed without the NAS"


def test_resume_starts_each_lane_in_the_fetch_budget(tmp_path: Path) -> None:
    lanes = (
        "# unit lane source items\n"
        "collections-cia lane-cia cia-reading-room b0001 b0002\n"
        "collections-retry lane-retry - fema-building-science borden-institute\n"
        "collections-refetch3 lane-refetch3 - fema-building-science  # listed twice\n"
    )
    code, err, started = _resume(tmp_path, "/", lanes)
    assert code == 0, err
    assert "fema-building-science" in err  # the duplicate is reported
    assert len(started) == 3
    assert all("--slice=whisipedia-background-fetch.slice" in s for s in started)
    assert started[0].endswith("tools/lanes/lane-run.sh lane-cia cia-reading-room b0001 b0002")


def test_the_timer_is_enabled_once_after_everything_is_done(tmp_path: Path) -> None:
    code, _, calls = _rebuild(tmp_path, {}, "--enable-timer")
    assert code == 0
    assert calls[-1] == "systemctl --user enable --now whisipedia-index.timer"
    code, _, calls = _rebuild(tmp_path, {}, "--enable-timer")
    assert code == 0
    assert calls == [], "every step, the timer included, was done already"


def test_a_failed_ingest_raises_a_critical_alert_and_success_resolves_it(tmp_path: Path) -> None:
    """2026-10-14: an ingest stopped on an interrupted adoption and nobody heard of it for hours."""
    lane = Lane(tmp_path)
    lane.script(fetch=[0], ingest=["1:VaultError: an interrupted adoption left x.adopting"])
    assert lane.run("osti") == 1
    raised = [a for a in lane.alerts() if a.startswith("alerts raise")]
    assert len(raised) == 1
    assert "--key lane:lane-t:osti --severity critical" in raised[0]
    assert "VaultError: an interrupted adoption left x.adopting" in raised[0]
    lane.script(fetch=[0], ingest=[0])
    assert lane.run("osti") == 0
    assert lane.alerts()[-1] == "alerts resolve --key lane:lane-t:osti"


def test_downloads_that_keep_failing_raise_a_warning(tmp_path: Path) -> None:
    lane = Lane(tmp_path)
    lane.script(fetch=[6, 6, 6], ingest=[0, 0, 0])
    assert lane.run("usgs", LANE_PARTIAL_TRIES="3") == 1
    raised = [a for a in lane.alerts() if a.startswith("alerts raise")]
    assert len(raised) == 1
    assert "--severity warning" in raised[0]
    assert "downloads still failing after 3 tries" in raised[0]
