"""``wsp ingest`` of the arXiv PDF mirror tree: per-chunk part generations, resumable, read-only dry run."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import subprocess
import urllib.error
from collections.abc import Iterator
from pathlib import Path
from typing import Any

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


def _arxiv_pull() -> Any:
    """``tools/arxiv_pull.py`` (a script, not a package module)."""
    import importlib.util

    path = Path(__file__).resolve().parents[3] / "tools" / "arxiv_pull.py"
    spec = importlib.util.spec_from_file_location("arxiv_pull_under_test", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_pull_reads_robots_txt_by_rfc_9309_again_each_day_and_checks_every_object(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """2026-11-04 trawl: tools/arxiv_pull.py read robots.txt once per multi-day run, for the bucket's
    root only, with urllib.robotparser (``*`` and ``$`` read literally, the first match wins)."""
    pull = _arxiv_pull()
    answers: list[bytes | int] = [b"User-agent: *\nDisallow: /*b.pdf$\n"]
    reads: list[str] = []

    def urlopen(req: Any, timeout: float = 0) -> io.BytesIO:
        reads.append(req.full_url)
        answer = answers[0]
        if isinstance(answer, int):
            raise urllib.error.HTTPError(req.full_url, answer, "down", None, None)  # pyright: ignore[reportArgumentType]
        return io.BytesIO(answer)

    monkeypatch.setattr(pull.urllib.request, "urlopen", urlopen)
    clock = [0.0]
    robots = pull.RobotsCache(clock=lambda: clock[0])
    monkeypatch.setattr(pull, "ROBOTS", robots)
    obj = pull.MEDIA + "arxiv/hep-th/pdf/9901/9901001"
    assert robots.allows(obj + "a.pdf")
    assert not robots.allows(obj + "b.pdf")  # the wildcard and the anchor match (robotparser: allowed)
    assert reads == ["https://storage.googleapis.com/robots.txt"]
    answers[0] = b"User-agent: *\nDisallow: /arxiv-dataset/\n"  # the host changes its rules mid-pull
    clock[0] += pull.ROBOTS_TTL - 1
    assert robots.allows(obj + "a.pdf")
    assert len(reads) == 1
    clock[0] += 1
    assert not robots.allows(obj + "a.pdf")
    assert len(reads) == 2
    answers[0] = 503
    clock[0] += pull.ROBOTS_TTL
    assert not robots.allows(obj + "a.pdf")
    assert len(reads) == 3
    assert not pull.RobotsCache().allows(obj + "a.pdf")  # unreachable and never read: disallowed

    # every object of a chunk is checked; one left out keeps the chunk from being marked done
    answers[0] = b"User-agent: *\nDisallow: /*b.pdf$\n"
    clock[0] += pull.ROBOTS_TTL
    md5 = base64.b64encode(b"\0" * 16).decode()
    items = [{"name": f"arxiv/hep-th/pdf/9901/9901001{c}.pdf", "size": "3", "md5Hash": md5} for c in "ab"]

    def listing(prefix: str, **_: object) -> Iterator[dict[str, Any]]:
        yield {"items": items}

    def aria2(cmd: list[str], **_: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(pull, "_list", listing)
    monkeypatch.setattr(pull, "STATE_DIR", tmp_path)
    monkeypatch.setattr(subprocess, "run", aria2)
    (tmp_path / "m").mkdir()
    assert pull.run_chunk("arxiv/hep-th/pdf/9901/", tmp_path / "dest", tmp_path / "m", 1, "aria2c") is False
    queued = (tmp_path / "hep-th-9901.aria2").read_text()
    assert "9901001a.pdf" in queued
    assert "9901001b.pdf" not in queued
