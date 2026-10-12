"""Regressions of the vault, orchestrator and lanes audit (docs/audits/2026-10-12/)."""

from __future__ import annotations

import errno
import hashlib
import os
from pathlib import Path

import pytest
from vaultkit import GEN, SOURCE, SealFn

from wsp_core.model import Checksum
from wsp_vault import atomic
from wsp_vault import vault as vault_mod
from wsp_vault.atomic import IntegrityError, publish_noreplace
from wsp_vault.ledger import JsonlLedger
from wsp_vault.vault import FileVault, VaultError


def _incoming(tmp_path: Path, name: str, data: bytes) -> Path:
    path = tmp_path / "vault" / "_incoming" / "src" / "2026-10-12" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _sha(data: bytes) -> Checksum:
    return Checksum("sha256", hashlib.sha256(data).hexdigest())


def test_a_refetched_catalog_replaces_the_staged_one_only_when_it_verifies(
    vault: FileVault, tmp_path: Path, ledger: JsonlLedger
) -> None:
    """H2: an ingest interrupted after staging; the lane fetched the same cycle again, and the catalog
    differs only in per-run ``fetched_at`` stamps. It used to fail that version on every run."""
    first = b'{"doc_key":"k1","fetched_at":"2026-10-12T05:00:00+00:00","sha256":"aa"}\n'
    again = b'{"doc_key":"k1","fetched_at":"2026-10-12T09:00:00+00:00","sha256":"aa"}\n'
    cat = _incoming(tmp_path, "documents.jsonl", first)
    vault.adopt(SOURCE, GEN, cat, "meta/documents.jsonl", upstream=[_sha(first)], replace_staged=True)
    cat.write_bytes(again)
    with pytest.raises(VaultError, match="different content"):  # the default still never replaces
        vault.adopt(SOURCE, GEN, cat, "meta/documents.jsonl", upstream=[_sha(again)])
    with pytest.raises(VaultError, match="unverifiable"):  # no checksum: never replaced
        vault.adopt(SOURCE, GEN, cat, "meta/documents.jsonl", replace_staged=True)
    assert cat.read_bytes() == again  # given back
    with pytest.raises(IntegrityError):  # the plan names other bytes: never replaced
        vault.adopt(SOURCE, GEN, cat, "meta/documents.jsonl", upstream=[_sha(first)], replace_staged=True)
    assert cat.read_bytes() == again
    art = vault.adopt(SOURCE, GEN, cat, "meta/documents.jsonl", upstream=[_sha(again)], replace_staged=True)
    assert art.sha256 == _sha(again).hexdigest
    assert not cat.exists()
    gen = vault.seal(SOURCE, GEN)
    (entry,) = gen.artifacts
    assert entry.sha256 == _sha(again).hexdigest
    assert (vault.generation_path(SOURCE, GEN) / "meta/documents.jsonl").read_bytes() == again
    restaged = [e for e in ledger.events() if e.kind == "vault.restaged"]
    assert [e.data["old_sha256"] for e in restaged] == [_sha(first).hexdigest]


def test_adopt_finishes_a_publish_interrupted_between_link_and_unlink(
    vault: FileVault, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M1: where renameat2(RENAME_NOREPLACE) is unsupported (some SMB mounts) the publish is link then
    unlink; a crash between them left the file under both names, and adopt and seal failed forever."""
    real_link = atomic.link_noreplace

    def unsupported(src: Path, dest: Path) -> None:
        raise OSError(errno.EINVAL, "renameat2 is not supported here")

    def crash_after_link(src: Path, dest: Path) -> None:
        os.link(src, dest)
        raise KeyboardInterrupt("power cut between link and unlink")

    data = b"x" * 1000
    path = _incoming(tmp_path, "a.bin", data)
    monkeypatch.setattr(atomic, "rename_noreplace", unsupported)
    monkeypatch.setattr(atomic, "link_noreplace", crash_after_link)
    with pytest.raises(KeyboardInterrupt):
        vault.adopt(SOURCE, GEN, path, "upstream/a.bin")
    staged = vault.staging_path(SOURCE, GEN) / "upstream"
    assert sorted(p.name for p in staged.iterdir()) == ["a.bin", "a.bin.adopting"]
    monkeypatch.setattr(atomic, "link_noreplace", real_link)
    art = vault.adopt(SOURCE, GEN, path, "upstream/a.bin", upstream=[_sha(data)])
    assert art.sha256 == _sha(data).hexdigest
    assert sorted(p.name for p in staged.iterdir()) == ["a.bin"]
    gen = vault.seal(SOURCE, GEN)
    assert [str(a.relpath) for a in gen.artifacts] == ["upstream/a.bin"]


def test_publish_noreplace_completes_a_move_left_as_two_links(tmp_path: Path) -> None:
    """M1, for every caller of publish_noreplace: the same inode under both names is a finished move."""
    src, dest = tmp_path / "f.partial", tmp_path / "f"
    src.write_bytes(b"data")
    os.link(src, dest)
    publish_noreplace(src, dest)
    assert not src.exists()
    assert dest.read_bytes() == b"data"
    other = tmp_path / "g.partial"
    other.write_bytes(b"other")
    with pytest.raises(FileExistsError):  # a different file at dest is still never replaced
        publish_noreplace(other, dest)
    assert other.read_bytes() == b"other"


def test_a_duplicate_check_never_deletes_bytes_renamed_onto_the_incoming_path(
    vault: FileVault, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """L6: the identical-duplicate branch compared ``path`` and then unlinked it; a concurrent fetch that
    renamed new bytes onto ``path`` in between lost them. The comparison now runs on a private name."""
    data = b"same bytes"
    path = _incoming(tmp_path, "doc.pdf", data)
    vault.adopt(SOURCE, GEN, path, "upstream/doc.pdf")
    path.write_bytes(data)  # fetched again: identical
    real = atomic.readback
    calls = {"n": 0}

    def racing_readback(p: Path, **kw: object) -> object:
        calls["n"] += 1
        if calls["n"] == 1:  # while the duplicate is compared, a fetch publishes a new edition
            path.write_bytes(b"newer edition")
        return real(p, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(vault_mod, "readback", racing_readback)
    vault.adopt(SOURCE, GEN, path, "upstream/doc.pdf")
    assert path.read_bytes() == b"newer edition"  # kept for the next ingest
    assert (vault.staging_path(SOURCE, GEN) / "upstream/doc.pdf").read_bytes() == data


def test_promote_if_newer_orders_generations_naturally(vault: FileVault, seal: SealFn) -> None:
    """M3: ``2026-10-12-r10`` is newer than ``-r4`` (plain string order said otherwise, so ingest never
    promoted it), and an older generation never replaces a newer current one."""
    seal("2026-10-12-r4", promote=False)
    seal("2026-10-12-r10", promote=False)
    assert vault.promote_if_newer(SOURCE, "2026-10-12-r4")
    assert vault.promote_if_newer(SOURCE, "2026-10-12-r10")
    assert vault.current_set_ids(SOURCE) == ["2026-10-12-r10"]
    assert not vault.promote_if_newer(SOURCE, "2026-10-12-r4")
    assert vault.current_set_ids(SOURCE) == ["2026-10-12-r10"]
    assert vault.promote_if_newer(SOURCE, "2026-10-12-r10")  # re-promoting the current one is fine


def test_promote_if_newer_compares_within_a_partition(tmp_path: Path) -> None:
    """ADR-0010: a partition's generation competes only with its own partition's current one."""
    from wsp_vault.signing import Ed25519Signer

    v = FileVault(
        tmp_path / "pv", Ed25519Signer.generate(), domains={SOURCE: "reference"}, partitioned=[SOURCE]
    )
    for gid in ("2026-10-12.ca", "2026-10-01.ak", "2026-09-01.ca"):
        v.stage(SOURCE, gid)
        local = tmp_path / "w" / gid
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_bytes(gid.encode())
        v.ingest(SOURCE, gid, local, "upstream/x")
        v.seal(SOURCE, gid)
    assert v.promote_if_newer(SOURCE, "2026-10-12.ca")
    assert v.promote_if_newer(SOURCE, "2026-10-01.ak")  # older than CURRENT.txt, but another partition
    assert not v.promote_if_newer(SOURCE, "2026-09-01.ca")
    assert v.current_set_ids(SOURCE) == ["2026-10-01.ak", "2026-10-12.ca"]
