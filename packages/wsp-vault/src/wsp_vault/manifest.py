"""``MANIFEST.json``: the signed, deterministic inventory of a sealed generation (PLAN §7, §8.1).

Serialization is canonical (sorted keys, entries sorted by path, two-space indent, trailing LF) so
the same generation always produces the same bytes and the signature covers exactly those bytes.
The bytes are exactly ``json.dumps(doc, sort_keys=True, indent=2, ensure_ascii=False) + "\\n"``; they
are produced by a specialised writer because the standard library falls back to its pure-Python
encoder whenever ``indent`` is set (300k entries: 2.5 s instead of well under a second).

Every path read from a manifest or a staging record is validated (:func:`check_stored_path`): a
manifest whose signature verifies is still never allowed to name ``../`` or an absolute path.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from json.encoder import encode_basestring  # the C implementation when available
from pathlib import PurePosixPath
from typing import Any, cast

from wsp_core.checksums import normalize_algorithm
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
_RESERVED_FOLDED = frozenset(n.casefold() for n in RESERVED_NAMES)
# Suffixes of the vault's own temporaries: never valid names for content.
RESERVED_SUFFIXES = (".partial", ".repair", ".adopting")


class ManifestError(ValueError):
    pass


_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")  # Unicode category Cc


def check_stored_path(relpath: str, role: str = ROLE_DATA) -> None:
    """Safety check for a path *read* from a manifest, sidecar or staging record.

    Rejects anything that could leave the generation directory (absolute, ``..``, empty or ``.``
    parts, backslashes, NUL and other control characters) and parity entries outside ``parity/``.
    It deliberately does not apply the naming reservations of :func:`check_relpath`, so manifests
    sealed before a reservation was added stay readable. String operations only: it runs once per
    entry of manifests with hundreds of thousands of entries.
    """
    parts = relpath.split("/")
    bad = (
        not relpath
        or relpath[0] == "/"
        or "\\" in relpath
        or _CONTROL.search(relpath) is not None
        or any(part in ("", ".", "..") for part in parts)
        or (role == ROLE_PARITY and (len(parts) < 2 or parts[0] != PARITY_DIR))
    )
    if bad:
        raise ManifestError(f"unsafe path in manifest: {relpath!r}")


def check_relpath(relpath: str) -> PurePosixPath:
    """Validate a *new* path relative to a generation directory (e.g. ``upstream/x.zim``).

    On top of :func:`check_stored_path`: the first part may not be a reserved name, compared without
    case (the SMB share is case-insensitive, so ``manifest.json`` *is* ``MANIFEST.json``), and no
    part may end in a suffix the vault uses for its temporaries (``.partial``, ``.repair``,
    ``.adopting``).
    """
    check_stored_path(relpath)
    path = PurePosixPath(relpath)
    bad = path.parts[0].casefold() in _RESERVED_FOLDED or any(
        part.casefold().endswith(RESERVED_SUFFIXES) for part in path.parts
    )
    if bad:
        raise ManifestError(f"invalid or reserved relpath: {relpath!r}")
    return path


def casefold_collisions(paths: Iterable[str]) -> list[tuple[str, str]]:
    """Pairs of distinct paths that name the same file on a case-insensitive share."""
    seen: dict[str, str] = {}
    out: list[tuple[str, str]] = []
    for p in paths:
        key = p.casefold()
        other = seen.setdefault(key, p)
        if other != p:
            out.append((other, p))
    return out


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
        ups = tuple(
            sorted(
                {Checksum(normalize_algorithm(c.algorithm), c.hexdigest.strip().lower()) for c in upstream},
                key=_ck,
            )
        )
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
        """Parse and validate one entry (path safety, field types)."""
        ups = cast(list[Mapping[str, str]], obj.get("upstream", []))
        role = str(obj.get("role", ROLE_DATA))
        path = obj["path"]
        size = obj["size"]
        if not isinstance(path, str) or not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise ManifestError(f"malformed manifest entry: {obj!r}"[:200])
        check_stored_path(path, role)
        return cls(
            path=path,
            size=size,
            sha256=str(obj["sha256"]),
            blake3=str(obj["blake3"]),
            role=role,
            upstream=tuple(Checksum(u["algorithm"], u["hexdigest"]) for u in ups),
        )


def _ck(c: Checksum) -> tuple[str, str]:
    return (c.algorithm, c.hexdigest)


def _entry_text(e: ManifestEntry) -> str:
    """One entry exactly as ``json.dumps(..., sort_keys=True, indent=2)`` writes it at depth 2."""
    if e.upstream:
        ups = ",\n".join(
            "        {\n"
            f'          "algorithm": {encode_basestring(c.algorithm)},\n'
            f'          "hexdigest": {encode_basestring(c.hexdigest)}\n'
            "        }"
            for c in e.upstream
        )
        upstream = f"[\n{ups}\n      ]"
    else:
        upstream = "[]"
    return (
        "    {\n"
        f'      "blake3": {encode_basestring(e.blake3)},\n'
        f'      "path": {encode_basestring(e.path)},\n'
        f'      "role": {encode_basestring(e.role)},\n'
        f'      "sha256": {encode_basestring(e.sha256)},\n'
        f'      "size": {int(e.size)},\n'
        f'      "upstream": {upstream}\n'
        "    }"
    )


@dataclass(frozen=True, slots=True)
class Manifest:
    source_id: str
    generation_id: str
    sealed_at: datetime
    entries: tuple[ManifestEntry, ...]
    _by_path: dict[str, ManifestEntry] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        by_path = {e.path: e for e in self.entries}
        if len(by_path) != len(self.entries):
            raise ManifestError("duplicate paths in manifest")
        object.__setattr__(self, "entries", tuple(sorted(self.entries, key=lambda e: e.path)))
        object.__setattr__(self, "_by_path", by_path)

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
        """O(1): the entry for ``path``, if listed."""
        return self._by_path.get(path)

    def artifacts(self) -> tuple[Artifact, ...]:
        return tuple(e.to_artifact(self.source_id, self.generation_id) for e in self.data)

    def to_bytes(self) -> bytes:
        """Canonical bytes; identical to the ``json.dumps`` form described in the module docstring."""
        files = ",\n".join(_entry_text(e) for e in self.entries)
        files_text = f"[\n{files}\n  ]" if self.entries else "[]"
        text = (
            "{\n"
            f'  "file_count": {len(self.entries)},\n'
            f'  "files": {files_text},\n'
            f'  "format": {encode_basestring(FORMAT)},\n'
            f'  "generation_id": {encode_basestring(self.generation_id)},\n'
            f'  "sealed_at": {encode_basestring(self.sealed_at.isoformat())},\n'
            f'  "source_id": {encode_basestring(self.source_id)},\n'
            f'  "total_bytes": {self.total_bytes}\n'
            "}\n"
        )
        return text.encode("utf-8")

    def to_bytes_reference(self) -> bytes:
        """The definition of the canonical form (slow; used by tests to check :meth:`to_bytes`)."""
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
