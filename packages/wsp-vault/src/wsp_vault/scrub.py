"""Rolling scrub (PLAN §8.2): re-hash every sealed byte once per cycle, rate-limited and resumable.

Progress lives in a small SQLite file on **local** disk (never on the vault, ADR-0002). Each run
first checks every sealed generation's manifest signature and looks for unexpected files, then
re-hashes the files that are due (never verified, failed last time, or older than ``cycle``),
oldest first, until the byte/file budget is spent or ``should_pause()`` says the machine is busy.
State is committed after every file, so a killed run resumes where it stopped.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from wsp_core.model import GenerationStatus, VerdictStatus
from wsp_core.ports import Ledger
from wsp_vault.atomic import DEFAULT_RETRY, RetryPolicy, readback
from wsp_vault.attest import FORMATS as ATTESTATION_DOCS
from wsp_vault.attest import SIG_SUFFIX, read_attestation
from wsp_vault.cancel import Interrupted, never
from wsp_vault.manifest import DEGRADED_NAME, MANIFEST_NAME, SIGNATURE_NAME, ManifestEntry
from wsp_vault.paritymeta import SIDECAR_PATH, is_parity_path, read_sidecar
from wsp_vault.vault import FileVault, SignatureError

__all__ = ["Interrupted", "RateLimiter", "ScrubFinding", "ScrubProgress", "ScrubReport", "Scrubber"]

DEFAULT_CYCLE = timedelta(days=30)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    key TEXT PRIMARY KEY,
    verified_at TEXT NOT NULL,
    ok INTEGER NOT NULL,
    detail TEXT NOT NULL
)
"""


@dataclass(frozen=True, slots=True)
class ScrubFinding:
    """A problem found by the scrubber. ``relpath`` is ``None`` for manifest/signature problems."""

    source_id: str
    generation_id: str
    relpath: str | None
    status: VerdictStatus
    detail: str


@dataclass(frozen=True, slots=True)
class ScrubReport:
    files_checked: int
    bytes_hashed: int
    findings: tuple[ScrubFinding, ...]
    complete: bool  # True when nothing due was left unchecked

    @property
    def ok(self) -> bool:
        return not self.findings


class RateLimiter:
    """Paces byte consumption to ``bytes_per_sec`` by sleeping. ``None`` disables limiting.

    With ``should_stop``, every call checks it and sleeps are cut into short slices, so a paced read
    of a huge file can be abandoned within a fraction of a second (:class:`Interrupted`).
    """

    def __init__(
        self,
        bytes_per_sec: float | None,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        should_stop: Callable[[], bool] = never,
    ) -> None:
        if bytes_per_sec is not None and bytes_per_sec <= 0:
            raise ValueError("bytes_per_sec must be positive")
        self._rate = bytes_per_sec
        self._monotonic = monotonic
        self._sleep = sleep
        self._start: float | None = None
        self._consumed = 0
        self.should_stop = should_stop

    def consume(self, n: int) -> None:
        if self.should_stop():
            raise Interrupted("stop requested")
        if self._rate is None:
            return
        if self._start is None:
            self._start = self._monotonic()
        self._consumed += n
        ahead = self._consumed / self._rate - (self._monotonic() - self._start)
        slice_ = ahead if self.should_stop is never else 0.25  # interruptible only when it matters
        while ahead > 0:
            step = min(ahead, slice_)
            self._sleep(step)
            ahead -= step
            if self.should_stop():
                raise Interrupted("stop requested")


@dataclass(frozen=True, slots=True)
class _Item:
    key: str
    source_id: str
    generation_id: str
    path: Path
    entry: ManifestEntry


class Scrubber:
    def __init__(
        self,
        vault: FileVault,
        state_path: Path,
        *,
        cycle: timedelta = DEFAULT_CYCLE,
        bytes_per_sec: float | None = None,
        ledger: Ledger | None = None,
        should_pause: Callable[[], bool] | None = None,
        should_stop: Callable[[], bool] | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        retry: RetryPolicy = DEFAULT_RETRY,
    ) -> None:
        if state_path.resolve().is_relative_to(vault.root.resolve()):
            raise ValueError("scrub state must live on local disk, not inside the vault (ADR-0002)")
        self._vault = vault
        self._state_path = state_path
        self._cycle = cycle
        self._ledger = ledger
        self._should_pause = should_pause or never
        self._should_stop = should_stop or never
        self._clock = clock
        self._limiter = RateLimiter(
            bytes_per_sec, monotonic=monotonic, sleep=sleep, should_stop=self._should_stop
        )
        self._retry = retry

    def _connect(self) -> sqlite3.Connection:
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self._state_path)
        db.execute(_SCHEMA)
        return db

    def verified_ok(self, keys: Sequence[str], *, now: datetime | None = None) -> set[str]:
        """Keys (``<source>/<generation>/<path>``) that passed a check within the current cycle."""
        if not self._state_path.exists():
            return set()
        now = now or self._clock()
        ok: set[str] = set()
        with closing(self._connect()) as db:
            for i in range(0, len(keys), 500):
                chunk = list(keys[i : i + 500])
                marks = ",".join("?" * len(chunk))
                for key, at, good in db.execute(
                    f"SELECT key, verified_at, ok FROM files WHERE key IN ({marks})",  # noqa: S608 - placeholders
                    chunk,
                ):
                    if good and now - datetime.fromisoformat(at) < self._cycle:
                        ok.add(str(key))
        return ok

    def run(self, *, max_bytes: int | None = None, max_files: int | None = None) -> ScrubReport:
        now = self._clock()
        findings: list[ScrubFinding] = []
        items: list[_Item] = []
        for source_id in self._vault.sources():
            for gen in self._vault.generations(source_id):
                if gen.status in (GenerationStatus.SEALED, GenerationStatus.DEGRADED):
                    findings.extend(self._inventory(source_id, gen.generation_id, items))
        checked = hashed = 0
        complete = True
        with closing(self._connect()) as db:
            state = {
                key: (datetime.fromisoformat(at), bool(ok))
                for key, at, ok in db.execute("SELECT key, verified_at, ok FROM files")
            }
            stale = set(state) - {i.key for i in items}
            db.executemany("DELETE FROM files WHERE key = ?", [(k,) for k in stale])
            db.commit()
            for item in self._due(items, state, now):
                over_budget = (max_files is not None and checked >= max_files) or (
                    max_bytes is not None and hashed >= max_bytes
                )
                if over_budget or self._should_stop() or self._should_pause():
                    complete = False
                    break
                try:
                    finding, nbytes = self.check(item.source_id, item.generation_id, item.path, item.entry)
                except Interrupted:  # shutdown: the file stays due, nothing is recorded for it
                    complete = False
                    break
                checked += 1
                hashed += nbytes
                db.execute(
                    "INSERT OR REPLACE INTO files VALUES (?, ?, ?, ?)",
                    (item.key, self._clock().isoformat(), finding is None, finding.detail if finding else ""),
                )
                db.commit()
                if finding is not None:
                    findings.append(finding)
        report = ScrubReport(checked, hashed, tuple(findings), complete)
        self._log_report(report)
        return report

    def scrub_generation(self, source_id: str, generation_id: str) -> list[ScrubFinding]:
        """Check one generation fully, now, without touching the schedule."""
        items: list[_Item] = []
        findings = self._inventory(source_id, generation_id, items)
        for item in items:
            finding, _ = self.check(item.source_id, item.generation_id, item.path, item.entry)
            if finding is not None:
                findings.append(finding)
        return findings

    def _inventory(self, source_id: str, generation_id: str, items: list[_Item]) -> list[ScrubFinding]:
        lib = self._vault.library_path(source_id, generation_id)
        try:
            manifest = self._vault.manifest(source_id, generation_id)
        except SignatureError as exc:
            return [ScrubFinding(source_id, generation_id, None, VerdictStatus.MISMATCH, str(exc))]
        known = {e.path for e in manifest.entries} | {MANIFEST_NAME, SIGNATURE_NAME, DEGRADED_NAME}
        findings: list[ScrubFinding] = []
        entries = list(manifest.entries)
        on_disk = sorted(_walk(lib))
        try:
            sidecar = read_sidecar(self._vault, source_id, generation_id)
        except SignatureError as exc:  # parity is derived: report once, do not list each file
            findings.append(
                ScrubFinding(source_id, generation_id, SIDECAR_PATH, VerdictStatus.MISMATCH, str(exc))
            )
            known |= {rel for rel in on_disk if is_parity_path(rel)}
        else:
            if sidecar is not None:
                known |= sidecar.paths
                entries += sidecar.entries
        for name in ATTESTATION_DOCS:
            if (lib / name).exists() or (lib / (name + SIG_SUFFIX)).exists():
                known |= {name, name + SIG_SUFFIX}
                try:
                    read_attestation(self._vault, source_id, generation_id, name)
                except SignatureError as exc:  # reported, never re-signed (ADR-0009)
                    findings.append(
                        ScrubFinding(source_id, generation_id, name, VerdictStatus.MISMATCH, str(exc))
                    )
        findings += [
            ScrubFinding(source_id, generation_id, rel, VerdictStatus.MISMATCH, "unexpected file")
            for rel in on_disk
            if rel not in known
        ]
        for e in entries:
            key = f"{source_id}/{generation_id}/{e.path}"
            items.append(_Item(key, source_id, generation_id, lib / e.path, e))
        return findings

    def _due(
        self, items: list[_Item], state: dict[str, tuple[datetime, bool]], now: datetime
    ) -> Iterator[_Item]:
        never = datetime.min.replace(tzinfo=UTC)

        def last(i: _Item) -> tuple[datetime, bool]:
            return state.get(i.key, (never, False))

        due = [i for i in items if not last(i)[1] or now - last(i)[0] >= self._cycle]
        yield from sorted(due, key=lambda i: (last(i)[1], last(i)[0], i.key))

    def check(
        self, source_id: str, generation_id: str, path: Path, entry: ManifestEntry
    ) -> tuple[ScrubFinding | None, int]:
        """Verify one file against its manifest entry. Returns the finding (if any) and bytes hashed."""

        def bad(status: VerdictStatus, detail: str) -> ScrubFinding:
            return ScrubFinding(source_id, generation_id, entry.path, status, detail)

        try:
            size = path.stat().st_size
        except FileNotFoundError:
            return bad(VerdictStatus.MISSING, "file missing"), 0
        if size != entry.size:
            return bad(VerdictStatus.MISMATCH, f"size {size} != manifest {entry.size}"), 0
        try:
            got = self._retry.run(lambda: readback(path, on_chunk=self._limiter.consume))
        except OSError as exc:
            return bad(VerdictStatus.MISMATCH, f"unreadable: {exc}"), 0
        if got != entry.digests:
            return bad(VerdictStatus.MISMATCH, f"sha256 {got.sha256} != manifest {entry.sha256}"), got.size
        return None, got.size

    def _log_report(self, report: ScrubReport) -> None:
        if self._ledger is None:
            return
        for f in report.findings:
            subject = f"{f.source_id}/{f.generation_id}"
            self._ledger.append(
                "scrub.finding", subject, {"path": f.relpath, "status": str(f.status), "detail": f.detail}
            )
        self._ledger.append(
            "scrub.run",
            "vault",
            {
                "files": report.files_checked,
                "bytes": report.bytes_hashed,
                "findings": len(report.findings),
                "complete": report.complete,
            },
        )


def _walk(root: Path) -> Iterator[str]:
    for path in root.rglob("*"):
        if path.is_file() or path.is_symlink():
            yield path.relative_to(root).as_posix()


@dataclass(frozen=True, slots=True)
class ScrubProgress:
    """Where the rolling scrub stands, from its local state file (no vault access)."""

    files_known: int  # files seen by the last run
    verified_in_cycle: int  # passed a check within the cycle
    failing: int  # failed their last check
    oldest_check: datetime | None

    @property
    def fraction(self) -> float:
        return self.verified_in_cycle / self.files_known if self.files_known else 0.0


def scrub_progress(
    state_path: Path, *, now: datetime, cycle: timedelta = DEFAULT_CYCLE
) -> ScrubProgress | None:
    """Read-only summary of ``scrub.sqlite``; ``None`` if the scrubber never ran."""
    if not state_path.is_file():
        return None
    db = sqlite3.connect(f"file:{state_path}?mode=ro", uri=True)
    try:
        rows = db.execute("SELECT verified_at, ok FROM files").fetchall()
    except sqlite3.Error:
        return None
    finally:
        db.close()
    stamps = [datetime.fromisoformat(str(at)) for at, _ in rows]
    fresh = sum(1 for (_, ok), at in zip(rows, stamps, strict=True) if ok and now - at < cycle)
    failing = sum(1 for _, ok in rows if not ok)
    return ScrubProgress(len(rows), fresh, failing, min(stamps, default=None))
