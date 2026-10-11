"""Vault mutations under real concurrency: threads and processes on a local temp dir (audit #4, #5)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from vaultkit import SOURCE

from wsp_vault.locks import LockOrderError, VaultLocks
from wsp_vault.manifest import STAGING_RECORD
from wsp_vault.signing import Ed25519Signer
from wsp_vault.vault import FileVault, VaultError

GEN = "2026-10-05"


def _files(tmp_path: Path, n: int) -> list[Path]:
    src = tmp_path / "src"
    src.mkdir(exist_ok=True)
    out: list[Path] = []
    for i in range(n):
        p = src / f"f{i:03d}.bin"
        p.write_bytes(os.urandom(4096) + i.to_bytes(4, "big"))
        out.append(p)
    return out


def test_concurrent_ingests_threads_record_every_file(tmp_path: Path) -> None:
    """The auditor's stagedemo: 4 threads x 10 files; before, 9 of 32 successes were recorded."""
    signer = Ed25519Signer.generate()
    vault = FileVault(tmp_path / "vault", signer, lock_dir=tmp_path / "locks")
    files = _files(tmp_path, 40)
    errors: list[BaseException] = []

    def work(chunk: list[Path]) -> None:
        for p in chunk:
            try:
                vault.ingest(SOURCE, GEN, p, f"upstream/{p.name}")
            except BaseException as exc:
                errors.append(exc)

    threads = [threading.Thread(target=work, args=(files[i::4],)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    record = json.loads((vault.staging_path(SOURCE, GEN) / STAGING_RECORD).read_bytes())
    assert len(record) == 40
    gen = vault.seal(SOURCE, GEN)
    assert len(gen.artifacts) == 40


_INGESTER = """
import sys
from pathlib import Path
from wsp_vault.signing import Ed25519Signer
from wsp_vault.vault import FileVault
root, locks, pem = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]).read_bytes()
vault = FileVault(root, Ed25519Signer.from_private_pem(pem), lock_dir=locks)
for name in sys.argv[4:]:
    p = Path(name)
    vault.ingest("test-src", "2026-10-05", p, f"upstream/{p.name}")
"""


def test_concurrent_ingests_processes_record_every_file(tmp_path: Path) -> None:
    signer = Ed25519Signer.generate()
    pem = tmp_path / "key.pem"
    pem.write_bytes(signer.private_pem())
    root, locks = tmp_path / "vault", tmp_path / "locks"
    files = _files(tmp_path, 40)
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", _INGESTER, str(root), str(locks), str(pem), *map(str, files[i::4])],
            stderr=subprocess.PIPE,
        )
        for i in range(4)
    ]
    for p in procs:
        _, err = p.communicate()
        assert p.returncode == 0, err.decode()
    vault = FileVault(root, signer, lock_dir=locks)
    record = json.loads((vault.staging_path(SOURCE, GEN) / STAGING_RECORD).read_bytes())
    assert len(record) == 40
    assert len(vault.seal(SOURCE, GEN).artifacts) == 40


def test_same_file_ingested_twice_concurrently_is_idempotent(tmp_path: Path) -> None:
    vault = FileVault(tmp_path / "vault", Ed25519Signer.generate(), lock_dir=tmp_path / "locks")
    (src,) = _files(tmp_path, 1)
    barrier = threading.Barrier(6)
    results: list[object] = []

    def work() -> None:
        barrier.wait()
        try:
            results.append(vault.ingest(SOURCE, GEN, src, "upstream/x.bin"))
        except BaseException as exc:
            results.append(exc)

    threads = [threading.Thread(target=work) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert all(not isinstance(r, BaseException) for r in results), results
    assert len({r for r in results}) == 1
    staged = sorted(p.name for p in vault.staging_path(SOURCE, GEN).rglob("*") if p.is_file())
    assert staged == sorted([STAGING_RECORD, "x.bin"])  # no temporaries left behind


def test_seal_waits_for_running_ingests(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An ingest holds the generation lock (shared); seal (exclusive) waits for it to finish."""
    vault = FileVault(tmp_path / "vault", Ed25519Signer.generate(), lock_dir=tmp_path / "locks")
    a, b = _files(tmp_path, 2)
    vault.ingest(SOURCE, GEN, a, "upstream/a")
    from wsp_vault.atomic import atomic_copy

    real_copy = atomic_copy
    entered, release = threading.Event(), threading.Event()

    def slow_copy(*args: object, **kw: object) -> object:
        entered.set()
        assert release.wait(10)
        return real_copy(*args, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr("wsp_vault.vault.atomic_copy", slow_copy)
    t = threading.Thread(target=lambda: vault.ingest(SOURCE, GEN, b, "upstream/b"))
    t.start()
    assert entered.wait(10)
    sealed: list[int] = []
    s = threading.Thread(target=lambda: sealed.append(len(vault.seal(SOURCE, GEN).artifacts)))
    s.start()
    time.sleep(0.3)
    assert s.is_alive()  # blocked on the generation lock
    release.set()
    t.join()
    s.join()
    assert sealed == [2]


def test_locks_shared_exclusive_reentrant(tmp_path: Path) -> None:
    locks = VaultLocks(tmp_path / "locks")
    with locks.generation("s", "g", exclusive=False), locks.generation("s", "g", exclusive=False):
        got: list[str] = []
        other = threading.Thread(target=lambda: _hold(locks, False, got))
        other.start()
        other.join(5)
        assert got == ["shared"]  # shared does not exclude shared
        blocked = threading.Thread(target=lambda: _hold(locks, True, got))
        blocked.start()
        time.sleep(0.2)
        assert got == ["shared"]  # exclusive waits for the shared holders
        with pytest.raises(LockOrderError), locks.generation("s", "g", exclusive=True):
            pass
    blocked.join(5)
    assert got == ["shared", "exclusive"]


def _hold(locks: VaultLocks, exclusive: bool, got: list[str]) -> None:
    with locks.generation("s", "g", exclusive=exclusive):
        got.append("exclusive" if exclusive else "shared")


_HOLDER = """
import sys, time
from pathlib import Path
from wsp_vault.locks import VaultLocks
with VaultLocks(Path(sys.argv[1])).source("test-src"):
    print("held", flush=True)
    time.sleep(float(sys.argv[2]))
"""


def test_source_lock_excludes_another_process(tmp_path: Path) -> None:
    holder = subprocess.Popen(
        [sys.executable, "-c", _HOLDER, str(tmp_path / "locks"), "1.0"], stdout=subprocess.PIPE
    )
    assert holder.stdout is not None
    assert holder.stdout.readline().strip() == b"held"
    t0 = time.monotonic()
    with VaultLocks(tmp_path / "locks").source("test-src"):
        waited = time.monotonic() - t0
    holder.wait()
    assert waited > 0.5


def test_lock_dir_inside_the_vault_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="local disk"):
        FileVault(tmp_path / "vault", Ed25519Signer.generate(), lock_dir=tmp_path / "vault" / "locks")


def test_case_collision_is_refused(tmp_path: Path) -> None:
    vault = FileVault(tmp_path / "vault", Ed25519Signer.generate(), lock_dir=tmp_path / "locks")
    a, b = _files(tmp_path, 2)
    vault.ingest(SOURCE, GEN, a, "upstream/Report.pdf")
    with pytest.raises(VaultError, match="case-insensitive"):
        vault.ingest(SOURCE, GEN, b, "upstream/report.PDF")
