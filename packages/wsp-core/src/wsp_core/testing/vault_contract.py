"""Contract suite for :class:`wsp_core.ports.Vault` (PLAN §7, ADR-0002).

Subclass in an implementation's tests and implement :meth:`VaultContract.make_vault`::

    class TestFileVault(VaultContract):
        def make_vault(self, root: Path) -> Vault:
            return FileVault(root, signer)
"""

from __future__ import annotations

import hashlib
from pathlib import Path, PurePosixPath

import blake3
import pytest

from wsp_core.model import GenerationStatus
from wsp_core.ports import Vault


class VaultContract:
    #: Exception type the implementation raises for invalid operations (e.g. promoting unsealed data).
    vault_error: type[BaseException] = Exception

    def make_vault(self, root: Path) -> Vault:
        raise NotImplementedError

    @pytest.fixture
    def vault(self, tmp_path: Path) -> Vault:
        return self.make_vault(tmp_path / "vault")

    @pytest.fixture
    def local(self, tmp_path: Path) -> Path:
        path = tmp_path / "work" / "sample.bin"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"whisipedia contract sample\n" * 1000)
        return path

    def _seal_one(self, vault: Vault, local: Path, generation_id: str = "2026-10-01") -> None:
        vault.stage("contract-src", generation_id)
        vault.ingest("contract-src", generation_id, local, "upstream/sample.bin")
        vault.seal("contract-src", generation_id)

    def test_stage_is_idempotent(self, vault: Vault) -> None:
        first = vault.stage("contract-src", "2026-10-01")
        assert first.is_dir()
        assert vault.stage("contract-src", "2026-10-01") == first

    def test_ingest_hashes_and_keeps_source(self, vault: Vault, local: Path) -> None:
        data = local.read_bytes()
        vault.stage("contract-src", "2026-10-01")
        art = vault.ingest("contract-src", "2026-10-01", local, "upstream/sample.bin")
        assert art.relpath == PurePosixPath("upstream/sample.bin")
        assert art.size == len(data)
        assert art.sha256 == hashlib.sha256(data).hexdigest()
        assert art.blake3 == blake3.blake3(data).hexdigest()
        assert local.read_bytes() == data

    def test_seal_lists_artifacts_and_keeps_bytes(self, vault: Vault, local: Path) -> None:
        self._seal_one(vault, local)
        gens = vault.generations("contract-src")
        assert [g.generation_id for g in gens] == ["2026-10-01"]
        gen = gens[0]
        assert gen.status is GenerationStatus.SEALED
        assert gen.sealed_at is not None
        assert [str(a.relpath) for a in gen.artifacts] == ["upstream/sample.bin"]
        stored = vault.generation_path("contract-src", "2026-10-01") / "upstream/sample.bin"
        assert stored.read_bytes() == local.read_bytes()

    def test_current_is_none_until_promoted(self, vault: Vault, local: Path) -> None:
        assert vault.current("contract-src") is None
        self._seal_one(vault, local)
        assert vault.current("contract-src") is None
        vault.promote("contract-src", "2026-10-01")
        current = vault.current("contract-src")
        assert current is not None
        assert current.generation_id == "2026-10-01"

    def test_promote_switches_between_generations(self, vault: Vault, local: Path) -> None:
        self._seal_one(vault, local, "2026-09-01")
        self._seal_one(vault, local, "2026-10-01")
        vault.promote("contract-src", "2026-10-01")
        vault.promote("contract-src", "2026-09-01")
        current = vault.current("contract-src")
        assert current is not None
        assert current.generation_id == "2026-09-01"
        assert [g.generation_id for g in vault.generations("contract-src")] == ["2026-09-01", "2026-10-01"]

    def test_promote_requires_sealed_generation(self, vault: Vault, local: Path) -> None:
        vault.stage("contract-src", "2026-10-01")
        vault.ingest("contract-src", "2026-10-01", local, "upstream/sample.bin")
        with pytest.raises(self.vault_error):
            vault.promote("contract-src", "2026-10-01")
        assert vault.current("contract-src") is None

    def test_sealed_generation_cannot_be_restaged(self, vault: Vault, local: Path) -> None:
        self._seal_one(vault, local)
        with pytest.raises(self.vault_error):
            vault.stage("contract-src", "2026-10-01")
