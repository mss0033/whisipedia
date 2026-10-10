from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from conftest import SealFn
from vaultkit import FILES, GEN, SOURCE, FakeParity, corrupt

from wsp_vault.ledger import JsonlLedger
from wsp_vault.manifest import PARITY_DIR
from wsp_vault.parity import Par2Parity
from wsp_vault.paritykeeper import PARITY_STAGING_DIR, ParityKeeper, ParityState
from wsp_vault.paritymeta import SIDECAR_PATH, read_sidecar, sidecar_paths
from wsp_vault.repair import RepairLadder, Rung
from wsp_vault.scrub import Scrubber
from wsp_vault.vault import FileVault


def keeper(vault: FileVault, tmp_path: Path, parity: object | None = None, **kw: object) -> ParityKeeper:
    return ParityKeeper(
        vault,
        parity or FakeParity(),  # type: ignore[arg-type]
        state_dir=tmp_path / "state",
        scratch=tmp_path / "scratch",
        **kw,  # type: ignore[arg-type]
    )


def test_backfill_creates_signed_sidecar_that_scrub_verifies(
    vault: FileVault, seal: SealFn, tmp_path: Path, ledger: JsonlLedger
) -> None:
    lib = seal(parity=None)
    k = keeper(vault, tmp_path, max_set_files=2)
    assert k.state(SOURCE, GEN) is ParityState.NONE
    report = k.run([(SOURCE, GEN)])
    assert report.created == (f"{SOURCE}/{GEN}",)
    assert report.pending == 0
    assert k.state(SOURCE, GEN) is ParityState.SIDECAR
    sidecar = read_sidecar(vault, SOURCE, GEN)
    assert sidecar is not None
    assert [list(s.files) for s in sidecar.sets] == [sorted(FILES)[:2], sorted(FILES)[2:]]
    assert (lib / PARITY_DIR / "set-00001").is_dir()
    assert not (vault.root / PARITY_STAGING_DIR).exists()
    assert any(e.kind == "parity.created" for e in ledger.events())

    report2 = Scrubber(vault, tmp_path / "scrub.sqlite").run()
    assert report2.findings == ()
    assert report2.files_checked == len(FILES) + len(sidecar.entries)
    cov = k.coverage([(SOURCE, GEN)])
    assert cov.covered == 1
    assert cov.fraction == 1.0


def test_damaged_data_is_repaired_from_a_sidecar_set(
    vault: FileVault, seal: SealFn, tmp_path: Path, ledger: JsonlLedger
) -> None:
    lib = seal(parity=None)
    parity = FakeParity()
    keeper(vault, tmp_path, parity=parity).run([(SOURCE, GEN)])
    corrupt(lib / "upstream/beta.txt")
    findings = Scrubber(vault, tmp_path / "scrub.sqlite").run().findings
    assert [f.relpath for f in findings] == ["upstream/beta.txt"]
    (result,) = RepairLadder(vault, ledger, parity=parity).repair_findings(findings)
    assert result.repaired == {"upstream/beta.txt": Rung.PAR2}
    assert (lib / "upstream/beta.txt").read_bytes() == FILES["upstream/beta.txt"]


def test_budget_leaves_partial_progress_that_resumes(vault: FileVault, seal: SealFn, tmp_path: Path) -> None:
    seal(parity=None)
    k = keeper(vault, tmp_path, max_set_files=1)
    first = k.run([(SOURCE, GEN)], max_bytes=1)
    assert first.in_progress == f"{SOURCE}/{GEN}"
    assert first.sets_done == 1
    assert k.state(SOURCE, GEN) is ParityState.PARTIAL
    second = keeper(vault, tmp_path, max_set_files=1).run([(SOURCE, GEN)])
    assert second.created == (f"{SOURCE}/{GEN}",)
    assert second.sets_done == 2
    sidecar = read_sidecar(vault, SOURCE, GEN)
    assert sidecar is not None
    assert len(sidecar.sets) == 3


def test_damaged_data_never_gets_parity(vault: FileVault, seal: SealFn, tmp_path: Path) -> None:
    lib = seal(parity=None)
    corrupt(lib / "upstream/alpha.zim")
    report = keeper(vault, tmp_path).run([(SOURCE, GEN)])
    assert report.created == ()
    assert "does not match the manifest" in report.skipped[0]
    assert not (lib / PARITY_DIR).exists()


def test_damaged_parity_is_reported_invalidated_and_recreated(
    vault: FileVault, seal: SealFn, tmp_path: Path, ledger: JsonlLedger
) -> None:
    lib = seal(parity=None)
    k = keeper(vault, tmp_path)
    k.run([(SOURCE, GEN)])
    sidecar = read_sidecar(vault, SOURCE, GEN)
    assert sidecar is not None
    victim = sidecar.entries[0].path
    corrupt(lib / victim, offset=0)
    findings = Scrubber(vault, tmp_path / "scrub.sqlite").run().findings
    assert [f.relpath for f in findings] == [victim]
    (result,) = RepairLadder(vault, ledger, parity=FakeParity()).repair_findings(findings)
    assert not result.degraded, "parity is derived data: never a reason to degrade"
    assert (lib / victim).exists(), "the ladder leaves parity files alone"
    assert k.invalidate(SOURCE, GEN, "scrub mismatch") is not None
    assert k.state(SOURCE, GEN) is ParityState.NONE
    assert k.run([(SOURCE, GEN)]).created == (f"{SOURCE}/{GEN}",)
    assert Scrubber(vault, tmp_path / "scrub2.sqlite").run().findings == ()


def test_tampered_sidecar_is_one_finding(vault: FileVault, seal: SealFn, tmp_path: Path) -> None:
    lib = seal(parity=None)
    k = keeper(vault, tmp_path)
    k.run([(SOURCE, GEN)])
    (lib / SIDECAR_PATH).chmod(0o644)
    (lib / SIDECAR_PATH).write_bytes(
        (lib / SIDECAR_PATH).read_bytes().replace(b'"percent": 10', b'"percent": 99')
    )
    assert sidecar_paths(vault, SOURCE, GEN) == frozenset()
    assert k.state(SOURCE, GEN) is ParityState.INVALID
    findings = Scrubber(vault, tmp_path / "scrub.sqlite").run().findings
    assert [(f.relpath, "signature" in f.detail) for f in findings] == [(SIDECAR_PATH, True)]


def test_parity_made_while_sealing_counts_as_covered(vault: FileVault, seal: SealFn, tmp_path: Path) -> None:
    seal(parity=10)
    k = keeper(vault, tmp_path)
    assert k.state(SOURCE, GEN) is ParityState.MANIFEST
    assert k.run([(SOURCE, GEN)]).created == ()


@pytest.mark.skipif(shutil.which("par2") is None, reason="par2 not installed")
def test_real_par2_round_trip(vault: FileVault, seal: SealFn, tmp_path: Path, ledger: JsonlLedger) -> None:
    lib = seal(parity=None)
    parity = Par2Parity(low_priority=True)
    keeper(vault, tmp_path, parity=parity).run([(SOURCE, GEN)])
    assert Scrubber(vault, tmp_path / "scrub.sqlite").run().findings == ()
    corrupt(lib / "upstream/alpha.zim", offset=1000)
    findings = Scrubber(vault, tmp_path / "scrub2.sqlite").run().findings
    (result,) = RepairLadder(vault, ledger, parity=parity).repair_findings(findings)
    assert result.repaired == {"upstream/alpha.zim": Rung.PAR2}
    assert (lib / "upstream/alpha.zim").read_bytes() == FILES["upstream/alpha.zim"]
