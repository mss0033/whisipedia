"""Rolling scrub (PLAN §8.2): re-hash every sealed byte once per cycle, rate-limited and resumable.

Progress lives in a small SQLite file on **local** disk (never on the vault, ADR-0002). A run:

1. checks the signer is the vault's pinned key and asks the **mount canary** whether the share is
   answering; if not, the run stops at once and records nothing (vault-fetch audit #2);
2. takes the **inventory** of every sealed generation, incrementally (audit #11): a generation's
   manifest, parity sidecar and attestations are signature-checked again only when their
   ``(mtime, size)`` changed, when they failed last time, or when the last check is older than the
   cycle (so bit rot in a manifest is still found within one cycle); its directory tree is walked for
   unexpected files when the generation directory changed or the last walk is older than
   ``walk_cycle`` (default one day; the walk was 82 s for 60k files over SMB, every hour);
3. re-hashes the files that are due (never verified, failed last time, or older than ``cycle``),
   oldest first, until the byte/file budget is spent or the owner starts using the library. State is
   committed after every file, so a killed run resumes where it stopped.

**Errors are classified.** A file that cannot be *read* (an I/O error that survives the per-chunk
retries of :func:`wsp_vault.atomic.readback`) is reported ``UNREADABLE``, never ``MISMATCH``, and
the canary is asked again: if the share is gone the slice stops without recording the file. Only
bytes that were read and differ (``MISMATCH``) or a file that is not there (``MISSING``) go to the
repair ladder, which ignores ``UNREADABLE`` findings.

Every file's MD5 is computed in the same read pass and kept with its result, so parity creation can
bind par2's recovery data to bytes the scrub verified (:meth:`Scrubber.verified_md5`, audit #7).
"""

from __future__ import annotations

import os
import sqlite3
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import closing, suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from wsp_core.model import GenerationStatus, VerdictStatus
from wsp_core.ports import Ledger
from wsp_vault.atomic import DEFAULT_READ_RETRY, RetryPolicy, readback
from wsp_vault.attest import FORMATS as ATTESTATION_DOCS
from wsp_vault.attest import SIG_SUFFIX, read_attestation
from wsp_vault.canary import MountCanary, MountUnavailableError
from wsp_vault.cancel import Interrupted, never
from wsp_vault.manifest import DEGRADED_NAME, MANIFEST_NAME, SIGNATURE_NAME, ManifestEntry
from wsp_vault.paritymeta import SIDECAR_PATH, SIDECAR_SIG_PATH, is_parity_path, read_sidecar
from wsp_vault.ratelimit import RateLimiter
from wsp_vault.vault import FileVault, SignatureError

__all__ = [
    "Interrupted",
    "RateLimiter",
    "ScrubFinding",
    "ScrubProgress",
    "ScrubReport",
    "Scrubber",
    "scrub_progress",
]

DEFAULT_CYCLE = timedelta(days=30)
DEFAULT_WALK_CYCLE = timedelta(days=1)

_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS files (
        key TEXT PRIMARY KEY,
        verified_at TEXT NOT NULL,
        ok INTEGER NOT NULL,
        detail TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS generations (
        key TEXT PRIMARY KEY,
        stamp TEXT NOT NULL,
        verified_at TEXT NOT NULL,
        ok INTEGER NOT NULL,
        walked_at TEXT,
        dir_mtime INTEGER
    )
    """,
)
_FILE_COLUMNS = {"md5": "TEXT", "status": "TEXT"}  # added in place to older state files


@dataclass(frozen=True, slots=True)
class ScrubFinding:
    """A problem found by the scrubber. ``relpath`` is ``None`` for manifest/signature problems."""

    source_id: str
    generation_id: str
    relpath: str | None
    status: VerdictStatus
    detail: str

    @property
    def unreadable(self) -> bool:
        """Storage could not be read: no statement about the content (never repaired)."""
        return self.status is VerdictStatus.UNREADABLE


@dataclass(frozen=True, slots=True)
class ScrubReport:
    files_checked: int
    bytes_hashed: int
    findings: tuple[ScrubFinding, ...]
    complete: bool  # True when nothing due was left unchecked
    aborted: str | None = None  # why the run stopped early without results (mount unavailable)

    @property
    def ok(self) -> bool:
        return not self.findings and self.aborted is None


@dataclass(frozen=True, slots=True)
class GenerationCheck:
    """Result of :meth:`Scrubber.verify_generation`: a full, unscheduled check of one generation."""

    findings: tuple[ScrubFinding, ...]
    files: int
    bytes: int

    @property
    def ok(self) -> bool:
        return not self.findings


@dataclass(frozen=True, slots=True)
class _Item:
    key: str
    source_id: str
    generation_id: str
    path: Path
    entry: ManifestEntry


@dataclass(frozen=True, slots=True)
class _Inventory:
    stamp: str
    items: tuple[_Item, ...]
    known: frozenset[str]
    findings: tuple[ScrubFinding, ...]


class _Abort(Exception):  # noqa: N818 - control flow inside run()
    pass


def _iso(t: datetime) -> str:
    return t.isoformat()


class Scrubber:
    def __init__(
        self,
        vault: FileVault,
        state_path: Path,
        *,
        cycle: timedelta = DEFAULT_CYCLE,
        walk_cycle: timedelta = DEFAULT_WALK_CYCLE,
        bytes_per_sec: float | None = None,
        ledger: Ledger | None = None,
        should_pause: Callable[[], bool] | None = None,
        should_stop: Callable[[], bool] | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        retry: RetryPolicy = DEFAULT_READ_RETRY,
        canary: Callable[[], None] | None = None,
    ) -> None:
        """``retry`` applies per chunk of every read; ``canary`` (default :class:`MountCanary` on
        the vault root) raises when the share is not answering."""
        if state_path.resolve().is_relative_to(vault.root.resolve()):
            raise ValueError("scrub state must live on local disk, not inside the vault (ADR-0002)")
        self._vault = vault
        self._state_path = state_path
        self._cycle = cycle
        self._walk_cycle = walk_cycle
        self._ledger = ledger
        self._should_pause = should_pause or never
        self._should_stop = should_stop or never
        self._clock = clock
        self._limiter = RateLimiter(
            bytes_per_sec,
            monotonic=monotonic,
            sleep=sleep,
            should_stop=self._should_stop,
            should_pause=self._should_pause,
        )
        self._retry = retry
        self._canary = canary or MountCanary(vault.root)
        self._inventories: dict[str, _Inventory] = {}

    # --- state -------------------------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self._state_path)
        for statement in _SCHEMA:
            db.execute(statement)
        have = {str(row[1]) for row in db.execute("PRAGMA table_info(files)")}
        for column, kind in _FILE_COLUMNS.items():
            if column not in have:
                db.execute(f"ALTER TABLE files ADD COLUMN {column} {kind}")
        db.commit()
        return db

    def _fresh_rows(self, keys: Sequence[str], now: datetime | None) -> list[tuple[str, str | None]]:
        if not self._state_path.exists():
            return []
        now = now or self._clock()
        out: list[tuple[str, str | None]] = []
        with closing(self._connect()) as db:
            for i in range(0, len(keys), 500):
                chunk = list(keys[i : i + 500])
                marks = ",".join("?" * len(chunk))
                for key, at, good, md5 in db.execute(
                    f"SELECT key, verified_at, ok, md5 FROM files WHERE key IN ({marks})",  # noqa: S608 - placeholders
                    chunk,
                ):
                    if good and now - datetime.fromisoformat(at) < self._cycle:
                        out.append((str(key), None if md5 is None else str(md5)))
        return out

    def verified_ok(self, keys: Sequence[str], *, now: datetime | None = None) -> set[str]:
        """Keys (``<source>/<generation>/<path>``) that passed a check within the current cycle."""
        return {key for key, _ in self._fresh_rows(keys, now)}

    def verified_md5(self, keys: Sequence[str], *, now: datetime | None = None) -> dict[str, str]:
        """``key -> md5`` of the bytes that passed a check within the cycle (rows from before MD5s
        were recorded are left out: their bytes must be read again before parity may encode them)."""
        return {key: md5 for key, md5 in self._fresh_rows(keys, now) if md5}

    # --- running -----------------------------------------------------------------------------------

    def run(self, *, max_bytes: int | None = None, max_files: int | None = None) -> ScrubReport:
        self._limiter.reset()
        try:
            self._canary()
            self._vault.check_signer()  # KeyPinError propagates: a wrong key must stop the Warden loudly
        except MountUnavailableError as exc:
            return self._aborted(str(exc))
        except OSError as exc:
            return self._aborted(f"cannot read the vault's keys: {exc}")
        now = self._clock()
        findings: list[ScrubFinding] = []
        items: list[_Item] = []
        checked = hashed = 0
        complete = True
        with closing(self._connect()) as db:
            try:
                inventory_failed = self._inventory_all(db, now, items, findings)
            except _Abort as exc:
                return self._aborted(str(exc))
            state = {
                key: (datetime.fromisoformat(at), bool(ok))
                for key, at, ok in db.execute("SELECT key, verified_at, ok FROM files")
            }
            if not inventory_failed:  # never forget history because a generation could not be read
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
                    finding, nbytes, md5 = self._check(
                        item.source_id, item.generation_id, item.path, item.entry
                    )
                except Interrupted:  # shutdown or busy: the file stays due, nothing is recorded for it
                    complete = False
                    break
                if finding is not None and finding.unreadable:
                    try:
                        self._canary()
                    except MountUnavailableError as exc:  # the share went away: not this file's fault
                        report = ScrubReport(checked, hashed, tuple(findings), False, f"mid-run: {exc}")
                        self._log_report(report)
                        return report
                checked += 1
                hashed += nbytes
                db.execute(
                    "INSERT OR REPLACE INTO files (key, verified_at, ok, detail, md5, status) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        item.key,
                        _iso(self._clock()),
                        finding is None,
                        finding.detail if finding else "",
                        md5,
                        str(finding.status) if finding else str(VerdictStatus.OK),
                    ),
                )
                db.commit()
                if finding is not None:
                    findings.append(finding)
        report = ScrubReport(checked, hashed, tuple(findings), complete)
        self._log_report(report)
        return report

    def _aborted(self, why: str) -> ScrubReport:
        report = ScrubReport(0, 0, (), False, why)
        if self._ledger is not None:
            with suppress(OSError):  # the ledger lives on the same share
                self._ledger.append("scrub.aborted", "vault", {"reason": why})
        return report

    def verify_generation(self, source_id: str, generation_id: str) -> GenerationCheck:
        """Check one generation fully, now, without touching the schedule: fresh signature checks of
        the manifest, parity sidecar and attestations, a walk for unexpected files, every byte."""
        inv = self._inventory_one(source_id, generation_id, verify=True, walk=True)
        findings = list(inv.findings)
        files = nbytes = 0
        for item in inv.items:
            finding, n, _ = self._check(item.source_id, item.generation_id, item.path, item.entry)
            files += 1
            nbytes += n
            if finding is not None:
                findings.append(finding)
        return GenerationCheck(tuple(findings), files, nbytes)

    def scrub_generation(self, source_id: str, generation_id: str) -> list[ScrubFinding]:
        """Check one generation fully, now, without touching the schedule (see :meth:`verify_generation`)."""
        return list(self.verify_generation(source_id, generation_id).findings)

    # --- inventory -----------------------------------------------------------------------------------

    def _inventory_all(
        self, db: sqlite3.Connection, now: datetime, items: list[_Item], findings: list[ScrubFinding]
    ) -> bool:
        """Fill ``items`` and ``findings``; True if some generation could not be read."""
        failed = False
        try:
            sources = self._vault.sources()
        except OSError as exc:
            raise _Abort(f"cannot list the vault: {exc}") from exc
        for source_id in sources:
            try:
                gens = [
                    g
                    for g in self._vault.generations(source_id)
                    if g.status in (GenerationStatus.SEALED, GenerationStatus.DEGRADED)
                ]
            except OSError as exc:
                failed = True
                findings.append(ScrubFinding(source_id, "*", None, VerdictStatus.UNREADABLE, str(exc)))
                self._recheck_canary()
                continue
            for gen in gens:
                inv = self._scheduled_inventory(db, now, source_id, gen.generation_id)
                failed = failed or any(f.unreadable and f.relpath is None for f in inv.findings)
                items.extend(inv.items)
                findings.extend(inv.findings)
        return failed

    def _recheck_canary(self) -> None:
        try:
            self._canary()
        except MountUnavailableError as exc:
            raise _Abort(f"mid-inventory: {exc}") from exc

    def _scheduled_inventory(
        self, db: sqlite3.Connection, now: datetime, source_id: str, generation_id: str
    ) -> _Inventory:
        key = f"{source_id}/{generation_id}"
        lib = self._vault.library_path(source_id, generation_id)
        try:
            stamp, dir_mtime = self._stamp(lib)
        except OSError as exc:
            self._recheck_canary()
            return _Inventory("", (), frozenset(), (self._unreadable(source_id, generation_id, exc),))
        row = db.execute(
            "SELECT stamp, verified_at, ok, walked_at, dir_mtime FROM generations WHERE key = ?", (key,)
        ).fetchone()
        cached = self._inventories.get(key)
        verify = (
            row is None
            or row[0] != stamp
            or not row[2]
            or now - datetime.fromisoformat(str(row[1])) >= self._cycle
            or cached is None
            or cached.stamp != stamp
        )
        walk = (
            row is None
            or row[3] is None
            or row[4] != dir_mtime
            or now - datetime.fromisoformat(str(row[3])) >= self._walk_cycle
        )
        if verify:
            inv = self._inventory_one(source_id, generation_id, verify=True, walk=walk, stamp=stamp)
        else:
            assert cached is not None
            inv = cached if not walk else self._with_walk(source_id, generation_id, cached)
        unreadable = any(f.unreadable for f in inv.findings)
        if unreadable:
            self._recheck_canary()
            return inv  # nothing recorded: the next run tries again
        structural_ok = not any(f.relpath is None or f.detail != "unexpected file" for f in inv.findings)
        clean_walk = not any(f.detail == "unexpected file" for f in inv.findings)
        verified_at = _iso(now) if verify else str(row[1]) if row is not None else _iso(now)
        walked_at = _iso(now) if walk and clean_walk else (row[3] if row is not None else None)
        db.execute(
            "INSERT OR REPLACE INTO generations VALUES (?, ?, ?, ?, ?, ?)",
            (key, stamp, verified_at, structural_ok, walked_at, dir_mtime if clean_walk else None),
        )
        db.commit()
        return inv

    @staticmethod
    def _stamp(lib: Path) -> tuple[str, int]:
        """``(mtime, size)`` of every signed document of a generation, and the directory's mtime."""
        parts: list[str] = []
        names = [
            MANIFEST_NAME,
            SIGNATURE_NAME,
            SIDECAR_PATH,
            SIDECAR_SIG_PATH,
            *(n for d in ATTESTATION_DOCS for n in (d, d + SIG_SUFFIX)),
        ]
        for name in names:
            try:
                st = os.stat(lib / name)
            except FileNotFoundError:
                parts.append("-")
            else:
                parts.append(f"{st.st_mtime_ns}:{st.st_size}")
        return "|".join(parts), os.stat(lib).st_mtime_ns

    def _unreadable(self, source_id: str, generation_id: str, exc: OSError) -> ScrubFinding:
        return ScrubFinding(source_id, generation_id, None, VerdictStatus.UNREADABLE, f"unreadable: {exc}")

    def _inventory_one(
        self, source_id: str, generation_id: str, *, verify: bool, walk: bool, stamp: str = ""
    ) -> _Inventory:
        key = f"{source_id}/{generation_id}"
        lib = self._vault.library_path(source_id, generation_id)
        try:
            manifest = self._vault.manifest(source_id, generation_id, fresh=verify)
        except SignatureError as exc:
            inv = _Inventory(
                stamp,
                (),
                frozenset(),
                (ScrubFinding(source_id, generation_id, None, VerdictStatus.MISMATCH, str(exc)),),
            )
            self._inventories.pop(key, None)
            return inv
        except OSError as exc:
            return _Inventory(stamp, (), frozenset(), (self._unreadable(source_id, generation_id, exc),))
        known = {e.path for e in manifest.entries} | {MANIFEST_NAME, SIGNATURE_NAME, DEGRADED_NAME}
        findings: list[ScrubFinding] = []
        entries = list(manifest.entries)
        try:
            try:
                sidecar = read_sidecar(self._vault, source_id, generation_id)
            except SignatureError as exc:  # parity is derived: report once, do not list each file
                findings.append(
                    ScrubFinding(source_id, generation_id, SIDECAR_PATH, VerdictStatus.MISMATCH, str(exc))
                )
                sidecar = None
                known |= {"parity"}  # every parity file is covered by the one finding
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
        except OSError as exc:
            return _Inventory(stamp, (), frozenset(), (self._unreadable(source_id, generation_id, exc),))
        items = tuple(
            _Item(f"{source_id}/{generation_id}/{e.path}", source_id, generation_id, lib / e.path, e)
            for e in entries
        )
        inv = _Inventory(stamp, items, frozenset(known), tuple(findings))
        if not any(f.unreadable for f in findings):
            self._inventories[key] = inv
        return self._with_walk(source_id, generation_id, inv) if walk else inv

    def _with_walk(self, source_id: str, generation_id: str, inv: _Inventory) -> _Inventory:
        lib = self._vault.library_path(source_id, generation_id)
        try:
            on_disk = sorted(_walk(lib))
        except OSError as exc:
            return _Inventory(
                inv.stamp,
                inv.items,
                inv.known,
                (*inv.findings, self._unreadable(source_id, generation_id, exc)),
            )
        known = inv.known
        parity_covered = "parity" in known
        stray = [
            ScrubFinding(source_id, generation_id, rel, VerdictStatus.MISMATCH, "unexpected file")
            for rel in on_disk
            if rel not in known and not (parity_covered and is_parity_path(rel))
        ]
        base = tuple(f for f in inv.findings if f.detail != "unexpected file")
        return _Inventory(inv.stamp, inv.items, inv.known, (*base, *stray))

    def _due(
        self, items: list[_Item], state: dict[str, tuple[datetime, bool]], now: datetime
    ) -> Iterator[_Item]:
        never_checked = datetime.min.replace(tzinfo=UTC)

        def last(i: _Item) -> tuple[datetime, bool]:
            return state.get(i.key, (never_checked, False))

        due = [i for i in items if not last(i)[1] or now - last(i)[0] >= self._cycle]
        yield from sorted(due, key=lambda i: (last(i)[1], last(i)[0], i.key))

    # --- one file --------------------------------------------------------------------------------------

    def check(
        self, source_id: str, generation_id: str, path: Path, entry: ManifestEntry
    ) -> tuple[ScrubFinding | None, int]:
        """Verify one file against its manifest entry. Returns the finding (if any) and bytes hashed."""
        finding, nbytes, _ = self._check(source_id, generation_id, path, entry)
        return finding, nbytes

    def _check(
        self, source_id: str, generation_id: str, path: Path, entry: ManifestEntry
    ) -> tuple[ScrubFinding | None, int, str | None]:
        def bad(status: VerdictStatus, detail: str) -> ScrubFinding:
            return ScrubFinding(source_id, generation_id, entry.path, status, detail)

        try:
            size = path.stat().st_size
        except FileNotFoundError:
            return bad(VerdictStatus.MISSING, "file missing"), 0, None
        except OSError as exc:
            return bad(VerdictStatus.UNREADABLE, f"unreadable: {exc}"), 0, None
        if size != entry.size:
            return bad(VerdictStatus.MISMATCH, f"size {size} != manifest {entry.size}"), 0, None
        try:
            got = readback(path, extra=("md5",), on_chunk=self._limiter.consume, retry=self._retry)
        except FileNotFoundError:
            return bad(VerdictStatus.MISSING, "file missing"), 0, None
        except OSError as exc:
            return bad(VerdictStatus.UNREADABLE, f"unreadable: {exc}"), 0, None
        if got != entry.digests:
            return (
                bad(VerdictStatus.MISMATCH, f"sha256 {got.sha256} != manifest {entry.sha256}"),
                got.size,
                None,
            )
        return None, got.size, got.extra.get("md5")

    def _log_report(self, report: ScrubReport) -> None:
        if self._ledger is None:
            return
        for f in report.findings:
            subject = f"{f.source_id}/{f.generation_id}"
            self._ledger.append(
                "scrub.finding", subject, {"path": f.relpath, "status": str(f.status), "detail": f.detail}
            )
        data: dict[str, object] = {
            "files": report.files_checked,
            "bytes": report.bytes_hashed,
            "findings": len(report.findings),
            "complete": report.complete,
        }
        if report.aborted is not None:
            data["aborted"] = report.aborted
        self._ledger.append("scrub.run", "vault", data)


def _walk(root: Path) -> Iterator[str]:
    for path in root.rglob("*"):
        if path.is_file() or path.is_symlink():
            yield path.relative_to(root).as_posix()


@dataclass(frozen=True, slots=True)
class ScrubProgress:
    """Where the rolling scrub stands, from its local state file (no vault access)."""

    files_known: int  # files seen by the last run
    verified_in_cycle: int  # passed a check within the cycle
    failing: int  # failed their last check (damaged, missing or unreadable)
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
