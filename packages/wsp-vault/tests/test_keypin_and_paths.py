"""Key pinning (audit #1), adoption TOCTOU (#24), path rules (#12, #22), manifest bytes (#19),
seal crash recovery (#24) and checksum normalization (#23)."""

from __future__ import annotations

import hashlib
import json
import os
import random
import shutil
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest
from vaultkit import FILES, GEN, SOURCE, SealFn

from wsp_core.model import Checksum, GenerationStatus
from wsp_vault import vault as vault_mod
from wsp_vault.atomic import IntegrityError, readback
from wsp_vault.hashing import Digests
from wsp_vault.manifest import (
    MANIFEST_NAME,
    SIGNATURE_NAME,
    STAGING_RECORD,
    Manifest,
    ManifestEntry,
    ManifestError,
    check_relpath,
)
from wsp_vault.signing import Ed25519Signer
from wsp_vault.vault import KEYS_DIR, FileVault, KeyPinError, VaultError

LocalFn = Callable[[str, bytes], Path]


# --- key pinning ---------------------------------------------------------------------------------


def test_a_new_key_is_refused_with_both_key_ids(vault: FileVault, seal: SealFn, tmp_path: Path) -> None:
    seal()
    (pinned,) = (tmp_path / "vault" / KEYS_DIR).glob("*.pub")
    stranger = Ed25519Signer.generate()
    other = FileVault(tmp_path / "vault", stranger, domains={SOURCE: "reference"})
    with pytest.raises(KeyPinError) as info:
        other.stage(SOURCE, "2026-11-01")
    message = str(info.value)
    assert pinned.stem in message  # the pinned key
    assert vault_mod.key_label(stranger.public_key_text(), stranger.key_id) in message  # the refused one
    assert "Restore the original private key" in message
    for mutate in (
        lambda: other.promote(SOURCE, GEN),
        lambda: other.mark_degraded(SOURCE, GEN, "x", []),
        lambda: other.prune(SOURCE, GEN),
        lambda: other.publish_key(),
        lambda: other.seal(SOURCE, GEN),
    ):
        with pytest.raises(KeyPinError):
            mutate()
    assert [p.name for p in (tmp_path / "vault" / KEYS_DIR).iterdir()] == [pinned.name]
    assert not (vault.library_path(SOURCE, GEN) / "DEGRADED.txt").exists()


def test_reading_with_a_wrong_key_reports_degraded_but_writes_nothing(seal: SealFn, tmp_path: Path) -> None:
    seal()
    reader = FileVault(tmp_path / "vault", Ed25519Signer.generate(), domains={SOURCE: "reference"})
    (gen,) = reader.generations(SOURCE)
    assert gen.status is GenerationStatus.DEGRADED  # the signature does not verify for this reader
    assert len(list((tmp_path / "vault" / KEYS_DIR).iterdir())) == 1


def test_empty_keys_dir_with_signed_content(vault: FileVault, seal: SealFn, tmp_path: Path) -> None:
    seal()
    shutil.rmtree(tmp_path / "vault" / KEYS_DIR)
    stranger = FileVault(tmp_path / "vault", Ed25519Signer.generate(), domains={SOURCE: "reference"})
    with pytest.raises(KeyPinError, match="verifies with this key"):
        stranger.stage(SOURCE, "2026-11-01")
    owner = FileVault(tmp_path / "vault", vault.signer, domains={SOURCE: "reference"})
    owner.check_signer()
    assert owner.publish_key() is not None  # the right key may restore keys/
    assert len(list((tmp_path / "vault" / KEYS_DIR).glob("*.pub"))) == 1


def test_first_seal_pins_the_key(tmp_path: Path, make_local: LocalFn) -> None:
    signer = Ed25519Signer.generate()
    v = FileVault(tmp_path / "vault", signer)
    v.ingest(SOURCE, GEN, make_local("a", b"a"), "upstream/a")
    v.seal(SOURCE, GEN)
    (key,) = (tmp_path / "vault" / KEYS_DIR).glob("*.pub")
    assert vault_mod.key_material(key.read_text()) == vault_mod.key_material(signer.public_key_text())
    # the same key under another comment/id is the same key
    renamed = Ed25519Signer.from_private_pem(signer.private_pem(), key_id="other-name")
    FileVault(tmp_path / "vault", renamed).check_signer()


# --- adoption ------------------------------------------------------------------------------------


def _incoming(tmp_path: Path, data: bytes, name: str = "big.zim") -> Path:
    path = tmp_path / "vault" / "_incoming" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def test_adopt_detects_a_file_changing_while_it_is_hashed(
    vault: FileVault, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = _incoming(tmp_path, FILES["upstream/alpha.zim"])
    real = readback

    def writer_still_appending(path: Path, **kw: object) -> Digests:
        got = real(path, **kw)  # type: ignore[arg-type]
        with path.open("ab") as f:  # a writer that still had the file open
            f.write(b"late bytes")
        return got

    monkeypatch.setattr("wsp_vault.vault.readback", writer_still_appending)
    with pytest.raises(VaultError, match="changed while it was being hashed"):
        vault.adopt(SOURCE, GEN, src, "upstream/big.zim")
    assert src.exists()  # given back
    assert not list(vault.staging_path(SOURCE, GEN).rglob("*.adopting"))
    assert not (vault.staging_path(SOURCE, GEN) / "upstream/big.zim").exists()


def test_adopt_with_bad_checksum_gives_the_file_back(vault: FileVault, tmp_path: Path) -> None:
    src = _incoming(tmp_path, b"payload")
    with pytest.raises(IntegrityError):
        vault.adopt(SOURCE, GEN, src, "upstream/p", upstream=[Checksum("SHA-256", "0" * 64)])
    assert src.read_bytes() == b"payload"


def test_adopt_resumes_after_a_crash_mid_adoption(vault: FileVault, tmp_path: Path) -> None:
    data = b"resumable"
    src = _incoming(tmp_path, data)
    staging = vault.stage(SOURCE, GEN)
    moving = staging / "upstream" / "r.bin.adopting"
    moving.parent.mkdir(parents=True)
    os.rename(src, moving)  # the crash happened right after moving the file aside
    with pytest.raises(VaultError, match="interrupted adoption"):
        vault.seal(SOURCE, GEN)
    art = vault.adopt(SOURCE, GEN, src, "upstream/r.bin")
    assert art.sha256 == hashlib.sha256(data).hexdigest()
    assert not moving.exists()
    assert (staging / "upstream/r.bin").read_bytes() == data


def test_a_leftover_adoption_with_the_same_bytes_as_a_new_fetch_is_dropped(
    vault: FileVault, tmp_path: Path
) -> None:
    """2026-10-20, OpenAlex: an ingest killed after moving a file aside, then the lane fetched the file
    again; the two copies were identical, and every later ingest stopped for a person."""
    data = b"same bytes"
    staging = vault.stage(SOURCE, GEN)
    moving = staging / "upstream" / "d.bin.adopting"
    moving.parent.mkdir(parents=True)
    moving.write_bytes(data)
    src = _incoming(tmp_path, data)
    art = vault.adopt(SOURCE, GEN, src, "upstream/d.bin")
    assert art.sha256 == hashlib.sha256(data).hexdigest()
    assert not moving.exists()
    assert (staging / "upstream/d.bin").read_bytes() == data


def test_a_leftover_adoption_that_differs_from_a_new_fetch_still_needs_a_person(
    vault: FileVault, tmp_path: Path
) -> None:
    staging = vault.stage(SOURCE, GEN)
    moving = staging / "upstream" / "e.bin.adopting"
    moving.parent.mkdir(parents=True)
    moving.write_bytes(b"old copy!")
    src = _incoming(tmp_path, b"new bytes")
    with pytest.raises(VaultError, match="inspect it"):
        vault.adopt(SOURCE, GEN, src, "upstream/e.bin")
    assert moving.read_bytes() == b"old copy!"  # both left in place
    assert src.read_bytes() == b"new bytes"


# --- paths and manifests -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        "upstream/x.repair",
        "upstream/x.adopting",
        "upstream/a\x00b",
        "upstream/tab\there",
        "upstream/bell\x07",
        "manifest.json",
        "Degraded.TXT",
        "PARITY/x",
        "upstream/x.PARTIAL",
    ],
)
def test_new_reservations(bad: str) -> None:
    with pytest.raises(ManifestError):
        check_relpath(bad)


def test_ordinary_unicode_names_are_fine() -> None:
    assert str(check_relpath("upstream/Ünïcode ~ ok/日本.txt")) == "upstream/Ünïcode ~ ok/日本.txt"


@pytest.mark.parametrize("evil", ["../../etc/passwd", "/etc/passwd", "upstream/../../x", "a\\b", "x/./y"])
def test_signed_manifest_with_unsafe_path_is_rejected(evil: str) -> None:
    doc = json.loads(
        Manifest("s", "g", datetime(2026, 1, 1, tzinfo=UTC), (ManifestEntry("upstream/a", 1, "a", "b"),))
        .to_bytes()
        .decode()
    )
    doc["files"][0]["path"] = evil
    with pytest.raises(ManifestError, match="unsafe"):
        Manifest.from_bytes(json.dumps(doc).encode())


def test_fast_serializer_is_byte_identical_to_the_definition() -> None:
    rng = random.Random(7)
    alphabet = "abcXYZ019 _-.()'\"\\é日本ü\u2028"
    entries: list[ManifestEntry] = []
    for i in range(400):
        name = "".join(rng.choice(alphabet) for _ in range(rng.randrange(1, 20)))
        ups = [
            Checksum(rng.choice(["md5", "sha1", "sha256"]), f"{rng.getrandbits(64):016x}")
        ] * rng.randrange(3)
        role = rng.choice(["data", "parity"])
        entries.append(
            ManifestEntry.from_digests(
                f"{'parity' if role == 'parity' else 'upstream'}/{i}/{name}",
                Digests(
                    rng.randrange(10**12), f"{rng.getrandbits(256):064x}", f"{rng.getrandbits(256):064x}"
                ),
                role=role,
                upstream=ups,
            )
        )
    m = Manifest("src-id", "2026-10.ca", datetime(2026, 10, 11, 1, 2, 3, 456789, tzinfo=UTC), tuple(entries))
    assert m.to_bytes() == m.to_bytes_reference()
    empty = Manifest("s", "g", datetime(2026, 1, 1, tzinfo=UTC), ())
    assert empty.to_bytes() == empty.to_bytes_reference()
    safe = Manifest(
        m.source_id, m.generation_id, m.sealed_at, tuple(e for e in entries if "\\" not in e.path)
    )
    assert Manifest.from_bytes(safe.to_bytes()) == safe
    with pytest.raises(ManifestError, match="unsafe"):  # a backslash is escaped correctly, then refused
        Manifest.from_bytes(m.to_bytes())


def test_entry_lookup_is_indexed() -> None:
    entries = tuple(ManifestEntry(f"upstream/{i:06d}", 1, "a", "b") for i in range(50_000))
    m = Manifest("s", "g", datetime(2026, 1, 1, tzinfo=UTC), entries)
    assert m.entry("upstream/049999") is entries[-1]
    assert m.entry("nope") is None


def test_upstream_algorithm_spellings_are_verified(vault: FileVault, make_local: LocalFn) -> None:
    """``SHA-256`` used to be recorded but never checked (audit #23)."""
    data = b"checksummed"
    with pytest.raises(IntegrityError):
        vault.ingest(
            SOURCE, GEN, make_local("x", data), "upstream/x", upstream=[Checksum("SHA-256", "0" * 64)]
        )
    with pytest.raises(IntegrityError):
        vault.ingest(SOURCE, GEN, make_local("y", data), "upstream/y", upstream=[Checksum("SHA-1", "0" * 40)])
    good = Checksum("SHA-1", hashlib.sha1(data).hexdigest().upper())
    art = vault.ingest(SOURCE, GEN, make_local("z", data), "upstream/z", upstream=[good])
    assert art.upstream == (Checksum("sha1", hashlib.sha1(data).hexdigest()),)


# --- seal crash recovery ---------------------------------------------------------------------------


def test_stale_staged_manifest_is_rewritten_when_more_was_ingested(
    vault: FileVault, make_local: LocalFn, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault.ingest(SOURCE, GEN, make_local("a", b"a"), "upstream/a")
    stg = vault.staging_path(SOURCE, GEN)
    real_unlink = Path.unlink

    def crash_on_record_unlink(self: Path, missing_ok: bool = False) -> None:
        if self.name == STAGING_RECORD:
            raise KeyboardInterrupt("killed after writing the manifest")
        real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", crash_on_record_unlink)
    with pytest.raises(KeyboardInterrupt):
        vault.seal(SOURCE, GEN)
    monkeypatch.undo()
    assert (stg / MANIFEST_NAME).exists()
    assert (stg / STAGING_RECORD).exists()
    vault.ingest(SOURCE, GEN, make_local("b", b"b"), "upstream/b")  # ingested after the stale manifest
    gen = vault.seal(SOURCE, GEN)
    assert {str(a.relpath) for a in gen.artifacts} == {"upstream/a", "upstream/b"}
    assert not (vault.library_path(SOURCE, GEN) / STAGING_RECORD).exists()


def test_record_is_never_published_after_a_crash_before_its_removal(
    vault: FileVault, make_local: LocalFn, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault.ingest(SOURCE, GEN, make_local("a", b"a"), "upstream/a")
    real_unlink = Path.unlink

    def crash(self: Path, missing_ok: bool = False) -> None:
        if self.name == STAGING_RECORD:
            raise KeyboardInterrupt
        real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", crash)
    with pytest.raises(KeyboardInterrupt):
        vault.seal(SOURCE, GEN)
    monkeypatch.undo()
    sig_before = (vault.staging_path(SOURCE, GEN) / SIGNATURE_NAME).read_bytes()
    vault.seal(SOURCE, GEN)
    lib = vault.library_path(SOURCE, GEN)
    assert not (lib / STAGING_RECORD).exists()
    assert (lib / SIGNATURE_NAME).read_bytes() == sig_before  # the valid manifest was kept, not rewritten
