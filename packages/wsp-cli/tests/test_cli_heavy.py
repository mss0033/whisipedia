"""The real composition root's heavy steps and policy screen (audit 2026-10-11: C2, C3, H1, H3, H7).

Child units are simulated with ``FakeLauncher`` (it runs the child function in this process, through
the same JSON request/result files); no test starts a systemd unit.
"""

from __future__ import annotations

import dataclasses
import io
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

import pytest
from test_cli_partitions import SID, fetch, sheet, spec

from wsp_adapters import available_adapters
from wsp_adapters.doccollection import DocCollectionAdapter
from wsp_adapters.doccollection.testing import PUBLIC_A, RESTRICTED_C, make_pdf
from wsp_cli import wiring
from wsp_cli.integrity import IsolatedProfiler, Profiler
from wsp_cli.locks import DRILL, EXIT_BUSY, HEAVY, INDEX, SCRUB, exclusive, lock_dir_for
from wsp_cli.wiring import (
    CHECKSUM_ONLY_ADAPTERS,
    SCREENED_ADAPTERS,
    ChildContext,
    HeavySettings,
    IndexHooks,
    document_screen,
    storage_probe,
)
from wsp_core.ids import make_doc_id
from wsp_core.model import Generation, RemoteFile, SourceSpec, VerdictStatus
from wsp_core.ports import Ctx
from wsp_index import TantivyIndex
from wsp_index.refinery import part_dir
from wsp_orchestrator.errors import ResourceBusyError, ResourceExhaustedError
from wsp_orchestrator.isolation import ChildExit, IsolatedRunner
from wsp_orchestrator.testing import FakeLauncher, make_spec
from wsp_vault.ledger import JsonlLedger
from wsp_vault.signing import Ed25519Signer
from wsp_vault.vault import FileVault


@pytest.fixture
def library(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    signer = Ed25519Signer.generate()
    root = tmp_path / "vault"
    vault = FileVault(
        root, signer, domains={SID: "maps"}, partitioned=[SID], ledger=JsonlLedger(root / "ledger", signer)
    )
    fetch(vault, tmp_path, "ak", "2026-10-03.ak", {"AK_Anchorage_1950.pdf": sheet("Anchorage")})
    cfg = {"index": {"root": str(tmp_path / "index")}, "orchestrator": {"workdir": str(tmp_path / "w")}}
    gen = next(g for g in vault.generations(SID) if g.generation_id == "2026-10-03.ak")

    def child_context(payload: Any) -> ChildContext:  # the child rebuilds this from the config
        assert payload["source_id"] == SID
        assert payload["config"] == cfg, "the whole config travels to the child"
        return ChildContext(cfg, (spec(),), vault, spec(), gen)

    monkeypatch.setattr(wiring, "child_context", child_context)
    launcher = FakeLauncher()
    runner = IsolatedRunner(tmp_path / "w", launcher=launcher, cwd=tmp_path)
    hooks = IndexHooks(cfg, vault, [spec()], runner)
    return {"vault": vault, "gen": gen, "hooks": hooks, "launcher": launcher, "cfg": cfg, "root": tmp_path}


def test_refine_runs_in_a_child_and_never_opens_tantivy(library: dict[str, Any]) -> None:
    hooks: IndexHooks = library["hooks"]
    gen: Generation = library["gen"]
    result = hooks.refine(spec(), gen, library["vault"].generation_path(SID, gen.generation_id))
    assert result.doc_count == 1
    index_root = library["root"] / "index"
    assert part_dir(index_root / "refinery", SID, gen.generation_id).is_dir()
    assert not (index_root / "lexical").exists(), "refine opened the Tantivy index"
    assert not (index_root / "changes.sqlite").exists()
    [(unit, argv, _)] = library["launcher"].started
    assert unit.startswith("whisipedia-step-refine-")
    assert argv[-3] == "wsp_cli.wiring:refine_child"


def test_index_runs_in_a_child_under_both_locks(library: dict[str, Any]) -> None:
    hooks: IndexHooks = library["hooks"]
    gen: Generation = library["gen"]
    hooks.refine(spec(), gen, library["vault"].generation_path(SID, gen.generation_id))
    hooks.index(spec(), gen)
    index = TantivyIndex(library["root"] / "index" / "lexical")
    try:
        hits = [h.doc_id for h in index.search("Anchorage")]
        assert hits == [make_doc_id(SID, "topo:AK_Anchorage_1950.pdf")]
    finally:
        index.close()


@pytest.mark.parametrize("held", [HEAVY, INDEX])
def test_a_held_lock_comes_back_as_busy(library: dict[str, Any], held: str) -> None:
    hooks: IndexHooks = library["hooks"]
    gen: Generation = library["gen"]
    locks = lock_dir_for(library["root"] / "index")
    with exclusive(locks, held), pytest.raises(ResourceBusyError, match=held):
        hooks.refine(spec(), gen, library["vault"].generation_path(SID, gen.generation_id))
    assert not part_dir(library["root"] / "index" / "refinery", SID, gen.generation_id).exists()


def test_an_oom_kill_comes_back_as_a_crash(library: dict[str, Any]) -> None:
    hooks: IndexHooks = library["hooks"]
    library["launcher"].exits.append(ChildExit("oom-kill", None, "KILL", "6.0G"))
    gen: Generation = library["gen"]
    with pytest.raises(ResourceExhaustedError, match="OOM-killed"):
        hooks.refine(spec(), gen, library["vault"].generation_path(SID, gen.generation_id))


def test_profile_reads_the_partition_refinery_in_a_child(library: dict[str, Any]) -> None:
    hooks: IndexHooks = library["hooks"]
    gen: Generation = library["gen"]
    gen_path = library["vault"].generation_path(SID, gen.generation_id)
    hooks.refine(spec(), gen, gen_path)
    local = Profiler(library["root"] / "index" / "refinery", sample_rate=1.0)
    assert local.refined_dir(spec(), gen.generation_id) == part_dir(
        library["root"] / "index" / "refinery", SID, gen.generation_id
    )
    prof = local(spec(), gen, gen_path)
    assert prof.doc_count == 1, "partitioned generations were invisible to the profiler (M6)"
    assert prof.total_bytes > 0
    isolated = IsolatedProfiler(library["cfg"], hooks.runner)
    remote = isolated(spec(), gen, gen_path)
    assert remote.doc_count == 1
    assert library["launcher"].started[-1][1][-3] == "wsp_cli.integrity:profile_child"


# --- document policy screen (H3) ---------------------------------------------------------------------


def test_every_installed_adapter_has_a_known_policy() -> None:
    """A new adapter must be classified: checksum-only, or screened here (else it fails closed)."""
    assert set(available_adapters()) == CHECKSUM_ONLY_ADAPTERS | SCREENED_ADAPTERS
    assert not CHECKSUM_ONLY_ADAPTERS & SCREENED_ADAPTERS


def _screen_file(tmp_path: Path, name: str, data: bytes, source: SourceSpec) -> Any:
    local = tmp_path / name
    local.write_bytes(data)
    adapters = {"doc-collection": DocCollectionAdapter(throttle=False)}
    ctx = Ctx(source=source, fetcher=None, workdir=tmp_path, user_agent="t")  # type: ignore[arg-type]
    remote = RemoteFile(url=f"https://x.example/{name}", name=name)
    return document_screen(adapters)(source, ctx, remote, local)  # type: ignore[arg-type]


def test_doc_collection_screen_withholds_restricted_documents(tmp_path: Path) -> None:
    base = spec()
    access = dict(cast(Mapping[str, Any], base.raw["access"]))
    access["filter"] = {"distribution": {"require": ["A"]}}  # was {"require": ...}: silently ignored (CA-20)
    gated = dataclasses.replace(base, raw={**base.raw, "access": access})
    restricted = _screen_file(tmp_path, "r.pdf", make_pdf([RESTRICTED_C]), gated)
    assert restricted.status in (VerdictStatus.REJECTED, VerdictStatus.HELD)
    assert restricted.withheld
    public = _screen_file(tmp_path, "p.pdf", make_pdf([PUBLIC_A]), gated)
    assert public.status is VerdictStatus.OK
    fake = _screen_file(tmp_path, "h.pdf", b"<html>login</html>", gated)
    assert fake.status is VerdictStatus.MISMATCH


def test_screen_fails_closed_for_unknown_adapters(tmp_path: Path) -> None:
    unknown = make_spec("x", adapter="brand-new")
    assert _screen_file(tmp_path, "a.bin", b"x", unknown) is None
    known = make_spec("w", adapter="wikimedia-dumps")
    adapters = {"wikimedia-dumps": object()}
    local = tmp_path / "w.bz2"
    local.write_bytes(b"x")
    verdict = document_screen(adapters)(known, None, RemoteFile(url="u", name="w.bz2"), local)  # type: ignore[arg-type]
    assert verdict is not None
    assert verdict.status is VerdictStatus.OK


# --- settings and probes -------------------------------------------------------------------------------


def test_heavy_settings_from_config() -> None:
    cfg = {
        "orchestrator": {
            "heavy": {
                "memory_max": "4G",
                "memory_high": "3G",
                "refine_timeout": "PT2H",
                "index_timeout": "none",
            }
        }
    }
    s = HeavySettings.from_config(cfg)
    assert (s.limits.memory_max, s.limits.memory_high, s.limits.memory_swap_max) == ("4G", "3G", "1G")
    assert s.refine_timeout == 7200
    assert s.index_timeout is None
    assert HeavySettings.from_config({}).limits.memory_max == "6G"


def test_storage_probe(tmp_path: Path) -> None:
    assert storage_probe(None)()
    assert not storage_probe(tmp_path / "missing")()
    (tmp_path / "mnt").mkdir()
    assert not storage_probe(tmp_path / "mnt")(), "an empty mount point is an unmounted share"
    (tmp_path / "mnt" / "library").mkdir()
    assert storage_probe(tmp_path / "mnt")()


def test_build_wiring_isolates_heavy_steps(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from test_cli_autonomy import ROOT

    monkeypatch.chdir(ROOT)
    monkeypatch.delenv("WSP_CONFIG_DIR", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))  # never the owner's signing key
    monkeypatch.setenv("WSP_NO_LOCAL_CONFIG", "1")
    for key, value in {
        "VAULT__ROOT": tmp_path / "vault",
        "INDEX__ROOT": tmp_path / "index",
        "ORCHESTRATOR__STATE_DB": tmp_path / "state" / "orchestrator.sqlite3",
        "ORCHESTRATOR__WORKDIR": tmp_path / "work",
        "CURATOR__OVERLAY": tmp_path / "registry.local.yaml",
    }.items():
        monkeypatch.setenv(f"WSP__{key}", str(value))
    (tmp_path / "vault" / "library").mkdir(parents=True)
    from wsp_cli import bootstrap

    w = wiring.build_wiring(bootstrap.load(bootstrap.layout()))
    hooks = getattr(w.refine, "__self__", None)
    assert isinstance(hooks, IndexHooks)
    assert w.document_screen is not None
    assert w.storage_available is not None
    assert w.storage_available()
    assert w.integrity is not None
    assert isinstance(w.integrity.profile, IsolatedProfiler)
    assert not (tmp_path / "index" / "lexical").exists(), "building the wiring opens no index"


# --- CLI commands share the locks (exit 3 when busy) --------------------------------------------------


@pytest.mark.parametrize(
    ("argv", "lock"),
    [
        (("index", "catalog"), HEAVY),
        (("index", "catalog"), INDEX),
        (("index", "build"), HEAVY),
        (("index", "rank"), HEAVY),
        (("index", "spell"), INDEX),
        (("scrub",), SCRUB),
        (("drill",), DRILL),
    ],
)
def test_cli_writers_exit_3_while_the_lock_is_held(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, argv: tuple[str, ...], lock: str
) -> None:
    from test_cli_autonomy import ROOT

    from wsp_cli.main import main

    monkeypatch.chdir(ROOT)
    monkeypatch.delenv("WSP_CONFIG_DIR", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("WSP__VAULT__ROOT", str(tmp_path / "vault"))
    monkeypatch.setenv("WSP__INDEX__ROOT", str(tmp_path / "index"))
    monkeypatch.setenv("WSP__ORCHESTRATOR__STATE_DB", str(tmp_path / "state" / "o.sqlite3"))
    monkeypatch.setenv("WSP__ORCHESTRATOR__WORKDIR", str(tmp_path / "work"))
    (tmp_path / "vault" / "library").mkdir(parents=True)
    with exclusive(lock_dir_for(tmp_path / "index"), lock):
        assert main(list(argv), out=io.StringIO()) == EXIT_BUSY


# --- units and services ---------------------------------------------------------------------------------


def _unit(name: str) -> dict[str, list[str]]:
    from test_cli_autonomy import ROOT

    out: dict[str, list[str]] = {}
    for line in (ROOT / "deploy" / "systemd" / name).read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            key, _, value = line.partition("=")
            out.setdefault(key.strip(), []).append(value.strip())
    return out


def test_orchestrator_unit_hygiene() -> None:
    u = _unit("whisipedia-orchestrator.service")
    assert u["StartLimitIntervalSec"] == ["2h"]
    assert u["StartLimitBurst"] == ["4"]
    assert u["KillMode"] == ["mixed"]
    assert u["OOMPolicy"] == ["continue"]
    assert u["Slice"] == ["whisipedia-background.slice"]
    assert u["IOSchedulingClass"] == ["best-effort"]
    assert "WSP_HEAVY_LAUNCHER=systemd" in u["Environment"]
    assert u["MemoryMax"] == ["2G"], "a control plane: heavy work runs in its own units"
    assert u["MemoryLow"] == ["256M"], "the control plane is not swapped out under pressure (H1)"
    idx = _unit("whisipedia-index.service")
    assert idx["SuccessExitStatus"] == ["3"]  # busy lock: skipped, not failed
    assert idx["KillMode"] == ["mixed"]
    assert idx["IOSchedulingClass"] == ["best-effort"]


def test_uninstall_leaves_the_owners_own_units_alone(tmp_path: Path) -> None:
    from test_cli_autonomy import ROOT, FakeSystemctl

    from wsp_cli.service import UNITS, ServiceManager

    fake = FakeSystemctl()
    mgr = ServiceManager(repo=ROOT, config_dir=ROOT / "config", target=tmp_path / "units", run=fake)
    assert mgr.install(io.StringIO()) == 0
    own = tmp_path / "units" / UNITS[1]
    own.write_text("[Service]\nExecStart=/bin/true\n")  # the owner replaced it with their own
    fake.calls.clear()
    assert mgr.uninstall(io.StringIO()) == 0
    disabled = [c for c in fake.calls if c[0] == "disable"]
    assert disabled
    assert UNITS[1] not in disabled[0]
    assert own.exists()


def _run_wsp_bg(tmp_path: Path, *args: str) -> tuple[int, list[str], str]:
    """Run tools/wsp-bg with a fake systemd-run that prints its argv (no unit is started)."""
    import subprocess

    from test_cli_autonomy import ROOT

    fakebin = tmp_path / "bin"
    fakebin.mkdir(exist_ok=True)
    fake = fakebin / "systemd-run"
    fake.write_text('#!/bin/sh\nfor a in "$@"; do printf "%s\\n" "$a"; done\n')
    fake.chmod(0o755)
    env = {
        "PATH": f"{fakebin}:/usr/bin:/bin",
        "WSP_LOCK_DIR": str(tmp_path / "locks"),
        "HOME": str(tmp_path),
        "WSP_BG_SLICE_HIGH": "7G",  # the template's MemoryHigh, whatever the test machine runs
    }
    done = subprocess.run(
        [str(ROOT / "tools" / "wsp-bg"), *args], capture_output=True, text=True, env=env, check=False
    )
    return done.returncode, done.stdout.splitlines(), done.stderr


def _wsp_bg(tmp_path: Path, *args: str) -> list[str]:
    code, argv, err = _run_wsp_bg(tmp_path, *args)
    assert code == 0, err
    return argv


def test_wsp_bg_holds_the_heavy_lock(tmp_path: Path) -> None:
    argv = _wsp_bg(tmp_path, "embed", "6G", "--", "python", "job.py")
    lock = str(tmp_path / "locks" / "heavy.lock")
    shell = argv.index("/bin/sh")
    assert argv[shell + 1] == "-c"
    take, *rest = argv[shell + 2 :]
    assert "flock --nonblock 9 || exit 75" in take
    assert take.endswith('exec "$@"'), "the job replaces the shell: it is the unit's main process (M5)"
    assert rest == [lock, "python", "job.py"]
    assert "WSP_INHERITED_LOCKS=heavy:9" in argv, "steps inside the job run under its lock (M2)"
    assert "--slice=whisipedia-background.slice" in argv
    assert "MemoryMax=6G" in argv
    assert "MemoryHigh=4608M" in argv
    assert "KillMode=control-group" in argv, "SIGTERM reaches the real job (M5)"
    assert "TimeoutStopSec=120" in argv
    assert "IOSchedulingClass=best-effort" in argv
    waiting = _wsp_bg(tmp_path, "--wait", "embed", "--", "python", "job.py")
    take = waiting[waiting.index("/bin/sh") + 2]
    assert "flock 9 &&" in take
    assert "--nonblock" not in take
    assert "MemoryMax=4G" in waiting
    assert "MemoryHigh=3072M" in waiting
    light = _wsp_bg(tmp_path, "--no-heavy-lock", "small", "--", "python", "job.py")
    assert "/bin/sh" not in light
    assert not any("WSP_INHERITED_LOCKS" in a for a in light)
    assert light[-2:] == ["python", "job.py"]


def test_wsp_bg_fetch_jobs_have_their_own_sub_budget(tmp_path: Path) -> None:
    argv = _wsp_bg(tmp_path, "--fetch", "lane-cia", "--", "tools/lanes/lane-run.sh", "lane-cia", "-", "x")
    assert "--slice=whisipedia-background-fetch.slice" in argv
    assert "MemoryMax=1G" in argv
    assert "/bin/sh" not in argv, "a download job never takes the heavy lock"


@pytest.mark.parametrize("size", ["7G", "8G", "7168M", "lots"])
def test_wsp_bg_refuses_a_job_as_big_as_the_whole_slice(tmp_path: Path, size: str) -> None:
    """Audit 2026-10-12 H1: a job at the slice's MemoryHigh throttles every other background job."""
    code, argv, err = _run_wsp_bg(tmp_path, "rebuild", size, "--", "python", "job.py")
    assert code == 2
    assert argv == []
    assert "MemoryHigh" in err or "a size" in err


def test_the_job_inherits_the_heavy_lock_and_nobody_else_can_forge_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What wsp-bg runs inside the unit: the job holds HEAVY through fd 9, and ``exclusive(HEAVY)`` in it
    (a `wsp index build` step of a rebuild) accepts that lock; anyone else still finds it held."""
    import subprocess
    import sys

    from wsp_cli.locks import LockHeldError

    argv = _wsp_bg(tmp_path, "rebuild", "6G", "--", "python", "job.py")
    shell = argv.index("/bin/sh")
    take, lock = argv[shell + 2], argv[shell + 3]
    probe = (
        "import sys; from pathlib import Path; from wsp_cli.locks import HEAVY, exclusive\n"
        "with exclusive(Path(sys.argv[1]), HEAVY): print('inherited')\n"
    )
    child = [sys.executable, "-c", probe, str(tmp_path / "locks")]
    env = {"PATH": "/usr/bin:/bin", "WSP_INHERITED_LOCKS": "heavy:9"}
    done = subprocess.run(["/bin/sh", "-c", take, lock, *child], capture_output=True, text=True, env=env)
    assert (done.returncode, done.stdout.strip()) == (0, "inherited"), done.stderr
    with exclusive(tmp_path / "locks", HEAVY):  # held elsewhere: the job does not start
        busy = subprocess.run(["/bin/sh", "-c", take, lock, *child], capture_output=True, text=True, env=env)
        assert (busy.returncode, busy.stdout) == (75, "")
        # A variable naming some other descriptor never stands in for the lock.
        monkeypatch.setenv("WSP_INHERITED_LOCKS", "heavy:0")
        with pytest.raises(LockHeldError), exclusive(tmp_path / "locks", HEAVY):
            pass


def _api_health(
    tmp_path: Path, *, started: str, full_avg60: str, unit_memory: tuple[str, str] | None = None
) -> tuple[str, list[str]]:
    """Run tools/wsp-api-health against a fake systemctl and an API that never answers.
    ``unit_memory``: the unit's (memory.current, memory.high) under a fake cgroup root."""
    import subprocess

    from test_cli_autonomy import ROOT

    fakebin = tmp_path / "bin"
    fakebin.mkdir(parents=True, exist_ok=True)
    calls = tmp_path / "calls"
    scripts = {
        "systemctl": (
            f'#!/bin/sh\necho "$*" >> {calls}\n'
            f'case "$*" in *ControlGroup*) echo /api.service ;; *show*) echo "{started}" ;; esac\n'
        ),
        "curl": "#!/bin/sh\nprintf 000\n",
        "sleep": "#!/bin/sh\n",
    }
    for name, text in scripts.items():
        (fakebin / name).write_text(text)
        (fakebin / name).chmod(0o755)
    pressure = tmp_path / "memory"
    pressure.write_text(
        "some avg10=1.00 avg60=1.00 avg300=1.00 total=1\n"
        f"full avg10=0.50 avg60={full_avg60} avg300=0.50 total=1\n"
    )
    cgroot = tmp_path / "cgroup"
    if unit_memory is not None:
        (cgroot / "api.service").mkdir(parents=True)
        (cgroot / "api.service" / "memory.current").write_text(unit_memory[0] + "\n")
        (cgroot / "api.service" / "memory.high").write_text(unit_memory[1] + "\n")
    env = {
        "PATH": f"{fakebin}:/usr/bin:/bin",
        "WSP_API_HEALTH_PRESSURE": str(pressure),
        "WSP_API_HEALTH_CGROUP_ROOT": str(cgroot),
        "TZ": "UTC",
    }
    done = subprocess.run(
        [str(ROOT / "tools" / "wsp-api-health")], capture_output=True, text=True, env=env, check=True
    )
    return done.stdout, calls.read_text().splitlines() if calls.exists() else []


def test_api_health_restarts_a_silent_api_but_not_under_machine_wide_pressure(tmp_path: Path) -> None:
    """Audit 2026-10-12 L3: no restart storm while the whole machine waits for memory."""
    from datetime import UTC, datetime, timedelta

    hour_ago = (datetime.now(UTC) - timedelta(hours=1)).strftime("%a %Y-%m-%d %H:%M:%S UTC")
    out, calls = _api_health(tmp_path, started=hour_ago, full_avg60="3.10")
    assert "restarting whisipedia-api.service" in out
    assert calls[-1] == "--user restart whisipedia-api.service"
    out, calls = _api_health(tmp_path / "p", started=hour_ago, full_avg60="64.60")
    assert "memory pressure" in out
    assert "not restarting" in out
    assert not any("restart" in c for c in calls)


def test_api_health_restarts_an_api_at_its_own_ceiling_even_under_pressure(tmp_path: Path) -> None:
    """2026-10-13: the API over its MemoryHigh thrashed the machine for 20 minutes, and the machine-wide
    pressure it caused kept the check from restarting it."""
    from datetime import UTC, datetime, timedelta

    hour_ago = (datetime.now(UTC) - timedelta(hours=1)).strftime("%a %Y-%m-%d %H:%M:%S UTC")
    gib = str(1 << 30)
    out, calls = _api_health(tmp_path, started=hour_ago, full_avg60="64.60", unit_memory=(gib, gib))
    assert "at its MemoryHigh" in out
    assert calls[-1] == "--user restart whisipedia-api.service"
    # Under its ceiling, or with no ceiling, the machine-wide rule still holds.
    for memory in ((str(1 << 29), gib), (gib, "max")):
        out, calls = _api_health(
            tmp_path / memory[1], started=hour_ago, full_avg60="64.60", unit_memory=memory
        )
        assert "not restarting" in out
        assert not any("restart" in c for c in calls)


@pytest.mark.parametrize("started", ["", "n/a"])
def test_api_health_waits_when_the_start_time_is_unknown(tmp_path: Path, started: str) -> None:
    """An empty ActiveEnterTimestamp used to break the shell arithmetic: the check errored out."""
    out, calls = _api_health(tmp_path, started=started, full_avg60="0.00")
    assert out == ""
    assert not any("restart" in c for c in calls)
