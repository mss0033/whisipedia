"""``ParityKeeper``: par2 recovery data for sealed generations, created after sealing (PLAN §8.1).

The owner has no snapshots and no second copy, so parity is the main defence against bit rot. It is
created for newly sealed generations and backfilled for older ones, in bounded, resumable steps:

1. The generation's data files are grouped into recovery sets (``max_set_files`` files or
   ``max_set_bytes`` bytes; a bigger file is a set on its own).
2. For each set, the data files are verified against the signed manifest first (skipped for files
   the rolling scrub verified within its cycle), so parity never encodes damaged bytes.
3. ``par2 create`` writes the set to **local scratch** (low CPU/IO priority); each file is then copied
   to ``<vault>/parity-staging/<domain>/<source>/<generation>/<set>/`` with a verified atomic copy,
   and progress is recorded on local disk. A crash loses at most the set in flight.
4. When every set is done, the signed ``PARITY.json`` sidecar (:mod:`wsp_vault.paritymeta`) is
   written into the staging directory, which is then renamed to ``<generation>/parity/`` in one step.

Throughput is limited to ``bytes_per_sec`` (verification reads and par2 input together) and each
call does at most ``max_bytes`` of work. Damaged parity is never repaired in place: it is moved to
``quarantine/`` (:meth:`ParityKeeper.invalidate`) and created again from verified data.
"""

from __future__ import annotations

import json
import os
import shutil
import time
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, cast

from wsp_core.ports import Ledger, ParityProvider
from wsp_vault.atomic import DEFAULT_RETRY, RetryPolicy, atomic_copy, atomic_write_bytes, fsync_dir, readback
from wsp_vault.manifest import PARITY_DIR, ROLE_PARITY, Manifest, ManifestEntry
from wsp_vault.paritymeta import (
    SIDECAR_NAME,
    SIDECAR_SIG,
    ParitySet,
    ParitySidecar,
    manifest_blake3,
    read_sidecar,
)
from wsp_vault.scrub import RateLimiter
from wsp_vault.vault import FileVault, SignatureError, VaultError, make_read_only

PARITY_STAGING_DIR = "parity-staging"
GIB = 1024**3


class ParityState(StrEnum):
    NONE = "none"  # no parity yet
    PARTIAL = "partial"  # creation in progress (sets staged)
    MANIFEST = "manifest"  # created while sealing, listed in MANIFEST.json
    SIDECAR = "sidecar"  # created after sealing, PARITY.json is valid
    INVALID = "invalid"  # parity/ exists but cannot be trusted (re-create after invalidate)


COVERED = frozenset({ParityState.MANIFEST, ParityState.SIDECAR})


class ParityDataError(VaultError):
    """A data file does not match the manifest; parity must not be created from it."""


@dataclass(frozen=True, slots=True)
class ParityReport:
    created: tuple[str, ...] = ()  # generations that now have parity
    in_progress: str | None = None  # generation left partially done (budget reached)
    sets_done: int = 0
    bytes_processed: int = 0
    pending: int = 0  # target generations still without parity
    skipped: tuple[str, ...] = ()  # "<source>/<generation>: reason"

    def to_json(self) -> dict[str, object]:
        return {
            "created": list(self.created),
            "in_progress": self.in_progress,
            "sets_done": self.sets_done,
            "bytes_processed": self.bytes_processed,
            "pending": self.pending,
            "skipped": list(self.skipped),
        }


@dataclass(frozen=True, slots=True)
class ParityCoverage:
    generations: int = 0
    covered: int = 0
    bytes_total: int = 0
    bytes_covered: int = 0
    states: dict[str, int] = field(default_factory=dict[str, int])
    uncovered: tuple[str, ...] = ()

    @property
    def fraction(self) -> float:
        return self.bytes_covered / self.bytes_total if self.bytes_total else 1.0


class ParityKeeper:
    def __init__(
        self,
        vault: FileVault,
        parity: ParityProvider,
        *,
        state_dir: Path,
        scratch: Path,
        percent: int = 10,
        max_set_bytes: int = 64 * GIB,
        max_set_files: int = 500,
        bytes_per_sec: float | None = None,
        ledger: Ledger | None = None,
        verified: Callable[[Sequence[str]], set[str]] | None = None,
        retry: RetryPolicy = DEFAULT_RETRY,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        for local in (state_dir, scratch):
            if local.resolve().is_relative_to(vault.root.resolve()):
                raise ValueError("parity state and scratch must be on local disk, not in the vault")
        if not 1 <= percent <= 100:
            raise ValueError("parity percent must be 1..100")
        self.vault = vault
        self.parity = parity
        self.state_dir = state_dir / "parity"
        self.scratch = scratch / "parity"
        self.percent = percent
        self.max_set_bytes = max_set_bytes
        self.max_set_files = max_set_files
        self.ledger = ledger if ledger is not None else vault.ledger
        self.verified = verified
        self.retry = retry
        self.clock = clock
        self.limiter = RateLimiter(bytes_per_sec, monotonic=monotonic, sleep=sleep)

    # --- state -----------------------------------------------------------------------------------

    def _progress_path(self, source_id: str, generation_id: str) -> Path:
        return self.state_dir / source_id / f"{generation_id}.json"

    def _stage_dir(self, source_id: str, generation_id: str) -> Path:
        domain = self.vault.domain_of(source_id)
        return self.vault.root / PARITY_STAGING_DIR / domain / source_id / generation_id

    def state(self, source_id: str, generation_id: str, manifest: Manifest | None = None) -> ParityState:
        lib = self.vault.library_path(source_id, generation_id)
        if (lib / PARITY_DIR).is_dir():
            try:
                if read_sidecar(self.vault, source_id, generation_id) is not None:
                    return ParityState.SIDECAR
            except SignatureError:
                return ParityState.INVALID
            manifest = manifest or self.vault.manifest(source_id, generation_id)
            return ParityState.MANIFEST if manifest.parity else ParityState.INVALID
        if self._progress_path(source_id, generation_id).is_file():
            return ParityState.PARTIAL
        return ParityState.NONE

    def coverage(self, targets: Iterable[tuple[str, str]]) -> ParityCoverage:
        states: Counter[str] = Counter()
        total = covered = bytes_total = bytes_covered = 0
        uncovered: list[str] = []
        for source_id, generation_id in targets:
            try:
                manifest = self.vault.manifest(source_id, generation_id)
                state = self.state(source_id, generation_id, manifest)
            except (SignatureError, OSError):
                state, manifest = ParityState.INVALID, None
            size = sum(e.size for e in manifest.data) if manifest else 0
            states[state.value] += 1
            total += 1
            bytes_total += size
            if state in COVERED:
                covered += 1
                bytes_covered += size
            else:
                uncovered.append(f"{source_id}/{generation_id}")
        return ParityCoverage(total, covered, bytes_total, bytes_covered, dict(states), tuple(uncovered))

    # --- planning --------------------------------------------------------------------------------

    def plan_sets(self, manifest: Manifest) -> list[list[str]]:
        sets: list[list[str]] = []
        current: list[str] = []
        size = 0
        for e in sorted(manifest.data, key=lambda e: e.path):
            if current and (len(current) >= self.max_set_files or size + e.size > self.max_set_bytes):
                sets.append(current)
                current, size = [], 0
            current.append(e.path)
            size += e.size
        if current:
            sets.append(current)
        return sets

    def _load_progress(
        self, source_id: str, generation_id: str, mhash: str, manifest: Manifest
    ) -> dict[str, Any]:
        path = self._progress_path(source_id, generation_id)
        if path.is_file():
            progress = cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))
            if progress.get("manifest_blake3") == mhash and progress.get("percent") == self.percent:
                return progress
        return {
            "manifest_blake3": mhash,
            "percent": self.percent,
            "sets": self.plan_sets(manifest),
            "done": {},
        }

    def _save_progress(self, source_id: str, generation_id: str, progress: dict[str, Any]) -> None:
        path = self._progress_path(source_id, generation_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".partial")
        tmp.write_text(json.dumps(progress, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)

    # --- creation --------------------------------------------------------------------------------

    def _preverify(self, source_id: str, generation_id: str, entries: Sequence[ManifestEntry]) -> None:
        lib = self.vault.library_path(source_id, generation_id)
        keys = [f"{source_id}/{generation_id}/{e.path}" for e in entries]
        fresh: set[str] = self.verified(keys) if self.verified is not None else set()
        for key, e in zip(keys, entries, strict=True):
            if key in fresh:
                continue
            path = lib / e.path
            try:
                got = self.retry.run(lambda p=path: readback(p, on_chunk=self.limiter.consume))
            except OSError as exc:
                raise ParityDataError(f"{source_id}/{generation_id} {e.path}: unreadable ({exc})") from exc
            if got != e.digests:
                raise ParityDataError(f"{source_id}/{generation_id} {e.path}: does not match the manifest")

    def create(
        self, source_id: str, generation_id: str, *, max_bytes: int | None = None
    ) -> tuple[bool, int, int]:
        """Create (or continue) parity for one generation. Returns ``(complete, bytes, sets done)``."""
        state = self.state(source_id, generation_id)
        if state in COVERED:
            return True, 0, 0
        if state is ParityState.INVALID:
            raise VaultError(
                f"{source_id}/{generation_id}: parity/ exists but is invalid; invalidate it first"
            )
        manifest = self.vault.manifest(source_id, generation_id)
        lib = self.vault.library_path(source_id, generation_id)
        mhash = manifest_blake3(self.vault, source_id, generation_id)
        progress = self._load_progress(source_id, generation_id, mhash, manifest)
        done = cast(dict[str, list[dict[str, Any]]], progress["done"])
        stage = self._stage_dir(source_id, generation_id)
        processed = sets_done = 0
        for i, files in enumerate(cast(list[list[str]], progress["sets"]), start=1):
            name = f"set-{i:05d}"
            if name in done:
                continue
            if max_bytes is not None and processed >= max_bytes:
                self._save_progress(source_id, generation_id, progress)
                return False, processed, sets_done
            entries = [e for e in (manifest.entry(p) for p in files) if e is not None]
            self._preverify(source_id, generation_id, entries)
            out = self._create_set(source_id, generation_id, name, [lib / e.path for e in entries], stage)
            done[name] = [e.to_json() for e in out]
            self._save_progress(source_id, generation_id, progress)
            set_bytes = sum(e.size for e in entries)
            self.limiter.consume(set_bytes)  # par2 read the whole set once
            processed += set_bytes
            sets_done += 1
        self._finish(source_id, generation_id, manifest, mhash, progress, stage)
        return True, processed, sets_done

    def _create_set(
        self, source_id: str, generation_id: str, name: str, files: Sequence[Path], stage: Path
    ) -> list[ManifestEntry]:
        tmp = self.scratch / f"{source_id}--{generation_id}--{name}"
        shutil.rmtree(tmp, ignore_errors=True)  # our own local scratch
        tmp.mkdir(parents=True)
        try:
            created = self.parity.create(files, tmp, self.percent)
            out: list[ManifestEntry] = []
            for path in created:
                dest = stage / name / path.name
                dest.parent.mkdir(parents=True, exist_ok=True)
                digests = atomic_copy(path, dest, overwrite=True, retry=self.retry)
                out.append(
                    ManifestEntry.from_digests(f"{PARITY_DIR}/{name}/{path.name}", digests, role=ROLE_PARITY)
                )
            return out
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def _finish(
        self,
        source_id: str,
        generation_id: str,
        manifest: Manifest,
        mhash: str,
        progress: dict[str, Any],
        stage: Path,
    ) -> None:
        done = cast(dict[str, list[dict[str, Any]]], progress["done"])
        sets = tuple(
            ParitySet(
                name=f"set-{i:05d}",
                files=tuple(files),
                parity=tuple(ManifestEntry.from_json(e) for e in done[f"set-{i:05d}"]),
            )
            for i, files in enumerate(cast(list[list[str]], progress["sets"]), start=1)
        )
        sidecar = ParitySidecar(source_id, generation_id, mhash, self.percent, self.clock(), sets)
        stage.mkdir(parents=True, exist_ok=True)
        listed = {e.path.removeprefix(f"{PARITY_DIR}/") for e in sidecar.entries}
        for path in sorted(stage.rglob("*")):  # strays from a crash between copy and progress record
            rel = path.relative_to(stage).as_posix()
            if path.is_file() and rel not in listed:
                path.unlink()
        data = sidecar.to_bytes()
        atomic_write_bytes(stage / SIDECAR_NAME, data, overwrite=True, retry=self.retry)
        atomic_write_bytes(
            stage / SIDECAR_SIG, self.vault.signer.sign(data), overwrite=True, retry=self.retry
        )
        lib = self.vault.library_path(source_id, generation_id)
        os.rename(stage, lib / PARITY_DIR)
        fsync_dir(lib)
        for path in (lib / PARITY_DIR).rglob("*"):
            if path.is_file():
                make_read_only(path)
        for parent in (stage.parent, stage.parent.parent, stage.parent.parent.parent):
            with suppress(OSError):
                parent.rmdir()
        self._progress_path(source_id, generation_id).unlink(missing_ok=True)
        self._log(
            "parity.created",
            f"{source_id}/{generation_id}",
            {
                "sets": len(sets),
                "percent": self.percent,
                "bytes": sidecar.total_bytes,
                "manifest_blake3": mhash,
            },
        )

    def run(
        self,
        targets: Sequence[tuple[str, str]],
        *,
        max_bytes: int | None = None,
        should_stop: Callable[[], bool] = lambda: False,
    ) -> ParityReport:
        """Work through ``targets`` (oldest first is a good order) until done or ``max_bytes`` is spent."""
        created: list[str] = []
        skipped: list[str] = []
        in_progress: str | None = None
        processed = sets = 0
        for source_id, generation_id in targets:
            subject = f"{source_id}/{generation_id}"
            if should_stop() or (max_bytes is not None and processed >= max_bytes):
                break
            if self.state(source_id, generation_id) in COVERED:
                continue
            budget = None if max_bytes is None else max_bytes - processed
            try:
                complete, nbytes, nsets = self.create(source_id, generation_id, max_bytes=budget)
            except (ParityDataError, VaultError, OSError) as exc:
                skipped.append(f"{subject}: {exc}")
                self._log("parity.skipped", subject, {"reason": str(exc)})
                continue
            except Exception as exc:  # par2 failure: record and move on to the next generation
                skipped.append(f"{subject}: {type(exc).__name__}: {exc}")
                self._log("parity.skipped", subject, {"reason": f"{type(exc).__name__}: {exc}"})
                continue
            processed += nbytes
            sets += nsets
            if complete:
                created.append(subject)
            else:
                in_progress = subject
                break
        pending = sum(1 for s, g in targets if self.state(s, g) not in COVERED)
        return ParityReport(tuple(created), in_progress, sets, processed, pending, tuple(skipped))

    def invalidate(self, source_id: str, generation_id: str, reason: str) -> Path | None:
        """Move a damaged or untrusted ``parity/`` aside (quarantine) so it is created again."""
        lib = self.vault.library_path(source_id, generation_id)
        if not (lib / PARITY_DIR).is_dir():
            return None
        if self.vault.manifest(source_id, generation_id).parity:
            return None  # parity listed in the signed manifest itself is never moved
        dest = self.vault.quarantine(lib / PARITY_DIR)
        self._log(
            "parity.invalidated",
            f"{source_id}/{generation_id}",
            {"reason": reason, "moved_to": str(dest.relative_to(self.vault.root))},
        )
        return dest

    def _log(self, kind: str, subject: str, data: dict[str, object]) -> None:
        if self.ledger is not None:
            self.ledger.append(kind, subject, data)
