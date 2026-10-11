import multiprocessing as mp
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC
from pathlib import Path

from wsp_vault.ledger import JsonlLedger
from wsp_vault.signing import Ed25519Signer


def _signer(tmp: Path) -> Ed25519Signer:
    key = tmp / "key.pem"
    if not key.exists():
        key.write_bytes(Ed25519Signer.generate().private_pem())
    return Ed25519Signer.from_private_pem(key.read_bytes())


def test_concurrent_threads_keep_one_chain(tmp_path: Path) -> None:
    ledger = JsonlLedger(tmp_path / "ledger", _signer(tmp_path))

    def write(i: int) -> None:
        ledger.append("t", f"s{i}", {"i": i})

    with ThreadPoolExecutor(8) as pool:
        list(pool.map(write, range(200)))
    seqs = [e.seq for e in ledger.events()]
    assert seqs == list(range(1, 201))
    assert ledger.verify()


def _worker(directory: str, lock: str, key: str, n: int) -> None:
    signer = Ed25519Signer.from_private_pem(Path(key).read_bytes())
    ledger = JsonlLedger(Path(directory), signer, lock_path=Path(lock))
    for i in range(n):
        ledger.append("p", "proc", {"i": i})


def test_two_processes_share_the_chain_through_the_lock(tmp_path: Path) -> None:
    signer = _signer(tmp_path)
    directory, lock = tmp_path / "ledger", tmp_path / "locks" / "ledger.lock"
    JsonlLedger(directory, signer, lock_path=lock).append("start", "x", {})
    ctx = mp.get_context("spawn")
    procs = [
        ctx.Process(target=_worker, args=(str(directory), str(lock), str(tmp_path / "key.pem"), 40))
        for _ in range(3)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(60)
        assert p.exitcode == 0
    ledger = JsonlLedger(directory, signer, lock_path=lock)
    assert [e.seq for e in ledger.events()] == list(range(1, 122))
    assert ledger.verify()


def test_quarantine_sets_damaged_day_aside_and_chain_continues(tmp_path: Path) -> None:
    ledger = JsonlLedger(tmp_path / "ledger", _signer(tmp_path))
    first = ledger.append("a", "x", {})
    day = first.at.astimezone(UTC).date()
    path = tmp_path / "ledger" / f"{day.isoformat()}.jsonl"
    with path.open("ab") as fh:
        fh.write(b"\x00" * 64 + b"\n")  # torn write, as an unlocked concurrent append left behind
    assert not JsonlLedger(tmp_path / "ledger", _signer(tmp_path)).verify()
    fresh = JsonlLedger(tmp_path / "ledger", _signer(tmp_path))
    event = fresh.quarantine_day(day, "concurrent unlocked appends")
    assert event.kind == "ledger.quarantine"
    assert list((tmp_path / "ledger").glob("*.quarantined-*"))
    assert fresh.verify()
