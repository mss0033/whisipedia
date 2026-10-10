"""``wsp ingest``: adopt completed bulk downloads from ``<vault>/_incoming/`` into sealed generations.

Bulk downloads (aria2c, rsync) land in ``<vault.root>/_incoming/<source_id>/<version>/<name>``, on the
same filesystem as the vault, so adoption is a rename, never a copy. The download plan is
``_incoming/_plans/targets.json``: a list of ``{source_id, version, name, uris, lane, checksum, size,
meta}`` where ``checksum`` is ``[algo, hex]`` (aria2 names: ``sha-1``, ``sha-256``, ``md5``) or null.

A file is **complete** when no sibling ``<name>.aria2`` control file exists, its size matches the plan
(when the plan knows it) and it has not been modified for ``settle_seconds``. A version is ingested
only when every planned file is complete (or already staged by an earlier, interrupted run):

1. expected checksums: the plan's; for Kiwix metalink items (``checksum: null``) the ``.zim.sha256``
   Kiwix publishes, fetched through the ``kiwix-opds`` adapter; otherwise the file is *unverifiable*
   (the ``.meta4`` files next to downloads are never stored);
2. ``Vault.adopt`` hashes each file in place, checks it and renames it into staging;
3. ``seal`` (signed manifest), ledger events, ``promote`` unless a newer generation is current;
4. ``README.txt``/``CATALOG.*`` are regenerated.

Idempotent and resumable: sealed generations are skipped, staged files are recognised. Nothing is
ever deleted; files that fail verification stay where they are. Kiwix multi-book sources (a bundle
selector) are one generation across all their version folders, named after the newest version.
``_incoming/gutenberg/mirror/`` (an rsync tree) is only ingested with ``--source gutenberg
--allow-mirror`` while no rsync process runs, by copy so the mirror stays usable for the next rsync.
"""

from __future__ import annotations

import json
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

from wsp_adapters.kiwix import KiwixOpdsAdapter, Selector
from wsp_core.model import Checksum, Generation, SourceSpec, SourceStatus
from wsp_core.ports import Ctx, Fetcher
from wsp_vault.atomic import IntegrityError
from wsp_vault.docs import write_library_docs
from wsp_vault.vault import FileVault, VaultError

PLANS_DIR = "_plans"
TARGETS_FILE = "targets.json"
CONTROL_SUFFIX = ".aria2"
MIRROR_SOURCE = "gutenberg"
MIRROR_DIR = "mirror"
ALGORITHMS = {"sha-1": "sha1", "sha1": "sha1", "sha-256": "sha256", "sha256": "sha256", "md5": "md5"}
_YYYYMMDD = re.compile(r"^(\d{4})(\d{2})(\d{2})$")
_YYMMDD = re.compile(r"^(\d{2})(\d{2})(\d{2})$")  # OSM planet files (planet-YYMMDD.osm.pbf)
_DATED = re.compile(r"^\d{4}-\d{2}(-\d{2})?$")


class FileState(StrEnum):
    COMPLETE = "complete"
    STAGED = "staged"  # already moved into staging by an earlier run
    DOWNLOADING = "downloading"  # aria2 control file present
    SIZE_MISMATCH = "size-mismatch"
    SETTLING = "settling"  # modified too recently
    MISSING = "missing"


DONE_STATES = frozenset({FileState.COMPLETE, FileState.STAGED})


class VersionStatus(StrEnum):
    READY = "ready"  # complete, would be (or was) ingested
    INGESTED = "ingested"
    SEALED = "already-sealed"
    INCOMPLETE = "incomplete"
    FAILED = "failed"
    SKIPPED = "skipped"


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

    def _say(self, text: str) -> None:
        if self.out is not None:
            self.out.write(text + "\n")
            self.out.flush()

    # --- planning (read-only) ------------------------------------------------------------------

    def plan(self, source_id: str | None = None) -> IngestReport:
        report = IngestReport(dry_run=True)
        targets_path = self.incoming / PLANS_DIR / TARGETS_FILE
        if not targets_path.is_file():
            report.notes.append(f"no download plan at {targets_path}")
            return report
        targets = [t for t in load_targets(targets_path) if source_id in (None, t.source_id)]
        groups: dict[tuple[str, str], list[Target]] = {}
        for t in targets:
            spec = self.specs.get(t.source_id)
            key = (t.source_id, "*" if spec is not None and _is_bundle(spec) else t.version)
            groups.setdefault(key, []).append(t)
        for (sid, version), group in sorted(groups.items()):
            report.versions.append(self._plan_group(sid, version, group))
        planned_dirs = {(t.source_id, t.version) for t in targets}
        if self.incoming.is_dir():
            for src in sorted(p for p in self.incoming.iterdir() if p.is_dir() and p.name != PLANS_DIR):
                if source_id not in (None, src.name):
                    continue
                for ver in sorted(p for p in src.iterdir() if p.is_dir()):
                    if (src.name, ver.name) not in planned_dirs and not (
                        src.name == MIRROR_SOURCE and ver.name == MIRROR_DIR
                    ):
                        report.notes.append(f"{src.name}/{ver.name}: folder has no entries in targets.json")
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
            self._classify(vp, group, staged_dir=lib / "upstream")
            leftovers = sum(f.path.exists() for f in vp.files)
            if leftovers:
                vp.reason = f"{leftovers} file(s) still in _incoming were left untouched"
            return vp
        self._classify(vp, group, staged_dir=self.vault.staging_path(sid, gid) / "upstream")
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
            if state is FileState.MISSING and staged_dir is not None and (staged_dir / t.name).is_file():
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
        if self.now() - st.st_mtime < self.settle_seconds:
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

    def run(self, *, source_id: str | None = None, dry_run: bool = False) -> IngestReport:
        report = self.plan(source_id)
        report.dry_run = dry_run
        changed = False
        for vp in report.versions:
            if vp.status is VersionStatus.READY:
                self.resolve_checksums(vp)
                if not dry_run:
                    changed |= self._ingest(vp)
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
                started = time.monotonic()
                self.vault.adopt(sid, gid, f.path, f"upstream/{f.target.name}", upstream=f.checksums)
                f.state = FileState.STAGED
                how = "verified" if f.verifiable else "unverifiable"
                self._say(f"  {f.target.name}: {how} ({time.monotonic() - started:.1f}s)")
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

    def _maybe_promote(self, gen: Generation) -> bool:
        current = self.vault.current(gen.source_id)
        if current is not None and current.generation_id > gen.generation_id:
            return False  # an older backfill must not replace a newer current generation
        self.vault.promote(gen.source_id, gen.generation_id)
        return True

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
