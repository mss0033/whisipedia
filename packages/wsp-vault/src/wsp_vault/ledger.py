"""Append-only, hash-chained JSONL ledger with signed daily checkpoints (PLAN §8.3).

Layout: ``<dir>/YYYY-MM-DD.jsonl`` (UTC days). Every line is one event::

    {"at": ..., "data": {...}, "hash": ..., "kind": ..., "prev_hash": ..., "seq": ..., "subject": ...}

``hash`` is the BLAKE3 of the canonical JSON (sorted keys, no whitespace) of every other field, and
``prev_hash`` is the previous event's ``hash`` (64 zeros for the first). When the first event of a
new day is appended, the previous day's file is closed with a signed checkpoint line::

    {"checkpoint": {"date": ..., "events": n, "last_hash": ..., "last_seq": ...}, "sig": "<base64>"}

``verify()`` replays the whole chain: sequence numbers, links, hashes, and every checkpoint and its
signature. Every file except the newest must end with a checkpoint. Truncating the newest, still
open day is only detectable once that day is checkpointed or against an external copy.

**Single writer.** There is no locking (SMB byte-range locks are unreliable, ADR-0002); exactly one
process may append. Appends are not rename-atomic, since rewriting a day file per event would be
quadratic. Instead each append is written with ``O_APPEND``, fsynced, read back and compared; a
failed append is truncated back to the previous length and retried. A torn last line left by a
crash is moved aside to ``<file>.torn`` (never deleted) the next time the ledger is opened.

**Idempotent appends.** ``append(..., idempotency_key=k)`` stores ``k`` as ``data["idempotency_key"]``
and returns the existing event if one with that key was already written. The key index is built
by one scan of the ledger on first use and kept in memory (single writer).
"""

from __future__ import annotations

import base64
import json
import os
import re
from collections import deque
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import suppress
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, cast

import blake3

from wsp_core.model import LedgerEvent
from wsp_core.ports import Signer
from wsp_vault.atomic import DEFAULT_RETRY, ReadbackError, RetryPolicy, fsync_dir

GENESIS_HASH = "0" * 64
IDEMPOTENCY_FIELD = "idempotency_key"
_DAY_FILE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.jsonl$")
_EVENT_KEYS = frozenset({"seq", "at", "kind", "subject", "data", "prev_hash", "hash"})


class LedgerError(Exception):
    pass


def utcnow() -> datetime:
    return datetime.now(UTC)


def canonical_json(obj: object) -> bytes:
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode()


def event_hash(fields: Mapping[str, object]) -> str:
    body = {k: v for k, v in fields.items() if k != "hash"}
    return blake3.blake3(canonical_json(body)).hexdigest()


def _day_events(path: Path) -> list[LedgerEvent]:
    out: list[LedgerEvent] = []
    for raw in path.read_bytes().splitlines():
        if not raw.strip():
            continue
        obj = cast(dict[str, Any], json.loads(raw))
        if "checkpoint" not in obj:
            out.append(_to_event(obj))
    return out


def _to_event(obj: Mapping[str, Any]) -> LedgerEvent:
    return LedgerEvent(
        seq=int(obj["seq"]),
        at=datetime.fromisoformat(str(obj["at"])),
        kind=str(obj["kind"]),
        subject=str(obj["subject"]),
        data=cast(dict[str, object], obj["data"]),
        prev_hash=str(obj["prev_hash"]),
        hash=str(obj["hash"]),
    )


class JsonlLedger:
    """Reference :class:`wsp_core.ports.Ledger`. See the module docstring for format and guarantees."""

    def __init__(
        self,
        directory: Path,
        signer: Signer,
        *,
        clock: Callable[[], datetime] = utcnow,
        retry: RetryPolicy = DEFAULT_RETRY,
    ) -> None:
        self._dir = directory
        self._signer = signer
        self._clock = clock
        self._retry = retry
        self._tail: tuple[int, str, date | None, bool] | None = None
        self._keys: dict[str, LedgerEvent] | None = None

    @property
    def directory(self) -> Path:
        return self._dir

    def day_files(self) -> list[tuple[date, Path]]:
        if not self._dir.is_dir():
            return []
        found: list[tuple[date, Path]] = []
        for path in self._dir.iterdir():
            m = _DAY_FILE.match(path.name)
            if m and path.is_file():
                found.append((date.fromisoformat(m.group(1)), path))
        return sorted(found)

    # --- writing ---------------------------------------------------------------------------------

    def append(
        self, kind: str, subject: str, data: Mapping[str, object], *, idempotency_key: str | None = None
    ) -> LedgerEvent:
        if idempotency_key is not None:
            keys = self._key_index()
            existing = keys.get(idempotency_key)
            if existing is not None:
                return existing
            data = {**data, IDEMPOTENCY_FIELD: idempotency_key}
        event = self._append(kind, subject, data)
        if idempotency_key is not None and self._keys is not None:
            self._keys[idempotency_key] = event
        return event

    def _key_index(self) -> dict[str, LedgerEvent]:
        if self._keys is None:
            self._keys = {}
            for ev in self.events():
                key = ev.data.get(IDEMPOTENCY_FIELD)
                if isinstance(key, str):
                    self._keys[key] = ev
        return self._keys

    def _append(self, kind: str, subject: str, data: Mapping[str, object]) -> LedgerEvent:
        seq, prev, last_day, closed = self._load_tail()
        now = self._clock().astimezone(UTC)
        day = now.date() if last_day is None else max(now.date(), last_day)
        if closed and day == last_day:
            raise LedgerError(f"clock is behind the last checkpointed day {last_day}")
        if last_day is not None and day > last_day and not closed:
            self._write_checkpoint(last_day, seq, prev)
        fields: dict[str, object] = {
            "seq": seq + 1,
            "at": now.isoformat(timespec="microseconds"),
            "kind": kind,
            "subject": subject,
            "data": json.loads(canonical_json(dict(data))),
            "prev_hash": prev,
        }
        fields["hash"] = event_hash(fields)
        self._append_line(self._path(day), canonical_json(fields) + b"\n")
        self._tail = (seq + 1, str(fields["hash"]), day, False)
        return _to_event(fields)

    def checkpoint_past_days(self) -> int:
        """Close any day file older than today that lacks a checkpoint. Returns how many were closed."""
        seq, prev, last_day, closed = self._load_tail()
        if last_day is None or closed or last_day >= self._clock().astimezone(UTC).date():
            return 0
        self._write_checkpoint(last_day, seq, prev)
        self._tail = (seq, prev, last_day, True)
        return 1

    def _path(self, day: date) -> Path:
        return self._dir / f"{day.isoformat()}.jsonl"

    def _write_checkpoint(self, day: date, seq: int, last_hash: str) -> None:
        path = self._path(day)
        count = sum(1 for line in path.read_bytes().splitlines() if line.strip())
        body = {"date": day.isoformat(), "events": count, "last_hash": last_hash, "last_seq": seq}
        sig = base64.b64encode(self._signer.sign(canonical_json(body))).decode()
        self._append_line(path, canonical_json({"checkpoint": body, "sig": sig}) + b"\n")

    def _append_line(self, path: Path, line: bytes) -> None:
        def attempt() -> None:
            self._dir.mkdir(parents=True, exist_ok=True)
            start = path.stat().st_size if path.exists() else 0
            try:
                fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
                try:
                    view = memoryview(line)
                    while view:
                        view = view[os.write(fd, view) :]
                    os.fsync(fd)
                finally:
                    os.close(fd)
                with path.open("rb") as f:
                    f.seek(start)
                    if f.read() != line:
                        raise ReadbackError(f"{path}: appended line did not read back")
            except BaseException:
                with suppress(OSError):
                    os.truncate(path, start)
                raise
            if start == 0:
                fsync_dir(self._dir)

        self._retry.run(attempt)

    def _load_tail(self) -> tuple[int, str, date | None, bool]:
        if self._tail is not None:
            return self._tail
        files = self.day_files()
        if not files:
            self._tail = (0, GENESIS_HASH, None, False)
            return self._tail
        day, path = files[-1]
        self._set_aside_torn_tail(path)
        seq, prev, closed = 0, GENESIS_HASH, False
        for raw in path.read_bytes().splitlines():
            if not raw.strip():
                continue
            obj = cast(dict[str, Any], json.loads(raw))
            if "checkpoint" in obj:
                closed = True
            else:
                seq, prev = int(obj["seq"]), str(obj["hash"])
        if seq == 0:
            raise LedgerError(f"{path}: day file has no events")
        self._tail = (seq, prev, day, closed)
        return self._tail

    @staticmethod
    def _set_aside_torn_tail(path: Path) -> None:
        data = path.read_bytes()
        if not data or data.endswith(b"\n"):
            return
        keep = data.rfind(b"\n") + 1
        torn = path.with_name(path.name + ".torn")
        with torn.open("ab") as f:
            f.write(data[keep:] + b"\n")
            f.flush()
            os.fsync(f.fileno())
        os.truncate(path, keep)

    # --- reading ---------------------------------------------------------------------------------

    def events(self, since_seq: int = 0) -> Iterator[LedgerEvent]:
        for _, path in self.day_files():
            for ev in _day_events(path):
                if ev.seq > since_seq:
                    yield ev

    def tail(self, n: int) -> Sequence[LedgerEvent]:
        """The newest ``n`` events, oldest first; reads day files newest first and stops early."""
        if n <= 0:
            return []
        out: deque[LedgerEvent] = deque()
        for _, path in reversed(self.day_files()):
            out.extendleft(reversed(_day_events(path)))
            if len(out) >= n:
                break
        return list(out)[-n:]

    def verify(self) -> bool:
        return self.verify_detail() is None

    def verify_detail(self) -> str | None:
        """``None`` if the chain is intact, else a description of the first problem found."""
        seq, prev = 0, GENESIS_HASH
        files = self.day_files()
        for index, (day, path) in enumerate(files):
            data = path.read_bytes()
            if not data.endswith(b"\n"):
                return f"{path.name}: torn or empty file"
            count, closed = 0, False
            for lineno, raw in enumerate(data.split(b"\n")[:-1], start=1):
                where = f"{path.name}:{lineno}"
                if closed:
                    return f"{where}: data after checkpoint"
                try:
                    obj = cast(dict[str, Any], json.loads(raw))
                except ValueError:
                    return f"{where}: not JSON"
                if "checkpoint" in obj:
                    problem = self._check_checkpoint(obj, day, count, seq, prev)
                    if problem:
                        return f"{where}: {problem}"
                    closed = True
                    continue
                if frozenset(obj) != _EVENT_KEYS:
                    return f"{where}: unexpected fields"
                if obj["seq"] != seq + 1:
                    return f"{where}: sequence gap ({obj['seq']} after {seq})"
                if obj["prev_hash"] != prev:
                    return f"{where}: broken link"
                if event_hash(obj) != obj["hash"]:
                    return f"{where}: hash mismatch"
                seq, prev, count = seq + 1, str(obj["hash"]), count + 1
            if count == 0:
                return f"{path.name}: no events"
            if index < len(files) - 1 and not closed:
                return f"{path.name}: missing checkpoint"
        return None

    def _check_checkpoint(self, obj: Mapping[str, Any], day: date, count: int, seq: int, prev: str) -> str:
        expected = {"date": day.isoformat(), "events": count, "last_hash": prev, "last_seq": seq}
        if set(obj) != {"checkpoint", "sig"} or obj["checkpoint"] != expected:
            return "checkpoint does not match chain"
        try:
            sig = base64.b64decode(str(obj["sig"]), validate=True)
        except ValueError:
            return "checkpoint signature is not base64"
        if not self._signer.verify(canonical_json(expected), sig):
            return "checkpoint signature invalid"
        return ""
