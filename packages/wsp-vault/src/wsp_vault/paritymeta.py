"""Parity added *after* sealing: ``parity/PARITY.json`` (+ ``.sig``) inside a sealed generation.

A sealed generation's ``MANIFEST.json`` is never rewritten, so parity created later (by
:class:`wsp_vault.paritykeeper.ParityKeeper`, or by the backfill of older generations) carries its
own signed inventory::

    <generation>/parity/PARITY.json          sets of data files and the par2 files protecting them
    <generation>/parity/PARITY.json.sig      signature by the vault's manifest signer
    <generation>/parity/set-00001/recovery.par2, recovery.vol000+100.par2, ...

Each set is an independent par2 recovery set over a group of data files (a huge file is a set of its
own; many small files are batched), so repair and re-creation never need the whole generation at
once. ``manifest_blake3`` binds the sidecar to the exact manifest it protects. The scrubber verifies
every listed parity file like a data file.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast

import blake3

from wsp_vault.manifest import MANIFEST_NAME, PARITY_DIR, ROLE_PARITY, ManifestEntry, ManifestError
from wsp_vault.vault import FileVault, SignatureError

PARITY_FORMAT = "whisipedia-parity/1"
SIDECAR_NAME = "PARITY.json"
SIDECAR_SIG = "PARITY.json.sig"
SIDECAR_PATH = f"{PARITY_DIR}/{SIDECAR_NAME}"
SIDECAR_SIG_PATH = f"{PARITY_DIR}/{SIDECAR_SIG}"


@dataclass(frozen=True, slots=True)
class ParitySet:
    name: str  # directory under parity/, e.g. "set-00001"
    files: tuple[str, ...]  # data paths (relative to the generation) this set protects
    parity: tuple[ManifestEntry, ...]  # its par2 files, paths relative to the generation

    def to_json(self) -> dict[str, object]:
        return {"name": self.name, "files": list(self.files), "parity": [e.to_json() for e in self.parity]}

    @classmethod
    def from_json(cls, obj: Mapping[str, Any]) -> ParitySet:
        return cls(
            name=str(obj["name"]),
            files=tuple(str(f) for f in cast(list[str], obj["files"])),
            parity=tuple(ManifestEntry.from_json(e) for e in cast(list[Mapping[str, Any]], obj["parity"])),
        )


@dataclass(frozen=True, slots=True)
class ParitySidecar:
    source_id: str
    generation_id: str
    manifest_blake3: str
    percent: int
    created_at: datetime
    sets: tuple[ParitySet, ...]

    @property
    def entries(self) -> tuple[ManifestEntry, ...]:
        return tuple(e for s in self.sets for e in s.parity)

    @property
    def paths(self) -> frozenset[str]:
        return frozenset({SIDECAR_PATH, SIDECAR_SIG_PATH, *(e.path for e in self.entries)})

    @property
    def total_bytes(self) -> int:
        return sum(e.size for e in self.entries)

    def set_for(self, path: str) -> ParitySet | None:
        return next((s for s in self.sets if path in s.files), None)

    def to_bytes(self) -> bytes:
        doc = {
            "format": PARITY_FORMAT,
            "source_id": self.source_id,
            "generation_id": self.generation_id,
            "manifest_blake3": self.manifest_blake3,
            "percent": self.percent,
            "created_at": self.created_at.isoformat(),
            "sets": [s.to_json() for s in self.sets],
        }
        return (json.dumps(doc, sort_keys=True, indent=1, ensure_ascii=False) + "\n").encode("utf-8")

    @classmethod
    def from_bytes(cls, data: bytes) -> ParitySidecar:
        try:
            doc = cast(dict[str, Any], json.loads(data))
            if doc.get("format") != PARITY_FORMAT:
                raise ManifestError(f"unsupported parity format {doc.get('format')!r}")
            sidecar = cls(
                source_id=str(doc["source_id"]),
                generation_id=str(doc["generation_id"]),
                manifest_blake3=str(doc["manifest_blake3"]),
                percent=int(doc["percent"]),
                created_at=datetime.fromisoformat(str(doc["created_at"])),
                sets=tuple(ParitySet.from_json(s) for s in cast(list[Mapping[str, Any]], doc["sets"])),
            )
        except (KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, ManifestError):
                raise
            raise ManifestError(f"malformed parity sidecar: {exc}") from exc
        for e in sidecar.entries:
            if e.role != ROLE_PARITY or not e.path.startswith(f"{PARITY_DIR}/"):
                raise ManifestError(f"parity sidecar lists a non-parity file: {e.path}")
        return sidecar


def manifest_blake3(vault: FileVault, source_id: str, generation_id: str) -> str:
    lib = vault.library_path(source_id, generation_id)
    return blake3.blake3((lib / MANIFEST_NAME).read_bytes()).hexdigest()


def read_sidecar(vault: FileVault, source_id: str, generation_id: str) -> ParitySidecar | None:
    """The verified sidecar, ``None`` if there is none. Raises :class:`SignatureError` if it is bad."""
    parity = vault.library_path(source_id, generation_id) / PARITY_DIR
    data_path = parity / SIDECAR_NAME
    if not data_path.exists():
        return None
    subject = f"{source_id}/{generation_id}"
    try:
        data = data_path.read_bytes()
        sig = (parity / SIDECAR_SIG).read_bytes()
    except FileNotFoundError as exc:
        raise SignatureError(f"{subject}: {exc.filename} missing") from exc
    if not vault.signer.verify(data, sig):
        raise SignatureError(f"{subject}: parity sidecar signature invalid")
    try:
        sidecar = ParitySidecar.from_bytes(data)
    except ManifestError as exc:
        raise SignatureError(f"{subject}: {exc}") from exc
    if (sidecar.source_id, sidecar.generation_id) != (source_id, generation_id):
        raise SignatureError(f"{subject}: parity sidecar names another generation")
    if sidecar.manifest_blake3 != manifest_blake3(vault, source_id, generation_id):
        raise SignatureError(f"{subject}: parity sidecar was made for another manifest")
    return sidecar


def sidecar_paths(vault: FileVault, source_id: str, generation_id: str) -> frozenset[str]:
    """Paths covered by a valid sidecar (empty if none or invalid)."""
    try:
        sidecar = read_sidecar(vault, source_id, generation_id)
    except SignatureError:
        return frozenset()
    return frozenset() if sidecar is None else sidecar.paths


def is_parity_path(relpath: str | None) -> bool:
    return relpath is not None and relpath.startswith(f"{PARITY_DIR}/")
