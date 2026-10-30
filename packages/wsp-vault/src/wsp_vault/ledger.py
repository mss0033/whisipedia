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

**Writers.** Writers are serialized by a thread lock and, across processes, by an ``flock`` on a
LOCAL lock file (``lock_path``; SMB byte-range locks are unreliable, ADR-0002). Appends are not
rename-atomic, since rewriting a day file per event would be quadratic. Instead each append is
written with ``O_APPEND``, fsynced, read back from storage (page cache dropped) and compared; a
failed append is truncated back to the previous length, the truncation is checked, and only then is
the append retried.

**A failed append whose truncation also fails** (a soft-mount outage can do both, vault-fetch audit
#3) leaves an unknown fragment at the end of the file. The ledger then marks itself *dirty* and does
not append again until a recovery cut the file back to its last valid hash-chained line: retrying
after the fragment is what used to merge a fragment and a valid line into one undecodable line and
break the ledger for every later writer. The next append (in this process or, since the fragment is
on disk, in any process) runs :meth:`JsonlLedger.recover` first: the bytes after the last valid line
are appended to ``<file>.torn`` (never deleted) and a ``ledger.recovered`` event records their size
and SHA-256. A trailing line without a newline (a crash mid-append) is cut the same way.
:meth:`JsonlLedger.recover` is also the owner's tool for a damaged tail; damage before valid events
is never cut automatically (use :meth:`JsonlLedger.quarantine_day`).

**Readers** (:meth:`JsonlLedger.events`, :meth:`JsonlLedger.tail`, and :meth:`JsonlLedger.verify`
for the newest day) ignore a trailing line without a newline: it is an append in progress or a torn
write that the next writer cuts.

**Known limits.** The newest, still open day is unsigned until it is checkpointed (the first event of
the next day, or :meth:`JsonlLedger.checkpoint_past_days`), so truncating it is only detectable
against an external copy. Write-once segment files would remove the in-place append entirely, but
would change the on-disk format that readers and ``wsp ledger verify`` rely on; they are not used.

**Idempotent appends.** ``append(..., idempotency_key=k)`` stores ``k`` as ``data["idempotency_key"]``
and returns the existing event if one with that key was already written. The key index is built
by one scan of the ledger on first use and kept in memory (single writer).
"""

from __future__ import annotations

import base64
import contextlib
import fcntl
import hashlib
import json
import os
import re
import threading
from collections import deque
from collections.abc import Callable, Generator, Iterator, Mapping, Sequence
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


class LedgerDirtyError(LedgerError):
    """An append failed and its bytes could not be removed; nothing is appended until
    :meth:`JsonlLedger.recover` cut the file back to its last valid line (the next append tries)."""


def utcnow() -> datetime:
    return datetime.now(UTC)


def canonical_json(obj: object) -> bytes:
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode()


def event_hash(fields: Mapping[str, object]) -> str:
    body = {k: v for k, v in fields.items() if k != "hash"}
    return blake3.blake3(canonical_json(body)).hexdigest()


def _complete_lines(data: bytes) -> list[bytes]:
    """Lines of a day file, without a trailing partial line (an append in progress or a torn write)."""
    end = data.rfind(b"\n") + 1
    return data[:end].splitlines()


def _day_events(path: Path) -> list[LedgerEvent]:
    out: list[LedgerEvent] = []
    for raw in _complete_lines(path.read_bytes()):
        if not raw.strip():
            continue
        obj = cast(dict[str, Any], json.loads(raw))
        if "checkpoint" not in obj:
            out.append(_to_event(obj))
    return out


def _read_back(path: Path, start: int, length: int) -> bytes:
    """Read ``length`` bytes at ``start`` from storage (page cache dropped first)."""
    fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
    try:
        with suppress(OSError, AttributeError):
            os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
        out = bytearray()
        while len(out) < length:
            chunk = os.pread(fd, length - len(out), start + len(out))
            if not chunk:
                break
            out += chunk
        return bytes(out)
    finally:
        os.close(fd)


def _valid_prefix(data: bytes) -> int:
    """Byte length of the longest prefix of complete lines that form a valid chain: every event line
    hashes correctly and links to the one before it (the first links to whatever came before the
    file); at most one checkpoint, as the last line."""
    keep = 0
    prev: str | None = None
    seq: int | None = None
    pos = 0
    while True:
        nl = data.find(b"\n", pos)
        if nl < 0:
            return keep
        raw = data[pos:nl]
        pos = nl + 1
        try:
            parsed: object = json.loads(raw)
        except ValueError:
            return keep
        if not isinstance(parsed, dict):
            return keep
        obj = cast(dict[str, Any], parsed)
        if "checkpoint" in obj:
            if set(obj) != {"checkpoint", "sig"} or prev is None:
                return keep
            return pos  # nothing may follow a checkpoint
        if frozenset(obj) != _EVENT_KEYS or event_hash(obj) != obj["hash"]:
            return keep
        if prev is not None and (obj["prev_hash"] != prev or obj["seq"] != (seq or 0) + 1):
            return keep
        prev, seq, keep = str(obj["hash"]), int(obj["seq"]), pos


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
        lock_path: Path | None = None,
    ) -> None:
        """``lock_path`` (on LOCAL disk; SMB locks are unreliable) serializes writers across processes,
        e.g. the orchestrator daemon and a manual ``wsp ingest``. Threads in one process are always
        serialized."""
        self._dir = directory
        self._signer = signer
        self._clock = clock
        self._retry = retry
        self._lock_path = lock_path
        self._mutex = threading.RLock()
        self._tail: tuple[int, str, date | None, bool] | None = None
        self._tail_size = -1  # size of the newest day file when the tail was cached
        self._keys: dict[str, LedgerEvent] | None = None
        self._keys_seq = 0  # the idempotency index covers events up to this seq
        self._dirty: Path | None = None  # a day file with an unremoved fragment of a failed append

    @property
    def directory(self) -> Path:
        return self._dir

    def day_files(self) -> list[tuple[date, Path]]:
        # Read under the same policy as the appends: every vault event lists the day files, and one
        # refusal after a seal left a generation sealed but never promoted (2026-10-29 spot check).
        def scan() -> list[tuple[date, Path]]:
            if not self._dir.is_dir():
                return []
            found: list[tuple[date, Path]] = []
            for path in self._dir.iterdir():
                m = _DAY_FILE.match(path.name)
                if m and path.is_file():
                    found.append((date.fromisoformat(m.group(1)), path))
            return sorted(found)

        return self._retry.run(scan)

    # --- writing ---------------------------------------------------------------------------------

    @contextlib.contextmanager
    def _writer(self) -> Generator[None]:
        """One writer at a time: a thread lock, plus an ``flock`` across processes when configured.
        The cached tail is dropped if another process appended since we last looked."""
        with self._mutex:
            fd: int | None = None
            if self._lock_path is not None:
                self._lock_path.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(self._lock_path, os.O_RDWR | os.O_CREAT, 0o644)
                fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                if self._tail is not None and self._newest_size() != self._tail_size:
                    self._tail = None  # another process appended: re-read the tail (newest file only);
                    # the idempotency index is brought up to date incrementally in _key_index
                yield
            finally:
                if fd is not None:
                    os.close(fd)

    def _newest_size(self) -> int:
        files = self.day_files()
        return self._retry.run(lambda: files[-1][1].stat().st_size) if files else 0

    def append(
        self, kind: str, subject: str, data: Mapping[str, object], *, idempotency_key: str | None = None
    ) -> LedgerEvent:
        with self._writer():
            return self._append_keyed(kind, subject, data, idempotency_key)

    def _append_keyed(
        self, kind: str, subject: str, data: Mapping[str, object], idempotency_key: str | None
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
            if event.seq == self._keys_seq + 1:
                self._keys_seq = event.seq
        return event

    def _key_index(self) -> dict[str, LedgerEvent]:
        """Idempotency keys, built by one full scan per process and then extended with only the
        events appended since (by any process), read from the newest day files backwards."""
        if self._keys is None:
            self._keys, self._keys_seq = {}, 0
        elif self._keys_seq == self._load_tail()[0]:
            return self._keys  # nothing was appended since the index was last brought up to date
        newer: list[LedgerEvent] = []
        for _, path in reversed(self.day_files()):
            events = _day_events(path)
            newer.extend(e for e in events if e.seq > self._keys_seq)
            if events and events[0].seq <= self._keys_seq:
                break
        for ev in sorted(newer, key=lambda e: e.seq):
            key = ev.data.get(IDEMPOTENCY_FIELD)
            if isinstance(key, str):
                self._keys[key] = ev
            self._keys_seq = max(self._keys_seq, ev.seq)
        return self._keys

    def _append(self, kind: str, subject: str, data: Mapping[str, object]) -> LedgerEvent:
        if self._dirty is not None:
            try:
                self._recover(self._dirty, "an append failed and its bytes could not be removed")
            except OSError as exc:
                raise LedgerDirtyError(f"{self._dirty}: still dirty, recovery failed ({exc})") from exc
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
        with self._writer():
            return self._checkpoint_past_days()

    def quarantine_day(self, day: date, reason: str) -> LedgerEvent:
        """Set the damaged *newest* day file aside (never edited or deleted) and continue the chain from
        the last intact event, recording what was set aside and why. The renamed file no longer
        matches the day-file pattern, so verification covers the intact chain only.

        Only the newest day can be set aside: the days after an older one chain from its last event,
        so removing it would break the chain for good (audit #17)."""
        with self._writer():
            path = self._path(day)
            if not path.is_file():
                raise LedgerError(f"no day file for {day}")
            files = self.day_files()
            if files and files[-1][0] != day:
                raise LedgerError(
                    f"{day} is not the newest day file ({files[-1][0]}); later days chain from it, so it "
                    "cannot be set aside without breaking the chain"
                )
            data = path.read_bytes()
            stamp = self._clock().astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
            target = path.with_name(f"{path.name}.quarantined-{stamp}")
            os.rename(path, target)
            fsync_dir(self._dir)
            self._tail, self._keys, self._dirty = None, None, None
            return self._append(
                "ledger.quarantine",
                day.isoformat(),
                {
                    "file": target.name,
                    "bytes": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "reason": reason,
                },
            )

    def _checkpoint_past_days(self) -> int:
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
        count = sum(1 for line in self._retry.run(path.read_bytes).splitlines() if line.strip())
        body = {"date": day.isoformat(), "events": count, "last_hash": last_hash, "last_seq": seq}
        sig = base64.b64encode(self._signer.sign(canonical_json(body))).decode()
        self._append_line(path, canonical_json({"checkpoint": body, "sig": sig}) + b"\n")

    def _append_line(self, path: Path, line: bytes) -> None:
        def attempt() -> None:
            self._dir.mkdir(parents=True, exist_ok=True)
            start = path.stat().st_size if path.exists() else 0
            try:
                fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_CLOEXEC, 0o644)
                try:
                    view = memoryview(line)
                    while view:
                        view = view[os.write(fd, view) :]
                    os.fsync(fd)
                finally:
                    os.close(fd)
                if _read_back(path, start, len(line) + 1) != line:
                    raise ReadbackError(f"{path}: appended line did not read back")
            except BaseException as exc:
                self._undo_append(path, start, exc)
                raise
            if start == 0:
                fsync_dir(self._dir)

        self._retry.run(attempt)
        self._tail_size = path.stat().st_size

    def _undo_append(self, path: Path, start: int, cause: BaseException) -> None:
        """Truncate a failed append away and check it worked; if not, mark the ledger dirty."""
        try:
            os.truncate(path, start)
            size = path.stat().st_size
        except OSError as exc:
            self._dirty = path
            raise LedgerDirtyError(
                f"{path}: append failed ({cause}) and truncating it failed too ({exc}); appends are held "
                "until recover() cuts the file at its last valid line"
            ) from exc
        if size != start:
            self._dirty = path
            raise LedgerDirtyError(
                f"{path}: append failed ({cause}); truncation left {size} bytes, not {start}"
            )

    def recover(self, reason: str = "owner-requested recovery") -> LedgerEvent | None:
        """Cut the newest day file back to its last valid hash-chained line (see the module docstring).

        Returns the ``ledger.recovered`` event, or ``None`` if there was nothing to cut.
        """
        with self._writer():
            files = self.day_files()
            if not files:
                return None
            return self._recover(files[-1][1], reason)

    def _recover(self, path: Path, reason: str) -> LedgerEvent | None:
        data = path.read_bytes()
        keep = _valid_prefix(data)
        if keep == len(data):
            self._dirty = None
            return None
        cut = data[keep:]
        torn = path.with_name(path.name + ".torn")
        evidence = cut if cut.endswith(b"\n") else cut + b"\n"
        kept = torn.read_bytes() if torn.exists() else b""
        if not kept.endswith(evidence):  # an earlier attempt that failed to truncate already saved it
            with torn.open("ab") as f:
                f.write(evidence)
                f.flush()
                os.fsync(f.fileno())
        os.truncate(path, keep)
        if path.stat().st_size != keep:
            raise LedgerDirtyError(f"{path}: recovery could not truncate to {keep} bytes")
        self._dirty, self._tail, self._tail_size = None, None, -1
        if keep == 0:
            path.unlink()  # nothing valid was ever written to this day: the chain continues from before
        return self._append(
            "ledger.recovered",
            path.name,
            {
                "reason": reason,
                "cut_bytes": len(cut),
                "cut_sha256": hashlib.sha256(cut).hexdigest(),
                "kept_bytes": keep,
                "set_aside_to": torn.name,
            },
        )

    def _load_tail(self) -> tuple[int, str, date | None, bool]:
        if self._tail is not None:
            return self._tail
        files = self.day_files()
        if not files:
            self._tail = (0, GENESIS_HASH, None, False)
            return self._tail
        day, path = files[-1]
        data = self._retry.run(path.read_bytes)
        if data and not data.endswith(b"\n"):  # a crash mid-append: cut and record it
            self._recover(path, "torn last line (crash during an append)")
            return self._load_tail()
        seq, prev, closed = 0, GENESIS_HASH, False
        for lineno, raw in enumerate(data.splitlines(), start=1):
            if not raw.strip():
                continue
            try:
                obj = cast(dict[str, Any], json.loads(raw))
                if "checkpoint" in obj:
                    closed = True
                else:
                    seq, prev = int(obj["seq"]), str(obj["hash"])
            except (ValueError, KeyError, TypeError) as exc:
                raise LedgerError(
                    f"{path.name}:{lineno} is not a ledger line; run recover() to cut a damaged tail, or "
                    "quarantine_day() to set the day aside"
                ) from exc
        if seq == 0:
            raise LedgerError(f"{path}: day file has no events")
        self._tail = (seq, prev, day, closed)
        self._tail_size = len(data)
        return self._tail

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
            out.extendleft(reversed(_day_events(path)))  # a trailing partial line is ignored
            if len(out) >= n:
                break
        return list(out)[-n:]

    def verify(self, *, recent_days: int | None = None) -> bool:
        return self.verify_detail(recent_days=recent_days) is None

    def verify_detail(self, *, recent_days: int | None = None) -> str | None:
        """``None`` if the chain is intact, else a description of the first problem found.

        ``recent_days`` (fast mode) checks only the newest day files, anchored on the signed
        checkpoint that closes the day before them; the full replay remains the default.
        """
        seq, prev = 0, GENESIS_HASH
        files = self.day_files()
        start = 0
        if recent_days is not None and len(files) > recent_days:
            start = len(files) - max(recent_days, 1)
            anchor = self._anchor(files[start - 1])
            if isinstance(anchor, str):
                return anchor
            seq, prev = anchor
        for index, (day, path) in enumerate(files[start:], start=start):
            data = path.read_bytes()
            newest = index == len(files) - 1
            if newest and data and not data.endswith(b"\n"):
                data = data[
                    : data.rfind(b"\n") + 1
                ]  # an append in progress (or torn): not yet part of the chain
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

    def _anchor(self, day_file: tuple[date, Path]) -> tuple[int, str] | str:
        """``(last_seq, last_hash)`` from the signed checkpoint closing ``day_file``, or a problem."""
        day, path = day_file
        lines = path.read_bytes().rstrip(b"\n").split(b"\n")
        try:
            obj = cast(dict[str, Any], json.loads(lines[-1]))
            cp = cast(dict[str, Any], obj["checkpoint"])
            seq, last = int(cp["last_seq"]), str(cp["last_hash"])
            count = int(cp["events"])
        except (ValueError, KeyError, TypeError):
            return f"{path.name}: missing checkpoint"
        problem = self._check_checkpoint(obj, day, count, seq, last)
        return f"{path.name}: {problem}" if problem else (seq, last)

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
