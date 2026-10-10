import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest
from vaultkit import Clock

from wsp_core.ports import Ledger
from wsp_core.testing.ledger_contract import LedgerContract
from wsp_vault.ledger import JsonlLedger, LedgerError
from wsp_vault.signing import Ed25519Signer

SIGNER = Ed25519Signer.generate()


class TestJsonlLedgerContract(LedgerContract):
    def make_ledger(self, directory: Path) -> Ledger:
        return JsonlLedger(directory, SIGNER)

    def reopen(self, directory: Path) -> Ledger:
        return JsonlLedger(directory, SIGNER)

    def tamper(self, directory: Path) -> None:
        path = sorted(directory.glob("*.jsonl"))[0]
        lines = path.read_text().splitlines()
        event = json.loads(lines[1])
        event["data"]["n"] = 99
        lines[1] = json.dumps(event, sort_keys=True, separators=(",", ":"))
        path.write_text("\n".join(lines) + "\n")


def test_ledger_satisfies_port(tmp_path: Path) -> None:
    assert isinstance(JsonlLedger(tmp_path, SIGNER), Ledger)


@pytest.fixture
def days(tmp_path: Path, clock: Clock) -> JsonlLedger:
    """Three events over two days: the first day file is checkpointed."""
    ledger = JsonlLedger(tmp_path / "ledger", SIGNER, clock=clock)
    ledger.append("a", "s", {"n": 1})
    ledger.append("a", "s", {"n": 2})
    clock.advance(days=1)
    ledger.append("a", "s", {"n": 3})
    return ledger


def test_daily_files_and_signed_checkpoint(days: JsonlLedger) -> None:
    files = [p.name for _, p in days.day_files()]
    assert files == ["2026-10-09.jsonl", "2026-10-10.jsonl"]
    last = json.loads(days.day_files()[0][1].read_text().splitlines()[-1])
    assert last["checkpoint"] == {
        "date": "2026-10-09",
        "events": 2,
        "last_hash": next(e.hash for e in days.events() if e.seq == 2),
        "last_seq": 2,
    }
    assert days.verify()


def test_event_hash_covers_canonical_fields(days: JsonlLedger) -> None:
    first = next(days.events())
    assert first.prev_hash == "0" * 64
    assert first.at == datetime(2026, 10, 9, 12, 0, tzinfo=UTC)


def _drop_event(lines: list[str]) -> list[str]:
    return lines[:1] + lines[2:]


def _bad_sig(lines: list[str]) -> list[str]:
    return [*lines[:-1], lines[-1].replace('"sig":"', '"sig":"AAAA')]


def _after_checkpoint(lines: list[str]) -> list[str]:
    return [*lines, lines[0]]


def _edit_data(lines: list[str]) -> list[str]:
    return [lines[0].replace('"n":1', '"n":7'), *lines[1:]]


@pytest.mark.parametrize(
    ("edit", "reason"),
    [
        (_drop_event, "checkpoint"),
        (_bad_sig, "checkpoint"),
        (_after_checkpoint, "after checkpoint"),
        (_edit_data, "hash mismatch"),
    ],
)
def test_tampering_with_closed_day_is_detected(
    days: JsonlLedger, edit: Callable[[list[str]], list[str]], reason: str
) -> None:
    path = days.day_files()[0][1]
    lines = path.read_text().splitlines()
    path.write_text("\n".join(edit(lines)) + "\n")
    detail = JsonlLedger(days.directory, SIGNER).verify_detail()
    assert detail is not None
    assert reason in detail


def test_checkpoint_signed_by_another_key_is_rejected(days: JsonlLedger) -> None:
    assert not JsonlLedger(days.directory, Ed25519Signer.generate()).verify()


def test_missing_checkpoint_on_older_day_is_detected(days: JsonlLedger) -> None:
    path = days.day_files()[0][1]
    path.write_text("\n".join(path.read_text().splitlines()[:-1]) + "\n")
    assert "missing checkpoint" in (JsonlLedger(days.directory, SIGNER).verify_detail() or "")


def test_checkpoint_past_days(tmp_path: Path, clock: Clock) -> None:
    ledger = JsonlLedger(tmp_path, SIGNER, clock=clock)
    assert ledger.checkpoint_past_days() == 0
    ledger.append("a", "s", {})
    assert ledger.checkpoint_past_days() == 0
    clock.advance(days=1)
    assert ledger.checkpoint_past_days() == 1
    assert ledger.checkpoint_past_days() == 0
    assert ledger.verify()
    ledger.append("a", "s", {})
    assert JsonlLedger(tmp_path, SIGNER).verify()


def test_clock_behind_closed_day_is_refused(tmp_path: Path, clock: Clock) -> None:
    ledger = JsonlLedger(tmp_path, SIGNER, clock=clock)
    ledger.append("a", "s", {})
    clock.advance(days=1)
    ledger.checkpoint_past_days()
    clock.advance(days=-1)
    with pytest.raises(LedgerError):
        ledger.append("a", "s", {})


def test_torn_tail_is_set_aside_not_deleted(tmp_path: Path, clock: Clock) -> None:
    ledger = JsonlLedger(tmp_path, SIGNER, clock=clock)
    ledger.append("a", "s", {"n": 1})
    path = ledger.day_files()[0][1]
    with path.open("ab") as f:
        f.write(b'{"seq": 2, "trunc')
    assert not JsonlLedger(tmp_path, SIGNER).verify()
    reopened = JsonlLedger(tmp_path, SIGNER, clock=clock)
    event = reopened.append("a", "s", {"n": 2})
    assert event.seq == 2
    assert reopened.verify()
    assert path.with_name(path.name + ".torn").read_bytes() == b'{"seq": 2, "trunc\n'


def test_non_json_data_is_rejected(tmp_path: Path) -> None:
    ledger = JsonlLedger(tmp_path, SIGNER)
    with pytest.raises(TypeError):
        ledger.append("a", "s", {"x": object()})
    assert list(ledger.events()) == []
