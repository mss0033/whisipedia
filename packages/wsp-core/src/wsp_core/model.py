"""Domain model shared by every Whisipedia component.

All types are immutable value objects. They form part of the public contract (PLAN §9.2, §11.1):
changing a field is a breaking change and requires an ADR.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import PurePosixPath

from wsp_core.ids import DocId


class Tier(StrEnum):
    T1 = "T1"
    T2 = "T2"
    T3 = "T3"
    EXCLUDED = "X"


class SourceStatus(StrEnum):
    CANDIDATE = "candidate"
    ACTIVE = "active"
    PAUSED = "paused"
    RETIRED = "retired"
    EXCLUDED = "excluded"


class UpdateStrategy(StrEnum):
    SNAPSHOT_RETENTION = "snapshot-retention"
    BASE_PLUS_INCREMENTAL = "base-plus-incremental"
    MIRROR = "mirror"


@dataclass(frozen=True, slots=True)
class SourceSpec:
    """One entry of ``registry/sources.yaml``."""

    id: str
    name: str
    tier: Tier
    status: SourceStatus
    license: str
    legal_basis: str
    domain: tuple[str, ...]
    adapter: str | None = None
    cadence: str | None = None  # ISO 8601 duration, e.g. "P1M"
    strategy: UpdateStrategy | None = None
    irreplaceable: bool = False
    raw: Mapping[str, object] = field(default_factory=dict[str, object])  # full registry entry


@dataclass(frozen=True, slots=True)
class Checksum:
    """A digest as published upstream or computed locally. ``algorithm`` is lowercase, e.g. ``sha256``."""

    algorithm: str
    hexdigest: str


@dataclass(frozen=True, slots=True)
class RemoteFile:
    """A single upstream file that a release consists of."""

    url: str
    name: str  # file name to store under ``upstream/``
    size: int | None = None
    checksums: tuple[Checksum, ...] = ()
    mirrors: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ReleaseRef:
    """An upstream release as discovered by an adapter (not yet fetched)."""

    source_id: str
    version: str  # upstream version label, sortable (e.g. "2026-10-01")
    published: datetime | None
    files: tuple[RemoteFile, ...]
    meta: Mapping[str, str] = field(default_factory=dict[str, str])


@dataclass(frozen=True, slots=True)
class FetchPlan:
    """What to download for a release, given what the vault already holds."""

    release: ReleaseRef
    files: tuple[RemoteFile, ...]  # subset of release.files still needed
    previous_generation: str | None = None


@dataclass(frozen=True, slots=True)
class Artifact:
    """A file that has landed on local or vault storage."""

    source_id: str
    version: str
    relpath: PurePosixPath  # relative to the generation directory, e.g. upstream/x.zim
    size: int
    sha256: str
    blake3: str
    upstream: tuple[Checksum, ...] = ()


class VerdictStatus(StrEnum):
    OK = "ok"
    MISMATCH = "mismatch"
    MISSING = "missing"
    UNVERIFIABLE = "unverifiable"  # upstream publishes no checksum


@dataclass(frozen=True, slots=True)
class Verdict:
    status: VerdictStatus
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status in (VerdictStatus.OK, VerdictStatus.UNVERIFIABLE)


class ReleaseState(StrEnum):
    """Release lifecycle (PLAN §6.3)."""

    DISCOVERED = "discovered"
    PLANNED = "planned"
    FETCHING = "fetching"
    FETCHED = "fetched"
    VERIFIED = "verified"
    SEALED = "sealed"
    REFINED = "refined"
    INDEXED = "indexed"
    PROMOTED = "promoted"
    FAILED = "failed"
    QUARANTINED = "quarantined"


_FORWARD: tuple[ReleaseState, ...] = (
    ReleaseState.DISCOVERED,
    ReleaseState.PLANNED,
    ReleaseState.FETCHING,
    ReleaseState.FETCHED,
    ReleaseState.VERIFIED,
    ReleaseState.SEALED,
    ReleaseState.REFINED,
    ReleaseState.INDEXED,
    ReleaseState.PROMOTED,
)


def allowed_transitions(state: ReleaseState) -> frozenset[ReleaseState]:
    """Legal next states. Any non-terminal state may fail; failed may retry from PLANNED or quarantine."""
    if state is ReleaseState.PROMOTED or state is ReleaseState.QUARANTINED:
        return frozenset()
    if state is ReleaseState.FAILED:
        return frozenset({ReleaseState.PLANNED, ReleaseState.QUARANTINED})
    nxt = _FORWARD[_FORWARD.index(state) + 1]
    return frozenset({nxt, ReleaseState.FAILED})


class GenerationStatus(StrEnum):
    STAGING = "staging"
    SEALED = "sealed"
    DEGRADED = "degraded"
    PRUNED = "pruned"


@dataclass(frozen=True, slots=True)
class Generation:
    """A sealed, immutable, dated copy of one release in the vault (PLAN §7)."""

    source_id: str
    generation_id: str  # directory name, e.g. "2026-10-01"
    status: GenerationStatus
    artifacts: tuple[Artifact, ...] = ()
    sealed_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class DocRecord:
    """Normalized document emitted by adapters for the Refinery (PLAN §9.1)."""

    doc_id: DocId
    source_id: str
    native_id: str
    title: str
    lang: str
    license: str
    text: str
    revision: str | None = None
    timestamp: datetime | None = None
    url_native: str | None = None
    html_ref: str | None = None  # pointer into the original artifact, e.g. "zim:A/Alan_Turing"
    entities: tuple[str, ...] = ()  # Wikidata QIDs
    links: tuple[str, ...] = ()  # outbound doc ids or native urls
    content_hash: str = ""  # blake3 of normalized text; see wsp_core.ids.content_hash


class ChangeKind(StrEnum):
    ADDED = "added"
    MODIFIED = "modified"
    REMOVED = "removed"  # tombstone: removed upstream, content kept in history
    RENAMED = "renamed"


@dataclass(frozen=True, slots=True)
class ChangeRecord:
    """One entry of the change feed (PLAN §11.1)."""

    doc_id: DocId
    kind: ChangeKind
    generation_id: str
    old_hash: str | None
    new_hash: str | None
    renamed_from: DocId | None = None


@dataclass(frozen=True, slots=True)
class SearchHit:
    doc_id: DocId
    score: float
    title: str
    snippet: str = ""
    source_id: str = ""


@dataclass(frozen=True, slots=True)
class LedgerEvent:
    """Append-only, hash-chained audit record (PLAN §8.3)."""

    seq: int
    at: datetime
    kind: str  # e.g. "release.state", "artifact.verified", "repair.par2", "policy.decision"
    subject: str  # e.g. "wikipedia-en-zim/2026-10-01"
    data: Mapping[str, object]
    prev_hash: str
    hash: str
