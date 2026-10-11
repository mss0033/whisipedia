"""Seal-time parity with the real par2 binary: bound to verified bytes (audit #7), no wedge (#14)."""

from __future__ import annotations

import os
import shutil
from collections.abc import Sequence
from pathlib import Path

import pytest
from vaultkit import SOURCE

from wsp_vault.manifest import PARITY_DIR, ROLE_PARITY
from wsp_vault.parity import Par2Parity, ParityError
from wsp_vault.scrub import Scrubber
from wsp_vault.signing import Ed25519Signer
from wsp_vault.vault import FileVault

pytestmark = pytest.mark.skipif(shutil.which("par2") is None, reason="par2 not installed")
GEN = "2026-10-07"


def _vault(tmp_path: Path, parity: Par2Parity) -> FileVault:
    return FileVault(tmp_path / "vault", Ed25519Signer.generate(), parity=parity, lock_dir=tmp_path / "locks")


def _stage(vault: FileVault, tmp_path: Path) -> None:
    for name, size in (("a.bin", 120_000), ("b.bin", 9_000)):
        src = tmp_path / "src" / name
        src.parent.mkdir(exist_ok=True)
        src.write_bytes(os.urandom(size))
        vault.ingest(SOURCE, GEN, src, f"upstream/{name}")


def test_seal_parity_is_created_and_scrubs_clean(tmp_path: Path) -> None:
    vault = _vault(tmp_path, Par2Parity())
    _stage(vault, tmp_path)
    vault.seal(SOURCE, GEN, parity_percent=10)
    manifest = vault.manifest(SOURCE, GEN)
    assert any(e.role == ROLE_PARITY for e in manifest.entries)
    assert Scrubber(vault, tmp_path / "s.sqlite").scrub_generation(SOURCE, GEN) == []


def test_bytes_changed_after_verification_are_never_encoded(tmp_path: Path) -> None:
    class Racing(Par2Parity):
        def create(self, files: Sequence[Path], out_dir: Path, percent: int) -> Sequence[Path]:
            victim = files[0]
            data = bytearray(victim.read_bytes())
            data[5] ^= 0xFF  # bit rot (or a stray writer) between our verification pass and par2's read
            victim.write_bytes(bytes(data))
            return super().create(files, out_dir, percent)

    vault = _vault(tmp_path, Racing())
    _stage(vault, tmp_path)
    with pytest.raises(ParityError, match="differ"):
        vault.seal(SOURCE, GEN, parity_percent=10)
    stg = vault.staging_path(SOURCE, GEN)
    assert not (stg / PARITY_DIR).exists()
    assert not vault.library_path(SOURCE, GEN).exists()


def test_leftovers_of_a_killed_seal_do_not_wedge_it(tmp_path: Path) -> None:
    vault = _vault(tmp_path, Par2Parity())
    _stage(vault, tmp_path)
    stale = vault.staging_path(SOURCE, GEN) / PARITY_DIR
    stale.mkdir()
    (stale / "recovery.par2").write_bytes(b"half-written by a killed par2")
    (stale / "recovery.vol000+01.par2").write_bytes(b"x")
    vault.seal(SOURCE, GEN, parity_percent=10)
    assert Scrubber(vault, tmp_path / "s.sqlite").scrub_generation(SOURCE, GEN) == []
