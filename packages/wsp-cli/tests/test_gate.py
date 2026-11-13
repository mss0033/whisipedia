"""tools/gate against fake ruff, pyright, python, npm, systemctl and systemd-run (trawl 2026-11-10,
ops-tests-docs-13).

Each test copies the script into a small git repository of its own, so the stamp and the dirty-tree
rules are tested against real git, and nothing here runs the real gate.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from test_cli_autonomy import ROOT

GATE = ROOT / "tools" / "gate"

# Every fake records its call. The Python fake answers for the test directory it is given (the last
# argument): $FAKE/rc/<dir with _ for /> holds the exit code, $FAKE/out/<...> the output; without
# them it "passes at 100%". $FAKE/hook, if there, runs before it answers (something happens mid-run).
FAKES = {
    "ruff": """#!/bin/sh
echo "ruff $*" >> "$FAKE/calls"
exit "$(cat "$FAKE/rc/ruff-$1" 2>/dev/null || echo 0)"
""",
    "pyright": """#!/bin/sh
echo "pyright $*" >> "$FAKE/calls"
exit "$(cat "$FAKE/rc/pyright" 2>/dev/null || echo 0)"
""",
    "python": """#!/bin/sh
echo "python $*" >> "$FAKE/calls"
for last; do :; done
key=$(printf %s "$last" | tr / _)
[ -x "$FAKE/hook" ] && "$FAKE/hook" "$last"
if [ -f "$FAKE/out/$key" ]; then cat "$FAKE/out/$key"; else printf '..... [100%%]\n5 passed in 0.01s\n'; fi
exit "$(cat "$FAKE/rc/$key" 2>/dev/null || echo 0)"
""",
    "npm": '#!/bin/sh\necho "npm $*" >> "$FAKE/calls"\nexit "$(cat "$FAKE/rc/npm" 2>/dev/null || echo 0)"\n',
    # systemd-run: records its own options, then runs what follows `--` (nice, timeout, the step).
    "systemd-run": """#!/bin/sh
[ -f "$FAKE/rc/systemd-run" ] && { echo "Failed to start transient scope unit"; exit 1; }
opts=""
while [ "$1" != -- ]; do opts="$opts $1"; shift; done
shift
echo "systemd-run$opts" >> "$FAKE/calls"
exec "$@"
""",
    "systemctl": """#!/bin/sh
echo "systemctl $*" >> "$FAKE/calls"
case "$*" in
  *whisipedia-index.service*ActiveState*) cat "$FAKE/index-state" ;;
  *WorkingDirectory*whisipedia-api.service*) cat "$FAKE/live" 2>/dev/null ;;
esac
case "$*" in *" show "*) exit "$(cat "$FAKE/rc/systemctl-show" 2>/dev/null || echo 0)" ;; esac
exit 0
""",
}

GIT_ENV = {
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.invalid",
}


class Gate:
    def __init__(self, tmp_path: Path) -> None:
        self.repo = tmp_path / "repo"
        self.fake = tmp_path / "fake"
        self.state = tmp_path / "state"
        self.tmp = tmp_path / "tmp"
        for d in (self.fake / "rc", self.fake / "out", self.tmp, self.repo / "tools"):
            d.mkdir(parents=True)
        for name, body in FAKES.items():
            path = self.fake / name
            path.write_text(body)
            path.chmod(0o755)
        (self.fake / "index-state").write_text("inactive\n")
        (self.fake / "meminfo").write_text("MemTotal: 16000000 kB\nMemAvailable:    8000000 kB\n")
        shutil.copy(GATE, self.repo / "tools" / "gate")
        for d in ("packages/wsp-a/tests", "packages/wsp-b/tests", "tests", "navigator"):
            (self.repo / d).mkdir(parents=True)
            (self.repo / d / "keep.txt").write_text("x\n")
        self.env = {
            "PATH": "/usr/bin:/bin",
            "HOME": str(tmp_path),
            "TMPDIR": str(self.tmp),
            "FAKE": str(self.fake),
            "GATE_BIN": str(self.fake),
            "GATE_SYSTEMD_RUN": str(self.fake / "systemd-run"),
            "GATE_SYSTEMCTL": str(self.fake / "systemctl"),
            "GATE_NPM": str(self.fake / "npm"),
            "GATE_MEMINFO": str(self.fake / "meminfo"),
            "WSP_STATE": str(self.state),
            **GIT_ENV,
        }
        self.git("init", "-q", "-b", "main")
        self.commit("first")

    def git(self, *args: str) -> str:
        done = subprocess.run(
            ["git", *args], cwd=self.repo, env=self.env, capture_output=True, text=True, check=True
        )
        return done.stdout.strip()

    def commit(self, message: str) -> str:
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)
        return self.git("rev-parse", "HEAD")

    def answer(self, test_dir: str, rc: int, out: str = "..... [100%]\n5 passed in 0.01s\n") -> None:
        key = test_dir.replace("/", "_")
        (self.fake / "rc" / key).write_text(f"{rc}\n")
        (self.fake / "out" / key).write_text(out)

    def hook(self, body: str) -> None:
        path = self.fake / "hook"
        path.write_text("#!/bin/sh\n" + body)
        path.chmod(0o755)

    def run(self, *args: str, **env: str) -> tuple[int, str]:
        """Exit code and the last line of stdout (the `gate:` line)."""
        done = subprocess.run(
            [str(self.repo / "tools" / "gate"), *args],
            env={**self.env, **env},
            cwd=os.sep,  # the gate must not depend on where it is called from
            capture_output=True,
            text=True,
            timeout=120,
        )
        lines = done.stdout.strip().splitlines()
        return done.returncode, lines[-1] if lines else ""

    def calls(self, prefix: str = "") -> list[str]:
        path = self.fake / "calls"
        lines = path.read_text().splitlines() if path.exists() else []
        return [line for line in lines if line.startswith(prefix)]

    def stamps(self) -> list[str]:
        return sorted(p.name for p in (self.state / "gate").glob("*.ok*"))

    def logs(self) -> list[Path]:
        return sorted(self.tmp.glob("wsp-gate.*"))


@pytest.fixture
def gate(tmp_path: Path) -> Gate:
    return Gate(tmp_path)


def test_a_clean_run_passes_and_writes_the_stamp_for_head(gate: Gate) -> None:
    head = gate.git("rev-parse", "HEAD")

    code, line = gate.run()

    assert code == 0, line
    assert line.startswith(f"gate: PASS {head[:12]} ruff pyright pytest 3/3 in ")
    assert gate.stamps() == [f"{head}.ok"]
    stamp = (gate.state / "gate" / f"{head}.ok").read_text()
    assert line in stamp
    # On the base branch itself there is nothing to compare navigator/ with: said, not "not needed".
    assert line.endswith("(navigator not checked: on main)")
    assert "navigator: not-checked" in stamp
    assert "directories: packages/wsp-a/tests packages/wsp-b/tests tests\n" in stamp
    assert "seams: GATE_BIN GATE_SYSTEMD_RUN GATE_SYSTEMCTL GATE_NPM GATE_MEMINFO\n" in stamp
    assert gate.calls("ruff") == ["ruff format --check .", "ruff check ."]
    tested = [c.rsplit(" ", 1)[1] for c in gate.calls("python")]
    assert tested == ["packages/wsp-a/tests", "packages/wsp-b/tests", "tests"]
    assert all("-m pytest -m not network" in c for c in gate.calls("python"))
    assert gate.logs() == []  # removed on PASS


def test_every_heavy_step_runs_in_a_capped_scope_that_is_stopped_afterwards(gate: Gate) -> None:
    gate.run()

    scopes = gate.calls("systemd-run")
    assert len(scopes) == 4  # pyright and three test directories
    for scope in scopes:
        assert "--user --scope" in scope
        assert "MemoryMax=4G" in scope
        assert "MemorySwapMax=0" in scope
    units = [word.removeprefix("--unit=") for scope in scopes for word in scope.split() if "--unit=" in word]
    assert len(set(units)) == 4
    # `timeout` ends only pytest itself: whatever it left behind goes with the scope.
    assert gate.calls("systemctl --user stop") == [f"systemctl --user stop {u}.scope" for u in units]


def test_a_failing_package_fails_the_gate_and_the_others_still_run(gate: Gate) -> None:
    gate.answer("packages/wsp-a/tests", 1, "..F.. [100%]\n1 failed, 4 passed in 0.01s\n")

    code, line = gate.run()

    assert code == 1
    assert line.startswith("gate: FAIL ")
    assert "pytest packages/wsp-a/tests (rc 1, log " in line
    assert "wsp-b" not in line
    assert len(gate.calls("python")) == 3
    assert gate.stamps() == []
    assert len(gate.logs()) == 1  # kept for reading
    assert "..F.." in (gate.logs()[0] / "pytest-packages_wsp-a_tests.log").read_text()


def test_a_failing_ruff_or_pyright_fails_the_gate_and_pytest_still_runs(gate: Gate) -> None:
    (gate.fake / "rc" / "ruff-check").write_text("1\n")
    (gate.fake / "rc" / "pyright").write_text("1\n")

    code, line = gate.run()

    assert code == 1
    assert "ruff-check (rc 1" in line
    assert "pyright (rc 1" in line
    assert len(gate.calls("python")) == 3


def test_exit_0_without_100_percent_is_a_failure(gate: Gate) -> None:
    gate.answer("packages/wsp-b/tests", 0, "........ [ 37%]\n........ [ 74%]\n")

    code, line = gate.run()

    assert code == 1
    assert "pytest packages/wsp-b/tests (exit 0 but the log ends at [74%]" in line
    assert gate.stamps() == []


def test_exit_5_nothing_collected_is_a_pass_only_where_it_is_expected(gate: Gate) -> None:
    gate.answer("tests", 5, "no tests ran\n")  # only network-marked tests live there
    assert gate.run()[0] == 0

    gate.answer("packages/wsp-a/tests", 5, "no tests ran\n")  # a real package lost its tests
    code, line = gate.run()

    assert code == 1
    assert "pytest packages/wsp-a/tests (no test collected" in line


def test_a_killed_run_is_named_as_memory_and_a_timeout_as_timed_out(gate: Gate) -> None:
    gate.answer("packages/wsp-a/tests", 137, "")
    gate.answer("packages/wsp-b/tests", 124, "")

    code, line = gate.run()

    assert code == 1
    assert "pytest packages/wsp-a/tests (killed (memory" in line
    assert "pytest packages/wsp-b/tests (timed out after 1800 s" in line


def test_a_directory_that_hangs_is_ended_by_the_time_limit(gate: Gate) -> None:
    gate.hook("case $1 in packages/wsp-a/tests) exec sleep 30 ;; esac\n")

    code, line = gate.run(GATE_PKG_TIMEOUT="1")

    assert code == 1
    assert "pytest packages/wsp-a/tests (timed out after 1 s" in line
    assert len(gate.calls("python")) == 3


@pytest.mark.parametrize("state", ["active", "activating", "deactivating", "reloading", "maintenance"])
def test_the_gate_does_not_run_while_the_index_build_is_active_or_activating(gate: Gate, state: str) -> None:
    (gate.fake / "index-state").write_text(state + "\n")

    code, line = gate.run()

    assert code == 75
    after = f"stopped after: ruff-format ruff-check; logs {gate.logs()[0]}"
    assert line == f"gate: NOT RUN (the index build is {state}; {after})"
    assert gate.calls("python") == []
    assert gate.calls("pyright") == []
    assert gate.stamps() == []


def test_an_index_build_that_starts_during_the_gate_stops_it(gate: Gate) -> None:
    gate.hook('echo activating > "$FAKE/index-state"\n')  # the 04:30 build starts under the first directory

    code, line = gate.run()

    assert code == 75
    assert line.startswith("gate: NOT RUN (the index build is activating; stopped after: ")
    assert len(gate.calls("python")) == 1
    assert gate.stamps() == []


def test_the_gate_does_not_run_under_5_gb_available(gate: Gate) -> None:
    (gate.fake / "meminfo").write_text("MemTotal: 16000000 kB\nMemAvailable:    5000000 kB\n")

    code, line = gate.run()

    assert code == 75
    assert "MemAvailable is 4882 MB, the gate needs over 5120 MB" in line
    assert gate.calls("python") == []

    assert gate.run(GATE_MIN_AVAIL_MB="4608")[0] == 0


def test_a_dirty_tree_passes_without_a_stamp(gate: Gate) -> None:
    (gate.repo / "tests" / "keep.txt").write_text("changed\n")

    code, line = gate.run()

    assert code == 0
    assert line.startswith("gate: PASS ")
    assert line.endswith("(uncommitted or untracked files: no stamp)")
    assert gate.stamps() == []


def test_a_file_never_added_to_git_blocks_the_stamp(gate: Gate) -> None:
    """The tests passed with a module the commit does not hold: the stamp would vouch for less."""
    (gate.repo / "packages" / "wsp-a" / "new_module.py").write_text("x = 1\n")

    code, line = gate.run()

    assert code == 0
    assert line.endswith("(uncommitted or untracked files: no stamp)")
    assert gate.stamps() == []


def test_a_commit_made_during_the_run_gets_no_stamp(gate: Gate) -> None:
    first = gate.git("rev-parse", "HEAD")
    gate.hook(
        f'cd "{gate.repo}" && [ ! -f later.txt ] && echo x > later.txt && git add later.txt'
        " && git commit -q -m later\nexit 0\n"
    )

    code, line = gate.run()

    assert gate.git("rev-parse", "HEAD") != first
    assert code == 1
    assert line.startswith(f"gate: FAIL {first[:12]}: the checkout changed during the run")
    assert gate.stamps() == []


def test_a_file_edited_during_the_run_gets_no_stamp(gate: Gate) -> None:
    gate.hook(f'echo edited > "{gate.repo}/tests/keep.txt"\n')

    code, line = gate.run()

    assert code == 1
    assert "the checkout changed during the run" in line
    assert gate.stamps() == []


def test_only_runs_the_named_directories_and_never_stamps(gate: Gate) -> None:
    code, line = gate.run("--only", "packages/wsp-b/tests")

    assert code == 0
    assert "pytest 1/1" in line
    assert line.endswith("(--only: no stamp)")
    assert [c.rsplit(" ", 1)[1] for c in gate.calls("python")] == ["packages/wsp-b/tests"]
    assert gate.stamps() == []

    code, line = gate.run("--only", "packages/nothing/tests")
    assert code == 1
    assert "pytest packages/nothing/tests (no such path)" in line


def test_pyright_gets_the_checkouts_own_python(gate: Gate) -> None:
    gate.run()

    assert gate.calls("pyright") == [f"pyright --pythonpath {gate.fake}/python"]


def test_the_navigator_step_runs_capped_when_the_branch_changed_it(gate: Gate) -> None:
    (gate.repo / "navigator" / "node_modules").mkdir()
    (gate.repo / ".gitignore").write_text("node_modules/\n")
    gate.commit("ignore")
    assert gate.run()[0] == 0
    assert gate.calls("npm") == []  # main itself: nothing differs

    gate.git("checkout", "-q", "-b", "work")
    (gate.repo / "navigator" / "keep.txt").write_text("changed\n")
    head = gate.commit("navigator change")
    code, line = gate.run()

    assert code == 0, line
    assert " navigator in " in line
    assert gate.calls("npm") == ["npm --prefix navigator run check"]
    assert len(gate.calls("systemd-run")) == 4 + 5  # the Navigator step is capped too
    assert "navigator: ran (changed)" in (gate.state / "gate" / f"{head}.ok").read_text()

    (gate.fake / "rc" / "npm").write_text("1\n")
    code, line = gate.run()
    assert code == 1
    assert "navigator (rc 1" in line


def test_navigator_without_node_modules_says_what_to_run(gate: Gate) -> None:
    code, line = gate.run("--navigator")

    assert code == 1
    assert "run npm ci in navigator" in line
    assert gate.calls("npm") == []


def test_a_base_that_cannot_be_compared_is_a_failure_not_no_change(gate: Gate) -> None:
    code, line = gate.run(GATE_BASE="no-such-branch")

    assert code == 1
    assert "navigator-diff (rc " in line
    assert gate.stamps() == []


def test_the_navigator_step_is_refused_in_the_checkout_the_api_serves_from(gate: Gate) -> None:
    """`npm run check` builds navigator/dist; in the live checkout readers would get that build."""
    (gate.repo / "navigator" / "node_modules").mkdir()
    (gate.fake / "live").write_text(f"{gate.repo}\n")

    code, line = gate.run("--navigator")

    assert code == 2
    assert line.startswith("gate: NOT RUN (the Navigator step builds navigator/dist")
    assert gate.calls("npm") == []
    assert gate.calls("ruff") == []

    assert gate.run()[0] == 0  # without the Navigator step the live checkout can be gated


def test_old_stamps_and_old_failed_logs_are_removed(gate: Gate) -> None:
    stamps = gate.state / "gate"
    stamps.mkdir(parents=True)
    (stamps / "dead.ok.tmp.123").write_text("a killed gate's\n")
    for i in range(4):
        old = stamps / f"{i:040d}.ok"
        old.write_text("old\n")
        os.utime(old, (1_000_000 + i, 1_000_000 + i))
    stale = gate.tmp / "wsp-gate.stale"
    stale.mkdir()
    os.utime(stale, (1_000_000, 1_000_000))
    recent = gate.tmp / "wsp-gate.recent"
    recent.mkdir()
    head = gate.git("rev-parse", "HEAD")

    assert gate.run(GATE_KEEP_STAMPS="3")[0] == 0

    assert gate.stamps() == sorted([f"{head}.ok", f"{2:040d}.ok", f"{3:040d}.ok"])
    assert not stale.exists()
    assert recent.exists()


def test_an_unknown_argument_is_refused_before_anything_runs(gate: Gate) -> None:
    done = subprocess.run(
        [str(gate.repo / "tools" / "gate"), "--fast"], env=gate.env, capture_output=True, text=True
    )

    assert done.returncode == 2
    assert "unknown argument" in done.stderr
    assert gate.calls() == []


def test_a_failure_hidden_behind_exit_0_at_100_percent_is_a_failure(gate: Gate) -> None:
    """A process that leaves with 0 after the last test (teardown, atexit) hides the F above it."""
    gate.answer("packages/wsp-a/tests", 0, "..F.. [100%]\n1 failed, 4 passed in 0.01s\n")
    gate.answer("packages/wsp-b/tests", 0, "..... [100%]\n")  # no count at all

    code, line = gate.run()

    assert code == 1
    assert "pytest packages/wsp-a/tests (exit 0 but the log ends with: 1 failed, 4 passed" in line
    assert "pytest packages/wsp-b/tests (exit 0 but the log does not end with pytest's count" in line


def test_an_expected_failure_in_the_count_is_not_a_failure(gate: Gate) -> None:
    """`4 passed, 1 xfailed` holds the word "failed": the gate refused it (verification of 7a)."""
    gate.answer("packages/wsp-a/tests", 0, "....x [100%]\n4 passed, 1 xfailed in 0.01s\n")
    gate.answer("packages/wsp-b/tests", 0, "....E [100%]\n4 passed, 1 error in 0.01s\n")

    code, line = gate.run()

    assert code == 1
    assert "packages/wsp-a/tests" not in line
    assert "pytest packages/wsp-b/tests (exit 0 but the log ends with: 4 passed, 1 error" in line


def test_the_callers_git_and_python_settings_do_not_reach_the_gate(gate: Gate, tmp_path: Path) -> None:
    """With GIT_DIR and GIT_WORK_TREE of another repository the stamp named that repository's commit
    while this checkout's steps ran; with PYTHONPATH the tests import another checkout's packages."""
    head = gate.git("rev-parse", "HEAD")
    other = tmp_path / "other"
    other.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=other, env=gate.env, check=True)
    subprocess.run(
        ["git", "commit", "-q", "--allow-empty", "-m", "another"], cwd=other, env=gate.env, check=True
    )
    gate.hook(
        'printf "%s|%s|%s\\n" "${PYTHONPATH-unset}" "${GIT_DIR-unset}" "${PYTEST_ADDOPTS-unset}"'
        ' >> "$FAKE/seen"\n'
    )

    code, line = gate.run(
        GIT_DIR=str(other / ".git"), GIT_WORK_TREE=str(other), PYTHONPATH=str(other), PYTEST_ADDOPTS="-k one"
    )

    assert code == 0, line
    assert gate.stamps() == [f"{head}.ok"]
    assert set((gate.fake / "seen").read_text().split()) == {"unset|unset|unset"}


def test_a_scope_that_cannot_be_started_fails_the_step(gate: Gate) -> None:
    (gate.fake / "rc" / "systemd-run").write_text("1\n")

    code, line = gate.run()

    assert code == 1
    assert "pyright (rc 1" in line
    assert "pytest tests (rc 1" in line
    assert gate.calls("python") == []
    assert gate.stamps() == []


def test_an_index_state_or_memory_that_cannot_be_read_stops_the_gate(gate: Gate) -> None:
    (gate.fake / "rc" / "systemctl-show").write_text("1\n")
    code, line = gate.run()
    assert code == 75
    assert "cannot read the state of whisipedia-index.service" in line

    (gate.fake / "rc" / "systemctl-show").unlink()
    (gate.fake / "index-state").write_text("")
    code, line = gate.run()
    assert code == 75
    assert "cannot read the state of whisipedia-index.service" in line

    (gate.fake / "index-state").write_text("failed\n")  # last night's build failed: it is not running
    assert gate.run()[0] == 0

    (gate.fake / "meminfo").write_text("MemTotal: 16000000 kB\n")
    code, line = gate.run()
    assert code == 75
    assert "cannot read MemAvailable" in line


def test_a_changed_directory_list_never_stamps(gate: Gate) -> None:
    code, line = gate.run(GATE_DIRS="packages/wsp-b/tests")
    assert code == 0
    assert "pytest 1/1" in line
    assert "(set: GATE_DIRS: no stamp)" in line

    gate.answer("packages/wsp-a/tests", 5, "no tests ran\n")
    code, line = gate.run(GATE_EMPTY_OK="tests packages/wsp-a/tests")
    assert code == 0
    assert "(set: GATE_EMPTY_OK: no stamp)" in line
    assert gate.stamps() == []


def test_a_second_gate_does_not_run_beside_the_first(gate: Gate) -> None:
    import fcntl

    (gate.state / "gate").mkdir(parents=True)
    with (gate.state / "gate" / ".lock").open("w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        code, line = gate.run()
    assert code == 75
    assert line == "gate: NOT RUN (another gate is running)"
    assert gate.calls("ruff") == []

    assert gate.run()[0] == 0


def test_an_interrupted_gate_stops_its_scope_at_once(gate: Gate) -> None:
    import signal
    import time

    gate.hook('echo "$$" > "$FAKE/hanging"; exec sleep 60\n')
    proc = subprocess.Popen(
        [str(gate.repo / "tools" / "gate")], env=gate.env, stdout=subprocess.PIPE, text=True
    )
    hanging = gate.fake / "hanging"
    try:
        deadline = time.monotonic() + 30
        while not (hanging.exists() and hanging.read_text().strip()) and time.monotonic() < deadline:
            time.sleep(0.05)
        began = time.monotonic()
        proc.send_signal(signal.SIGTERM)
        out, _ = proc.communicate(timeout=20)
        took = time.monotonic() - began
    finally:
        proc.kill()
        if hanging.exists():  # the fake scope cannot be stopped by the fake systemctl: end the sleep here
            with contextlib.suppress(ProcessLookupError, ValueError):
                os.kill(int(hanging.read_text()), signal.SIGKILL)
    assert took < 10  # not after the step's 1800 s
    assert proc.returncode == 143
    assert out.strip().splitlines()[-1].startswith("gate: NOT RUN (interrupted")
    assert len(gate.calls("systemctl --user stop")) == 2  # pyright's scope, then the hanging one's
    assert gate.stamps() == []


def test_navigator_files_not_committed_count_as_a_change(gate: Gate) -> None:
    (gate.repo / "navigator" / "node_modules").mkdir()
    (gate.repo / ".gitignore").write_text("node_modules/\n")
    gate.commit("ignore")
    (gate.repo / "navigator" / "new.ts").write_text("x\n")

    code, line = gate.run()

    assert code == 0, line
    assert gate.calls("npm") == ["npm --prefix navigator run check"]
    assert line.endswith("(uncommitted or untracked files: no stamp)")


def test_navigator_asked_for_on_the_base_branch_is_recorded_as_run(gate: Gate) -> None:
    (gate.repo / "navigator" / "node_modules").mkdir()
    (gate.repo / ".gitignore").write_text("node_modules/\n")
    head = gate.commit("ignore")

    code, line = gate.run("--navigator")

    assert code == 0, line
    assert "navigator not checked" not in line
    assert "navigator: ran (asked)" in (gate.state / "gate" / f"{head}.ok").read_text()
    # the pre-checks ran again before the Navigator step: once per capped step
    assert len(gate.calls("systemctl --user show whisipedia-index.service")) == 5


def test_the_live_checkout_is_recognised_through_a_symlink_and_an_unreadable_answer_refuses(
    gate: Gate, tmp_path: Path
) -> None:
    (gate.repo / "navigator" / "node_modules").mkdir()
    link = tmp_path / "link"
    link.symlink_to(gate.repo)
    (gate.fake / "live").write_text(f"{link}\n")
    code, line = gate.run("--navigator")
    assert code == 2
    assert "which the API serves from this" in line

    (gate.fake / "live").write_text("\n")
    (gate.fake / "rc" / "systemctl-show").write_text("1\n")
    code, line = gate.run("--navigator")
    assert code == 2
    assert "cannot tell which checkout the API serves from" in line
    assert gate.calls("npm") == []
