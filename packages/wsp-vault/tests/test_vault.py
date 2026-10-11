import hashlib
import json
import os
import stat
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest
from vaultkit import FILES, GEN, SOURCE, SealFn, corrupt

from wsp_core.model import Checksum, GenerationStatus
from wsp_core.ports import Vault
from wsp_core.testing.vault_contract import VaultContract
from wsp_vault.atomic import IntegrityError
from wsp_vault.ledger import JsonlLedger
from wsp_vault.manifest import Manifest, ManifestError, check_relpath
from wsp_vault.signing import Ed25519Signer
from wsp_vault.vault import CURRENT_NAME, FileVault, SignatureError, VaultError, domains_from_specs

LocalFn = Callable[[str, bytes], Path]


class TestFileVaultContract(VaultContract):
    vault_error = VaultError

    def make_vault(self, root: Path) -> Vault:
        return FileVault(root, Ed25519Signer.generate())


def test_layout_and_manifest(vault: FileVault, seal: SealFn, tmp_path: Path) -> None:
    lib = seal()
    root = tmp_path / "vault"
    assert lib == root / "library" / "reference" / SOURCE / GEN
    assert (root / "library/reference" / SOURCE / CURRENT_NAME).read_text() == f"{GEN}\n"
    assert not (root / "staging/reference" / SOURCE).exists()
    data = (lib / "MANIFEST.json").read_bytes()
    assert vault.signer.verify(data, (lib / "MANIFEST.json.sig").read_bytes())
    doc = json.loads(data)
    paths = [f["path"] for f in doc["files"]]
    assert paths == sorted(paths)
    assert {p for p in paths if p.startswith("upstream/")} == set(FILES)
    assert any(f["role"] == "parity" for f in doc["files"])
    beta = next(f for f in doc["files"] if f["path"] == "upstream/beta.txt")
    assert beta["sha256"] == hashlib.sha256(FILES["upstream/beta.txt"]).hexdigest()
    assert Manifest.from_bytes(data).to_bytes() == data
    assert list((root / "keys").glob("whisipedia-*.pub"))


def test_sealed_files_are_read_only(seal: SealFn) -> None:
    lib = seal()
    for path in [lib / "MANIFEST.json", lib / "upstream/beta.txt"]:
        assert not os.stat(path).st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)


def test_ledger_records_lifecycle(vault: FileVault, seal: SealFn, ledger: JsonlLedger) -> None:
    seal()
    kinds = [e.kind for e in ledger.events()]
    assert kinds.count("vault.ingest") == len(FILES)
    assert kinds[-2:] == ["vault.seal", "vault.promote"]
    assert ledger.verify()


def test_ingest_verifies_upstream_checksums(vault: FileVault, make_local: LocalFn) -> None:
    data = FILES["upstream/beta.txt"]
    local = make_local("b", data)
    good = Checksum("MD5", hashlib.md5(data).hexdigest())
    art = vault.ingest(SOURCE, GEN, local, "upstream/b", upstream=[good, Checksum("crc32c", "abc")])
    assert {c.algorithm for c in art.upstream} == {"md5", "crc32c"}
    with pytest.raises(IntegrityError):
        vault.ingest(SOURCE, GEN, local, "upstream/c", upstream=[Checksum("sha1", "0" * 40)])
    assert not (vault.staging_path(SOURCE, GEN) / "upstream/c").exists()


def test_ingest_is_idempotent_but_never_replaces(vault: FileVault, make_local: LocalFn) -> None:
    first = vault.ingest(SOURCE, GEN, make_local("a", b"one"), "upstream/a")
    assert vault.ingest(SOURCE, GEN, make_local("a2", b"one"), "upstream/a") == first
    with pytest.raises(VaultError):
        vault.ingest(SOURCE, GEN, make_local("a3", b"two"), "upstream/a")
    assert (vault.staging_path(SOURCE, GEN) / "upstream/a").read_bytes() == b"one"


@pytest.mark.parametrize(
    "bad", ["", "/abs", "../x", "a/../b", "MANIFEST.json", "parity/x", "x.partial", "a//b"]
)
def test_reserved_or_unsafe_relpaths_are_rejected(bad: str) -> None:
    with pytest.raises(ManifestError):
        check_relpath(bad)


def test_adopt_renames_after_verifying(vault: FileVault, tmp_path: Path) -> None:
    incoming = tmp_path / "vault" / "_incoming" / "big.zim"
    incoming.parent.mkdir(parents=True)
    data = FILES["upstream/alpha.zim"]
    incoming.write_bytes(data)
    sha = Checksum("sha256", hashlib.sha256(data).hexdigest())
    art = vault.adopt(SOURCE, GEN, incoming, "upstream/big.zim", upstream=[sha])
    assert not incoming.exists()
    assert art.sha256 == sha.hexdigest
    assert (vault.staging_path(SOURCE, GEN) / "upstream/big.zim").read_bytes() == data
    # resuming after a crash between rename and record is idempotent
    assert vault.adopt(SOURCE, GEN, incoming, "upstream/big.zim") == art
    gen = vault.seal(SOURCE, GEN)
    assert [str(a.relpath) for a in gen.artifacts] == ["upstream/big.zim"]


def test_adopt_many_batches_records_and_resumes(vault: FileVault, tmp_path: Path) -> None:
    incoming = tmp_path / "vault" / "_incoming" / "tree"
    items: list[tuple[Path, str, Sequence[Checksum]]] = []
    for i in range(7):
        path = incoming / f"d{i % 2}" / f"f{i}.pdf"
        path.parent.mkdir(parents=True, exist_ok=True)
        data = f"pdf {i}".encode()
        path.write_bytes(data)
        items.append((path, f"upstream/d{i % 2}/f{i}.pdf", [Checksum("md5", hashlib.md5(data).hexdigest())]))
    # a bad checksum stops the batch; files adopted before it stay recorded
    bad = incoming / "bad.pdf"
    bad.write_bytes(b"bad")
    with pytest.raises(IntegrityError):
        vault.adopt_many(SOURCE, GEN, [*items[:3], (bad, "upstream/bad.pdf", [Checksum("md5", "0" * 32)])],
                         workers=1, flush_every=2)  # fmt: skip
    assert bad.exists()
    assert not items[0][0].exists()
    # re-run over everything: already staged files are re-hashed, the rest are renamed
    assert vault.adopt_many(SOURCE, GEN, items, workers=3, flush_every=2) == 7
    gen = vault.seal(SOURCE, GEN)
    assert sorted(str(a.relpath) for a in gen.artifacts) == sorted(rel for _, rel, _ in items)
    assert all(a.upstream for a in gen.artifacts)


def test_adopt_with_wrong_hash_leaves_file_in_place(vault: FileVault, tmp_path: Path) -> None:
    incoming = tmp_path / "vault" / "_incoming" / "big.zim"
    incoming.parent.mkdir(parents=True)
    incoming.write_bytes(b"partial download")
    with pytest.raises(IntegrityError):
        vault.adopt(SOURCE, GEN, incoming, "upstream/big.zim", upstream=[Checksum("sha256", "0" * 64)])
    assert incoming.read_bytes() == b"partial download"


def test_adopt_refuses_other_filesystem_and_symlinks(
    vault: FileVault, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = tmp_path / "vault" / "_incoming" / "f"
    src.parent.mkdir(parents=True)
    src.write_bytes(b"x")
    link = src.with_name("link")
    link.symlink_to(src)
    with pytest.raises(VaultError, match="regular file"):
        vault.adopt(SOURCE, GEN, link, "upstream/f")
    real_stat = os.stat

    def fake_stat(path: object, *a: object, **kw: object) -> os.stat_result:
        st = real_stat(path, *a, **kw)  # type: ignore[arg-type]
        if Path(str(path)) == src:
            return os.stat_result((st.st_mode, st.st_ino, st.st_dev + 1, *tuple(st)[3:]))
        return st

    monkeypatch.setattr(os, "stat", fake_stat)
    with pytest.raises(VaultError, match="different filesystem"):
        vault.adopt(SOURCE, GEN, src, "upstream/f")
    assert src.exists()


def test_seal_rejects_unrecorded_files_and_cleans_partials(vault: FileVault, make_local: LocalFn) -> None:
    vault.ingest(SOURCE, GEN, make_local("a", b"one"), "upstream/a")
    stg = vault.staging_path(SOURCE, GEN)
    (stg / "upstream/leftover.partial").write_bytes(b"half")
    (stg / "upstream/sneaky").write_bytes(b"?")
    with pytest.raises(VaultError, match="sneaky"):
        vault.seal(SOURCE, GEN)
    assert not (stg / "upstream/leftover.partial").exists()
    assert (stg / "upstream/sneaky").exists()


def test_seal_empty_or_missing_staging_fails(vault: FileVault) -> None:
    with pytest.raises(VaultError):
        vault.seal(SOURCE, GEN)
    vault.stage(SOURCE, GEN)
    with pytest.raises(VaultError):
        vault.seal(SOURCE, GEN)


def test_seal_resumes_after_crash_before_rename(vault: FileVault, make_local: LocalFn) -> None:
    vault.ingest(SOURCE, GEN, make_local("a", b"one"), "upstream/a")
    stg = vault.staging_path(SOURCE, GEN)
    vault._write_manifest(SOURCE, GEN, stg, None)  # pyright: ignore[reportPrivateUsage]
    gen = vault.seal(SOURCE, GEN)
    assert gen.status is GenerationStatus.SEALED
    assert vault.seal(SOURCE, GEN) == gen


def test_promote_refuses_bad_signature_and_degraded(vault: FileVault, seal: SealFn) -> None:
    lib = seal("2026-09-01")
    seal(GEN)
    vault.mark_degraded(SOURCE, GEN, "drill", ["upstream/beta.txt"])
    assert vault.current(SOURCE) is not None
    assert [g.status for g in vault.generations(SOURCE)] == [
        GenerationStatus.SEALED,
        GenerationStatus.DEGRADED,
    ]
    with pytest.raises(VaultError, match="DEGRADED"):
        vault.promote(SOURCE, GEN)
    corrupt(lib / "MANIFEST.json")
    with pytest.raises(SignatureError):
        vault.promote(SOURCE, "2026-09-01")
    assert not vault.verify_manifest(SOURCE, "2026-09-01").ok
    (lib / "MANIFEST.json").write_bytes(b"not json")
    assert vault.generations(SOURCE)[0].status is GenerationStatus.DEGRADED


def test_torn_pointer_write_does_not_affect_current(vault: FileVault, seal: SealFn) -> None:
    seal()
    pointer = vault.source_dir(SOURCE) / CURRENT_NAME
    pointer.with_name(CURRENT_NAME + ".partial").write_text("garbage")
    current = vault.current(SOURCE)
    assert current is not None
    assert current.generation_id == GEN


def test_dangling_current_raises(vault: FileVault, seal: SealFn) -> None:
    seal()
    (vault.source_dir(SOURCE) / CURRENT_NAME).write_text("2099-01-01\n")
    with pytest.raises(VaultError, match="missing generation"):
        vault.current(SOURCE)


def test_staging_generations_are_listed(vault: FileVault, seal: SealFn, make_local: LocalFn) -> None:
    seal()
    vault.ingest(SOURCE, "2026-11-01", make_local("n", b"new"), "upstream/n")
    gens = vault.generations(SOURCE)
    assert [(g.generation_id, g.status) for g in gens] == [
        (GEN, GenerationStatus.SEALED),
        ("2026-11-01", GenerationStatus.STAGING),
    ]
    assert vault.generation_path(SOURCE, "2026-11-01") == vault.staging_path(SOURCE, "2026-11-01")
    assert vault.sources() == [SOURCE]


def test_domain_resolution(tmp_path: Path, signer: Ed25519Signer) -> None:
    vault = FileVault(tmp_path, signer, domains={"a": "books"})
    assert vault.domain_of("a") == "books"
    assert vault.domain_of("b") == "misc"
    (tmp_path / "library" / "maps" / "a").mkdir(parents=True)
    assert vault.domain_of("a") == "maps"
    with pytest.raises(VaultError):
        vault.domain_of("Bad/Id")
    with pytest.raises(VaultError):
        FileVault(tmp_path, signer, domains={"a": "../up"})
    with pytest.raises(VaultError):
        vault.staging_path("a", "../x")


def test_domains_from_specs() -> None:
    from wsp_core.model import SourceSpec, SourceStatus, Tier

    spec = SourceSpec(
        "s", "S", Tier.T1, SourceStatus.ACTIVE, "CC0-1.0", "open-license", ("encyclopedia", "x")
    )
    assert domains_from_specs([spec]) == {"s": "encyclopedia"}


def test_manifest_parse_errors() -> None:
    with pytest.raises(ManifestError):
        Manifest.from_bytes(b"{}")
    with pytest.raises(ManifestError):
        Manifest.from_bytes(b'{"format": "other"}')


def test_prune_moves_aside_and_guards_current(vault: FileVault, seal: SealFn, ledger: JsonlLedger) -> None:
    from wsp_core.ports import Warden

    assert isinstance(vault, Warden)
    seal("2026-08-01", promote=False)
    seal("2026-09-01")
    seal("2026-10-01", promote=False)
    with pytest.raises(VaultError, match="current"):
        vault.prune(SOURCE, "2026-09-01")
    with pytest.raises(VaultError, match="newest"):
        vault.prune(SOURCE, "2026-10-01")
    dest = vault.prune(SOURCE, "2026-08-01")
    assert dest.is_relative_to(vault.root / "pruned")
    assert (dest / "MANIFEST.json").is_file()
    assert [g.generation_id for g in vault.generations(SOURCE)] == ["2026-09-01", "2026-10-01"]
    assert ledger.tail(1)[0].kind == "vault.prune"
    with pytest.raises(VaultError, match="not a sealed"):
        vault.prune(SOURCE, "2026-08-01")


def test_manifests_are_parsed_once_and_revalidated(
    vault: FileVault, seal: SealFn, monkeypatch: pytest.MonkeyPatch
) -> None:
    lib = seal()
    calls: list[int] = []
    parse = Manifest.from_bytes

    def counting(data: bytes) -> Manifest:
        calls.append(1)
        return parse(data)

    monkeypatch.setattr(Manifest, "from_bytes", staticmethod(counting))
    first = vault.current(SOURCE)
    assert vault.current(SOURCE) == first
    assert len(calls) == 1
    path = lib / "MANIFEST.json"
    os.chmod(path, 0o644)
    os.utime(path, ns=(1, 1))  # a changed stamp (e.g. a repaired manifest) is read again
    assert vault.current(SOURCE) == first
    assert len(calls) == 2
