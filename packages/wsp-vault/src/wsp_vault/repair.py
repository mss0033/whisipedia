"""Automatic repair ladder (PLAN §8.4).

For one generation, damaged files are tried against each rung in order, and every rung that runs
writes a ledger event:

1. **par2** — ``ParityProvider.repair`` in place; leftovers it creates are quarantined.
2. **mirror** — the same relative path under a second copy (``mirror_root`` has the vault layout).
3. **re-fetch** — an injected callable downloads the file again into local scratch space.
4. **degraded** — ``DEGRADED.txt`` + ledger event; if the generation is current, ``CURRENT.txt``
   is pointed back at the newest earlier generation that still verifies.

A replacement is accepted only if it matches the manifest (size, SHA-256, BLAKE3). A damaged
manifest or signature can only be restored from the mirror: re-signing would launder tampering.
Damaged files are quarantined, never deleted.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from wsp_core.model import Checksum, GenerationStatus
from wsp_core.ports import Ledger, ParityProvider
from wsp_vault.atomic import IntegrityError, atomic_copy, fsync_dir, readback
from wsp_vault.hashing import hash_file
from wsp_vault.manifest import MANIFEST_NAME, PARITY_DIR, SIGNATURE_NAME, Manifest, ManifestEntry
from wsp_vault.scrub import ScrubFinding
from wsp_vault.vault import FileVault, SignatureError, VaultError, make_read_only


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


@dataclass(frozen=True, slots=True)
class RepairResult:
    source_id: str
    generation_id: str
    repaired: Mapping[str, Rung] = field(default_factory=dict[str, Rung])
    unrepaired: tuple[str, ...] = ()
    degraded: bool = False
    rolled_back_to: str | None = None

    @property
    def ok(self) -> bool:
        return not self.degraded


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
    ) -> None:
        self._vault = vault
        self._ledger = ledger
        self._parity = parity
        self._mirror = mirror_root
        self._refetch = refetch
        self._scratch = scratch
        self._rollback = rollback_current

    def repair_findings(self, findings: Iterable[ScrubFinding]) -> list[RepairResult]:
        groups: dict[tuple[str, str], list[str]] = {}
        manifest_level: set[tuple[str, str]] = set()
        for f in findings:
            key = (f.source_id, f.generation_id)
            groups.setdefault(key, [])
            if f.relpath is None:
                manifest_level.add(key)
            else:
                groups[key].append(f.relpath)
        return [
            self.repair(s, g, None if (s, g) in manifest_level else paths) for (s, g), paths in groups.items()
        ]

    def repair(
        self, source_id: str, generation_id: str, relpaths: Sequence[str] | None = None
    ) -> RepairResult:
        """Repair ``relpaths`` (or every file, re-checked, when ``None``) of one sealed generation."""
        subject = f"{source_id}/{generation_id}"
        try:
            manifest = self._vault.manifest(source_id, generation_id)
        except SignatureError as exc:
            restored = self._restore_manifest(source_id, generation_id)
            self._event(Rung.MIRROR, subject, [MANIFEST_NAME, SIGNATURE_NAME], restored, str(exc))
            if not restored:
                return self._degrade(source_id, generation_id, f"manifest: {exc}", [MANIFEST_NAME])
            manifest = self._vault.manifest(source_id, generation_id)
            relpaths = None

        lib = self._vault.library_path(source_id, generation_id)
        unexpected = {p for p in relpaths or () if manifest.entry(p) is None}
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
            return self._degrade(
                source_id, generation_id, "files could not be repaired", [e.path for e in bad], repaired
            )
        if repaired or unexpected:
            self._ledger.append("repair.ok", subject, {"repaired": {p: str(r) for p, r in repaired.items()}})
        return RepairResult(source_id, generation_id, repaired)

    # --- rungs -----------------------------------------------------------------------------------

    def _try_par2(
        self, source_id: str, generation_id: str, manifest: Manifest, bad: list[ManifestEntry]
    ) -> set[str] | None:
        lib = self._vault.library_path(source_id, generation_id)
        if self._parity is None or not manifest.parity or not (lib / PARITY_DIR).is_dir():
            return None
        known = {lib / e.path for e in manifest.entries} | {lib / MANIFEST_NAME, lib / SIGNATURE_NAME}
        before = {p for p in lib.rglob("*") if p.is_file()}
        self._parity.repair([lib / e.path for e in manifest.data], lib / PARITY_DIR)
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
        if not src.is_file() or src.stat().st_size != entry.size or hash_file(src) != entry.digests:
            return False
        try:
            self._vault.restore_file(source_id, generation_id, entry, src)
        except (IntegrityError, OSError):
            return False
        return True

    def _intact(self, lib: Path, entry: ManifestEntry) -> bool:
        path = lib / entry.path
        try:
            return path.stat().st_size == entry.size and readback(path) == entry.digests
        except OSError:
            return False

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
            staged = target.with_name(name + ".repair")
            atomic_copy(mirror_lib / name, staged, overwrite=True)
            if target.exists():
                self._vault.quarantine(target)
            os.rename(staged, target)
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
        self._vault.mark_degraded(source_id, generation_id, reason, files)
        rolled_back = self._roll_back(source_id, generation_id) if self._rollback else None
        self._ledger.append(
            "repair.degraded",
            f"{source_id}/{generation_id}",
            {"reason": reason, "files": sorted(files), "rolled_back_to": rolled_back},
        )
        return RepairResult(
            source_id, generation_id, dict(repaired or {}), tuple(sorted(files)), True, rolled_back
        )

    def _roll_back(self, source_id: str, generation_id: str) -> str | None:
        try:
            current = self._vault.current(source_id)
        except VaultError:
            current = None
        if current is None or current.generation_id != generation_id:
            return None
        for gen in reversed(self._vault.generations(source_id)):
            if gen.generation_id >= generation_id or gen.status is not GenerationStatus.SEALED:
                continue
            try:
                self._vault.promote(source_id, gen.generation_id)
            except VaultError:
                continue
            return gen.generation_id
        return None

    def _event(self, rung: Rung, subject: str, files: Sequence[str], ok: bool, detail: str) -> None:
        self._ledger.append(f"repair.{rung}", subject, {"files": sorted(files), "ok": ok, "detail": detail})
