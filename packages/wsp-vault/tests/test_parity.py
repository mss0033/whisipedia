from collections.abc import Sequence
from pathlib import Path

import pytest

from wsp_core.ports import ParityProvider
from wsp_vault.parity import INDEX_NAME, Par2Parity, ParityError


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
    base = f"-B{tmp_path / 'gen' / 'upstream'}"
    index = str(out / INDEX_NAME)
    assert run.calls[0] == ["par2", "create", "-q", "-t2", "-r10", base, index, *map(str, files)]
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
    assert not parity.repair(files, tmp_path / "missing")
    (tmp_path / "p" / INDEX_NAME).write_bytes(b"idx")
    assert not parity.repair(files, tmp_path / "p")


def test_missing_binary_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    def which(name: str) -> None:
        return None

    monkeypatch.setattr("shutil.which", which)
    with pytest.raises(ParityError, match="not found"):
        Par2Parity()
