"""Automatic repair ladder (PLAN §8.4).

For one generation, damaged files are tried against each rung in order, and every rung that runs
writes a ledger event:

1. **par2** — ``ParityProvider.repair`` in place; leftovers it creates are quarantined.
2. **mirror** — the same relative path under a second copy (``mirror_root`` has the vault layout).
3. **re-fetch** — an injected callable downloads the file again into local scratch space.
4. **degraded** — ``DEGRADED.txt`` + ledger event; if the generation is in the source's current set,
   its pointer is moved to the newest *older* generation of the same partition (by seal time) that
   passes a full verification first: signature, and every data file re-hashed now.

A replacement is accepted only if it matches the manifest (size, SHA-256, BLAKE3). A damaged
manifest or signature can only be restored from the mirror: re-signing would launder tampering.
Damaged files are quarantined, never deleted. Each generation is repaired under its exclusive vault
lock; the rollback takes the source's pointer lock afterwards (one lock order, no deadlock).

**What is never repaired** (vault-fetch audit #1, #2):

* ``UNREADABLE`` findings, and files that cannot be read while the ladder checks them: an I/O error
  says nothing about the content. The generation is left alone (``repair.deferred``).
* Anything, when too many generations fail at manifest/signature level in one batch: more than
  ``max_manifest_failures`` (2), or at least two and more than ``max_manifest_fraction`` (5 %) of
  the sealed generations. That pattern is a key, mount or software problem, not bit rot. The ladder
  logs ``repair.circuit_open`` and raises :class:`RepairCircuitOpenError` before touching anything.

A generation marked DEGRADED by mistake is cleared only by a full re-verification that passes:
:func:`wsp_vault.undegrade.reverify_and_clear`.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from wsp_core.model import Checksum, GenerationStatus, VerdictStatus
from wsp_core.partitions import partition_of
from wsp_core.ports import Ledger, ParityProvider
from wsp_vault.atomic import IntegrityError, atomic_copy, fsync_dir, publish_noreplace, readback, temp_path
from wsp_vault.manifest import (
    DEGRADED_NAME,
    MANIFEST_NAME,
    PARITY_DIR,
    SIGNATURE_NAME,
    Manifest,
    ManifestEntry,
)
from wsp_vault.paritymeta import is_parity_path, read_sidecar, sidecar_paths
from wsp_vault.scrub import ScrubFinding
from wsp_vault.vault import FileVault, SignatureError, VaultError, make_read_only

MANIFEST_LEVEL = frozenset({VerdictStatus.MISMATCH, VerdictStatus.MISSING})


@dataclass(frozen=True, slots=True)
class RefetchRequest:
    source_id: str
    generation_id: str
    relpath: str
    size: int
    sha256: str
    blake3: str
    upstream: tuple[Checksum, ...] = ()


Refetcher = Callable[[RefetchRequest, Path], Path | None]
"""Download the requested file into the given local scratch directory and return its path."""


class Rung(StrEnum):
    PAR2 = "par2"
    MIRROR = "mirror"
    REFETCH = "refetch"


class RepairCircuitOpenError(VaultError):
    """Too many generations failed at manifest level at once; nothing was repaired or degraded."""


class _Unreadable(Exception):  # noqa: N818 - control flow
    pass


@dataclass(frozen=True, slots=True)
class RepairResult:
    source_id: str
    generation_id: str
    repaired: Mapping[str, Rung] = field(default_factory=dict[str, Rung])
    unrepaired: tuple[str, ...] = ()
    degraded: bool = False
    rolled_back_to: str | None = None
    deferred: str | None = None  # the generation could not be read: nothing was decided

    @property
    def ok(self) -> bool:
        return not self.degraded and self.deferred is None


class RepairLadder:
    def __init__(
        self,
        vault: FileVault,
        ledger: Ledger,
        *,
        parity: ParityProvider | None = None,
        mirror_root: Path | None = None,
        refetch: Refetcher | None = None,
        scratch: Path | None = None,
        rollback_current: bool = True,
        max_manifest_failures: int = 2,
        max_manifest_fraction: float = 0.05,
    ) -> None:
        self._vault = vault
        self._ledger = ledger
        self._parity = parity
        self._mirror = mirror_root
        self._refetch = refetch
        self._scratch = scratch
        self._rollback = rollback_current
        self._max_failures = max_manifest_failures
        self._max_fraction = max_manifest_fraction

    def repair_findings(self, findings: Iterable[ScrubFinding]) -> list[RepairResult]:
        """Repair what a scrub found. ``UNREADABLE`` findings are skipped (logged); a generation that
        turns out unreadable while being repaired is skipped too and left out of the results.

        Raises :class:`RepairCircuitOpenError` (after a ``repair.circuit_open`` event) when the batch
        has too many manifest-level failures; then nothing is touched.
        """
        groups: dict[tuple[str, str], list[str]] = {}
        manifest_level: set[tuple[str, str]] = set()
        unreadable: dict[tuple[str, str], int] = {}
        for f in findings:
            key = (f.source_id, f.generation_id)
            if f.status is VerdictStatus.UNREADABLE:
                unreadable[key] = unreadable.get(key, 0) + 1
                continue
            groups.setdefault(key, [])
            if f.relpath is None:
                if f.status in MANIFEST_LEVEL:
                    manifest_level.add(key)
            else:
                groups[key].append(f.relpath)
        for (source_id, generation_id), count in sorted(unreadable.items()):
            self._ledger.append(
                "repair.deferred",
                f"{source_id}/{generation_id}",
                {"reason": "unreadable", "findings": count},
            )
        self._check_circuit(sorted(manifest_level))
        results: list[RepairResult] = []
        for (s, g), paths in groups.items():
            result = self.repair(s, g, None if (s, g) in manifest_level else paths)
            if result.deferred is None:
                results.append(result)
        return results

    def _check_circuit(self, failing: Sequence[tuple[str, str]]) -> None:
        if not failing:
            return
        total = sum(
            1
            for s in self._vault.sources()
            for g in self._vault.generations(s)
            if g.status is not GenerationStatus.STAGING
        )
        fraction = len(failing) / max(total, 1)
        if len(failing) > self._max_failures or (len(failing) >= 2 and fraction > self._max_fraction):
            subjects = [f"{s}/{g}" for s, g in failing]
            self._ledger.append(
                "repair.circuit_open",
                "vault",
                {"manifest_failures": len(failing), "generations": total, "subjects": subjects[:50]},
            )
            raise RepairCircuitOpenError(
                f"{len(failing)} of {total} sealed generations fail at manifest/signature level in one "
                "scrub; that is a signing-key, mount or software problem, not bit rot. Nothing was "
                f"repaired or degraded. First failures: {', '.join(subjects[:5])}"
            )

    def repair(
        self, source_id: str, generation_id: str, relpaths: Sequence[str] | None = None
    ) -> RepairResult:
        """Repair ``relpaths`` (or every file, re-checked, when ``None``) of one sealed generation."""
        subject = f"{source_id}/{generation_id}"
        try:
            with self._vault.exclusive_generation(source_id, generation_id):
                result, reason = self._repair_locked(source_id, generation_id, relpaths)
        except _Unreadable as exc:
            self._ledger.append("repair.deferred", subject, {"reason": f"unreadable: {exc}"})
            return RepairResult(source_id, generation_id, deferred=str(exc))
        if not result.degraded:
            return result
        rolled_back = self._roll_back(source_id, generation_id) if self._rollback else None
        self._ledger.append(
            "repair.degraded",
            subject,
            {"reason": reason, "files": list(result.unrepaired), "rolled_back_to": rolled_back},
        )
        return RepairResult(source_id, generation_id, result.repaired, result.unrepaired, True, rolled_back)

    def _repair_locked(
        self, source_id: str, generation_id: str, relpaths: Sequence[str] | None
    ) -> tuple[RepairResult, str]:
        """The rungs, under the generation's exclusive lock. Returns the result and, if it degraded,
        why."""
        subject = f"{source_id}/{generation_id}"
        try:
            manifest = self._vault.manifest(source_id, generation_id, fresh=True)
        except OSError as exc:
            raise _Unreadable(str(exc)) from exc
        except SignatureError as exc:
            restored = self._restore_manifest(source_id, generation_id)
            self._event(Rung.MIRROR, subject, [MANIFEST_NAME, SIGNATURE_NAME], restored, str(exc))
            if not restored:
                reason = f"manifest: {exc}"
                return self._degrade(source_id, generation_id, reason, [MANIFEST_NAME]), reason
            manifest = self._vault.manifest(source_id, generation_id, fresh=True)
            relpaths = None

        lib = self._vault.library_path(source_id, generation_id)
        derived = sidecar_paths(self._vault, source_id, generation_id)  # parity: rebuilt, not repaired
        relpaths = None if relpaths is None else [p for p in relpaths if p not in derived]
        unexpected = {p for p in relpaths or () if manifest.entry(p) is None and not is_parity_path(p)}
        for rel in sorted(unexpected):
            if (lib / rel).exists():
                self._vault.quarantine(lib / rel)
        if unexpected:
            self._ledger.append("repair.quarantine", subject, {"files": sorted(unexpected)})
        wanted = None if relpaths is None else set(relpaths)
        candidates = [e for e in manifest.entries if wanted is None or e.path in wanted]
        bad: list[ManifestEntry] = [e for e in candidates if not self._intact(lib, e)]
        repaired: dict[str, Rung] = {}

        for rung, attempt in (
            (Rung.PAR2, self._try_par2),
            (Rung.MIRROR, self._try_mirror),
            (Rung.REFETCH, self._try_refetch),
        ):
            if not bad:
                break
            fixed = attempt(source_id, generation_id, manifest, bad)
            if fixed is None:
                continue
            self._event(rung, subject, [e.path for e in bad], bool(fixed), f"repaired {sorted(fixed)}")
            repaired.update(dict.fromkeys(fixed, rung))
            bad = [e for e in bad if e.path not in fixed]

        if bad:
            reason = "files could not be repaired"
            return self._degrade(source_id, generation_id, reason, [e.path for e in bad], repaired), reason
        if repaired or unexpected:
            self._ledger.append("repair.ok", subject, {"repaired": {p: str(r) for p, r in repaired.items()}})
        return RepairResult(source_id, generation_id, repaired), ""

    # --- rungs -----------------------------------------------------------------------------------

    def _try_par2(
        self, source_id: str, generation_id: str, manifest: Manifest, bad: list[ManifestEntry]
    ) -> set[str] | None:
        lib = self._vault.library_path(source_id, generation_id)
        if self._parity is None or not (lib / PARITY_DIR).is_dir():
            return None
        try:
            sidecar = read_sidecar(self._vault, source_id, generation_id)
        except SignatureError:
            sidecar = None
        runs: list[tuple[list[Path], Path]] = []
        if manifest.parity:  # parity created while sealing, listed in the manifest itself
            runs.append(([lib / e.path for e in manifest.data], lib / PARITY_DIR))
        if sidecar is not None:  # parity added after sealing: one recovery set per group of files
            wanted = {e.path for e in bad}
            runs += [
                ([lib / f for f in s.files], lib / PARITY_DIR / s.name)
                for s in sidecar.sets
                if wanted & set(s.files)
            ]
        if not runs:
            return None
        known = {lib / e.path for e in manifest.entries} | {lib / MANIFEST_NAME, lib / SIGNATURE_NAME}
        before = {p for p in lib.rglob("*") if p.is_file()}
        for files, parity_dir in runs:
            self._parity.repair(files, parity_dir)
        for leftover in sorted({p for p in lib.rglob("*") if p.is_file()} - before - known):
            self._vault.quarantine(leftover)
        fixed: set[str] = set()
        for e in bad:
            if self._intact(lib, e):
                make_read_only(lib / e.path)
                fixed.add(e.path)
        return fixed

    def _try_mirror(
        self, source_id: str, generation_id: str, manifest: Manifest, bad: list[ManifestEntry]
    ) -> set[str] | None:
        mirror_lib = self._mirror_lib(source_id, generation_id)
        if mirror_lib is None:
            return None
        fixed: set[str] = set()
        for e in bad:
            if self._restore_from(source_id, generation_id, e, mirror_lib / e.path):
                fixed.add(e.path)
        return fixed

    def _try_refetch(
        self, source_id: str, generation_id: str, manifest: Manifest, bad: list[ManifestEntry]
    ) -> set[str] | None:
        if self._refetch is None:
            return None
        fixed: set[str] = set()
        if self._scratch is not None:
            self._scratch.mkdir(parents=True, exist_ok=True)
        for e in bad:
            req = RefetchRequest(source_id, generation_id, e.path, e.size, e.sha256, e.blake3, e.upstream)
            with tempfile.TemporaryDirectory(prefix="wsp-refetch-", dir=self._scratch) as tmp:
                try:
                    got = self._refetch(req, Path(tmp))
                except Exception as exc:  # an adapter failure must not stop the ladder
                    self._ledger.append(
                        "repair.refetch.error", f"{source_id}/{generation_id}", {"error": str(exc)}
                    )
                    continue
                if got is not None and self._restore_from(source_id, generation_id, e, got):
                    fixed.add(e.path)
        return fixed

    # --- helpers ---------------------------------------------------------------------------------

    def _restore_from(self, source_id: str, generation_id: str, entry: ManifestEntry, src: Path) -> bool:
        try:
            if not src.is_file() or src.stat().st_size != entry.size or readback(src) != entry.digests:
                return False
            self._vault.restore_file(source_id, generation_id, entry, src)
        except (IntegrityError, OSError):
            return False
        return True

    def _intact(self, lib: Path, entry: ManifestEntry) -> bool:
        """The file matches its entry. Missing or different: False. Unreadable: raises _Unreadable."""
        path = lib / entry.path
        try:
            return path.stat().st_size == entry.size and readback(path) == entry.digests
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise _Unreadable(f"{entry.path}: {exc}") from exc

    def _mirror_lib(self, source_id: str, generation_id: str) -> Path | None:
        if self._mirror is None:
            return None
        rel = self._vault.library_path(source_id, generation_id).relative_to(self._vault.root)
        path = self._mirror / rel
        return path if path.is_dir() else None

    def _restore_manifest(self, source_id: str, generation_id: str) -> bool:
        mirror_lib = self._mirror_lib(source_id, generation_id)
        if mirror_lib is None:
            return False
        try:
            data = (mirror_lib / MANIFEST_NAME).read_bytes()
            sig = (mirror_lib / SIGNATURE_NAME).read_bytes()
        except OSError:
            return False
        if not self._vault.signer.verify(data, sig):
            return False
        lib = self._vault.library_path(source_id, generation_id)
        for name in (MANIFEST_NAME, SIGNATURE_NAME):
            target = lib / name
            if target.exists() and target.read_bytes() == (data if name == MANIFEST_NAME else sig):
                continue
            staged = temp_path(target, "repair")
            try:
                atomic_copy(mirror_lib / name, staged)
                if target.exists():
                    self._vault.quarantine(target)
                publish_noreplace(staged, target)
            except BaseException:
                staged.unlink(missing_ok=True)
                raise
            fsync_dir(lib)
            make_read_only(target)
        return True

    def _degrade(
        self,
        source_id: str,
        generation_id: str,
        reason: str,
        files: Sequence[str],
        repaired: Mapping[str, Rung] | None = None,
    ) -> RepairResult:
        """Rung 4 (under the generation lock): DEGRADED.txt. The rollback follows outside the lock."""
        self._vault.mark_degraded(source_id, generation_id, reason, files)
        return RepairResult(source_id, generation_id, dict(repaired or {}), tuple(sorted(files)), True)

    def _roll_back(self, source_id: str, generation_id: str) -> str | None:
        """Point the degraded generation's partition at the newest older generation that verifies.

        Only when the degraded generation is in the current set. Candidates are sealed (not degraded)
        generations of the same partition, sealed before it, newest first (ordered by the manifest's
        ``sealed_at``, not by id: ``2026-10-11.ca`` sorts before ``2026-10.ca``). Each candidate is
        verified in full (fresh signature check, every data file re-hashed) before it is promoted; an
        I/O error stops the rollback (nothing is promoted on unread bytes).
        """
        subject = f"{source_id}/{generation_id}"
        try:
            if generation_id not in self._vault.current_set_ids(source_id):
                return None
            gens = self._vault.generations(source_id)
        except (OSError, VaultError):
            return None
        partitioned = self._vault.is_partitioned(source_id)
        mine = next((g for g in gens if g.generation_id == generation_id), None)
        cutoff = mine.sealed_at if mine is not None else None
        candidates = [
            g
            for g in gens
            if g.generation_id != generation_id
            and g.status is GenerationStatus.SEALED
            and g.sealed_at is not None
            and (cutoff is None or g.sealed_at < cutoff)
            and (not partitioned or partition_of(g.generation_id) == partition_of(generation_id))
        ]
        candidates.sort(key=lambda g: (g.sealed_at, g.generation_id), reverse=True)
        for gen in candidates:
            try:
                problem = self._verify_for_rollback(source_id, gen.generation_id)
            except OSError as exc:
                self._ledger.append(
                    "repair.rollback_deferred", subject, {"candidate": gen.generation_id, "reason": str(exc)}
                )
                return None
            if problem:
                self._ledger.append(
                    "repair.rollback_skipped", subject, {"candidate": gen.generation_id, "reason": problem}
                )
                continue
            try:
                self._vault.promote(source_id, gen.generation_id)
            except VaultError as exc:
                self._ledger.append(
                    "repair.rollback_skipped", subject, {"candidate": gen.generation_id, "reason": str(exc)}
                )
                continue
            return gen.generation_id
        return None

    def _verify_for_rollback(self, source_id: str, generation_id: str) -> str:
        """Empty if the generation verifies in full now; else what is wrong. OSError propagates."""
        lib = self._vault.library_path(source_id, generation_id)
        try:
            manifest = self._vault.manifest(source_id, generation_id, fresh=True)
        except SignatureError as exc:
            return str(exc)
        if (lib / DEGRADED_NAME).exists():
            return "DEGRADED"
        for e in manifest.data:
            path = lib / e.path
            if not path.is_file() or os.stat(path).st_size != e.size or readback(path) != e.digests:
                return f"{e.path} does not match its manifest"
        return ""

    def _event(self, rung: Rung, subject: str, files: Sequence[str], ok: bool, detail: str) -> None:
        self._ledger.append(f"repair.{rung}", subject, {"files": sorted(files), "ok": ok, "detail": detail})
