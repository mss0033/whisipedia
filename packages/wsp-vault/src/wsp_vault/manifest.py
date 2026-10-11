"""``MANIFEST.json``: the signed, deterministic inventory of a sealed generation (PLAN §7, §8.1).

Serialization is canonical (sorted keys, entries sorted by path, two-space indent, trailing LF) so
the same generation always produces the same bytes and the signature covers exactly those bytes.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import PurePosixPath
from typing import Any, cast

from wsp_core.model import Artifact, Checksum
from wsp_vault.hashing import Digests

FORMAT = "whisipedia-manifest/1"
MANIFEST_NAME = "MANIFEST.json"
SIGNATURE_NAME = "MANIFEST.json.sig"
DEGRADED_NAME = "DEGRADED.txt"
PARITY_DIR = "parity"
STAGING_RECORD = ".wsp-staging.json"

ROLE_DATA = "data"
ROLE_PARITY = "parity"

# Signed attestations next to the manifest (wsp_vault.attest, ADR-0009).
ATTESTATION_NAMES = frozenset(
    {"PROVENANCE.json", "PROVENANCE.json.sig", "PROMOTION.json", "PROMOTION.json.sig"}
)

RESERVED_NAMES = frozenset(
    {MANIFEST_NAME, SIGNATURE_NAME, DEGRADED_NAME, STAGING_RECORD, PARITY_DIR, *ATTESTATION_NAMES}
)


class ManifestError(ValueError):
    pass


def check_relpath(relpath: str) -> PurePosixPath:
    """Validate a path relative to a generation directory (e.g. ``upstream/x.zim``)."""
    path = PurePosixPath(relpath)
    bad = (
        not relpath
        or "\\" in relpath
        or path.is_absolute()
        or str(path) != relpath
        or any(part in ("", ".", "..") for part in path.parts)
        or path.parts[0] in RESERVED_NAMES
        or path.name.endswith(".partial")
    )
    if bad:
        raise ManifestError(f"invalid or reserved relpath: {relpath!r}")
    return path


@dataclass(frozen=True, slots=True)
class ManifestEntry:
    path: str
    size: int
    sha256: str
    blake3: str
    role: str = ROLE_DATA
    upstream: tuple[Checksum, ...] = ()

    @classmethod
    def from_digests(
        cls, path: str, digests: Digests, *, role: str = ROLE_DATA, upstream: Sequence[Checksum] = ()
    ) -> ManifestEntry:
        ups = tuple(sorted({Checksum(c.algorithm.lower(), c.hexdigest.lower()) for c in upstream}, key=_ck))
        return cls(path, digests.size, digests.sha256, digests.blake3, role, ups)

    @property
    def digests(self) -> Digests:
        return Digests(self.size, self.sha256, self.blake3)

    def to_artifact(self, source_id: str, version: str) -> Artifact:
        return Artifact(
            source_id=source_id,
            version=version,
            relpath=PurePosixPath(self.path),
            size=self.size,
            sha256=self.sha256,
            blake3=self.blake3,
            upstream=self.upstream,
        )

    def to_json(self) -> dict[str, object]:
        return {
            "path": self.path,
            "size": self.size,
            "sha256": self.sha256,
            "blake3": self.blake3,
            "role": self.role,
            "upstream": [{"algorithm": c.algorithm, "hexdigest": c.hexdigest} for c in self.upstream],
        }

    @classmethod
    def from_json(cls, obj: Mapping[str, Any]) -> ManifestEntry:
        ups = cast(list[Mapping[str, str]], obj.get("upstream", []))
        return cls(
            path=str(obj["path"]),
            size=int(obj["size"]),
            sha256=str(obj["sha256"]),
            blake3=str(obj["blake3"]),
            role=str(obj.get("role", ROLE_DATA)),
            upstream=tuple(Checksum(u["algorithm"], u["hexdigest"]) for u in ups),
        )


def _ck(c: Checksum) -> tuple[str, str]:
    return (c.algorithm, c.hexdigest)


@dataclass(frozen=True, slots=True)
class Manifest:
    source_id: str
    generation_id: str
    sealed_at: datetime
    entries: tuple[ManifestEntry, ...]

    def __post_init__(self) -> None:
        paths = [e.path for e in self.entries]
        if len(set(paths)) != len(paths):
            raise ManifestError("duplicate paths in manifest")
        object.__setattr__(self, "entries", tuple(sorted(self.entries, key=lambda e: e.path)))

    @property
    def data(self) -> tuple[ManifestEntry, ...]:
        return tuple(e for e in self.entries if e.role == ROLE_DATA)

    @property
    def parity(self) -> tuple[ManifestEntry, ...]:
        return tuple(e for e in self.entries if e.role == ROLE_PARITY)

    @property
    def total_bytes(self) -> int:
        return sum(e.size for e in self.entries)

    def entry(self, path: str) -> ManifestEntry | None:
        return next((e for e in self.entries if e.path == path), None)

    def artifacts(self) -> tuple[Artifact, ...]:
        return tuple(e.to_artifact(self.source_id, self.generation_id) for e in self.data)

    def to_bytes(self) -> bytes:
        doc = {
            "format": FORMAT,
            "source_id": self.source_id,
            "generation_id": self.generation_id,
            "sealed_at": self.sealed_at.isoformat(),
            "file_count": len(self.entries),
            "total_bytes": self.total_bytes,
            "files": [e.to_json() for e in self.entries],
        }
        return (json.dumps(doc, sort_keys=True, indent=2, ensure_ascii=False) + "\n").encode("utf-8")

    @classmethod
    def from_bytes(cls, data: bytes) -> Manifest:
        try:
            doc = cast(dict[str, Any], json.loads(data))
            if doc.get("format") != FORMAT:
                raise ManifestError(f"unsupported manifest format {doc.get('format')!r}")
            files = cast(list[Mapping[str, Any]], doc["files"])
            return cls(
                source_id=str(doc["source_id"]),
                generation_id=str(doc["generation_id"]),
                sealed_at=datetime.fromisoformat(str(doc["sealed_at"])),
                entries=tuple(ManifestEntry.from_json(f) for f in files),
            )
        except (KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, ManifestError):
                raise
            raise ManifestError(f"malformed manifest: {exc}") from exc
