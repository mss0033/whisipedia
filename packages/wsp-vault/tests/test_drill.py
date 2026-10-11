import random
from pathlib import Path

import pytest
from vaultkit import GEN, SOURCE, FakeParity, SealFn

from wsp_vault.drill import DrillKind, run_drill
from wsp_vault.ledger import JsonlLedger
from wsp_vault.vault import FileVault


@pytest.mark.parametrize("kind", list(DrillKind))
def test_drill_detects_and_repairs_from_second_copy(
    kind: DrillKind, vault: FileVault, seal: SealFn, ledger: JsonlLedger, tmp_path: Path
) -> None:
    lib = seal()
    before = {p: p.read_bytes() for p in lib.rglob("*") if p.is_file()}
    result = run_drill(vault, SOURCE, GEN, tmp_path / "drill", kind, rng=random.Random(7), ledger=ledger)
    assert result.passed, result
    assert {p: p.read_bytes() for p in lib.rglob("*") if p.is_file()} == before  # real vault untouched
    last = list(ledger.events())[-1]
    assert (last.kind, last.data["detected"], last.data["repaired"]) == ("drill.result", True, True)


def test_drill_uses_par2_rung_when_given(vault: FileVault, seal: SealFn, tmp_path: Path) -> None:
    seal()
    parity = FakeParity()
    result = run_drill(
        vault, SOURCE, GEN, tmp_path / "d", DrillKind.BIT_FLIP, rng=random.Random(1), parity=parity
    )
    assert result.passed
    assert parity.repair_calls == 1
    assert "via par2" in result.detail


def test_drill_refuses_unsafe_workdirs(vault: FileVault, seal: SealFn, tmp_path: Path) -> None:
    seal()
    with pytest.raises(ValueError, match="outside"):
        run_drill(vault, SOURCE, GEN, tmp_path / "vault" / "drill", DrillKind.DELETE)
    busy = tmp_path / "busy"
    busy.mkdir()
    (busy / "x").write_text("x")
    with pytest.raises(ValueError, match="not empty"):
        run_drill(vault, SOURCE, GEN, busy, DrillKind.DELETE)
