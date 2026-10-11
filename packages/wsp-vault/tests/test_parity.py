import hashlib
import os
import shutil
from collections.abc import Sequence
from pathlib import Path

import pytest

from wsp_core.ports import ParityProvider
from wsp_vault.parity import (
    INDEX_NAME,
    MAX_FILES,
    Par2Parity,
    ParityError,
    check_parity_binding,
    file_descriptions,
)


class Recorder:
    def __init__(self, rc: int = 0) -> None:
        self.calls: list[list[str]] = []
        self.rc = rc

    def __call__(self, args: Sequence[str]) -> int:
        self.calls.append(list(args))
        if args[1] == "create" and self.rc == 0:
            index = Path(next(a for a in args if a.endswith(".par2")))
            index.write_bytes(b"idx")
            index.with_name("recovery.vol00+01.par2").write_bytes(b"vol")
        return self.rc


@pytest.fixture
def files(tmp_path: Path) -> list[Path]:
    out = [tmp_path / "gen" / "upstream" / "a", tmp_path / "gen" / "upstream" / "sub" / "b"]
    for f in out:
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b"x")
    return out


def test_create_and_repair_command_lines(files: list[Path], tmp_path: Path) -> None:
    run = Recorder()
    parity = Par2Parity(par2="par2", runner=run)
    assert isinstance(parity, ParityProvider)
    out = tmp_path / "gen" / "parity"
    created = parity.create(files, out, 10)
    assert [p.name for p in created] == [INDEX_NAME, "recovery.vol00+01.par2"]
    assert all(p.parent == out for p in created)
    base = f"-B{tmp_path / 'gen' / 'upstream'}"
    # par2 writes into a fresh private directory next to ``out``, renamed into place when done
    work_index = Path(run.calls[0][6])
    assert work_index.name == INDEX_NAME
    assert work_index.parent.parent == out.parent
    assert work_index.parent.name.startswith(".parity.")
    assert work_index.parent.name.endswith(".par2-tmp")
    assert not work_index.parent.exists()
    assert run.calls[0] == ["par2", "create", "-q", "-t2", "-r10", base, str(work_index), *map(str, files)]
    assert parity.repair(files, out)
    assert run.calls[1][:5] == ["par2", "repair", "-q", "-t2", base]


def test_failures(files: list[Path], tmp_path: Path) -> None:
    parity = Par2Parity(par2="par2", runner=Recorder(rc=1))
    with pytest.raises(ParityError):
        parity.create(files, tmp_path / "p", 10)
    with pytest.raises(ParityError):
        parity.create(files, tmp_path / "p", 0)
    with pytest.raises(ParityError):
        parity.create([], tmp_path / "p", 10)
    assert not (tmp_path / "p").exists()  # a failed run leaves nothing behind
    assert not list(tmp_path.glob(".p.*"))
    assert not parity.repair(files, tmp_path / "missing")
    (tmp_path / "p").mkdir()
    (tmp_path / "p" / INDEX_NAME).write_bytes(b"idx")
    assert not parity.repair(files, tmp_path / "p")


def test_missing_binary_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    def which(name: str) -> None:
        return None

    monkeypatch.setattr("shutil.which", which)
    with pytest.raises(ParityError, match="not found"):
        Par2Parity()


# --- real par2: binding to verified bytes, wedge, limits (audit #7, #14) --------------------------

needs_par2 = pytest.mark.skipif(shutil.which("par2") is None, reason="par2 not installed")


def _data(tmp_path: Path) -> list[Path]:
    out: list[Path] = []
    for rel, size in (("upstream/a.bin", 70_000), ("upstream/sub/b.bin", 5_000)):
        p = tmp_path / "gen" / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(os.urandom(size))
        out.append(p)
    return out


@needs_par2
def test_describe_reads_par2_file_descriptions(tmp_path: Path) -> None:
    files = _data(tmp_path)
    parity = Par2Parity()
    created = parity.create(files, tmp_path / "gen" / "parity", 10)
    described = parity.describe(created, files)
    assert described == {f: (hashlib.md5(f.read_bytes()).hexdigest(), f.stat().st_size) for f in files}
    index = next(p for p in created if p.name == INDEX_NAME)
    raw = bytearray(index.read_bytes())
    raw[200] ^= 1
    index.write_bytes(bytes(raw))
    with pytest.raises(ParityError, match=r"MD5|packet"):
        file_descriptions(index)


@needs_par2
def test_crashed_create_no_longer_wedges(tmp_path: Path) -> None:
    files = _data(tmp_path)
    parity = Par2Parity()
    work = tmp_path / "gen" / ".parity.deadbeef.par2-tmp"  # left by a killed run
    work.mkdir()
    (work / "recovery.vol000+01.par2").write_bytes(b"half")
    created = parity.create(files, tmp_path / "gen" / "parity", 10)
    assert created
    assert parity.repair(files, tmp_path / "gen" / "parity")
    # a second create into the same, now full, directory is refused instead of failing inside par2
    with pytest.raises(ParityError, match="not empty"):
        parity.create(files, tmp_path / "gen" / "parity", 10)


def test_limits_are_checked_before_running(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run = Recorder()
    parity = Par2Parity(par2="par2", runner=run)
    many = [tmp_path / "gen" / f"f{i}" for i in range(MAX_FILES + 1)]
    with pytest.raises(ParityError, match="limit"):
        parity.create(many, tmp_path / "out", 10)
    monkeypatch.setattr("wsp_vault.parity.arg_budget", lambda: 64 * 1024)
    long = [tmp_path / "gen" / ("x" * 200) / f"{i}" for i in range(1000)]
    with pytest.raises(ParityError, match="command line"):
        parity.create(long, tmp_path / "out", 10)
    assert run.calls == []


def test_binding_rejects_bytes_that_changed_after_verification() -> None:
    a, b = Path("/g/a"), Path("/g/b")
    verified = {a: ("11" * 16, 5), b: ("22" * 16, 7)}
    check_parity_binding(dict(verified), verified)
    with pytest.raises(ParityError, match="differ"):
        check_parity_binding({a: ("11" * 16, 5), b: ("33" * 16, 7)}, verified)
    with pytest.raises(ParityError, match="missing"):
        check_parity_binding({a: ("11" * 16, 5)}, verified)
