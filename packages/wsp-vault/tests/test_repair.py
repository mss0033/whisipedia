import shutil
from pathlib import Path

import pytest
from vaultkit import FILES, GEN, SOURCE, Clock, FakeParity, SealFn, corrupt

from wsp_core.model import GenerationStatus
from wsp_core.ports import ParityProvider
from wsp_vault.ledger import JsonlLedger
from wsp_vault.repair import RefetchRequest, RepairLadder, Rung
from wsp_vault.scrub import Scrubber
from wsp_vault.vault import FileVault

TARGET = "upstream/beta.txt"


def kinds(ledger: JsonlLedger) -> list[str]:
    return [e.kind for e in ledger.events() if e.kind.startswith("repair.")]


def scrub_clean(vault: FileVault, tmp_path: Path) -> bool:
    return Scrubber(vault, tmp_path / "check.sqlite").scrub_generation(SOURCE, GEN) == []


def test_fake_parity_satisfies_port() -> None:
    assert isinstance(FakeParity(), ParityProvider)


def test_par2_rung_repairs_and_quarantines_leftovers(
    vault: FileVault, seal: SealFn, ledger: JsonlLedger, fake_parity: FakeParity, tmp_path: Path
) -> None:
    lib = seal()
    corrupt(lib / TARGET)
    result = RepairLadder(vault, ledger, parity=fake_parity).repair(SOURCE, GEN, [TARGET])
    assert result.ok
    assert result.repaired == {TARGET: Rung.PAR2}
    assert (lib / TARGET).read_bytes() == FILES[TARGET]
    assert not (lib / (TARGET + ".1")).exists()
    assert list((tmp_path / "vault" / "quarantine").rglob("beta.txt.1.*"))
    assert kinds(ledger) == ["repair.par2", "repair.ok"]
    assert scrub_clean(vault, tmp_path)


def test_mirror_rung(vault: FileVault, seal: SealFn, ledger: JsonlLedger, tmp_path: Path) -> None:
    lib = seal()
    mirror = tmp_path / "mirror"
    shutil.copytree(tmp_path / "vault" / "library", mirror / "library")
    (lib / TARGET).unlink()
    result = RepairLadder(vault, ledger, mirror_root=mirror).repair(SOURCE, GEN)
    assert result.repaired == {TARGET: Rung.MIRROR}
    assert scrub_clean(vault, tmp_path)


def test_refetch_rung_accepts_only_matching_bytes(
    vault: FileVault, seal: SealFn, ledger: JsonlLedger, tmp_path: Path
) -> None:
    lib = seal()
    corrupt(lib / TARGET)
    requests: list[RefetchRequest] = []

    def bad_then_good(req: RefetchRequest, scratch: Path) -> Path:
        requests.append(req)
        out = scratch / "dl"
        out.write_bytes(FILES[req.relpath] if len(requests) > 1 else b"tampered")
        return out

    ladder = RepairLadder(vault, ledger, refetch=bad_then_good, scratch=tmp_path / "scratch")
    first = ladder.repair(SOURCE, GEN, [TARGET])
    assert first.degraded
    assert (lib / "DEGRADED.txt").exists()
    second = ladder.repair(SOURCE, GEN, [TARGET])
    assert second.repaired == {TARGET: Rung.REFETCH}
    assert requests[0].sha256 == requests[1].sha256
    assert scrub_clean(vault, tmp_path)


def test_refetch_errors_do_not_stop_the_ladder(vault: FileVault, seal: SealFn, ledger: JsonlLedger) -> None:
    corrupt(seal() / TARGET)

    def boom(req: RefetchRequest, scratch: Path) -> Path:
        raise ConnectionError("offline")

    result = RepairLadder(vault, ledger, refetch=boom).repair(SOURCE, GEN, [TARGET])
    assert result.degraded
    assert "repair.refetch.error" in kinds(ledger)


def test_degraded_rolls_back_current(
    vault: FileVault, seal: SealFn, ledger: JsonlLedger, clock: Clock, tmp_path: Path
) -> None:
    seal("2026-09-01")
    lib = seal(GEN)
    corrupt(lib / TARGET)
    result = RepairLadder(vault, ledger).repair(SOURCE, GEN, [TARGET])
    assert result.degraded
    assert result.unrepaired == (TARGET,)
    assert result.rolled_back_to == "2026-09-01"
    current = vault.current(SOURCE)
    assert current is not None
    assert current.generation_id == "2026-09-01"
    assert vault.generations(SOURCE)[-1].status is GenerationStatus.DEGRADED
    assert TARGET in (lib / "DEGRADED.txt").read_text()
    assert (lib / TARGET).exists()  # damaged bytes are kept, never deleted
    assert kinds(ledger)[-1] == "repair.degraded"
    assert ledger.verify()


def test_bad_signature_restored_from_mirror_only(
    vault: FileVault, seal: SealFn, ledger: JsonlLedger, tmp_path: Path
) -> None:
    lib = seal()
    mirror = tmp_path / "mirror"
    shutil.copytree(tmp_path / "vault" / "library", mirror / "library")
    corrupt(lib / "MANIFEST.json.sig", offset=60)
    scrub = Scrubber(vault, tmp_path / "s.sqlite")
    findings = scrub.run().findings
    assert [f.relpath for f in findings] == [None]
    no_mirror = RepairLadder(vault, ledger, rollback_current=False).repair_findings(findings)
    assert no_mirror[0].degraded
    results = RepairLadder(vault, ledger, mirror_root=mirror).repair_findings(findings)
    assert [r.ok for r in results] == [True]
    assert vault.verify_manifest(SOURCE, GEN).ok


def test_unexpected_files_are_quarantined(
    vault: FileVault, seal: SealFn, ledger: JsonlLedger, tmp_path: Path
) -> None:
    lib = seal()
    (lib / "upstream/stray").write_bytes(b"?")
    findings = Scrubber(vault, tmp_path / "s.sqlite").run().findings
    assert RepairLadder(vault, ledger).repair_findings(findings)[0].ok
    assert not (lib / "upstream/stray").exists()
    assert "repair.quarantine" in kinds(ledger)


def test_nothing_to_do_writes_no_events(vault: FileVault, seal: SealFn, ledger: JsonlLedger) -> None:
    seal()
    assert RepairLadder(vault, ledger).repair(SOURCE, GEN).ok
    assert kinds(ledger) == []


@pytest.mark.skipif(shutil.which("par2") is None, reason="par2 not installed")
def test_real_par2_round_trip(tmp_path: Path, signer: object) -> None:
    from wsp_vault.parity import Par2Parity

    gen = tmp_path / "gen"
    (gen / "upstream").mkdir(parents=True)
    f = gen / "upstream" / "data.bin"
    f.write_bytes(bytes(range(256)) * 400)
    parity = Par2Parity()
    created = parity.create([f], gen / "parity", 10)
    assert created
    raw = bytearray(f.read_bytes())
    raw[100] ^= 0xFF
    f.write_bytes(bytes(raw))
    assert parity.repair([f], gen / "parity")
    assert f.read_bytes() == bytes(range(256)) * 400
