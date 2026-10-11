"""The Warden under storage faults and mass failures (vault-fetch audit #1, #2, #7, #10, #11, #13, #18)."""

from __future__ import annotations

import errno
import hashlib
import json
import random
import shutil
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path

import pytest
from vaultkit import FILES, GEN, SOURCE, Clock, FakeParity, SealFn, corrupt

from wsp_core.model import GenerationStatus, VerdictStatus
from wsp_core.ports import ParityProvider
from wsp_vault import scrub as scrub_mod
from wsp_vault.atomic import readback
from wsp_vault.canary import MountCanary, MountUnavailableError
from wsp_vault.cancel import Paused
from wsp_vault.drill import _flip_bit  # pyright: ignore[reportPrivateUsage]
from wsp_vault.hashing import Digests
from wsp_vault.ledger import JsonlLedger
from wsp_vault.manifest import DEGRADED_NAME, SIGNATURE_NAME
from wsp_vault.parity import Par2Parity
from wsp_vault.paritykeeper import ParityKeeper, ParityState, Verified
from wsp_vault.ratelimit import RateLimiter
from wsp_vault.repair import RepairCircuitOpenError, RepairLadder
from wsp_vault.scrub import Scrubber
from wsp_vault.signing import Ed25519Signer
from wsp_vault.undegrade import reverify_and_clear
from wsp_vault.vault import FileVault, KeyPinError

TARGET = "upstream/beta.txt"
LocalFn = Callable[[str, bytes], Path]


def kinds(ledger: JsonlLedger, prefix: str = "") -> list[str]:
    return [e.kind for e in ledger.events() if e.kind.startswith(prefix)]


# --- rate limiter (audit #10) ----------------------------------------------------------------------


class FakeTime:
    def __init__(self) -> None:
        self.now = 0.0
        self.slept = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, s: float) -> None:
        self.slept += s
        self.now += s


def test_idle_time_is_not_banked_as_credit() -> None:
    """The auditor's demo: 80 MiB, an hour idle, then a 25 GB slice at 50 MB/s."""
    t = FakeTime()
    rate = 50e6
    limiter = RateLimiter(rate, monotonic=t.monotonic, sleep=t.sleep)
    for _ in range(10):
        limiter.consume(8 << 20)
    t.now += 3600
    t.slept = 0.0
    for _ in range(3000):
        limiter.consume(8 << 20)
    expected = 3000 * (8 << 20) / rate
    assert t.slept == pytest.approx(expected - 1.0, abs=0.01)  # at most one second of burst credit
    limiter.reset()  # a new run starts with an empty bucket
    t.now += 3600
    t.slept = 0.0
    for _ in range(100):
        limiter.consume(8 << 20)
    assert t.slept == pytest.approx(100 * (8 << 20) / rate, abs=0.01)


def test_pause_is_checked_inside_consume() -> None:
    t = FakeTime()
    busy = {"now": False}
    limiter = RateLimiter(
        1e6, monotonic=t.monotonic, sleep=t.sleep, should_pause=lambda: busy["now"], check_every=0.0
    )
    limiter.consume(10)
    busy["now"] = True
    with pytest.raises(Paused):
        limiter.consume(4_000_000)  # stops within one sleep slice, not after 4 s
    assert t.slept <= 0.25 + 1e-9


def test_pause_mid_file_leaves_it_due(vault: FileVault, seal: SealFn, tmp_path: Path) -> None:
    seal()
    busy = {"calls": 0}

    def busy_after_first_check() -> bool:
        busy["calls"] += 1
        return busy["calls"] > 1

    s = Scrubber(vault, tmp_path / "s.sqlite", should_pause=busy_after_first_check)
    report = s.run()
    assert report.files_checked == 0
    assert not report.complete
    assert s.verified_ok([f"{SOURCE}/{GEN}/{p}" for p in FILES]) == set()


# --- I/O errors are not damage (audit #2) ------------------------------------------------------------


def _eio_for(name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    real = readback

    def flaky(path: Path, **kw: object) -> Digests:
        if path.name == name:
            raise OSError(errno.EIO, "soft mount: server not responding")
        return real(path, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr("wsp_vault.scrub.readback", flaky)


def test_unreadable_file_is_reported_as_such_and_never_repaired(
    vault: FileVault, seal: SealFn, ledger: JsonlLedger, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seal("2026-09-01")
    lib = seal(GEN)
    _eio_for("beta.txt", monkeypatch)
    report = Scrubber(vault, tmp_path / "s.sqlite").run()
    assert [(f.relpath, f.status) for f in report.findings] == [
        (TARGET, VerdictStatus.UNREADABLE),
        (TARGET, VerdictStatus.UNREADABLE),
    ]
    assert RepairLadder(vault, ledger, parity=FakeParity()).repair_findings(report.findings) == []
    assert not (lib / DEGRADED_NAME).exists()
    current = vault.current(SOURCE)
    assert current is not None
    assert current.generation_id == GEN  # no rollback either
    assert "repair.deferred" in kinds(ledger)


def test_unreadable_during_repair_defers_instead_of_degrading(
    vault: FileVault, seal: SealFn, ledger: JsonlLedger, monkeypatch: pytest.MonkeyPatch
) -> None:
    lib = seal()
    corrupt(lib / TARGET)
    real = readback

    def gone(path: Path, **kw: object) -> Digests:
        if path.name == "beta.txt":
            raise OSError(errno.EHOSTDOWN, "host is down")
        return real(path, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr("wsp_vault.repair.readback", gone)
    result = RepairLadder(vault, ledger).repair(SOURCE, GEN, [TARGET])
    assert result.deferred is not None
    assert not result.degraded
    assert not (lib / DEGRADED_NAME).exists()


def test_mount_outage_mid_run_records_nothing(
    vault: FileVault, seal: SealFn, ledger: JsonlLedger, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seal()
    _eio_for("beta.txt", monkeypatch)
    state = {"up": True}

    def canary() -> None:
        if not state["up"]:
            raise MountUnavailableError(errno.EHOSTDOWN, "share gone")

    real = readback

    def outage(path: Path, **kw: object) -> Digests:
        if path.name == "beta.txt":
            state["up"] = False
            raise OSError(errno.EIO, "server not responding")
        return real(path, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr("wsp_vault.scrub.readback", outage)
    s = Scrubber(vault, tmp_path / "s.sqlite", canary=canary, ledger=ledger)
    report = s.run()
    assert report.aborted is not None
    assert "share gone" in report.aborted
    assert all(f.relpath != TARGET for f in report.findings)
    assert f"{SOURCE}/{GEN}/{TARGET}" not in s.verified_ok([f"{SOURCE}/{GEN}/{TARGET}"])
    state["up"] = False
    again = s.run()
    assert (again.files_checked, again.findings, again.complete) == (0, (), False)
    assert again.aborted is not None


def test_unmounted_share_aborts_and_keeps_history(vault: FileVault, seal: SealFn, tmp_path: Path) -> None:
    seal()
    s = Scrubber(vault, tmp_path / "s.sqlite")
    assert s.run().ok
    keys = [f"{SOURCE}/{GEN}/{p}" for p in FILES]
    assert s.verified_ok(keys) == set(keys)
    hidden = vault.root.with_name("unmounted")
    vault.root.rename(hidden)  # what /mnt/z looks like when the share is not mounted
    report = s.run()
    assert report.aborted is not None
    hidden.rename(vault.root)
    assert s.verified_ok(keys) == set(keys)  # nothing was forgotten as "stale"


def test_mount_canary_detects_a_changed_key_file(tmp_path: Path) -> None:
    (tmp_path / "keys").mkdir()
    key = tmp_path / "keys" / "k.pub"
    key.write_text("ssh-ed25519 AAAA k\n")
    canary = MountCanary(tmp_path)
    canary()
    key.write_text("garbage\n")
    with pytest.raises(MountUnavailableError):
        canary()


# --- incremental inventory (audit #11) --------------------------------------------------------------


def test_inventory_is_incremental(
    vault: FileVault, seal: SealFn, clock: Clock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lib = seal()
    walks: list[Path] = []
    real_walk = scrub_mod._walk  # pyright: ignore[reportPrivateUsage]

    def counting_walk(root: Path) -> Iterator[str]:
        walks.append(root)
        return real_walk(root)

    monkeypatch.setattr("wsp_vault.scrub._walk", counting_walk)
    verifies: list[int] = []
    real_verify = vault.signer.verify

    def counting_verify(payload: bytes, signature: bytes) -> bool:
        verifies.append(1)
        return real_verify(payload, signature)

    monkeypatch.setattr(vault.signer, "verify", counting_verify)
    s = Scrubber(vault, tmp_path / "s.sqlite", clock=clock)
    s.run()
    first_walks, first_verifies = len(walks), len(verifies)
    assert first_walks == 1
    assert first_verifies >= 1
    s.run()
    assert (len(walks), len(verifies)) == (first_walks, first_verifies)  # nothing changed: no work
    (lib / "stray.bin").write_bytes(b"?")  # changes the generation directory's mtime
    found = s.run().findings
    assert [f.relpath for f in found] == ["stray.bin"]
    (lib / "stray.bin").unlink()
    clock.advance(days=31)
    s.run()
    assert len(verifies) > first_verifies  # re-verified once per cycle (bit rot in a manifest)


def test_verified_md5_is_recorded_for_parity_binding(vault: FileVault, seal: SealFn, tmp_path: Path) -> None:
    seal(parity=None)
    s = Scrubber(vault, tmp_path / "s.sqlite")
    s.run()
    key = f"{SOURCE}/{GEN}/{TARGET}"
    assert s.verified_md5([key]) == {key: hashlib.md5(FILES[TARGET]).hexdigest()}


def test_old_state_files_gain_the_new_columns(vault: FileVault, seal: SealFn, tmp_path: Path) -> None:
    import sqlite3

    seal()
    path = tmp_path / "old.sqlite"
    db = sqlite3.connect(path)
    db.execute(
        "CREATE TABLE files (key TEXT PRIMARY KEY, verified_at TEXT NOT NULL, ok INTEGER NOT NULL, "
        "detail TEXT NOT NULL)"
    )
    db.execute("INSERT INTO files VALUES ('x/y/z', '2026-10-01T00:00:00+00:00', 1, '')")
    db.commit()
    db.close()
    assert Scrubber(vault, path).run().ok


# --- circuit breaker and key pinning (audit #1) -----------------------------------------------------


def test_mass_signature_failures_open_the_circuit(
    vault: FileVault, seal: SealFn, ledger: JsonlLedger, tmp_path: Path
) -> None:
    gens = ["2026-07-01", "2026-08-01", "2026-09-01", GEN]
    for g in gens:
        seal(g)
    for g in gens[:3]:
        corrupt(vault.library_path(SOURCE, g) / SIGNATURE_NAME, offset=50)
    findings = Scrubber(vault, tmp_path / "s.sqlite").run().findings
    assert len([f for f in findings if f.relpath is None]) == 3
    with pytest.raises(RepairCircuitOpenError, match="3 of 4"):
        RepairLadder(vault, ledger).repair_findings(findings)
    assert not any((vault.library_path(SOURCE, g) / DEGRADED_NAME).exists() for g in gens)
    assert "repair.circuit_open" in kinds(ledger)
    assert vault.current(SOURCE) is not None


def test_a_single_bad_manifest_still_follows_the_ladder(
    vault: FileVault, seal: SealFn, ledger: JsonlLedger, tmp_path: Path
) -> None:
    for g in ["2026-08-01", "2026-09-01", GEN]:
        seal(g)
    corrupt(vault.library_path(SOURCE, "2026-08-01") / SIGNATURE_NAME, offset=50)
    findings = Scrubber(vault, tmp_path / "s.sqlite").run().findings
    (result,) = RepairLadder(vault, ledger).repair_findings(findings)
    assert result.degraded  # no mirror: rung 4, as before


def test_a_scrub_with_a_foreign_key_stops_before_anything(seal: SealFn, tmp_path: Path) -> None:
    seal()
    stranger = FileVault(tmp_path / "vault", Ed25519Signer.generate(), domains={SOURCE: "reference"})
    with pytest.raises(KeyPinError):
        Scrubber(stranger, tmp_path / "s.sqlite").run()


# --- rollback (audit #2, #13) ---------------------------------------------------------------------


def _partitioned(tmp_path: Path, clock: Clock, signer: Ed25519Signer, ledger: JsonlLedger) -> FileVault:
    return FileVault(
        tmp_path / "pvault",
        signer,
        domains={SOURCE: "reference"},
        partitioned=[SOURCE],
        ledger=ledger,
        clock=clock,
    )


def _seal_one(vault: FileVault, gid: str, make_local: LocalFn, clock: Clock) -> Path:
    clock.advance(hours=1)
    for rel, data in FILES.items():
        vault.ingest(SOURCE, gid, make_local(f"{gid}/{rel}", data), rel)
    vault.seal(SOURCE, gid)
    vault.promote(SOURCE, gid)
    return vault.library_path(SOURCE, gid)


def test_rollback_stays_in_the_partition_and_orders_by_seal_time(
    tmp_path: Path, clock: Clock, signer: Ed25519Signer, ledger: JsonlLedger, make_local: LocalFn
) -> None:
    v = _partitioned(tmp_path, clock, signer, ledger)
    _seal_one(v, "2026-09.ak", make_local, clock)  # another partition: never a rollback target
    _seal_one(v, "2026-10.ca", make_local, clock)  # sealed first: the older generation of "ca"
    newest = _seal_one(v, "2026-10-11.ca", make_local, clock)  # sorts *before* "2026-10.ca" by id
    corrupt(newest / TARGET)
    result = RepairLadder(v, ledger).repair(SOURCE, "2026-10-11.ca", [TARGET])
    assert result.degraded
    assert result.rolled_back_to == "2026-10.ca"
    assert v.current_set_ids(SOURCE) == sorted(["2026-09.ak", "2026-10.ca"])


def test_rollback_verifies_the_target_first(
    vault: FileVault, seal: SealFn, ledger: JsonlLedger, clock: Clock
) -> None:
    clock.advance(hours=1)
    seal("2026-08-01")
    clock.advance(hours=1)
    rotten = seal("2026-09-01")
    clock.advance(hours=1)
    lib = seal(GEN)
    corrupt(rotten / TARGET)  # the previous generation rotted too, unnoticed so far
    corrupt(lib / TARGET)
    result = RepairLadder(vault, ledger).repair(SOURCE, GEN, [TARGET])
    assert result.rolled_back_to == "2026-08-01"
    assert "repair.rollback_skipped" in kinds(ledger)


# --- verified un-degrade ----------------------------------------------------------------------------


def test_degraded_is_cleared_only_after_a_full_verification(
    vault: FileVault, seal: SealFn, ledger: JsonlLedger, tmp_path: Path
) -> None:
    lib = seal()
    original = (lib / TARGET).read_bytes()
    corrupt(lib / TARGET)
    assert RepairLadder(vault, ledger, rollback_current=False).repair(SOURCE, GEN, [TARGET]).degraded
    scrubber = Scrubber(vault, tmp_path / "s.sqlite")
    refused = reverify_and_clear(vault, scrubber, SOURCE, GEN)
    assert not refused.cleared
    assert [f.relpath for f in refused.findings] == [TARGET]
    assert "vault.undegrade_refused" in kinds(ledger)
    assert (lib / DEGRADED_NAME).exists()

    (lib / TARGET).chmod(0o644)
    (lib / TARGET).write_bytes(original)  # e.g. the bytes came back after a transient fault
    cleared = reverify_and_clear(vault, scrubber, SOURCE, GEN)
    assert cleared.cleared
    assert cleared.files == len(FILES) + 4  # every data and parity file re-hashed
    assert not (lib / DEGRADED_NAME).exists()
    assert list((vault.root / "quarantine").rglob("DEGRADED.txt.*"))  # kept as history
    (event,) = [e for e in ledger.events() if e.kind == "vault.undegraded"]
    assert event.data["files_verified"] == cleared.files
    gens = {g.generation_id: g.status for g in vault.generations(SOURCE)}
    assert gens[GEN] is GenerationStatus.SEALED
    vault.promote(SOURCE, GEN)
    assert reverify_and_clear(vault, scrubber, SOURCE, GEN).detail == "not degraded"


# --- parity keeper (audit #7, #18) -----------------------------------------------------------------


def _keeper(
    vault: FileVault, tmp_path: Path, parity: ParityProvider, *, verified: Verified | None = None, **kw: int
) -> ParityKeeper:
    return ParityKeeper(
        vault,
        parity,
        state_dir=tmp_path / "state",
        scratch=tmp_path / "scratch",
        verified=verified,
        **kw,  # type: ignore[arg-type]
    )


def _md5_lookup(table: dict[str, str]) -> Verified:
    def lookup(keys: Sequence[str]) -> dict[str, str]:
        return {k: table[k] for k in keys}

    return lookup


@pytest.mark.skipif(shutil.which("par2") is None, reason="par2 not installed")
def test_keeper_binds_par2_to_the_scrubs_md5(
    vault: FileVault, seal: SealFn, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seal(parity=None)
    keys = {f"{SOURCE}/{GEN}/{p}": hashlib.md5(d).hexdigest() for p, d in FILES.items()}
    reads: list[Path] = []
    real = readback

    def counting(path: Path, **kw: object) -> Digests:
        reads.append(path)
        return real(path, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr("wsp_vault.paritykeeper.readback", counting)
    lying = {**keys, f"{SOURCE}/{GEN}/{TARGET}": "0" * 32}  # md5 of other bytes than par2 reads
    bad = _keeper(vault, tmp_path, Par2Parity(), verified=_md5_lookup(lying)).run([(SOURCE, GEN)])
    assert bad.created == ()
    assert "differ" in bad.skipped[0]
    good_keeper = _keeper(vault, tmp_path, Par2Parity(), verified=_md5_lookup(keys))
    good = good_keeper.run([(SOURCE, GEN)])
    assert good.created == (f"{SOURCE}/{GEN}",)
    assert [p for p in reads if p.parent.name != "parity"] == []  # no data file read again
    assert Scrubber(vault, tmp_path / "s.sqlite").scrub_generation(SOURCE, GEN) == []


def test_keeper_rereads_when_the_scrub_cannot_vouch_for_content(
    vault: FileVault, seal: SealFn, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seal(parity=None)
    reads: list[str] = []
    real = readback

    def counting(path: Path, **kw: object) -> Digests:
        reads.append(path.name)
        return real(path, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr("wsp_vault.paritykeeper.readback", counting)
    every = {f"{SOURCE}/{GEN}/{p}" for p in FILES}

    def keys_only(keys: Sequence[str]) -> set[str]:
        return set(keys) & every

    _keeper(vault, tmp_path, FakeParity(), verified=keys_only).run([(SOURCE, GEN)])
    assert sorted(reads)[:3] == sorted(p.rsplit("/", 1)[-1] for p in FILES)


def test_torn_progress_file_is_set_aside_not_skipped_forever(
    vault: FileVault, seal: SealFn, tmp_path: Path
) -> None:
    seal(parity=None)
    k = _keeper(vault, tmp_path, FakeParity(), max_set_files=1)
    assert not k.run([(SOURCE, GEN)], max_bytes=1).created  # one set done, progress saved
    progress = tmp_path / "state" / "parity" / SOURCE / f"{GEN}.json"
    assert json.loads(progress.read_text())["done"]
    progress.write_text('{"manifest_blake3": "')  # torn by a power cut
    report = k.run([(SOURCE, GEN)])
    assert report.created == (f"{SOURCE}/{GEN}",)
    assert k.state(SOURCE, GEN) is ParityState.SIDECAR
    assert list(progress.parent.glob(f"{GEN}.json.corrupt-*"))


def test_keeper_defers_on_unreadable_data(
    vault: FileVault, seal: SealFn, tmp_path: Path, ledger: JsonlLedger, monkeypatch: pytest.MonkeyPatch
) -> None:
    seal(parity=None)

    def dead(path: Path, **kw: object) -> Digests:
        raise OSError(errno.EIO, "server not responding")

    monkeypatch.setattr("wsp_vault.paritykeeper.readback", dead)
    report = _keeper(vault, tmp_path, FakeParity()).run([(SOURCE, GEN)])
    assert report.created == ()
    assert "unreadable" in report.skipped[0]
    assert "parity.deferred" in kinds(ledger)


# --- drills (audit #20) -----------------------------------------------------------------------------


def test_bit_flip_touches_exactly_one_bit(tmp_path: Path) -> None:
    path = tmp_path / "f"
    data = bytes(range(256)) * 100
    path.write_bytes(data)
    _flip_bit(path, random.Random(3))
    after = path.read_bytes()
    diff = [(a ^ b) for a, b in zip(data, after, strict=True) if a != b]
    assert len(diff) == 1
    assert bin(diff[0]).count("1") == 1
