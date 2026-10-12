from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest
from vaultkit import FILES, GEN, SOURCE, Clock, FakeParity, SealFn

from wsp_vault.ledger import JsonlLedger
from wsp_vault.signing import Ed25519Signer
from wsp_vault.vault import FileVault


@pytest.fixture
def signer() -> Ed25519Signer:
    return Ed25519Signer.generate()


@pytest.fixture
def clock() -> Clock:
    return Clock(datetime(2026, 10, 9, 12, 0, tzinfo=UTC))


@pytest.fixture
def ledger(tmp_path: Path, signer: Ed25519Signer, clock: Clock) -> JsonlLedger:
    return JsonlLedger(tmp_path / "vault" / "ledger", signer, clock=clock)


@pytest.fixture
def fake_parity() -> FakeParity:
    return FakeParity()


@pytest.fixture
def vault(
    tmp_path: Path, signer: Ed25519Signer, ledger: JsonlLedger, fake_parity: FakeParity, clock: Clock
) -> FileVault:
    # The test clock, so seal times follow `clock.advance` (with the wall clock, seals a test meant to be
    # hours apart could share a timestamp and a rollback found no earlier generation).
    return FileVault(
        tmp_path / "vault",
        signer,
        domains={SOURCE: "reference"},
        ledger=ledger,
        parity=fake_parity,
        clock=clock,
    )


@pytest.fixture
def make_local(tmp_path: Path) -> Callable[[str, bytes], Path]:
    def make(name: str, data: bytes) -> Path:
        path = tmp_path / "work" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    return make


@pytest.fixture
def seal(vault: FileVault, make_local: Callable[[str, bytes], Path]) -> SealFn:
    """Stage, ingest FILES, seal (with fake parity by default) and promote; returns the generation dir."""

    def run(generation_id: str = GEN, *, parity: int | None = 10, promote: bool = True) -> Path:
        vault.stage(SOURCE, generation_id)
        for rel, data in FILES.items():
            vault.ingest(SOURCE, generation_id, make_local(f"{generation_id}/{rel}", data), rel)
        vault.seal(SOURCE, generation_id, parity_percent=parity)
        if promote:
            vault.promote(SOURCE, generation_id)
        return vault.library_path(SOURCE, generation_id)

    return run
