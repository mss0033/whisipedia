"""Signing-key handling in the composition root (vault-fetch audit #1). Everything lives in tmp_path:
the owner's real key and ~/.config are never touched."""

from __future__ import annotations

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
