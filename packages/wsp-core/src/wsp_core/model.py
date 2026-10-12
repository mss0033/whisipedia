"""Domain model shared by every Whisipedia component.

All types are immutable value objects. They form part of the public contract (PLAN §9.2, §11.1):
changing a field is a breaking change and requires an ADR.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path, PurePosixPath

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
    cadence: str | None = None  # ISO 8601 override, e.g. "P3M"; None follows ``acquirer.cycle``
    strategy: UpdateStrategy | None = None
    irreplaceable: bool = False
    raw: Mapping[str, object] = field(default_factory=dict[str, object])  # full registry entry

    @property
    def vault_domain(self) -> str:
        """The shelf this source lives on in the vault (``library/<domain>/<id>/``): its first domain.

        Resolved from the registry everywhere (ADR-0005); a source that already has a directory keeps it.
        """
        return self.domain[0] if self.domain else "misc"

    @property
    def partitioned(self) -> bool:
        """``access.partitioned: true``: acquired one partition per run (ADR-0010).

        Its generations are ``<cycle>.<partition>`` and its content is the current generation of
        every partition (:mod:`wsp_core.partitions`), not just the newest generation.
        """
        access = self.raw.get("access")
        return isinstance(access, Mapping) and access.get("partitioned") is True  # pyright: ignore[reportUnknownMemberType]


@dataclass(frozen=True, slots=True)
class Checksum:
    """A digest as published upstream or computed locally. ``algorithm`` is lowercase, e.g. ``sha256``."""

    algorithm: str
    hexdigest: str


@dataclass(frozen=True, slots=True)
class RemoteFile:
    """A single upstream file that a release consists of.

    ``mirrors`` are full URLs of byte-identical copies (never metalinks or directory bases).
    ``metalink`` is the URL of a Metalink 4 document for this file, if upstream publishes one; a
    fetcher may use it for more mirrors, the exact size and per-piece hashes.
    """

    url: str
    name: str  # file name to store under ``upstream/``
    size: int | None = None
    checksums: tuple[Checksum, ...] = ()
    mirrors: tuple[str, ...] = ()
    metalink: str | None = None


@dataclass(frozen=True, slots=True)
class ReleaseRef:
    """An upstream release as discovered by an adapter (not yet fetched).

    ``license`` and ``format`` are what upstream *reports* for this release (SPDX id, format label
    such as ``zim/6`` or ``mediawiki-xml/0.11``); ``None`` when upstream does not say. The policy
    gate compares them with the registry and the previous release (PLAN §5.4). ``meta`` holds
    adapter-private strings only.
    """

    source_id: str
    version: str  # upstream version label, sortable (e.g. "2026-10-01")
    published: datetime | None
    files: tuple[RemoteFile, ...]
    meta: Mapping[str, str] = field(default_factory=dict[str, str])
    license: str | None = None
    format: str | None = None


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
    # Document policy (ADR-0010): the bytes are intact but must not enter the vault.
    REJECTED = "rejected"  # restricted or out of scope: deleted at once, the release goes on without it
    HELD = "held"  # needs an owner decision: quarantined outside the vault, the release goes on
    # Storage could not be read (an I/O error that outlived the retries, e.g. a soft-mount outage).
    # Says nothing about the content: never a reason to repair, degrade or roll back.
    UNREADABLE = "unreadable"


@dataclass(frozen=True, slots=True)
class Verdict:
    status: VerdictStatus
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status in (VerdictStatus.OK, VerdictStatus.UNVERIFIABLE)

    @property
    def withheld(self) -> bool:
        """The file is left out of the generation by document policy (not an integrity failure)."""
        return self.status in (VerdictStatus.REJECTED, VerdictStatus.HELD)


@dataclass(frozen=True, slots=True)
class FetchResult:
    """What ``Fetcher.download`` produced: the file and the digests computed while downloading.

    ``sha256`` and ``blake3`` are always our own digests; ``verified`` lists the upstream digests
    that matched (empty when upstream publishes none). Downstream steps use these instead of
    re-hashing the local file; the vault still reads its own copy back (PLAN §8.1).
    """

    path: Path
    size: int
    sha256: str
    blake3: str
    verified: tuple[Checksum, ...] = ()
    # Upstream publishes no checksum, but two independent full downloads produced the same bytes
    # (the fetcher's ``unverified_policy = "corroborate"``). Still UNVERIFIABLE: agreement is not
    # authenticity, but a truncated or spliced download cannot pass it. Keyword-only, so subclasses
    # that add fields positionally (``wsp_fetch.FetchReport``, test doubles) are unaffected.
    corroborated: bool = field(default=False, kw_only=True)

    @property
    def verdict(self) -> Verdict:
        if not self.verified:
            if self.corroborated:
                return Verdict(VerdictStatus.UNVERIFIABLE, "no upstream checksum; two full downloads agree")
            return Verdict(VerdictStatus.UNVERIFIABLE, "upstream publishes no checksum")
        return Verdict(VerdictStatus.OK, ", ".join(c.algorithm for c in self.verified))

    @property
    def checksums(self) -> tuple[Checksum, ...]:
        """Our digests plus the verified upstream ones (deduplicated by algorithm)."""
        out = {"sha256": Checksum("sha256", self.sha256), "blake3": Checksum("blake3", self.blake3)}
        for c in self.verified:
            out.setdefault(c.algorithm.lower(), c)
        return tuple(out.values())


@dataclass(frozen=True, slots=True)
class RemoteHead:
    """Metadata of a remote resource without its body (optional ``Fetcher.head``)."""

    url: str  # final URL after redirects
    size: int | None = None
    etag: str | None = None
    last_modified: datetime | None = None
    accepts_ranges: bool = False


def searchable(spec: SourceSpec) -> bool:
    """Whether a source is refined and indexed for search (registry ``index``, default true).

    ``index: false`` marks sources served as stored or not text: a ZIM edition that duplicates an indexed
    one (wikipedia-en-zim renders wikipedia-top10's English pages), map tiles, an edit history.
    """
    return spec.raw.get("index", True) is not False


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
    """Legal next states. Any non-terminal state may fail; failed may retry from PLANNED or quarantine.

    QUARANTINED is not a final judgement on the data: the owner's retry (``wsp retry``) returns it
    to FAILED, from where the normal retry path applies. PROMOTED is the only terminal state.
    """
    if state is ReleaseState.PROMOTED:
        return frozenset()
    if state is ReleaseState.QUARANTINED:
        return frozenset({ReleaseState.FAILED})
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
    aliases: tuple[str, ...] = ()  # alternative titles (redirects, labels); feed suggest and search
    # Per-document metadata strings (v0.3, additive; empty for every pre-existing adapter). Document
    # collections carry issuer, publication number, edition, distribution, currency and the
    # Navigator banner here (see wsp_core.banners); keys are lowercase snake_case.
    meta: Mapping[str, str] = field(default_factory=dict[str, str])


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
    lang: str = ""  # the document's language (BCP-47 primary subtag), when the index knows it


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


# --- Read-side browsing (Navigator portal, directory, assets, gaps; moved from wsp_api.seams in v0.2) ----


@dataclass(frozen=True, slots=True)
class Category:
    """A top-level category of one source (e.g. a Gutenberg LCC class or a Wikidata class)."""

    slug: str  # path segment, [a-z0-9-]+
    name: str
    count: int


@dataclass(frozen=True, slots=True)
class Asset:
    """A file inside a sealed container (ZIM image, stylesheet, ...)."""

    content: bytes
    mimetype: str


@dataclass(frozen=True, slots=True)
class Gap:
    """A "not in library" report for the gap miner (PLAN §5.3, §10.2)."""

    at: datetime
    href: str  # the link as it appeared, or a wsp:// address that did not resolve
    from_doc_id: str | None = None


@dataclass(frozen=True, slots=True)
class DayEvent:
    """An "On this day" entry for the start page (PLAN §10.2), e.g. from a Wikipedia date page."""

    year: int  # negative for BC
    text: str
    doc_id: DocId | None = None  # page the entry comes from (or is about)
