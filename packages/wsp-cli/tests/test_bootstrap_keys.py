"""Signing-key handling in the composition root (vault-fetch audit #1). Everything lives in tmp_path:
the owner's real key and ~/.config are never touched."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from wsp_cli import bootstrap
from wsp_vault.signing import Ed25519Signer, KeyringVerifier, SigningError
from wsp_vault.vault import FileVault, KeyPinError


def _cfg(tmp_path: Path) -> dict[str, Any]:
    return {
        "vault": {"root": str(tmp_path / "vault"), "signing_key": str(tmp_path / "keys" / "signing.pem")},
        "index": {"root": str(tmp_path / "index")},
    }


def _sealed_vault(tmp_path: Path, signer: Ed25519Signer) -> FileVault:
    vault = FileVault(tmp_path / "vault", signer, lock_dir=tmp_path / "locks")
    src = tmp_path / "f.txt"
    src.write_bytes(b"content")
    vault.ingest("src", "2026-10-01", src, "upstream/f.txt")
    vault.seal("src", "2026-10-01")
    return vault


def test_first_key_is_created_for_an_empty_vault(tmp_path: Path) -> None:
    (tmp_path / "vault").mkdir()
    signer = bootstrap.load_signer(_cfg(tmp_path), environ={})
    assert isinstance(signer, Ed25519Signer)
    key = tmp_path / "keys" / "signing.pem"
    assert key.is_file()
    assert key.stat().st_mode & 0o777 == 0o600


def test_no_key_is_created_once_the_vault_has_signed_content(tmp_path: Path) -> None:
    _sealed_vault(tmp_path, Ed25519Signer.generate())
    with pytest.raises(bootstrap.ConfigError, match="Refusing to create a new key") as info:
        bootstrap.load_signer(_cfg(tmp_path), environ={})
    assert str(tmp_path / "keys" / "signing.pem") in str(info.value)
    assert not (tmp_path / "keys" / "signing.pem").exists()


def test_signed_manifests_without_keys_dir_also_block_key_creation(tmp_path: Path) -> None:
    import shutil

    _sealed_vault(tmp_path, Ed25519Signer.generate())
    shutil.rmtree(tmp_path / "vault" / "keys")
    with pytest.raises(bootstrap.ConfigError, match="none published"):
        bootstrap.load_signer(_cfg(tmp_path), environ={})


def test_readers_without_a_private_key_verify_with_the_published_keys(tmp_path: Path) -> None:
    owner = Ed25519Signer.generate()
    _sealed_vault(tmp_path, owner)
    signer = bootstrap.load_signer(_cfg(tmp_path), create=False, environ={})
    assert isinstance(signer, KeyringVerifier)
    reader = FileVault(tmp_path / "vault", signer, lock_dir=tmp_path / "locks")
    assert reader.manifest("src", "2026-10-01").entries  # verifies
    with pytest.raises(SigningError):
        signer.sign(b"x")


def test_a_replaced_key_file_is_refused_by_the_vault(tmp_path: Path) -> None:
    _sealed_vault(tmp_path, Ed25519Signer.generate())
    key = tmp_path / "keys" / "signing.pem"
    key.parent.mkdir()
    key.write_bytes(Ed25519Signer.generate().private_pem())  # e.g. restored the wrong backup
    cfg = _cfg(tmp_path)
    vault = bootstrap.build_vault(cfg, (), bootstrap.load_signer(cfg, environ={}), ledger=False)
    with pytest.raises(KeyPinError):
        vault.stage("src", "2026-11-01")


def test_vault_locks_live_in_their_own_local_directory(tmp_path: Path) -> None:
    (tmp_path / "vault").mkdir()
    cfg = _cfg(tmp_path)
    vault = bootstrap.build_vault(cfg, (), bootstrap.load_signer(cfg, environ={}), ledger=False)
    assert vault.locks.directory == tmp_path / "locks" / "vault"


def test_ingest_pace_follows_the_vault_section(tmp_path: Path) -> None:
    """``ingest_bytes_per_sec`` paces an ingest's copies and hashes; 0 switches the pacing off."""
    from wsp_cli.bootstrap import ingest_pace

    base = {"vault": {"root": str(tmp_path)}, "index": {"root": str(tmp_path / "index")}}
    assert ingest_pace({**base, "vault": {**base["vault"], "ingest_bytes_per_sec": 0}}) is None
    pace = ingest_pace(base)
    assert pace is not None
    pace(1024)  # within the burst: returns at once


def test_ingest_pace_slows_down_while_the_owner_browses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Interactive use slows an ingest to a share of its rate; it never raises (a seal step that
    failed on a pause counted an attempt against the release, 2026-10-14)."""
    from wsp_cli import bootstrap

    def always_busy(path: Path | None, window_seconds: float, *, now: float | None = None) -> bool:
        return True

    monkeypatch.setattr("wsp_core.activity.recently_active", always_busy)
    base = {
        "vault": {"root": str(tmp_path), "ingest_bytes_per_sec": 1e9},
        "index": {"root": str(tmp_path / "i")},
    }
    pace = bootstrap.ingest_pace(base)
    assert pace is not None
    pace(4096)  # busy: the slow limiter, still within its burst


def test_ingest_peers_count_the_live_ingests_and_drop_stale_slots(tmp_path: Path) -> None:
    """The ingest budget is shared by the ingests alive at once; a slot of a dead process is dropped."""
    from wsp_cli.bootstrap import ingest_peers

    slots = tmp_path / "ingest-slots"
    assert ingest_peers(slots) == 1
    (slots / str(os.getppid())).touch()  # another live process
    dead = str(2**22 + 1)  # above this kernel's pid_max (4194304): cannot exist
    (slots / dead).touch()
    (slots / "notes.txt").touch()  # not a slot
    assert ingest_peers(slots) == 2
    assert not (slots / dead).exists()
    assert (slots / str(os.getpid())).exists()


def test_an_idle_process_with_a_slot_stops_counting(tmp_path: Path) -> None:
    """The daemon seals in a long-lived process: its slot counts only while it paces, and a slot
    left untouched for long (a reused pid) goes."""
    import time

    from wsp_cli.bootstrap import INGEST_SLOT_FORGET, INGEST_SLOT_LIVE, ingest_peers

    slots = tmp_path / "ingest-slots"
    slots.mkdir()
    peer = slots / str(os.getppid())  # a live process
    peer.touch()
    now = time.time()
    assert ingest_peers(slots, now=now) == 2
    os.utime(peer, (now - INGEST_SLOT_LIVE - 1, now - INGEST_SLOT_LIVE - 1))
    assert ingest_peers(slots, now=now) == 1  # idle: not counted, kept
    assert peer.exists()
    os.utime(peer, (now - INGEST_SLOT_FORGET - 1, now - INGEST_SLOT_FORGET - 1))
    assert ingest_peers(slots, now=now) == 1
    assert not peer.exists()  # forgotten


def test_map_jobs_pace_from_their_own_budget_and_pool(tmp_path: Path) -> None:
    """``pool="maps"`` paces from ``maps_bytes_per_sec`` in its own slot pool, so the ingest lanes do
    not split the map budget (owner, 2026-10-18); with no maps budget it shares the ingest one."""
    from wsp_cli.bootstrap import INGEST_SLOTS_DIR, MAPS_SLOTS_DIR, ingest_pace

    state = tmp_path / "state"
    base = {
        "vault": {"root": str(tmp_path), "ingest_bytes_per_sec": 0, "maps_bytes_per_sec": 1e9},
        "index": {"root": str(tmp_path / "i")},
        "orchestrator": {"state_db": str(state / "orchestrator.db")},
    }
    assert ingest_pace(base) is None  # the ingest budget is off
    pace = ingest_pace(base, pool="maps")
    assert pace is not None
    pace(1024)
    by_key = ingest_pace({**base, "vault": {**base["vault"], "pace_pool": "maps"}})
    assert by_key is not None
    assert {p.name for p in (state / MAPS_SLOTS_DIR).iterdir()} == {str(os.getpid())}
    assert not (state / INGEST_SLOTS_DIR).exists()
    shared = {**base, "vault": {"root": str(tmp_path), "ingest_bytes_per_sec": 1e9}}
    shared_pace = ingest_pace(shared, pool="maps")
    assert shared_pace is not None
    shared_pace(1024)
    assert (state / INGEST_SLOTS_DIR / str(os.getpid())).exists()  # no maps budget: the ingest pool
