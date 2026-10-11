"""FileVault and partitioned sources (ADR-0010): per-partition pointers, current set, prune guards."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from wsp_core.model import SourceSpec, SourceStatus, Tier
from wsp_vault.ledger import JsonlLedger
from wsp_vault.signing import Ed25519Signer
from wsp_vault.vault import CURRENT_NAME, PARTITIONS_DIR, FileVault, VaultError, partitioned_from_specs

SRC = "topo"
LocalFn = Callable[[str, bytes], Path]


@pytest.fixture
def pvault(tmp_path: Path, signer: Ed25519Signer, ledger: JsonlLedger) -> FileVault:
    return FileVault(tmp_path / "vault", signer, domains={SRC: "maps"}, partitioned=[SRC], ledger=ledger)


def seal(vault: FileVault, make_local: LocalFn, gid: str, *, promote: bool = True) -> None:
    vault.stage(SRC, gid)
    vault.ingest(SRC, gid, make_local(f"{gid}/a.pdf", gid.encode()), "upstream/a.pdf")
    vault.seal(SRC, gid)
    if promote:
        vault.promote(SRC, gid)


def ids(vault: FileVault) -> list[str]:
    return [g.generation_id for g in vault.current_set(SRC)]


def test_promote_keeps_every_partition_current(pvault: FileVault, make_local: LocalFn) -> None:
    seal(pvault, make_local, "2026-10-01.ak")
    seal(pvault, make_local, "2026-10-02.ca")
    seal(pvault, make_local, "2026-10-03.hi")
    assert ids(pvault) == ["2026-10-01.ak", "2026-10-02.ca", "2026-10-03.hi"]
    current = pvault.current(SRC)
    assert current is not None
    assert current.generation_id == "2026-10-03.hi"
    seal(pvault, make_local, "2026-11-01.ak")  # a later generation of the same partition replaces it
    assert ids(pvault) == ["2026-10-02.ca", "2026-10-03.hi", "2026-11-01.ak"]
    pointer = pvault.source_dir(SRC) / PARTITIONS_DIR / "ak.txt"
    assert pointer.read_text(encoding="utf-8") == "2026-11-01.ak\n"
    assert (pvault.source_dir(SRC) / CURRENT_NAME).read_text(encoding="utf-8") == "2026-11-01.ak\n"


def test_promote_records_the_partition(pvault: FileVault, make_local: LocalFn, ledger: JsonlLedger) -> None:
    seal(pvault, make_local, "2026-10-01.ak")
    seal(pvault, make_local, "2026-11-01.ak")
    event = ledger.tail(1)[0]
    assert event.kind == "vault.promote"
    assert event.data["partition"] == "ak"
    assert event.data["previous_in_partition"] == "2026-10-01.ak"


def test_prune_never_retires_a_partition(pvault: FileVault, make_local: LocalFn) -> None:
    seal(pvault, make_local, "2026-09-01.ak")
    seal(pvault, make_local, "2026-10-01.ak")
    seal(pvault, make_local, "2026-10-02.ca")
    seal(pvault, make_local, "2026-10-03.hi", promote=False)  # newest of its partition, not current
    with pytest.raises(VaultError, match="current"):
        pvault.prune(SRC, "2026-10-02.ca")  # current of its partition (not CURRENT.txt)
    with pytest.raises(VaultError, match="current"):
        pvault.prune(SRC, "2026-10-01.ak")
    with pytest.raises(VaultError, match="newest"):
        pvault.prune(SRC, "2026-10-03.hi")
    pvault.prune(SRC, "2026-09-01.ak")  # an older generation of a partition may go
    assert ids(pvault) == ["2026-10-01.ak", "2026-10-02.ca"]


def test_pointers_on_disk_win_over_configuration(
    tmp_path: Path, signer: Ed25519Signer, pvault: FileVault, make_local: LocalFn
) -> None:
    seal(pvault, make_local, "2026-10-01.ak")
    seal(pvault, make_local, "2026-10-02.ca")
    plain = FileVault(tmp_path / "vault", signer)  # a reader that does not know the registry
    assert plain.is_partitioned(SRC)
    assert [g.generation_id for g in plain.current_set(SRC)] == ["2026-10-01.ak", "2026-10-02.ca"]


def test_generation_promoted_before_partitioning_stays_in_the_set(
    tmp_path: Path, signer: Ed25519Signer, make_local: LocalFn
) -> None:
    root = tmp_path / "vault"
    before = FileVault(root, signer, domains={SRC: "maps"})
    seal(before, make_local, "2026-09-01")  # no partition in its id, promoted while unpartitioned
    after = FileVault(root, signer, domains={SRC: "maps"}, partitioned=[SRC])
    assert [g.generation_id for g in after.current_set(SRC)] == ["2026-09-01"]
    seal(after, make_local, "2026-10-01.ak")
    assert [g.generation_id for g in after.current_set(SRC)] == ["2026-10-01.ak"]  # CURRENT.txt moved on
    assert after.current_set_ids(SRC) == ["2026-10-01.ak"]


def test_ordinary_sources_are_unchanged(vault: FileVault, make_local: LocalFn) -> None:
    for gid in ("2026-09-01.x", "2026-10-01.y"):
        vault.stage("plain", gid)
        vault.ingest("plain", gid, make_local(f"p/{gid}", b"x"), "upstream/x")
        vault.seal("plain", gid)
        vault.promote("plain", gid)
    assert not vault.is_partitioned("plain")
    assert [g.generation_id for g in vault.current_set("plain")] == ["2026-10-01.y"]
    assert not (vault.source_dir("plain") / PARTITIONS_DIR).exists()


def test_partitioned_from_specs() -> None:
    def spec(sid: str, access: dict[str, object]) -> SourceSpec:
        return SourceSpec(
            sid, sid, Tier.T2, SourceStatus.CANDIDATE, "PD", "pd", ("maps",), raw={"access": access}
        )

    specs = [spec("a", {"partitioned": True}), spec("b", {"partitioned": "yes"}), spec("c", {})]
    assert partitioned_from_specs(specs) == {"a"}
