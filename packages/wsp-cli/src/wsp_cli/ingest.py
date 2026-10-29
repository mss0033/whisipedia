"""``wsp ingest``: adopt completed bulk downloads from ``<vault>/_incoming/`` into sealed generations.

Bulk downloads (aria2c, rsync) land in ``<vault.root>/_incoming/<source_id>/<version>/<name>``, on the
same filesystem as the vault, so adoption is a rename, never a copy. The download plan is
``_incoming/_plans/targets.json``: a list of ``{source_id, version, name, uris, lane, checksum, size,
meta}`` where ``checksum`` is ``[algo, hex]`` (aria2 names: ``sha-1``, ``sha-256``, ``md5``) or null.
Per-source plans in ``_incoming/_plans/targets.d/*.json`` (same format; written by ``wsp collections
fetch`` for document collections) are read too. A target whose ``meta.relpath`` is set (the
collection catalog ``meta/documents.jsonl``) is stored at that path instead of ``upstream/<name>``.

A file is **complete** when no sibling ``<name>.aria2`` control file exists, its size matches the plan
(when the plan knows it) and it has not been modified for ``settle_seconds``. Files of the
``collections`` lane skip the settle wait: ``wsp collections fetch`` hashes each file and renames it
into place only when it is whole, so a file that exists is finished (a lane that ingested right after
fetching used to see every file as "modified moments ago" and silently ingest nothing). A version is ingested
only when every planned file is complete (or already staged by an earlier, interrupted run):

1. expected checksums: the plan's; for Kiwix metalink items (``checksum: null``) the ``.zim.sha256``
   Kiwix publishes, fetched through the ``kiwix-opds`` adapter; otherwise the file is *unverifiable*
   (the ``.meta4`` files next to downloads are never stored);
2. ``Vault.adopt`` hashes each file in place, checks it and renames it into staging;
3. ``seal`` (signed manifest), ledger events, ``promote`` unless a newer generation is current
   (for a partitioned source, ADR-0010: a newer generation *of the same partition*);
4. ``README.txt``/``CATALOG.*`` are regenerated.

Idempotent and resumable: sealed generations are skipped, staged files are recognised (a staged file
that differs from a re-fetched one is replaced only by bytes that verify against the plan's checksum).
Nothing is ever deleted; files that fail verification stay where they are. ``run(version=...)`` (``wsp
ingest --source S --version V``) plans and ingests that one incoming version, so a lane learns whether
*its* cycle landed and one stuck version cannot fail the others. Kiwix multi-book sources (a bundle
selector) are one generation across all their version folders, named after the newest version.
``_incoming/gutenberg/mirror/`` (an rsync tree) is only ingested with ``--source gutenberg
--allow-mirror`` while no rsync process runs, by copy so the mirror stays usable for the next rsync.

**arXiv PDF mirror** (``_incoming/arxiv/<snapshot>/{pdf/<archive>/<yymm>/*.pdf,manifests/<chunk>.jsonl}``,
written by ``tools/arxiv_pull.py``; millions of files, still growing while the pull runs). It is not
in ``targets.json``; :func:`plan_arxiv` reads it directly. The incremental model:

* the unit is a **chunk** (one archive-month, ``acc-phys-9411`` or ``arxiv-2301``) and each chunk
  becomes its own sealed **part generation** ``<snapshot>.pdf.<chunk>``; the snapshot's content is
  the union of its parts, exactly as its manifest is the union of the chunk manifests;
* a chunk is ready only when the pull recorded it as finished in ``done-<snapshot>.txt`` in its
  local state directory (``$WSP_ARXIV_STATE_DIR``, else ``$XDG_STATE_HOME/whisipedia/arxiv``, else
  ``~/.local/state/whisipedia/arxiv``). Chunks still downloading are never touched, so ingest can
  run while the pull continues; without a done file, a chunk is ready when every manifest entry
  exists with its size and no ``.aria2`` control file;
* each file is adopted by **rename** after checking the manifest's MD5 and computing our
  SHA-256/BLAKE3 (``FileVault.adopt_many``: hashing on threads, batched staging record); the chunk
  manifest is kept as ``upstream/manifests/<chunk>.jsonl``; then the part is sealed. Parts are never
  promoted (``CURRENT.txt`` names the metadata generation that the indexer reads);
* resumable at any point: sealed parts are skipped, staged files are re-hashed and recorded.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, TextIO, cast

from wsp_adapters.kiwix import KiwixOpdsAdapter, Selector, checksum_dirs, saved_checksum
from wsp_cli import nasretry
from wsp_core import diag
from wsp_core.model import Checksum, Generation, SourceSpec, SourceStatus
from wsp_core.ports import Ctx, Fetcher
from wsp_vault.atomic import IntegrityError
from wsp_vault.docs import write_library_docs
from wsp_vault.vault import ADOPTING_SUFFIX, FileVault, VaultError

ARXIV_SOURCE = "arxiv"
PLANS_DIR = "_plans"
TARGETS_FILE = "targets.json"
TARGETS_DIR = "targets.d"  # per-source plans (`wsp collections fetch`): same format as targets.json
CONTROL_SUFFIX = ".aria2"
MIRROR_SOURCE = "gutenberg"
MIRROR_DIR = "mirror"
ALGORITHMS = {"sha-1": "sha1", "sha1": "sha1", "sha-256": "sha256", "sha256": "sha256", "md5": "md5"}
_YYYYMMDD = re.compile(r"^(\d{4})(\d{2})(\d{2})$")
_YYMMDD = re.compile(r"^(\d{2})(\d{2})(\d{2})$")  # OSM planet files (planet-YYMMDD.osm.pbf)
_DATED = re.compile(r"^\d{4}-\d{2}(-\d{2})?$")
PROGRESS = logging.getLogger("wsp.ingest.progress")


class FileState(StrEnum):
    COMPLETE = "complete"
    STAGED = "staged"  # already moved into staging by an earlier run
    DOWNLOADING = "downloading"  # aria2 control file present
    SIZE_MISMATCH = "size-mismatch"
    SETTLING = "settling"  # modified too recently
    MISSING = "missing"


DONE_STATES = frozenset({FileState.COMPLETE, FileState.STAGED})


ATOMIC_LANE = "collections"  # written by `wsp collections fetch`: renamed into place only when complete


class VersionStatus(StrEnum):
    READY = "ready"  # complete, would be (or was) ingested
    INGESTED = "ingested"
    SEALED = "already-sealed"
    INCOMPLETE = "incomplete"
    FAILED = "failed"
    SKIPPED = "skipped"
    YIELDED = "yielded"  # stopped after max_seconds to let other ingests run; the next run goes on


@dataclass(frozen=True, slots=True)
class Target:
    source_id: str
    version: str
    name: str
    uris: tuple[str, ...]
    lane: str
    checksum: Checksum | None
    size: int | None
    meta: Mapping[str, object]

    @property
    def relpath(self) -> str:
        """Where the file goes in the generation: ``upstream/<name>``, or ``meta.relpath`` (catalogs)."""
        rel = str(self.meta.get("relpath") or "")
        if rel and not rel.startswith("/") and ".." not in rel.split("/"):
            return rel
        return f"upstream/{self.name}"

    @property
    def is_kiwix_metalink(self) -> bool:
        return self.checksum is None and any(u.endswith(".meta4") for u in self.uris)

    @property
    def kiwix_file_url(self) -> str | None:
        meta4 = next((u for u in self.uris if u.endswith(".meta4")), None)
        return meta4.removesuffix(".meta4") if meta4 else None


def load_targets(path: Path) -> list[Target]:
    raw = cast(list[Mapping[str, Any]], json.loads(path.read_text(encoding="utf-8")))
    out: list[Target] = []
    for item in raw:
        ck = item.get("checksum")
        checksum = None
        if isinstance(ck, list) and len(cast(list[object], ck)) == 2:
            algo, hexdigest = (str(x) for x in cast(list[object], ck))
            checksum = Checksum(ALGORITHMS.get(algo.lower(), algo.lower()), hexdigest.lower())
        size = item.get("size")
        out.append(
            Target(
                source_id=str(item["source_id"]),
                version=str(item["version"]),
                name=str(item["name"]),
                uris=tuple(str(u) for u in cast(list[object], item.get("uris") or [])),
                lane=str(item.get("lane", "")),
                checksum=checksum,
                size=int(size) if isinstance(size, int) else None,
                meta=cast(Mapping[str, object], item.get("meta") or {}),
            )
        )
    return out


@dataclass
class FilePlan:
    target: Target
    path: Path  # in _incoming
    state: FileState
    checksums: tuple[Checksum, ...] = ()
    note: str = ""

    @property
    def verifiable(self) -> bool:
        return bool(self.checksums)


@dataclass
class VersionPlan:
    source_id: str
    generation_id: str
    versions: tuple[str, ...]  # incoming version folders it draws from
    files: list[FilePlan] = field(default_factory=list[FilePlan])
    status: VersionStatus = VersionStatus.INCOMPLETE
    reason: str = ""
    ignored: int = 0  # files in the folders that are not in the plan (.meta4, .aria2, ...)
    promoted: bool = False

    def count(self, *states: FileState) -> int:
        return sum(f.state in states for f in self.files)

    @property
    def complete(self) -> bool:
        return bool(self.files) and all(f.state in DONE_STATES for f in self.files)

    @property
    def bytes(self) -> int:
        return sum(f.target.size or 0 for f in self.files)

    def to_json(self) -> dict[str, object]:
        return {
            "source_id": self.source_id,
            "generation_id": self.generation_id,
            "versions": list(self.versions),
            "status": self.status.value,
            "reason": self.reason,
            "files": len(self.files),
            "states": {s.value: self.count(s) for s in FileState if self.count(s)},
            "verifiable": sum(f.verifiable for f in self.files if f.state in DONE_STATES),
            "unverifiable": sum(not f.verifiable for f in self.files if f.state in DONE_STATES),
            "planned_bytes": self.bytes,
            "ignored_files": self.ignored,
            "promoted": self.promoted,
        }


@dataclass
class IngestReport:
    dry_run: bool
    versions: list[VersionPlan] = field(default_factory=list[VersionPlan])
    notes: list[str] = field(default_factory=list[str])

    def to_json(self) -> dict[str, object]:
        return {
            "dry_run": self.dry_run,
            "versions": [v.to_json() for v in self.versions],
            "notes": self.notes,
        }


def generation_id_for(version: str) -> str:
    """Upstream version label -> generation id: dates become ISO ``YYYY-MM-DD`` (matching the adapters)."""
    if m := _YYYYMMDD.match(version):
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    if m := _YYMMDD.match(version):
        return f"20{m.group(1)}-{m.group(2)}-{m.group(3)}"
    return version


def rsync_running(proc: Path = Path("/proc")) -> bool:
    for entry in proc.iterdir() if proc.is_dir() else ():
        if entry.name.isdigit():
            try:
                if (entry / "comm").read_text(encoding="utf-8").strip() == "rsync":
                    return True
            except OSError:
                continue
    return False


def _is_bundle(spec: SourceSpec) -> bool:
    if spec.adapter != "kiwix-opds":
        return False
    try:
        return not Selector.from_spec(spec).single_book
    except ValueError:
        return False


class Ingestor:
    def __init__(
        self,
        vault: FileVault,
        specs: Sequence[SourceSpec],
        *,
        fetcher: Fetcher | None = None,
        out: TextIO | None = None,
        settle_seconds: float = 120.0,
        now: Callable[[], float] = time.time,
        rsync_check: Callable[[], bool] = rsync_running,
        user_agent: str = "Whisipedia ingest",
        arxiv_state_dir: Path | None = None,
        hash_workers: int = 4,
        max_seconds: float | None = None,
    ) -> None:
        self.vault = vault
        self.specs = {s.id: s for s in specs}
        self.fetcher = fetcher
        self.out = out
        self.settle_seconds = settle_seconds
        self.now = now
        self.rsync_check = rsync_check
        self.user_agent = user_agent
        self.incoming = vault.root / "_incoming"
        self.arxiv_state_dir = arxiv_state_dir if arxiv_state_dir is not None else default_arxiv_state_dir()
        self.hash_workers = hash_workers
        # One ingest holds the machine's ingest lock: a version of 22,000 files held it for hours while
        # every lane's ingest waited (2026-10-21). With max_seconds an ingest stops after the file in
        # hand once that long has passed (adoption resumes; the seal comes when all are in).
        self.max_seconds = max_seconds
        self._started = time.monotonic()

    def _say(self, text: str) -> None:
        PROGRESS.info(text)  # the run log has every progress line, timestamped
        if self.out is not None:
            self.out.write(text + "\n")
            self.out.flush()

    # --- planning (read-only) ------------------------------------------------------------------

    def plan(self, source_id: str | None = None, version: str | None = None) -> IngestReport:
        """Read-only plan. ``version`` (with ``source_id``): only that incoming version folder, the
        one a lane just fetched; other versions of the source are neither reported nor touched, so
        one stuck version cannot fail them (audit 2026-10-12, H3)."""
        if version is not None and source_id is None:
            raise ValueError("a version needs its source")
        report = IngestReport(dry_run=True)
        targets_path = self.incoming / PLANS_DIR / TARGETS_FILE
        extra_plans = sorted((self.incoming / PLANS_DIR / TARGETS_DIR).glob("*.json"))
        targets: list[Target] = []
        for path in ([targets_path] if targets_path.is_file() else []) + extra_plans:
            targets += [
                t
                for t in load_targets(path)
                if source_id in (None, t.source_id) and version in (None, t.version)
            ]
        if not targets_path.is_file() and not extra_plans:
            report.notes.append(f"no download plan at {targets_path}")
        elif version is not None and not targets:
            report.notes.append(f"{source_id}/{version}: nothing planned (no download plan names it)")
        groups: dict[tuple[str, str], list[Target]] = {}
        for t in targets:
            spec = self.specs.get(t.source_id)
            key = (t.source_id, "*" if spec is not None and _is_bundle(spec) else t.version)
            groups.setdefault(key, []).append(t)
        # Not ``version``: the loop variable shadowed the parameter, so the checks below (arXiv, the
        # unplanned folders) ran for the last group's version (2026-10-26 review).
        for (sid, group_version), group in sorted(groups.items()):
            report.versions.append(self._plan_group(sid, group_version, group))
        planned_dirs = {(t.source_id, t.version) for t in targets}
        if self.incoming.is_dir():
            for src in sorted(p for p in self.incoming.iterdir() if p.is_dir() and p.name != PLANS_DIR):
                if source_id not in (None, src.name):
                    continue
                for ver in sorted(p for p in src.iterdir() if p.is_dir()):
                    if version not in (None, ver.name):
                        continue
                    if (
                        (src.name, ver.name) not in planned_dirs
                        and not (src.name == MIRROR_SOURCE and ver.name == MIRROR_DIR)
                        and src.name != ARXIV_SOURCE
                    ):
                        report.notes.append(f"{src.name}/{ver.name}: folder has no entries in targets.json")
        if source_id in (None, ARXIV_SOURCE) and version is None and ARXIV_SOURCE in self.specs:
            arxiv_root = self.incoming / ARXIV_SOURCE
            report.versions.extend(plan_arxiv(self.vault, arxiv_root, self.arxiv_state_dir))
        return report

    def _plan_group(self, sid: str, version: str, group: list[Target]) -> VersionPlan:
        versions = tuple(sorted({t.version for t in group}))
        spec = self.specs.get(sid)
        if version == "*":
            dated = [v for v in versions if _DATED.match(generation_id_for(v))]
            gid = generation_id_for(max(dated)) if dated else versions[-1]
        else:
            gid = generation_id_for(version)
        vp = VersionPlan(sid, gid, versions)
        vp.ignored = self._ignored(sid, versions, {t.name for t in group})
        if spec is None:
            vp.status, vp.reason = VersionStatus.SKIPPED, "source is not in the registry"
            vp.files = [FilePlan(t, self._path(t), FileState.MISSING) for t in group]
            self._classify(vp, group, staged_dir=None)
            return vp
        if spec.status is SourceStatus.EXCLUDED:
            vp.status, vp.reason = VersionStatus.SKIPPED, "source is excluded"
            return vp
        if gid == "latest":
            gid = self._latest_generation(group)
            vp.generation_id = gid
        if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]*$", gid) or gid == "unknown":
            vp.status, vp.reason = VersionStatus.SKIPPED, f"cannot derive a generation id from {version!r}"
            self._classify(vp, group, staged_dir=None)
            return vp
        lib = self.vault.library_path(sid, gid)
        if (lib / "MANIFEST.json").is_file():
            vp.status = VersionStatus.SEALED
            self._classify(vp, group, staged_dir=lib)
            leftovers = sum(f.path.exists() for f in vp.files)
            if leftovers:
                # Adoption renames files out of _incoming, so a file still there after the seal was
                # fetched again for a sealed version: it was never ingested (2026-10-12 audit, CA-15).
                vp.status = VersionStatus.FAILED
                vp.reason = (
                    f"{leftovers} file(s) in _incoming were fetched after {gid} was sealed and are not "
                    f"ingested; fetch again under a new version (wsp collections fetch picks one)"
                )
            return vp
        self._classify(vp, group, staged_dir=self.vault.staging_path(sid, gid))
        if vp.complete:
            vp.status = VersionStatus.READY
        else:
            waiting = len(vp.files) - vp.count(*DONE_STATES)
            vp.reason = f"{waiting} of {len(vp.files)} file(s) not complete"
        return vp

    def _path(self, t: Target) -> Path:
        return self.incoming / t.source_id / t.version / t.name

    def _ignored(self, sid: str, versions: Sequence[str], names: set[str]) -> int:
        n = 0
        for v in versions:
            folder = self.incoming / sid / v
            if folder.is_dir():
                n += sum(1 for p in folder.iterdir() if p.is_file() and p.name not in names)
        return n

    def _latest_generation(self, group: Sequence[Target]) -> str:
        mtimes = [p.stat().st_mtime for t in group if (p := self._path(t)).is_file()]
        if not mtimes:
            return "latest"
        return datetime.fromtimestamp(max(mtimes), UTC).strftime("%Y-%m-%d")

    def _classify(self, vp: VersionPlan, group: Sequence[Target], *, staged_dir: Path | None) -> None:
        files: list[FilePlan] = []
        for t in group:
            path = self._path(t)
            state, note = self._state(t, path)
            # Moved into staging by an earlier run: adopted already, or still under its private
            # ``.adopting`` name because that run was stopped while hashing it (the vault finishes it,
            # checked against the plan's checksum; ERA5 2025's precipitation, 2026-10-21).
            if state is FileState.MISSING and staged_dir is not None:
                staged = staged_dir / t.relpath
                if staged.is_file() or staged.with_name(staged.name + ADOPTING_SUFFIX).is_file():
                    state = FileState.STAGED
            checksums = (t.checksum,) if t.checksum is not None else ()
            files.append(FilePlan(t, path, state, checksums, note))
        vp.files = files

    def _state(self, t: Target, path: Path) -> tuple[FileState, str]:
        try:
            st = path.stat()
        except FileNotFoundError:
            return FileState.MISSING, ""
        if path.with_name(path.name + CONTROL_SUFFIX).exists():
            return FileState.DOWNLOADING, "aria2 control file present"
        if t.size is not None and st.st_size != t.size:
            return FileState.SIZE_MISMATCH, f"{st.st_size} of {t.size} bytes"
        if t.lane != ATOMIC_LANE and self.now() - st.st_mtime < self.settle_seconds:
            return FileState.SETTLING, "modified moments ago"
        return FileState.COMPLETE, ""

    # --- checksums -----------------------------------------------------------------------------

    def resolve_checksums(self, vp: VersionPlan) -> None:
        """Fill in Kiwix ``.zim.sha256`` digests for complete metalink items (needs a fetcher)."""
        spec = self.specs.get(vp.source_id)
        todo = [
            f for f in vp.files if not f.checksums and f.target.is_kiwix_metalink and f.state in DONE_STATES
        ]
        if spec is None or not todo:
            return
        # Kiwix's .sha256 files saved by the owner (or recorded from the vault) first: Kiwix's own host
        # refuses robots since 2026-10, so a fetched .sha256 is no longer the way (review split item).
        dirs = checksum_dirs()
        for f in todo:
            url = f.target.kiwix_file_url
            digest = saved_checksum(url.rsplit("/", 1)[-1], dirs) if url else None
            if digest is not None:
                f.checksums = (Checksum("sha256", digest),)
        todo = [f for f in todo if not f.checksums]
        if not todo:
            return
        if self.fetcher is None:
            for f in todo:
                f.note = "offline: Kiwix .sha256 not fetched"
            return
        adapter = KiwixOpdsAdapter()
        with tempfile.TemporaryDirectory(prefix="wsp-ingest-") as tmp:
            ctx = Ctx(source=spec, fetcher=self.fetcher, workdir=Path(tmp), user_agent=self.user_agent)
            for f in todo:
                url = f.target.kiwix_file_url
                f.checksums = adapter.upstream_checksums(ctx, url) if url else ()
                if not f.checksums:
                    f.note = "Kiwix publishes no .sha256 for this file"

    # --- execution -----------------------------------------------------------------------------

    def run(
        self, *, source_id: str | None = None, version: str | None = None, dry_run: bool = False
    ) -> IngestReport:
        report = self.plan(source_id, version)
        report.dry_run = dry_run
        changed = False
        for vp in report.versions:
            if any(v.status is VersionStatus.YIELDED for v in report.versions):
                break  # out of time: the rest waits for the next run
            if vp.status is VersionStatus.READY:
                if isinstance(vp, ArxivSnapshotPlan):
                    if not dry_run:
                        changed |= self._ingest_arxiv(vp)
                    continue
                self.resolve_checksums(vp)
                if not dry_run:
                    with diag.phase(
                        "ingest-version", source=vp.source_id, generation=vp.generation_id
                    ) as info:
                        changed |= self._ingest(vp)
                        info.update(status=vp.status.value, files=len(vp.files), bytes=vp.bytes)
        if not dry_run and changed:
            write_library_docs(self.vault, list(self.specs.values()))
        return report

    def _ingest(self, vp: VersionPlan) -> bool:
        sid, gid = vp.source_id, vp.generation_id
        spec = self.specs[sid]
        self._say(f"{sid}/{gid}: adopting {len(vp.files)} file(s)")
        try:
            self.vault.stage(sid, gid)
            for f in vp.files:
                if self.max_seconds is not None and time.monotonic() - self._started > self.max_seconds:
                    vp.status, vp.reason = VersionStatus.YIELDED, f"stopped after {self.max_seconds:.0f} s"
                    self._say(f"  yielded after {self.max_seconds:.0f} s; run again to continue")
                    return False
                started = time.monotonic()
                # A verified file replaces a different one staged by an interrupted ingest of the same
                # version (re-fetched since: the plan is the newer truth, H2); unverifiable ones never.
                # The share refused for a moment (ECONNABORTED, EAGAIN) mid-adoption: tried again
                # (adopt resumes from any step); a NARA partition of 401 files failed on one, and its
                # lane fetched it all again (2026-10-28).
                nasretry.retrying(
                    f"adopt {f.target.name}",
                    lambda f=f: self.vault.adopt(
                        sid, gid, f.path, f.target.relpath, upstream=f.checksums, replace_staged=f.verifiable
                    ),
                )
                f.state = FileState.STAGED
                how = "verified" if f.verifiable else "unverifiable"
                self._say(f"  {f.target.name}: {how} ({time.monotonic() - started:.1f}s)")
            with diag.phase("seal", source=sid, generation=gid):
                gen = self.vault.seal(sid, gid, parity_percent=10 if spec.irreplaceable else None)
        except (IntegrityError, VaultError, OSError) as exc:
            vp.status, vp.reason = VersionStatus.FAILED, f"{type(exc).__name__}: {exc}"
            self._say(f"  FAILED: {vp.reason} (files left in place)")
            return False
        vp.status = VersionStatus.INGESTED
        vp.promoted = self._maybe_promote(gen)
        if self.vault.ledger is not None:
            self.vault.ledger.append(
                "ingest.adopted",
                f"{sid}/{gid}",
                {
                    "incoming_versions": list(vp.versions),
                    "files": len(vp.files),
                    "verified": sum(f.verifiable for f in vp.files),
                    "unverifiable": [f.target.name for f in vp.files if not f.verifiable],
                    "promoted": vp.promoted,
                },
            )
        return True

    def _ingest_arxiv(self, vp: ArxivSnapshotPlan) -> bool:
        """Seal every ready chunk of an arXiv snapshot as its own part generation."""
        sid = vp.source_id
        sealed = 0
        for chunk in vp.chunks:
            if chunk.state is not ChunkState.READY:
                continue
            gid = chunk.generation_id
            started = time.monotonic()
            manifest = vp.snapshot_dir / "manifests" / f"{chunk.name}.jsonl"
            try:
                items = list(chunk.items(vp.snapshot_dir))
                self.vault.stage(sid, gid)
                n = self.vault.adopt_many(sid, gid, items, workers=self.hash_workers)
                self.vault.adopt(sid, gid, manifest, f"upstream/manifests/{manifest.name}")
                self.vault.seal(sid, gid)
            except (IntegrityError, VaultError, OSError, ValueError) as exc:
                chunk.state = ChunkState.FAILED
                vp.status, vp.reason = VersionStatus.FAILED, f"{chunk.name}: {type(exc).__name__}: {exc}"
                self._say(f"  {sid}/{gid}: FAILED {vp.reason} (files left in place)")
                break
            chunk.state = ChunkState.SEALED
            sealed += 1
            self._say(f"  {sid}/{gid}: {n} files verified and sealed ({time.monotonic() - started:.1f}s)")
            if self.vault.ledger is not None:
                self.vault.ledger.append(
                    "ingest.adopted",
                    f"{sid}/{gid}",
                    {"incoming_versions": [vp.snapshot], "chunk": chunk.name, "files": n, "promoted": False},
                )
        failed = vp.status is VersionStatus.FAILED
        vp.refresh()
        if not failed:
            vp.status = VersionStatus.INGESTED
        return sealed > 0

    def _maybe_promote(self, gen: Generation) -> bool:
        """Promote unless a newer generation is current (for a partitioned source, ADR-0010: a newer
        generation *of the same partition*): an older backfill must not replace it. Natural order
        (``-r10`` after ``-r4``), checked under the pointer lock with the write (audit 2026-10-12, M3).
        """
        return self.vault.promote_if_newer(gen.source_id, gen.generation_id)

    # --- rsync mirror (Gutenberg) --------------------------------------------------------------

    def ingest_mirror(self, *, dry_run: bool) -> VersionPlan:
        root = self.incoming / MIRROR_SOURCE / MIRROR_DIR
        gid = datetime.now(UTC).strftime("%Y-%m-%d")
        vp = VersionPlan(MIRROR_SOURCE, gid, (MIRROR_DIR,))
        if MIRROR_SOURCE not in self.specs:
            vp.status, vp.reason = VersionStatus.SKIPPED, "source is not in the registry"
            return vp
        if not root.is_dir():
            vp.status, vp.reason = VersionStatus.SKIPPED, f"no mirror at {root}"
            return vp
        if self.rsync_check():
            vp.status, vp.reason = VersionStatus.SKIPPED, "an rsync process is running"
            return vp
        files = sorted(_walk_files(root))
        vp.files = [
            FilePlan(
                Target(MIRROR_SOURCE, MIRROR_DIR, p.name, (), "rsync", None, None, {}), p, FileState.COMPLETE
            )
            for p in files
        ]
        if (self.vault.library_path(MIRROR_SOURCE, gid) / "MANIFEST.json").is_file():
            vp.status = VersionStatus.SEALED
            return vp
        vp.status = VersionStatus.READY
        if dry_run:
            return vp
        try:
            self.vault.stage(MIRROR_SOURCE, gid)
            for i, f in enumerate(vp.files, start=1):
                rel = f.path.relative_to(root).as_posix()
                self.vault.ingest(MIRROR_SOURCE, gid, f.path, f"upstream/{rel}")  # copy: keep the mirror
                if i % 1000 == 0:
                    self._say(f"  {i:,}/{len(vp.files):,} files")
            gen = self.vault.seal(MIRROR_SOURCE, gid)
        except (IntegrityError, VaultError, OSError) as exc:
            vp.status, vp.reason = VersionStatus.FAILED, f"{type(exc).__name__}: {exc}"
            return vp
        vp.status = VersionStatus.INGESTED
        vp.promoted = self._maybe_promote(gen)
        write_library_docs(self.vault, list(self.specs.values()))
        return vp


# --- arXiv PDF mirror ------------------------------------------------------------------------------


def default_arxiv_state_dir() -> Path:
    """Where ``tools/arxiv_pull.py`` records finished chunks (local disk, never the NAS)."""
    if env := os.environ.get("WSP_ARXIV_STATE_DIR"):
        return Path(env)
    base = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    return base / "whisipedia" / "arxiv"


class ChunkState(StrEnum):
    READY = "ready"
    SEALED = "sealed"
    IN_PROGRESS = "in-progress"
    FAILED = "failed"


@dataclass
class ArxivChunk:
    name: str  # <archive>-<yymm>
    generation_id: str
    state: ChunkState
    files: int = 0
    bytes: int = 0
    manifest: Path | None = None  # in _incoming, or already staged after an interrupted run

    def items(self, snapshot_dir: Path) -> Iterator[tuple[Path, str, Sequence[Checksum]]]:
        """``(incoming path, relpath, [md5])`` for every manifest entry."""
        if self.manifest is None:
            return
        for path, _size, md5 in read_chunk_manifest(self.manifest):
            yield snapshot_dir / path, f"upstream/{path}", (Checksum("md5", md5),)


def read_chunk_manifest(path: Path) -> list[tuple[str, int, str]]:
    """``(path, size, md5)`` of a chunk manifest; paths must stay under ``pdf/``."""
    out: list[tuple[str, int, str]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        item = cast(Mapping[str, Any], json.loads(line))
        rel = str(item["path"])
        if not rel.startswith("pdf/") or ".." in rel.split("/"):
            raise ValueError(f"{path.name}: unsafe path {rel!r}")
        out.append((rel, int(item["size"]), str(item["md5"]).lower()))
    return out


_CHUNK_FILE_STATE = {
    ChunkState.SEALED: FileState.STAGED,
    ChunkState.READY: FileState.COMPLETE,
    ChunkState.IN_PROGRESS: FileState.DOWNLOADING,
    ChunkState.FAILED: FileState.COMPLETE,
}


@dataclass
class ArxivSnapshotPlan(VersionPlan):
    """One arXiv snapshot folder. Its rows ("files") are chunks; each becomes a part generation."""

    snapshot_dir: Path = field(default_factory=Path)
    snapshot: str = ""
    chunks: list[ArxivChunk] = field(default_factory=list[ArxivChunk])

    def refresh(self) -> None:
        self.files = [
            FilePlan(
                Target(self.source_id, self.snapshot, c.name, (), "arxiv-pull", None, c.bytes or None, {}),
                self.snapshot_dir / "manifests" / f"{c.name}.jsonl",
                _CHUNK_FILE_STATE[c.state],
                (Checksum("md5", "per-file"),),  # every PDF is checked against the manifest's MD5
            )
            for c in self.chunks
        ]
        count = {s: sum(c.state is s for c in self.chunks) for s in ChunkState}
        ready = [c for c in self.chunks if c.state is ChunkState.READY]
        self.reason = (
            f"chunks: {count[ChunkState.SEALED]} sealed, {len(ready)} ready "
            f"({sum(c.files for c in ready):,} PDFs, {sum(c.bytes for c in ready) / 1e9:.1f} GB), "
            f"{count[ChunkState.IN_PROGRESS]} in progress, {count[ChunkState.FAILED]} failed"
        )
        if ready:
            self.status = VersionStatus.READY
        elif count[ChunkState.FAILED]:
            self.status = VersionStatus.FAILED
        elif self.chunks and not count[ChunkState.IN_PROGRESS]:
            self.status = VersionStatus.SEALED
        else:
            self.status = VersionStatus.INCOMPLETE


def _chunk_complete_on_disk(snapshot_dir: Path, manifest: Path) -> bool:
    """Readiness without pull state: every entry present with its size, no aria2 control file."""
    for rel, size, _md5 in read_chunk_manifest(manifest):
        path = snapshot_dir / rel
        try:
            if path.stat().st_size != size or path.with_name(path.name + CONTROL_SUFFIX).exists():
                return False
        except FileNotFoundError:
            return False
    return True


def plan_arxiv(vault: FileVault, root: Path, state_dir: Path) -> list[ArxivSnapshotPlan]:
    """Read-only plan of every arXiv snapshot under ``_incoming/arxiv`` (see the module docstring)."""
    plans: list[ArxivSnapshotPlan] = []
    if not root.is_dir():
        return plans
    parts = [g.generation_id for g in vault.generations(ARXIV_SOURCE)]
    for snap in sorted(p for p in root.iterdir() if p.is_dir()):
        manifests = snap / "manifests"
        done_file = state_dir / f"done-{snap.name}.txt"
        done = set(done_file.read_text(encoding="utf-8").split()) if done_file.is_file() else None
        prefix = f"{snap.name}.pdf."
        names = {p.stem for p in manifests.glob("*.jsonl")} if manifests.is_dir() else set[str]()
        names |= {g.removeprefix(prefix) for g in parts if g.startswith(prefix)}
        vp = ArxivSnapshotPlan(ARXIV_SOURCE, snap.name, (snap.name,), snapshot_dir=snap, snapshot=snap.name)
        for name in sorted(names):
            gid = prefix + name
            manifest = manifests / f"{name}.jsonl"
            staged = vault.staging_path(ARXIV_SOURCE, gid) / "upstream" / "manifests" / f"{name}.jsonl"
            if (vault.library_path(ARXIV_SOURCE, gid) / "MANIFEST.json").is_file():
                vp.chunks.append(ArxivChunk(name, gid, ChunkState.SEALED))
            elif staged.is_file():  # interrupted after the manifest was adopted: finish it
                entries = read_chunk_manifest(staged)
                size = sum(e[1] for e in entries)
                vp.chunks.append(ArxivChunk(name, gid, ChunkState.READY, len(entries), size, staged))
            elif not manifest.is_file():
                vp.chunks.append(ArxivChunk(name, gid, ChunkState.IN_PROGRESS))
            elif (name in done) if done is not None else _chunk_complete_on_disk(snap, manifest):
                entries = read_chunk_manifest(manifest)
                size = sum(e[1] for e in entries)
                vp.chunks.append(ArxivChunk(name, gid, ChunkState.READY, len(entries), size, manifest))
            else:
                vp.chunks.append(ArxivChunk(name, gid, ChunkState.IN_PROGRESS))
        vp.refresh()
        if done is None:
            vp.reason += "; no pull state file, files checked on disk"
        plans.append(vp)
    return plans


def _walk_files(root: Path) -> Iterator[Path]:
    for dirpath, _dirs, names in os.walk(root):
        for name in names:
            path = Path(dirpath) / name
            if path.is_file() and not path.is_symlink() and not name.endswith(".partial"):
                yield path


def render(report: IngestReport, out: TextIO) -> None:
    """Human summary: one line per source generation, then notes."""
    mode = "DRY RUN (nothing changed)" if report.dry_run else "ingest"
    out.write(f"wsp ingest: {mode}\n")
    header = (
        f"{'source':<24} {'generation':<12} {'status':<14} {'files':>5} {'done':>5} {'dl':>4} "
        f"{'miss':>5} {'verif.':>6} {'unverif.':>8}  note\n"
    )
    out.write(header)
    for vp in report.versions:
        done = vp.count(*DONE_STATES)
        waiting = vp.count(FileState.DOWNLOADING, FileState.SIZE_MISMATCH, FileState.SETTLING)
        missing = vp.count(FileState.MISSING)
        verif = sum(f.verifiable for f in vp.files if f.state in DONE_STATES)
        unverif = done - verif
        note = vp.reason
        if vp.promoted:
            note = (note + "; " if note else "") + "promoted"
        out.write(
            f"{vp.source_id:<24} {vp.generation_id:<12} {vp.status.value:<14} {len(vp.files):>5} {done:>5} "
            f"{waiting:>4} {missing:>5} {verif:>6} {unverif:>8}  {note}\n"
        )
    for note in report.notes:
        out.write(f"note: {note}\n")
