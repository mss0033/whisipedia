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

import wsp_cli.ingest as ingest_mod
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
from wsp_vault.atomic import IntegrityError
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
    assert len(reads) == 4

    # 2026-11-04 review: a failed re-read is tried again after ROBOTS_RETRY, not at every object of a
    # chunk (one robots.txt request per object, up to 60 s apiece, to a host already failing)
    for _ in range(50):
        assert not robots.allows(obj + "a.pdf")  # the old copy stays in force
    assert len(reads) == 4
    clock[0] += pull.ROBOTS_RETRY - 1
    assert not robots.allows(obj + "a.pdf")
    assert len(reads) == 4
    clock[0] += 1
    for _ in range(50):
        assert not robots.allows(obj + "a.pdf")
    assert len(reads) == 5
    fresh = pull.RobotsCache(clock=lambda: clock[0])  # never read and unreachable: same pacing
    for _ in range(50):
        assert not fresh.allows(obj + "a.pdf")
    assert len(reads) == 6

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


# --- redo of one damaged PDF (2026-11-10 trawl, 5b-2) ------------------------------------------------

CHUNK = "hep-th-9901"
BAD = b"%PDF-1.4 tampered!!!!!!"


def _good(i: int, archive: str = "hep-th", yymm: str = "9901") -> bytes:
    return f"%PDF-1.4 {archive} {yymm} {i}".encode()


def _damage(path: Path) -> None:
    path.write_bytes(BAD[: len(_good(1))])  # the right size, the wrong bytes


def _damaged_chunk(vault: FileVault, tmp_path: Path) -> tuple[Path, Path, list[Path]]:
    """A finished chunk of three PDFs whose second one was damaged after the download:
    ``(state dir, snapshot dir, paths)``."""
    state = tmp_path / "state"
    state.mkdir()
    paths = _chunk(vault.root, "hep-th", "9901", 3)
    _damage(paths[1])
    (state / f"done-{SNAP}.txt").write_text(f"{CHUNK}\n", encoding="utf-8")
    return state, vault.root / "_incoming" / "arxiv" / SNAP, paths


def _redo_rows(state: Path) -> list[dict[str, Any]]:
    path = state / f"redo-{SNAP}.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


class _Aria2:
    """Stands in for aria2c: writes each file its input names (the right bytes unless ``content``
    says otherwise) and keeps the commands and inputs it was given."""

    def __init__(self, content: bytes | None = None, code: int = 0) -> None:
        self.content, self.code = content, code
        self.cmds: list[list[str]] = []
        self.inputs: list[str] = []
        self.during: Any = None  # called while "aria2" runs

    def __call__(self, cmd: list[str], **_: object) -> subprocess.CompletedProcess[bytes]:
        self.cmds.append(cmd)
        text = Path(next(a for a in cmd if a.startswith("--input-file=")).split("=", 1)[1]).read_text()
        self.inputs.append(text)
        lines = text.splitlines()
        for i in range(0, len(lines), 4):
            folder, name = lines[i + 1].split("=", 1)[1], lines[i + 2].split("=", 1)[1]
            if self.code == 0:
                Path(folder).mkdir(parents=True, exist_ok=True)
                good = _good(int(name.removesuffix("v1.pdf")[-3:]), *Path(folder).parts[-2:])
                Path(folder, name).write_bytes(self.content if self.content is not None else good)
        if self.during is not None:
            self.during()
        return subprocess.CompletedProcess(cmd, self.code)


def _pull(monkeypatch: pytest.MonkeyPatch, state: Path, aria2: _Aria2) -> Any:
    """The pull script with its state in ``state``, robots.txt allowing all, and ``aria2`` as aria2c."""
    pull = _arxiv_pull()

    class Allow:
        def allows(self, url: str) -> bool:
            return True

    monkeypatch.setattr(pull, "STATE_DIR", state)
    monkeypatch.setattr(pull, "ROBOTS", Allow())
    monkeypatch.setattr(subprocess, "run", aria2)
    return pull


def test_a_damaged_pdf_alone_is_moved_aside_and_recorded_with_its_tries(
    vault: FileVault, tmp_path: Path
) -> None:
    """A PDF damaged after the download failed its chunk at every ingest, for ever: the file was left
    in place and the pull never looked at a finished chunk again (core-correctness-12)."""
    state, snap, paths = _damaged_chunk(vault, tmp_path)
    report = _ingestor(vault, state).run(source_id="arxiv")
    (vp,) = report.versions
    assert isinstance(vp, ArxivSnapshotPlan)
    assert not paths[1].exists()  # moved, not deleted:
    aside = snap / "_damaged" / "hep-th" / "9901" / "9901001v1.pdf.try1"
    assert aside.read_bytes() == BAD[: len(_good(1))]
    assert [p for p in (snap / "_damaged").rglob("*") if p.is_file()] == [aside]  # that one file only
    assert not paths[0].exists()  # the file before it stays adopted
    assert _redo_rows(state) == [
        {"chunk": CHUNK, "path": "pdf/hep-th/9901/9901001v1.pdf", "tries": 1, "failed": False, "reason": ""}
    ]
    assert [c.state for c in vp.chunks] == [ingest_mod.ChunkState.REDO]
    assert vp.status is VersionStatus.INCOMPLETE  # not "ingested", and not failed for good either
    assert "redo pending" in vp.reason
    assert not (vault.library_path("arxiv", f"{SNAP}.pdf.{CHUNK}") / "MANIFEST.json").exists()
    bad = tmp_path / "m.jsonl"
    bad.write_text(json.dumps({"path": "_damaged/hep-th/9901/x.pdf", "size": 1, "md5": "0" * 32}))
    with pytest.raises(ValueError, match="unsafe"):  # no manifest can name the folder for adoption
        read_chunk_manifest(bad)


def test_the_plan_says_redo_pending_and_does_not_offer_the_chunk(vault: FileVault, tmp_path: Path) -> None:
    state, snap, _paths = _damaged_chunk(vault, tmp_path)
    ing = _ingestor(vault, state)
    ing.run(source_id="arxiv")
    (vp,) = plan_arxiv(vault, vault.root / "_incoming" / "arxiv", state)
    assert [c.state for c in vp.chunks] == [ingest_mod.ChunkState.REDO]
    assert "0 ready" in vp.reason
    assert "1 redo pending (1 file(s))" in vp.reason
    assert vp.status is VersionStatus.INCOMPLETE
    out = io.StringIO()
    render(ing.run(source_id="arxiv", dry_run=True), out)
    assert "redo pending" in out.getvalue()
    # a second ingest before the pull has fetched the file: nothing is tried, nothing is counted
    again = ing.run(source_id="arxiv")
    assert again.versions[0].status is VersionStatus.INCOMPLETE
    assert [r["tries"] for r in _redo_rows(state)] == [1]
    assert len([p for p in (snap / "_damaged").rglob("*") if p.is_file()]) == 1


def test_the_pull_fetches_only_the_named_file_not_the_adopted_ones(
    vault: FileVault, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rejected design handed the chunk back to the pull, whose listing fetches every file that is
    missing from ``_incoming``: the adopted part of the chunk, a second time (2026-11-10 review)."""
    state, snap, paths = _damaged_chunk(vault, tmp_path)
    _ingestor(vault, state).run(source_id="arxiv")
    assert not paths[0].exists()  # adopted: gone from _incoming, and it must not come back
    aria2 = _Aria2()
    pull = _pull(monkeypatch, state, aria2)

    def no_listing(*_: object, **__: object) -> Iterator[dict[str, Any]]:
        raise AssertionError("a redo lists nothing")

    monkeypatch.setattr(pull, "_list", no_listing)
    assert pull.run_redo(SNAP, snap, 1, "aria2c") == 1
    (queued,) = aria2.inputs
    assert queued.splitlines() == [
        pull.MEDIA + "arxiv/hep-th/pdf/9901/9901001v1.pdf",
        f"  dir={paths[1].parent}",
        "  out=9901001v1.pdf",
        f"  checksum=md5={hashlib.md5(_good(1)).hexdigest()}",
    ]
    assert "9901000v1.pdf" not in queued
    assert "9901002v1.pdf" not in queued
    assert paths[1].read_bytes() == _good(1)
    assert not paths[0].exists()
    assert json.loads((state / f"refetched-{SNAP}.jsonl").read_text()) == {
        "path": "pdf/hep-th/9901/9901001v1.pdf",
        "tries": 1,
    }
    assert pull.open_redo(SNAP) == []
    assert pull.run_redo(SNAP, snap, 1, "aria2c") == 0  # fetched: not asked of aria2 again
    assert len(aria2.cmds) == 1


def test_a_download_that_does_not_match_the_manifest_is_not_reported_as_fetched(
    vault: FileVault, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state, snap, _paths = _damaged_chunk(vault, tmp_path)
    _ingestor(vault, state).run(source_id="arxiv")
    aria2 = _Aria2(content=BAD)  # an aria2 that did not check
    pull = _pull(monkeypatch, state, aria2)
    attempts: dict[tuple[str, int], int] = {}
    for _ in range(pull.REDO_DOWNLOAD_TRIES + 2):
        assert pull.run_redo(SNAP, snap, 1, "aria2c", "0", attempts) == 0
    assert not (state / f"refetched-{SNAP}.jsonl").exists()
    assert len(aria2.cmds) == pull.REDO_DOWNLOAD_TRIES  # then it waits for a restart, not every chunk
    (vp,) = plan_arxiv(vault, vault.root / "_incoming" / "arxiv", state)
    assert [c.state for c in vp.chunks] == [ingest_mod.ChunkState.REDO]
    kept = [p for p in (snap / "_damaged").rglob("*") if p.is_file()]
    # none deleted, none replaced: ingest's copy, the pull's two, and the last one still in place
    assert sorted(p.name for p in kept) == [f"9901001v1.pdf.try1{n}" for n in ("", ".2", ".3")]
    assert all(p.read_bytes() == BAD[: len(_good(1))] if p.name.endswith("try1") else BAD for p in kept)


def test_after_the_redo_the_rest_of_the_chunk_is_adopted_and_sealed(
    vault: FileVault, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state, snap, paths = _damaged_chunk(vault, tmp_path)
    ing = _ingestor(vault, state)
    ing.run(source_id="arxiv")
    pull = _pull(monkeypatch, state, _Aria2())
    assert pull.run_redo(SNAP, snap, 1, "aria2c") == 1
    report = ing.run(source_id="arxiv")
    (vp,) = report.versions
    assert vp.status is VersionStatus.INGESTED
    gid = f"{SNAP}.pdf.{CHUNK}"
    pdfs = [a for a in vault.manifest("arxiv", gid).artifacts() if a.relpath.suffix == ".pdf"]
    assert sorted(str(a.relpath) for a in pdfs) == [
        f"upstream/pdf/hep-th/9901/9901{i:03d}v1.pdf" for i in range(3)
    ]
    assert not any(p.exists() for p in paths)
    gen = vault.library_path("arxiv", gid)
    assert (gen / "upstream/pdf/hep-th/9901/9901001v1.pdf").read_bytes() == _good(1)
    assert _redo_rows(state) == []  # settled: the sealed chunk's line is gone
    (after,) = plan_arxiv(vault, vault.root / "_incoming" / "arxiv", state)
    assert [c.state for c in after.chunks] == [ChunkState.SEALED]


def test_the_third_failure_of_a_file_fails_the_chunk_with_an_alert_and_no_further_redo(
    vault: FileVault, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rejected design had no bound: every failure asked for the chunk again."""
    state, snap, paths = _damaged_chunk(vault, tmp_path)
    alerts: list[tuple[str, str]] = []
    ing = Ingestor(
        vault,
        [SPEC],
        out=io.StringIO(),
        arxiv_state_dir=state,
        hash_workers=2,
        alert=lambda key, message: alerts.append((key, message)),
    )
    aria2 = _Aria2()
    pull = _pull(monkeypatch, state, aria2)
    for tries in (1, 2):
        ing.run(source_id="arxiv")
        assert [(r["tries"], r["failed"]) for r in _redo_rows(state)] == [(tries, False)]
        assert pull.run_redo(SNAP, snap, 1, "aria2c") == 1
        _damage(paths[1])  # the share damages it again after the pull checked it
    assert alerts == []
    report = ing.run(source_id="arxiv")
    (vp,) = report.versions
    assert vp.status is VersionStatus.FAILED
    assert "failed its MD5 3 times" in vp.reason
    assert [(r["tries"], r["failed"]) for r in _redo_rows(state)] == [(3, True)]
    ((key, message),) = alerts
    assert key == f"ingest:arxiv:{SNAP}:{CHUNK}"
    assert "9901001v1.pdf" in message
    assert paths[1].exists()  # the last copy stays in place; the two before it are kept aside
    assert len([p for p in (snap / "_damaged").rglob("*") if p.is_file()]) == 2
    # no further redo: the pull has nothing to fetch, the plan shows FAILED, a rerun tries nothing
    assert pull.open_redo(SNAP) == []
    assert pull.run_redo(SNAP, snap, 1, "aria2c") == 0
    assert len(aria2.cmds) == 2
    (plan,) = plan_arxiv(vault, vault.root / "_incoming" / "arxiv", state)
    assert [c.state for c in plan.chunks] == [ChunkState.FAILED]
    assert plan.status is VersionStatus.FAILED
    assert "failed its MD5 3 times" in plan.reason
    again = ing.run(source_id="arxiv")
    assert again.versions[0].status is VersionStatus.FAILED
    assert len(alerts) == 1
    assert [(r["tries"], r["failed"]) for r in _redo_rows(state)] == [(3, True)]


@pytest.mark.parametrize("where", ["seal", "adopt_many"])
def test_an_integrity_error_a_download_cannot_cure_is_not_queued(
    vault: FileVault, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, where: str
) -> None:
    """The vault raises IntegrityError for more than a damaged incoming file (staged bytes that no
    longer match their record, a digest that disagrees with a fetch): the incoming PDFs are whole,
    and fetching one again would cure nothing.

    The "nothing is queued, nothing is moved" half is a GUARD (true of the old code too, which had no
    redo; it holds the line against queueing on the exception type alone). What the old code failed:
    the plan's reason lost the cause to the chunk counts."""
    state = tmp_path / "state"
    state.mkdir()
    paths = _chunk(vault.root, "hep-th", "9901", 3)
    (state / f"done-{SNAP}.txt").write_text(f"{CHUNK}\n", encoding="utf-8")

    def broken(*_: object, **__: object) -> None:
        raise IntegrityError("staged bytes no longer match the ingest record")

    monkeypatch.setattr(vault, where, broken)
    report = _ingestor(vault, state).run(source_id="arxiv")
    (vp,) = report.versions
    assert vp.status is VersionStatus.FAILED
    assert "IntegrityError: staged bytes no longer match" in vp.reason
    assert not (state / f"redo-{SNAP}.jsonl").exists()
    assert not (vault.root / "_incoming" / "arxiv" / SNAP / "_damaged").exists()
    if where == "adopt_many":
        assert all(p.exists() for p in paths)
    (plan,) = plan_arxiv(vault, vault.root / "_incoming" / "arxiv", state)
    assert [c.state for c in plan.chunks] == [ChunkState.READY]  # tried again next run, as before


def _one_file_chunk(pull: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    md5 = base64.b64encode(hashlib.md5(_good(0)).digest()).decode()
    item = {"name": "arxiv/hep-th/pdf/9901/9901000v1.pdf", "size": str(len(_good(0))), "md5Hash": md5}

    def listing(prefix: str, **_: object) -> Iterator[dict[str, Any]]:
        yield {"items": [item]}

    monkeypatch.setattr(pull, "_list", listing)


def test_every_option_the_pull_gives_aria2_is_one_the_real_aria2c_accepts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The 2026-11-10 plan asked for ``--max-redirect=0``; aria2 has no such option and ends at once
    with exit 28, so every chunk of the live job would have failed, while every test passed because
    aria2 was faked (2026-11-13 review). Here the real program reads the options, with an empty input
    file: nothing is requested."""
    import shutil

    real = shutil.which("aria2c") or str(Path.home() / ".local/bin/aria2c")
    if not Path(real).is_file():
        pytest.skip("aria2c is not installed")
    run = subprocess.run
    aria2 = _Aria2()
    pull = _pull(monkeypatch, tmp_path, aria2)
    _one_file_chunk(pull, monkeypatch)
    (tmp_path / "m").mkdir()
    assert pull.run_chunk("arxiv/hep-th/pdf/9901/", tmp_path / "dest", tmp_path / "m", 1, "aria2c") is True
    (cmd,) = aria2.cmds
    assert not any("redirect" in arg for arg in cmd)
    empty = tmp_path / "empty.aria2"
    empty.write_text("")
    args = [a for a in cmd[1:] if not a.startswith(("--input-file=", "--log="))]
    done = run(
        [real, f"--input-file={empty}", *args], capture_output=True, text=True, timeout=60, check=False
    )
    assert "unrecognized option" not in done.stderr + done.stdout
    assert done.returncode != pull.BAD_OPTION_EXIT, done.stderr


def test_options_aria2_refuses_are_said_in_a_line_of_their_own(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    aria2 = _Aria2(code=28)
    pull = _pull(monkeypatch, tmp_path, aria2)
    _one_file_chunk(pull, monkeypatch)
    (tmp_path / "m").mkdir()
    assert pull.run_chunk("arxiv/hep-th/pdf/9901/", tmp_path / "dest", tmp_path / "m", 1, "aria2c") is False
    assert "hep-th-9901: aria2 refused its options (exit 28)" in capsys.readouterr().out
    aria2.code = 1
    assert pull.run_chunk("arxiv/hep-th/pdf/9901/", tmp_path / "dest", tmp_path / "m", 1, "aria2c") is False
    assert "refused its options" not in capsys.readouterr().out


def test_a_given_up_line_the_owner_removes_starts_a_new_count_the_pull_answers(
    vault: FileVault, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """2026-11-13 review: the count began at 1 again, the pull's old "fetched (path, 1)" line answered
    it, the plan offered the chunk with its file moved aside, and every run failed on the missing file."""
    state, snap, paths = _damaged_chunk(vault, tmp_path)
    ing = _ingestor(vault, state)
    pull = _pull(monkeypatch, state, _Aria2())
    for _ in (1, 2):
        ing.run(source_id="arxiv")
        assert pull.run_redo(SNAP, snap, 1, "aria2c") == 1
        _damage(paths[1])
    assert ing.run(source_id="arxiv").versions[0].status is VersionStatus.FAILED  # given up
    (state / f"redo-{SNAP}.jsonl").write_text("")  # the owner: try again
    ing.run(source_id="arxiv")
    (row,) = _redo_rows(state)
    assert (row["tries"], row["base"], row["failed"]) == (3, 2, False)
    (plan,) = plan_arxiv(vault, vault.root / "_incoming" / "arxiv", state)
    assert [c.state for c in plan.chunks] == [ChunkState.REDO]
    assert "redo pending (1 file(s)" in plan.reason
    assert pull.open_redo(SNAP) == [(CHUNK, "pdf/hep-th/9901/9901001v1.pdf", 3)]
    assert pull.run_redo(SNAP, snap, 1, "aria2c") == 1
    assert ing.run(source_id="arxiv").versions[0].status is VersionStatus.INGESTED


def test_a_run_beside_a_given_up_chunk_is_failed_and_an_alert_store_in_trouble_does_not_end_it(
    vault: FileVault, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """2026-11-13 review: with one chunk given up and another ready, the run said "ingested" (exit 0)."""
    state, snap, paths = _damaged_chunk(vault, tmp_path)

    def broken(key: str, message: str) -> None:
        raise OSError("state DB locked")

    out = io.StringIO()
    ing = Ingestor(vault, [SPEC], out=out, arxiv_state_dir=state, hash_workers=2, alert=broken)
    pull = _pull(monkeypatch, state, _Aria2())
    for _ in (1, 2):
        ing.run(source_id="arxiv")
        assert pull.run_redo(SNAP, snap, 1, "aria2c") == 1
        _damage(paths[1])
    assert ing.run(source_id="arxiv").versions[0].status is VersionStatus.FAILED  # no crash
    assert "alert not raised (OSError: state DB locked)" in out.getvalue()
    _chunk(vault.root, "hep-th", "9902", 2)  # a later chunk arrives
    (state / f"done-{SNAP}.txt").write_text(f"{CHUNK}\nhep-th-9902\n", encoding="utf-8")
    (vp,) = ing.run(source_id="arxiv").versions
    assert vp.status is VersionStatus.FAILED
    (plan,) = plan_arxiv(vault, vault.root / "_incoming" / "arxiv", state)
    assert {c.name: c.state for c in plan.chunks} == {
        CHUNK: ChunkState.FAILED,
        "hep-th-9902": ChunkState.SEALED,
    }


def test_the_pull_ignores_a_redo_line_whose_chunk_is_not_a_chunk_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pull = _pull(monkeypatch, tmp_path, _Aria2())
    rows = [
        {"chunk": "../../etc/x", "path": "pdf/hep-th/9901/9901000v1.pdf", "tries": 1},
        {"chunk": "hep-th-9901/../x", "path": "pdf/hep-th/9901/9901000v1.pdf", "tries": 1},
        {"chunk": CHUNK, "path": "pdf/hep-th/9901/9901000v1.pdf", "tries": 1},
    ]
    (tmp_path / f"redo-{SNAP}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    assert pull.open_redo(SNAP) == [(CHUNK, "pdf/hep-th/9901/9901000v1.pdf", 1)]


def test_an_entry_ingest_writes_while_the_pull_holds_an_older_view_is_not_lost(
    vault: FileVault, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pull runs for days. Had it rewritten the redo file from the view it read (the rejected
    design: tmp + replace without the fetched chunk), an entry ingest added meanwhile was gone. Each
    side writes only its own file, so the pull's write cannot touch ingest's entries."""
    state, snap, _paths = _damaged_chunk(vault, tmp_path)
    ing = _ingestor(vault, state)
    ing.run(source_id="arxiv")
    aria2 = _Aria2()
    pull = _pull(monkeypatch, state, aria2)

    def ingest_meanwhile() -> None:  # while the pull's aria2 runs, on the view it read before
        other = _chunk(vault.root, "math", "0001", 2)
        other[0].write_bytes(b"%PDF-1.4 tampered!!!!!!"[: other[0].stat().st_size])
        with (state / f"done-{SNAP}.txt").open("a") as fh:
            fh.write("math-0001\n")
        ing.run(source_id="arxiv")

    aria2.during = ingest_meanwhile
    assert pull.run_redo(SNAP, snap, 1, "aria2c") == 1  # the pull writes down what it fetched
    rows = {r["path"]: r for r in _redo_rows(state)}
    assert set(rows) == {"pdf/hep-th/9901/9901001v1.pdf", "pdf/math/0001/0001000v1.pdf"}
    assert pull.open_redo(SNAP) == [("math-0001", "pdf/math/0001/0001000v1.pdf", 1)]
    (vp,) = plan_arxiv(vault, vault.root / "_incoming" / "arxiv", state)
    states = {c.name: c.state for c in vp.chunks}
    assert states == {CHUNK: ChunkState.READY, "math-0001": ingest_mod.ChunkState.REDO}
    # and the other way round: ingest's rewrite (settling hep-th) keeps what the pull appended
    aria2.during = None
    assert pull.run_redo(SNAP, snap, 1, "aria2c") == 1
    ing.run(source_id="arxiv")
    fetched = [json.loads(x) for x in (state / f"refetched-{SNAP}.jsonl").read_text().splitlines()]
    assert {r["path"] for r in fetched} == set(rows)
    (vp,) = plan_arxiv(vault, vault.root / "_incoming" / "arxiv", state)
    assert {c.state for c in vp.chunks} == {ChunkState.SEALED}


def test_config_alert_opens_a_warning_on_the_orchestrators_alerts(tmp_path: Path) -> None:
    from wsp_orchestrator.store import StateStore

    db = tmp_path / "state.db"
    StateStore(db, check_local=False).close()
    alert = ingest_mod.config_alert({"orchestrator": {"state_db": str(db)}})
    alert("ingest:arxiv:2026-10:hep-th-9901", "given up")
    store = StateStore(db, check_local=False)
    try:
        ((key, severity),) = [(str(r["key"]), str(r["severity"])) for r in store.open_alerts()]
    finally:
        store.close()
    assert (key, severity) == ("ingest:arxiv:2026-10:hep-th-9901", "warning")
    ingest_mod.config_alert({})("k", "no state DB: nothing to do, no error")


def test_wsp_ingest_gives_the_ingestor_the_alert_hook(monkeypatch: pytest.MonkeyPatch) -> None:
    """The hook is of no use unless the command that lanes and jobs run passes it on."""
    import argparse

    from wsp_cli import ops

    class StopError(Exception):
        pass

    got: dict[str, Any] = {}

    def ingestor(*_a: Any, **kw: Any) -> None:
        got.update(kw)
        raise StopError

    def hook(key: str, message: str) -> None:
        del key, message

    cfgs: list[Any] = []

    def make(cfg: Any) -> Any:
        cfgs.append(cfg)
        return hook

    monkeypatch.setattr(ops, "Ingestor", ingestor)
    monkeypatch.setattr(ops, "config_alert", make)

    def nothing(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(ops, "_vault", nothing)
    monkeypatch.setattr(ops, "_ingest_version", nothing)
    cfg = {"orchestrator": {"state_db": "x"}}
    for dry, want in ((False, hook), (True, None)):
        got.clear()
        args = argparse.Namespace(dry_run=dry, offline=True, json=True, settle_seconds=0)
        with pytest.raises(StopError):
            ops._ingest(args, io.StringIO(), None, cfg, ())  # type: ignore[arg-type]  # pyright: ignore[reportPrivateUsage]
        assert got["alert"] is want
    assert cfgs == [cfg]
