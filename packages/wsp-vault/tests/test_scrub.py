import os
from collections.abc import Callable
from pathlib import Path

import pytest
from conftest import SealFn
from vaultkit import GEN, SOURCE, Clock, corrupt

from wsp_core.model import VerdictStatus
from wsp_vault.ledger import JsonlLedger
from wsp_vault.scrub import RateLimiter, Scrubber
from wsp_vault.vault import FileVault


@pytest.fixture
def scrubber(vault: FileVault, tmp_path: Path, clock: Clock, ledger: JsonlLedger) -> Scrubber:
    return Scrubber(vault, tmp_path / "local" / "scrub.sqlite", ledger=ledger, clock=clock)


def test_clean_vault_scrubs_clean_and_schedules(scrubber: Scrubber, seal: SealFn, clock: Clock) -> None:
    seal()
    first = scrubber.run()
    assert first.ok
    assert first.complete
    assert first.files_checked == 7  # 3 data + 4 fake parity files
    assert first.bytes_hashed > 0
    assert scrubber.run().files_checked == 0
    clock.advance(days=31)
    assert scrubber.run().files_checked == 7


def _truncate(path: Path) -> None:
    os.chmod(path, 0o644)
    os.truncate(path, 3)


def _delete(path: Path) -> None:
    path.unlink()


@pytest.mark.parametrize(
    ("damage", "status"),
    [
        (corrupt, VerdictStatus.MISMATCH),
        (_truncate, VerdictStatus.MISMATCH),
        (_delete, VerdictStatus.MISSING),
    ],
)
def test_damage_is_detected(
    scrubber: Scrubber, seal: SealFn, damage: Callable[[Path], None], status: VerdictStatus
) -> None:
    lib = seal()
    damage(lib / "upstream/beta.txt")
    report = scrubber.run()
    assert [(f.relpath, f.status) for f in report.findings] == [("upstream/beta.txt", status)]


def test_bad_signature_and_unexpected_files(scrubber: Scrubber, seal: SealFn, vault: FileVault) -> None:
    lib = seal()
    (lib / "upstream/stray.bin").write_bytes(b"?")
    corrupt(lib / "MANIFEST.json.sig", offset=50)
    report = scrubber.run()
    assert [(f.relpath, f.detail) for f in report.findings] == [
        (None, f"{SOURCE}/{GEN}: manifest signature invalid")
    ]


def test_unexpected_file_reported(scrubber: Scrubber, seal: SealFn) -> None:
    lib = seal()
    (lib / "upstream/stray.bin").write_bytes(b"?")
    assert [(f.relpath, f.detail) for f in scrubber.run().findings] == [
        ("upstream/stray.bin", "unexpected file")
    ]


def test_budget_makes_run_resumable(scrubber: Scrubber, seal: SealFn, ledger: JsonlLedger) -> None:
    seal()
    partial = scrubber.run(max_files=2)
    assert (partial.files_checked, partial.complete) == (2, False)
    rest = scrubber.run(max_bytes=1)
    assert (rest.files_checked, rest.complete) == (1, False)
    final = scrubber.run()
    assert (final.files_checked, final.complete) == (4, True)
    assert [e.data["complete"] for e in ledger.events() if e.kind == "scrub.run"] == [False, False, True]


def test_failed_files_are_rechecked_every_run(scrubber: Scrubber, seal: SealFn) -> None:
    lib = seal()
    corrupt(lib / "upstream/beta.txt")
    assert len(scrubber.run().findings) == 1
    again = scrubber.run()
    assert again.files_checked == 1
    assert len(again.findings) == 1


def test_pause_stops_between_files(vault: FileVault, tmp_path: Path, seal: SealFn) -> None:
    seal()
    report = Scrubber(vault, tmp_path / "s.sqlite", should_pause=lambda: True).run()
    assert (report.files_checked, report.complete) == (0, False)


def test_state_must_not_live_on_the_vault(vault: FileVault, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="local disk"):
        Scrubber(vault, tmp_path / "vault" / "scrub.sqlite")


def test_pruned_generations_drop_out_of_state(scrubber: Scrubber, seal: SealFn, vault: FileVault) -> None:
    seal()
    scrubber.run()
    lib = vault.library_path(SOURCE, GEN)
    lib.rename(vault.root.parent / "pruned-elsewhere")
    report = scrubber.run()
    assert report.findings == ()


def test_rate_limiter_paces_with_injected_clock() -> None:
    now = [0.0]
    slept: list[float] = []

    def sleep(s: float) -> None:
        slept.append(s)
        now[0] += s

    limiter = RateLimiter(100.0, monotonic=lambda: now[0], sleep=sleep)
    limiter.consume(50)
    limiter.consume(150)
    assert slept == [0.5, 1.5]
    RateLimiter(None).consume(10**9)
    with pytest.raises(ValueError, match="positive"):
        RateLimiter(0)


def test_rate_limit_is_applied_during_scrub(vault: FileVault, tmp_path: Path, seal: SealFn) -> None:
    seal()
    now = [0.0]
    slept: list[float] = []

    def sleep(s: float) -> None:
        slept.append(s)
        now[0] += s

    report = Scrubber(
        vault, tmp_path / "s.sqlite", bytes_per_sec=1000, monotonic=lambda: now[0], sleep=sleep
    ).run()
    assert sum(slept) == pytest.approx(report.bytes_hashed / 1000)
