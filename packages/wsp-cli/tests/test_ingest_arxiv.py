"""``wsp ingest`` of the arXiv PDF mirror tree: per-chunk part generations, resumable, read-only dry run."""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path

import pytest

from wsp_cli.ingest import (
    ArxivSnapshotPlan,
    ChunkState,
    Ingestor,
    VersionStatus,
    plan_arxiv,
    read_chunk_manifest,
    render,
)
from wsp_core.model import SourceSpec, SourceStatus, Tier
from wsp_vault.ledger import JsonlLedger
from wsp_vault.signing import Ed25519Signer
from wsp_vault.vault import FileVault

SNAP = "2026-10"
SPEC = SourceSpec(
    id="arxiv",
    name="arXiv",
    tier=Tier.T1,
    status=SourceStatus.ACTIVE,
    license="LicenseRef-per-item",
    legal_basis="bulk-access-terms",
    domain=("science",),
    adapter="arxiv",
)


def _chunk(root: Path, archive: str, yymm: str, n: int, *, write_files: bool = True) -> list[Path]:
    snap = root / "_incoming" / "arxiv" / SNAP
    (snap / "manifests").mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    paths: list[Path] = []
    for i in range(n):
        rel = f"pdf/{archive}/{yymm}/{yymm}{i:03d}v1.pdf"
        data = f"%PDF-1.4 {archive} {yymm} {i}".encode()
        lines.append(json.dumps({"path": rel, "size": len(data), "md5": hashlib.md5(data).hexdigest()}))
        path = snap / rel
        if write_files:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        paths.append(path)
    (snap / "manifests" / f"{archive}-{yymm}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return paths


@pytest.fixture
def vault(tmp_path: Path) -> FileVault:
    signer = Ed25519Signer.generate()
    root = tmp_path / "vault"
    return FileVault(root, signer, domains={"arxiv": "science"}, ledger=JsonlLedger(root / "ledger", signer))


def _ingestor(vault: FileVault, state: Path) -> Ingestor:
    return Ingestor(vault, [SPEC], out=io.StringIO(), arxiv_state_dir=state, hash_workers=2)


def test_only_finished_chunks_are_sealed_and_rerun_is_idempotent(vault: FileVault, tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    done = _chunk(vault.root, "acc-phys", "9411", 3)
    running = _chunk(vault.root, "adap-org", "9606", 2)
    (state / f"done-{SNAP}.txt").write_text("acc-phys-9411\n", encoding="utf-8")
    ing = _ingestor(vault, state)

    dry = ing.run(source_id="arxiv", dry_run=True)
    (vp,) = dry.versions
    assert vp.status is VersionStatus.READY
    assert "1 ready (3 PDFs" in vp.reason
    assert all(p.exists() for p in done + running)  # the dry run changed nothing
    out = io.StringIO()
    render(dry, out)
    assert "arxiv" in out.getvalue()

    report = ing.run(source_id="arxiv")
    (vp,) = report.versions
    assert vp.status is VersionStatus.INGESTED
    gid = f"{SNAP}.pdf.acc-phys-9411"
    gen = vault.library_path("arxiv", gid)
    assert (gen / "MANIFEST.json").is_file()
    assert (gen / "upstream" / "manifests" / "acc-phys-9411.jsonl").is_file()
    assert not any(p.exists() for p in done)  # adopted by rename
    assert all(p.exists() for p in running)  # the chunk still downloading is untouched
    assert vault.current("arxiv") is None  # parts are never promoted
    artifacts = vault.manifest("arxiv", gid).artifacts()
    assert sum(1 for a in artifacts if any(c.algorithm == "md5" for c in a.upstream)) == 3

    again = ing.run(source_id="arxiv")
    (snapshot,) = again.versions
    assert isinstance(snapshot, ArxivSnapshotPlan)
    states = {c.name: c.state for c in snapshot.chunks}
    assert states == {"acc-phys-9411": ChunkState.SEALED, "adap-org-9606": ChunkState.IN_PROGRESS}


def test_corrupt_pdf_fails_the_chunk_and_leaves_it_in_place(vault: FileVault, tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    paths = _chunk(vault.root, "hep-th", "9901", 2)
    paths[1].write_bytes(b"%PDF-1.4 tampered!!!!!!"[: paths[1].stat().st_size])
    (state / f"done-{SNAP}.txt").write_text("hep-th-9901\n", encoding="utf-8")
    report = _ingestor(vault, state).run(source_id="arxiv")
    assert report.versions[0].status is VersionStatus.FAILED
    assert paths[1].exists()
    assert not (vault.library_path("arxiv", f"{SNAP}.pdf.hep-th-9901") / "MANIFEST.json").exists()


def test_without_pull_state_files_on_disk_decide(vault: FileVault, tmp_path: Path) -> None:
    _chunk(vault.root, "acc-phys", "9502", 2)
    _chunk(vault.root, "acc-phys", "9503", 2, write_files=False)
    (vp,) = plan_arxiv(vault, vault.root / "_incoming" / "arxiv", tmp_path / "no-state")
    states = {c.name: c.state for c in vp.chunks}
    assert states == {"acc-phys-9502": ChunkState.READY, "acc-phys-9503": ChunkState.IN_PROGRESS}
    assert "no pull state" in vp.reason


def test_manifest_paths_must_stay_in_the_tree(tmp_path: Path) -> None:
    bad = tmp_path / "m.jsonl"
    bad.write_text(json.dumps({"path": "pdf/../../etc/passwd", "size": 1, "md5": "0" * 32}) + "\n")
    with pytest.raises(ValueError, match="unsafe"):
        read_chunk_manifest(bad)
