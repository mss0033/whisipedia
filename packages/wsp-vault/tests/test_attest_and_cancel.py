"""Signed attestations next to the manifest, and prompt cancellation of Warden work (ADR-0009)."""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest
from vaultkit import GEN, SOURCE, Clock, FakeParity, SealFn, corrupt

from wsp_core.model import VerdictStatus
from wsp_vault.attest import (
    PROMOTION_NAME,
    PROVENANCE_NAME,
    attestation_state,
    read_attestation,
    write_attestation,
)
from wsp_vault.cancel import Interrupted
from wsp_vault.ledger import JsonlLedger
from wsp_vault.manifest import ManifestError, check_relpath
from wsp_vault.parity import Par2Parity, stoppable_runner
from wsp_vault.paritykeeper import ParityKeeper
from wsp_vault.scrub import RateLimiter, Scrubber
from wsp_vault.vault import FileVault, SignatureError, VaultError


def test_attestation_round_trip_and_scrub(
    seal: SealFn, vault: FileVault, tmp_path: Path, clock: Clock
) -> None:
    lib = seal()
    digest = write_attestation(vault, SOURCE, GEN, PROVENANCE_NAME, {"release": {"files": [{"name": "a"}]}})
    assert (
        write_attestation(vault, SOURCE, GEN, PROVENANCE_NAME, {"release": {"files": [{"name": "a"}]}})
        == digest
    )
    doc = read_attestation(vault, SOURCE, GEN, PROVENANCE_NAME)
    assert doc is not None
    assert doc["release"] == {"files": [{"name": "a"}]}
    assert doc["generation_id"] == GEN
    assert len(doc["manifest_blake3"]) == 64
    assert attestation_state(vault, SOURCE, GEN, PROMOTION_NAME) == "missing"
    with pytest.raises(VaultError, match="other content"):  # provenance is write-once
        write_attestation(vault, SOURCE, GEN, PROVENANCE_NAME, {"release": {"files": []}})
    write_attestation(vault, SOURCE, GEN, PROMOTION_NAME, {"verdict": "hold"})
    write_attestation(vault, SOURCE, GEN, PROMOTION_NAME, {"verdict": "approved"})  # replaceable
    assert (read_attestation(vault, SOURCE, GEN, PROMOTION_NAME) or {})["verdict"] == "approved"
    with pytest.raises(VaultError, match="may not set"):
        write_attestation(vault, SOURCE, GEN, PROMOTION_NAME, {"generation_id": "other"})
    with pytest.raises(ManifestError):  # nobody can ingest a file over an attestation name
        check_relpath(PROVENANCE_NAME)

    scrub = Scrubber(vault, tmp_path / "local" / "scrub.sqlite", clock=clock)
    assert scrub.run().ok  # attestations are known files, verified by signature
    corrupt(lib / PROVENANCE_NAME, offset=5)
    with pytest.raises(SignatureError):
        read_attestation(vault, SOURCE, GEN, PROVENANCE_NAME)
    assert attestation_state(vault, SOURCE, GEN, PROVENANCE_NAME) == "invalid"
    findings = scrub.run().findings
    assert [(f.relpath, f.status) for f in findings] == [(PROVENANCE_NAME, VerdictStatus.MISMATCH)]


def test_attestation_is_bound_to_its_manifest(seal: SealFn, vault: FileVault) -> None:
    lib = seal()
    seal("2026-11-01")
    write_attestation(vault, SOURCE, GEN, PROMOTION_NAME, {"verdict": "pass"})
    other = vault.library_path(SOURCE, "2026-11-01")
    for name in (PROMOTION_NAME, PROMOTION_NAME + ".sig"):  # a valid pair copied to another generation
        (other / name).write_bytes((lib / name).read_bytes())
    with pytest.raises(SignatureError, match="another generation"):
        read_attestation(vault, SOURCE, "2026-11-01", PROMOTION_NAME)


def test_rate_limiter_is_interruptible() -> None:
    stop = threading.Event()
    limiter = RateLimiter(1.0, should_stop=stop.is_set)  # 1 byte/s: consuming 60 bytes sleeps a minute
    threading.Timer(0.2, stop.set).start()
    started = time.monotonic()
    with pytest.raises(Interrupted):
        limiter.consume(60)
    assert time.monotonic() - started < 2.0


def test_scrub_stops_mid_file_and_keeps_it_due(
    seal: SealFn, vault: FileVault, tmp_path: Path, clock: Clock, ledger: JsonlLedger
) -> None:
    seal(parity=None)
    stop = threading.Event()
    scrub = Scrubber(
        vault, tmp_path / "local" / "s.sqlite", clock=clock, bytes_per_sec=1.0, should_stop=stop.is_set
    )
    threading.Timer(0.2, stop.set).start()
    started = time.monotonic()
    report = scrub.run()
    assert time.monotonic() - started < 3.0
    assert not report.complete
    assert report.files_checked == 0  # the interrupted file was not recorded: still due next time
    stop.clear()
    fast = Scrubber(vault, tmp_path / "local" / "s.sqlite", clock=clock)
    assert fast.run().files_checked == 3


def test_scrub_pauses_for_interactive_use(
    seal: SealFn, vault: FileVault, tmp_path: Path, clock: Clock
) -> None:
    seal(parity=None)
    busy = Scrubber(vault, tmp_path / "local" / "s.sqlite", clock=clock, should_pause=lambda: True)
    report = busy.run()
    assert (report.files_checked, report.complete) == (0, False)


def test_par2_command_caps_threads_and_memory(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def run(args: object) -> int:
        calls.append(list(args))  # type: ignore[call-overload]
        return 1

    f = tmp_path / "a"
    f.write_bytes(b"x")
    parity = Par2Parity(par2="par2", runner=run, threads=2, memory_mb=256)
    assert not parity.repair([f], tmp_path)  # no index: nothing run
    (tmp_path / "recovery.par2").write_bytes(b"")
    parity.repair([f], tmp_path)
    assert calls[0][:5] == ["par2", "repair", "-q", "-t2", "-m256"]


@pytest.mark.skipif(sys.platform != "linux", reason="process groups")
def test_stoppable_runner_kills_the_process_group() -> None:
    stop = threading.Event()
    run = stoppable_runner(stop.is_set, poll=0.05)
    threading.Timer(0.2, stop.set).start()
    started = time.monotonic()
    with pytest.raises(Interrupted):
        run(["sh", "-c", "sleep 30 & sleep 30"])
    assert time.monotonic() - started < 5.0


def test_parity_keeper_yields_when_paused_or_stopped(
    seal: SealFn, vault: FileVault, tmp_path: Path, fake_parity: FakeParity
) -> None:
    seal(parity=None)
    paused = True
    keeper = ParityKeeper(
        vault,
        fake_parity,  # type: ignore[arg-type]
        state_dir=tmp_path / "state",
        scratch=tmp_path / "scratch",
        max_set_files=1,
        should_pause=lambda: paused,
    )
    report = keeper.run([(SOURCE, GEN)])
    assert (report.created, report.in_progress, report.sets_done) == ((), f"{SOURCE}/{GEN}", 0)
    paused = False
    assert keeper.run([(SOURCE, GEN)]).created == (f"{SOURCE}/{GEN}",)
