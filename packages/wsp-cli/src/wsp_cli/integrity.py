"""Source-integrity hooks for the real composition root (ADR-0009).

* ``attest``: :func:`wsp_vault.attest.write_attestation` (signed ``PROVENANCE.json`` /
  ``PROMOTION.json`` next to the manifest);
* ``profile``: a content profile of a sealed generation, from its Refinery Parquet (documents:
  namespaces, sizes, content hashes, titles) plus, for ZIM files, the archive's entries (MIME types;
  ``<script>`` density, binary and executable content in a deterministic sample). With a runner it is
  computed in a child unit (:class:`IsolatedProfiler`, audit C2): pyarrow and libzim never run in the
  daemon. Partitioned sources are read from ``refinery/<id>/_parts/<gid>`` (ADR-0010, audit M6);
  document sizes come from narrow, small batches of the ``text`` column (``utf8_length``), so memory
  stays bounded whatever the generation's size;
* ``scrubbed``: whether the rolling scrub has verified every file of a generation in its cycle;
* coverage and channel status for ``wsp doctor`` and the System Monitor.

A profile in a child unit saves its progress (:class:`ProfileCheckpoint`): a profile of a big ZIM
runs for days, and a restart of the machine used to throw all of it away (58 hours, 2026-11-10).

Everything runs on local disk except reading the vault; nothing here edits content.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
import os
import shutil
import sqlite3
import time
import zlib
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import closing, suppress
from dataclasses import dataclass, field, fields
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from wsp_cli import bootstrap
from wsp_cli.locks import PROFILE, exclusive, lock_dir_for
from wsp_core.memory import drop_file_cache
from wsp_core.model import Artifact, Generation, GenerationStatus, SourceSpec
from wsp_orchestrator.integrity import (
    ContentProfile,
    IntegrityHooks,
    IntegritySettings,
    ProfileBuilder,
    merge_profiles,
)
from wsp_orchestrator.integrity.anomaly import PROFILE_FORMAT
from wsp_orchestrator.isolation import IsolatedRunner
from wsp_vault.attest import PROVENANCE_NAME, attestation_state, write_attestation
from wsp_vault.scrub import Scrubber
from wsp_vault.vault import FileVault, SignatureError

log = logging.getLogger(__name__)

DEFAULT_SAMPLE_RATE = 1 / 32
PROFILE_MIN_RATE = 0.25e6
"""Bytes per second a paced ZIM profile is allowed at the slowest (Arabic Wikipedia read ~0.9 MB/s with
the NAS busy, 2026-10-26; German, 63 GB, was not done after 44 h, 2026-10-28); ``[integrity]
profile_min_rate``. A profile takes the PROFILE lock, not HEAVY: a long one holds no other job up."""
# Rows per batch when reading Refinery parts: the text column of 256 large documents (books) stays
# in the tens of MB, where 8,192 rows of Gutenberg could reach gigabytes.
PROFILE_BATCH_ROWS = 256


def _namespace(native_id: str) -> str:
    head, sep, _ = native_id.partition("/")
    return head if sep and len(head) <= 40 else "main"


class ReadPacer:
    """Paces a bulk reader by time: it works at most ``share`` of the wall clock, in bursts of about
    ``burst`` seconds, checked every ``every`` calls of :meth:`tick`.

    The profiler's reads happen inside pyarrow and libzim (a ZIM entry's content costs a whole
    compressed cluster), which do not say how many bytes they read, and this kernel keeps no
    per-process read counter (no ``/proc/self/io``). Unpaced they fill the NAS link, which the site
    reads documents over (2026-10-13: a profile of a 9 GB ZIM held it saturated for hours, and the
    health check restarted the API every nine minutes); ``ionice`` does not throttle a CIFS mount.
    Working a quarter of the time at the link's ~110 MB/s averages ~28 MB/s, and no burst holds the
    link longer than about half a second. ``share`` of ``None`` or ``>= 1``: unpaced.
    """

    def __init__(
        self,
        share: float | None,
        *,
        burst: float = 0.5,
        every: int = 16,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if share is not None and share <= 0:
            raise ValueError("share must be positive")
        self.share = share if share is not None and share < 1 else None
        self.burst = burst
        self.every = every
        self.clock = clock
        self.sleep = sleep
        self._calls = 0
        self._start: float | None = None

    def tick(self) -> None:
        if self.share is None:
            return
        self._calls += 1
        if self._calls % self.every:
            return
        now = self.clock()
        if self._start is None:
            self._start = now
            return
        worked = now - self._start
        if worked >= self.burst:
            self.sleep(worked * (1 - self.share) / self.share)
            self._start = self.clock()


UNPACED = ReadPacer(None)


def refinery_parts(gen_dir: Path) -> list[Path]:
    """The part files of a refined generation, in the order they are profiled."""
    return sorted(gen_dir.glob("part-*.parquet"))


def refinery_documents(
    gen_dir: Path,
    builder: ProfileBuilder,
    pacer: ReadPacer = UNPACED,
    *,
    resume: Callable[[int], int] | None = None,
    saved: Callable[[int], None] | None = None,
) -> int:
    """Add every refined document of a generation (sealed Refinery Parquet) to ``builder``.

    For a profile that saves its progress: ``resume(parts)`` is asked once how many part files
    ``builder`` already holds (they are skipped), and ``saved(done)`` is called after each part
    with the number of parts ``builder`` holds then. Returns the documents added by this call.
    """
    import pyarrow.compute as pc  # pyright: ignore[reportMissingTypeStubs]
    import pyarrow.parquet as pq  # pyright: ignore[reportMissingTypeStubs]

    n = 0
    utf8_length = cast(Callable[[Any], Any], pc.utf8_length)  # pyright: ignore[reportUnknownMemberType]
    parts = refinery_parts(gen_dir)
    start = resume(len(parts)) if resume is not None else 0
    for index, part in enumerate(parts):
        if index < start:
            continue
        try:
            n += _profile_part(part, builder, pacer, utf8_length, pq)
        finally:
            # Read once: on the NAS its pages stay charged to the step's memory ceiling (a profile
            # step held 2.6 GB of cache against 94 MB of heap, 2026-10-14).
            drop_file_cache(part)
        if saved is not None:
            saved(index + 1)
    return n


def _profile_part(
    part: Path, builder: ProfileBuilder, pacer: ReadPacer, utf8_length: Callable[[Any], Any], pq: Any
) -> int:
    n = 0
    pf = pq.ParquetFile(part)
    batches = pf.iter_batches(  # pyright: ignore[reportUnknownMemberType]
        columns=["native_id", "title", "content_hash", "text"], batch_size=PROFILE_BATCH_ROWS
    )
    for batch in batches:
        pacer.tick()
        ids = cast(list[str], batch.column(0).to_pylist())
        titles = cast(list[str], batch.column(1).to_pylist())
        hashes = cast(list[str], batch.column(2).to_pylist())
        sizes = cast(list[int], utf8_length(batch.column(3)).to_pylist())  # lengths only, no copies
        del batch
        for native_id, title, digest, size in zip(ids, titles, hashes, sizes, strict=True):
            builder.add(
                native_id,
                namespace=_namespace(native_id),
                size=int(size),
                title=title,
                content_hash=digest[:16],
            )
            n += 1
    return n


def zim_entries(
    path: Path,
    builder: ProfileBuilder,
    *,
    documents: bool,
    prefix: str = "",
    pacer: ReadPacer = UNPACED,
    resume: Callable[[int], int] | None = None,
    saved: Callable[[int], None] | None = None,
) -> int:
    """Entries of a ZIM: MIME types for all, raw bytes scanned for the deterministic sample.

    With ``documents=True`` (no Refinery output to use) every non-redirect entry is also counted as
    a document; sizes come from the sample only (reading every body would decompress the archive).

    For a profile that saves its progress: ``resume(entry_count)`` is asked once for the entry id
    to start at (``builder`` holds every entry before it), and ``saved(i)`` is called every
    :data:`CHECKPOINT_STRIDE` entries, before entry ``i`` is read. The archive's entry count is
    only known here, which is why the caller is asked instead of telling.
    """
    from libzim.reader import Archive  # pyright: ignore[reportMissingTypeStubs]

    archive = Archive(path)
    try:
        return _zim_entries(
            archive,
            builder,
            documents=documents,
            prefix=prefix,
            pacer=pacer,
            path=path,
            resume=resume,
            saved=saved,
        )
    finally:
        drop_file_cache(path)


ZIM_DROP_EVERY = 50_000
"""Entries between page-cache drops of a ZIM being profiled: often enough that a whole-archive read
does not sit in the step's memory, rarely enough that the API's pages of the same file (dropped too:
the cache is shared) are not evicted at every entry."""


CHECKPOINT_STRIDE = 4096
"""Entries of a ZIM between two looks at the checkpoint clock."""


def _zim_entries(
    archive: Any,
    builder: ProfileBuilder,
    *,
    documents: bool,
    prefix: str,
    pacer: ReadPacer,
    path: Path,
    resume: Callable[[int], int] | None = None,
    saved: Callable[[int], None] | None = None,
) -> int:
    """Entries are read by id, so the order is the same in every run: an entry id is a place to
    continue from. Returns the entries added by this call."""
    n = 0
    total = int(archive.entry_count)
    start = resume(total) if resume is not None else 0
    for i in range(start, total):
        if saved is not None and i > start and i % CHECKPOINT_STRIDE == 0:
            saved(i)  # everything before entry i is in the builder
        pacer.tick()
        if i and i % ZIM_DROP_EVERY == 0:
            drop_file_cache(path)
        entry = archive._get_entry_by_id(i)  # pyright: ignore[reportPrivateUsage, reportUnknownMemberType, reportUnknownVariableType]
        if entry.is_redirect:  # pyright: ignore[reportUnknownMemberType]
            continue
        item = entry.get_item()  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        key = prefix + str(entry.path)  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
        mime = str(item.mimetype)  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
        content = bytes(item.content) if builder.wants(key) else None  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
        if documents:
            builder.add(
                key,
                namespace=_namespace(key),
                mimetype=mime,
                size=None,
                title=str(entry.title),  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
                content=content,
            )
        else:
            builder.add_blob(key, mimetype=mime, content=content)
        n += 1
    return n


# --- a profile that continues where it stopped ------------------------------------------------------

CHECKPOINT_FORMAT = "whisipedia-profile-checkpoint/1"
"""Names what a saved part of a profile means. It is part of every checkpoint's fingerprint, so a
new value throws away every checkpoint on disk. Change it with ANY change to what a profile counts
or to the order it is made in: the sampling (``sample_key``, the sample rate rule), ``ProfileBuilder``
(``add``, ``add_blob``, ``_scan``, ``_bucket``, ``short_hash``), the entry walk of ``_zim_entries``,
the namespace rule (``_namespace``), the key prefix of a ZIM, the order of the units, or the files
written here. Otherwise a half made by the old rule is merged with a half made by the new one.
``test_the_profile_of_a_fixed_input_is_pinned`` fails when the result changes without it."""
CHECKPOINT_SECONDS = 300.0
"""Seconds between two saves inside one unit; ``[integrity] profile_checkpoint_seconds`` (0: a save
only when a unit ends)."""
CHECKPOINT_KEEP_DAYS = 30
"""A checkpoint nothing wrote to for this long is removed by the next profile of any source."""
CHECKPOINT_SLOWDOWN = 20.0
"""The next save is due no sooner than this many times the last save took: a save that got slow
(a big sample, a busy disk) never takes more than a twentieth of the run."""
_UNREADABLE = (
    OSError,
    EOFError,
    zlib.error,
    ValueError,
    LookupError,
    TypeError,
    AttributeError,
    ArithmeticError,  # int(1e999) of a garbled number
    RecursionError,  # JSON nested too deep
)
"""Whatever reading and parsing a checkpoint file can raise: all of it means "not there"."""


ASIDE_SUFFIX = ".aside"
"""A checkpoint whose fingerprint did not match is kept under this name, next to the one in use."""


def _fingerprint_at(root: Path) -> str | None:
    """The fingerprint of the checkpoint at ``root`` if it is of this format; never raises."""
    try:
        meta = json.loads((root / "meta.json").read_text(encoding="utf-8"))
        if isinstance(meta, dict) and meta.get("format") == CHECKPOINT_FORMAT:  # pyright: ignore[reportUnknownMemberType]
            return str(meta.get("fingerprint"))  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
    except _UNREADABLE:
        pass
    return None


def _write_atomic(path: Path, data: bytes) -> None:
    tmp = path.with_name(f"{path.name}.{os.getpid()}.partial")
    try:
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except OSError:
        with suppress(OSError):
            tmp.unlink()
        raise
    with suppress(OSError):
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _read_gz_json(path: Path) -> Mapping[str, Any]:
    doc = json.loads(gzip.decompress(path.read_bytes()))
    if not isinstance(doc, dict):
        raise TypeError("not a JSON object")
    return cast(Mapping[str, Any], doc)


def _gz_json(doc: Mapping[str, Any]) -> bytes:
    return gzip.compress(json.dumps(doc, sort_keys=True).encode(), compresslevel=3, mtime=0)


def zim_artifacts(generation: Generation) -> list[Artifact]:
    """The generation's ZIM files, in the order they are profiled."""
    return sorted(
        (a for a in generation.artifacts if str(a.relpath).endswith(".zim")), key=lambda a: str(a.relpath)
    )


def checkpoint_fingerprint(
    source_id: str, generation: Generation, gen_dir: Path | None, sample_rate: float
) -> str:
    """What a checkpoint was made from. Another value means its files are of no use.

    ``gen_dir`` is the sealed Refinery output read by unit 0, or ``None``. A generation refined again
    is seen by its files' times: ``_SUCCESS.json`` holds only counts, so the same documents refined
    again write the same seal, byte for byte. May raise ``OSError`` (a file that cannot be read).
    """
    refinery: dict[str, Any] | None = None
    if gen_dir is not None:
        parts: list[list[Any]] = []
        for part in refinery_parts(gen_dir):
            st = part.stat()
            parts.append([part.name, st.st_size, st.st_mtime_ns])
        ids: list[int] | None = None
        with suppress(FileNotFoundError):
            st = (gen_dir / "_ids.parquet").stat()
            ids = [st.st_size, st.st_mtime_ns]
        refinery = {
            "parts": parts,
            "ids": ids,
            "seal": hashlib.sha256((gen_dir / "_SUCCESS.json").read_bytes()).hexdigest(),
        }
    doc = {
        "checkpoint": CHECKPOINT_FORMAT,
        "profile": PROFILE_FORMAT,
        "source": source_id,
        "generation": generation.generation_id,
        "sample_rate": sample_rate,
        "refinery": refinery,
        "zims": [[str(a.relpath), a.size, a.sha256] for a in zim_artifacts(generation)],
    }
    return hashlib.sha256(json.dumps(doc, sort_keys=True).encode()).hexdigest()


@dataclass
class UnitCheckpoint:
    """One unit's two callables for a reader: :meth:`resume` and :meth:`saved`."""

    checkpoint: ProfileCheckpoint
    unit: int
    builder: ProfileBuilder
    total: int | None = None

    def resume(self, total: int) -> int:
        """Where to start, of ``total`` steps; the saved part (if it is of this ``total``) is put
        into the builder."""
        self.total = total
        found = self.checkpoint.partial(self.unit, total)
        if found is None:
            return 0
        cursor, restored = found
        for f in fields(ProfileBuilder):
            setattr(self.builder, f.name, getattr(restored, f.name))
        log.info("profile checkpoint: unit %d continues at %d of %d", self.unit, cursor, total)
        return cursor

    def saved(self, cursor: int) -> None:
        """The builder holds everything before ``cursor``: saved if a save is due."""
        if self.total is not None and self.checkpoint.due():
            self.checkpoint.save_partial(self.unit, cursor, self.total, self.builder)


@dataclass
class ProfileCheckpoint:
    """The progress of one generation's profile, on local disk, written by the profile child only
    (under the ``PROFILE`` lock: one writer).

    Unit 0 is the Refinery pass (an empty profile when there is none); units 1..n are the ZIM files
    in the order they are profiled. In the directory:

    * ``meta.json`` (plain JSON): format, fingerprint (:func:`checkpoint_fingerprint`), date;
    * ``unit-NNN.done.json.gz``: a finished unit's ``ContentProfile``;
    * ``unit-NNN.part.json.gz``: ``{"cursor", "total", "profile"}`` of the unit in progress, where
      ``cursor`` is the number of Refinery part files done (unit 0) or the next ZIM entry id.

    Every file is replaced atomically. The checkpoint only saves time: no error in reading or
    writing it may fail the profile (a failed profile holds the release for the owner), so a file
    that cannot be read is a unit to do again, and a save that fails is logged and forgotten. With
    ``root=None`` (the directory could not be used) nothing is read or written.
    """

    root: Path | None
    every: float = CHECKPOINT_SECONDS
    clock: Callable[[], float] = time.monotonic
    _next: float = field(default=0.0, repr=False)

    @classmethod
    def open(
        cls,
        root: Path,
        fingerprint: str,
        *,
        every: float = CHECKPOINT_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> ProfileCheckpoint:
        """The checkpoint at ``root``, emptied first unless it was made from the same input by the
        same code (``fingerprint``). Never raises: a directory that cannot be read or written
        (anything but a missing or garbled ``meta.json``) is left as it is and not used."""
        try:
            done = cls._prepare(root, fingerprint)
        except OSError as exc:
            log.warning("profile checkpoint: off for this run, %s cannot be used: %r", root, exc)
            return cls(None, every, clock)
        if done is None:
            log.info("profile checkpoint: starting (%s)", root)
        else:
            log.info("profile checkpoint: resuming, %d units done (%s)", done, root)
        return cls(root, every, clock, clock() + every)

    @staticmethod
    def _prepare(root: Path, fingerprint: str) -> int | None:
        """Units done in a checkpoint that is kept; ``None`` for a new (or emptied) one."""
        meta_path = root / "meta.json"
        aside = root.with_name(root.name + ASIDE_SUFFIX)
        kept = False
        if root.exists():
            why = ""
            other = False
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                if not isinstance(meta, dict):
                    raise TypeError("not a JSON object")
                meta = cast(Mapping[str, Any], meta)
                if meta.get("format") != CHECKPOINT_FORMAT or meta.get("fingerprint") != fingerprint:
                    why = "it was made from other input or by other code"
                    other = True
            except FileNotFoundError:
                why = "its meta.json is missing"
            except (ValueError, TypeError, RecursionError) as exc:  # another OSError: off, files kept
                why = f"its meta.json cannot be read ({exc!r})"
            if other:
                # Not deleted: the input may only have looked different for a moment (a Refinery
                # that was not mounted yet, file times changed by a copy), and it holds days of
                # work. It waits next to this one; the sweep removes it a month later.
                log.warning("profile checkpoint: put aside, %s (%s)", why, root)
                spare = root.with_name(root.name + ".swap")
                shutil.rmtree(spare, ignore_errors=True)
                root.rename(spare)
                if _fingerprint_at(aside) == fingerprint:
                    aside.rename(root)
                    kept = True
                    log.warning("profile checkpoint: taking back the one put aside earlier (%s)", root)
                else:
                    shutil.rmtree(aside, ignore_errors=True)
                spare.rename(aside)
            elif why:
                log.warning("profile checkpoint: discarded, %s (%s)", why, root)
                shutil.rmtree(root)
            else:
                kept = True
        elif _fingerprint_at(aside) == fingerprint:
            aside.rename(root)
            kept = True
            log.warning("profile checkpoint: taking back the one put aside earlier (%s)", root)
        root.mkdir(parents=True, exist_ok=True)
        for leftover in root.glob("*.partial"):
            leftover.unlink(missing_ok=True)
        if not kept:
            meta = {
                "format": CHECKPOINT_FORMAT,
                "fingerprint": fingerprint,
                "created": datetime.now(UTC).isoformat(timespec="seconds"),
            }
            _write_atomic(meta_path, json.dumps(meta, sort_keys=True).encode())
            return None
        return sum(1 for _ in root.glob("unit-*.done.json.gz"))

    def _done_path(self, root: Path, unit: int) -> Path:
        return root / f"unit-{unit:03d}.done.json.gz"

    def _part_path(self, root: Path, unit: int) -> Path:
        return root / f"unit-{unit:03d}.part.json.gz"

    def done(self, unit: int) -> ContentProfile | None:
        """The finished unit's profile; ``None`` when it is not there or cannot be read."""
        if self.root is None:
            return None
        path = self._done_path(self.root, unit)
        try:
            return ContentProfile.from_json(_read_gz_json(path))
        except FileNotFoundError:
            return None
        except _UNREADABLE as exc:
            log.warning("profile checkpoint: unit %d is done again, %s cannot be read: %r", unit, path, exc)
            return None

    def partial(self, unit: int, total: int) -> tuple[int, ProfileBuilder] | None:
        """``(cursor, builder)`` of a unit in progress, if its saved part is of ``total`` steps."""
        if self.root is None:
            return None
        path = self._part_path(self.root, unit)
        try:
            doc = _read_gz_json(path)
            cursor, saved_total = doc["cursor"], doc["total"]
            if type(cursor) is not int or type(saved_total) is not int:
                raise TypeError("cursor and total must be whole numbers")
            if saved_total != total:
                raise ValueError(f"saved for {saved_total} steps, there are {total}")
            if not 0 <= cursor <= total:
                raise ValueError(f"cursor {cursor} is outside 0..{total}")
            profile = ContentProfile.from_json(cast(Mapping[str, Any], doc["profile"]))
        except FileNotFoundError:
            return None
        except _UNREADABLE as exc:
            log.warning("profile checkpoint: unit %d starts again, %s is of no use: %r", unit, path, exc)
            return None
        return cursor, ProfileBuilder.from_profile(profile)

    def due(self) -> bool:
        return self.root is not None and self.every > 0 and self.clock() >= self._next

    def save_partial(self, unit: int, cursor: int, total: int, builder: ProfileBuilder) -> None:
        """Save a unit in progress. A failure is logged; the profile goes on without this save."""
        if self.root is None:
            return
        began = self.clock()
        try:
            _write_atomic(
                self._part_path(self.root, unit),
                _gz_json({"cursor": cursor, "total": total, "profile": builder.build().to_json()}),
            )
        except OSError as exc:
            log.warning("profile checkpoint: not saved (unit %d at %d): %r", unit, cursor, exc)
        else:
            log.info("profile checkpoint saved: unit %d at %d of %d", unit, cursor, total)
        now = self.clock()
        self._next = now + max(self.every, CHECKPOINT_SLOWDOWN * (now - began))

    def unit(self, unit: int, builder: ProfileBuilder) -> UnitCheckpoint:
        return UnitCheckpoint(self, unit, builder)

    def finish(self, unit: int, builder: ProfileBuilder) -> ContentProfile:
        """The unit is complete: its profile, saved as done (a failure is logged, not raised)."""
        profile = builder.build()
        if self.root is None:
            return profile
        try:
            _write_atomic(self._done_path(self.root, unit), _gz_json(profile.to_json()))
            self._part_path(self.root, unit).unlink(missing_ok=True)
        except OSError as exc:
            log.warning("profile checkpoint: unit %d is not saved as done: %r", unit, exc)
        else:
            log.info("profile checkpoint: unit %d done", unit)
        self._next = self.clock() + self.every
        return profile


def _checkpoint_seconds(sec: Mapping[str, Any]) -> float:
    """``[integrity] profile_checkpoint_seconds``; a value that is no number is the default (a
    setting of the checkpoint must not fail the profile)."""
    try:
        return float(sec.get("profile_checkpoint_seconds", CHECKPOINT_SECONDS))
    except (TypeError, ValueError):
        log.warning("profile_checkpoint_seconds is not a number; using %s", CHECKPOINT_SECONDS)
        return CHECKPOINT_SECONDS


def sweep_checkpoints(profiles_root: Path, *, keep: Path, now: float | None = None) -> list[Path]:
    """Remove the checkpoints under ``<workdir>/_profiles`` (every source) that nothing wrote to
    for :data:`CHECKPOINT_KEEP_DAYS` days, except ``keep``. A finished profile leaves its checkpoint
    behind on purpose (see :func:`profile_child`); this is what removes it. Never raises."""
    limit = (time.time() if now is None else now) - CHECKPOINT_KEEP_DAYS * 86400.0
    removed: list[Path] = []
    with suppress(OSError):
        for d in sorted(profiles_root.glob("*/*.ckpt*")):
            if d == keep or d == keep.with_name(keep.name + ASIDE_SUFFIX) or not d.is_dir():
                continue
            with suppress(OSError):
                newest = max((f.stat().st_mtime for f in d.iterdir()), default=d.stat().st_mtime)
                if newest < limit:
                    shutil.rmtree(d)
                    removed.append(d)
    if removed:
        log.info("profile checkpoint: removed %d old ones: %s", len(removed), [str(d) for d in removed])
    return removed


@dataclass
class Profiler:
    """``IntegrityHooks.profile`` for the real library."""

    refinery_root: Path | None
    sample_rate: float = DEFAULT_SAMPLE_RATE
    zim_reader: Callable[..., int] = field(default=zim_entries)
    busy_share: float | None = None

    def refined_dir(self, spec: SourceSpec, generation_id: str) -> Path | None:
        """Where the generation was refined: ``_parts/<gid>`` for a partitioned source (ADR-0010)."""
        if self.refinery_root is None:
            return None
        from wsp_index.refinery import generation_dir as gen_path
        from wsp_index.refinery import part_dir

        if spec.partitioned:
            return part_dir(self.refinery_root, spec.id, generation_id)
        return gen_path(self.refinery_root, spec.id, generation_id)

    def __call__(
        self,
        spec: SourceSpec,
        generation: Generation,
        generation_dir: Path,
        *,
        checkpoint_dir: Path | None = None,
        checkpoint_seconds: float = CHECKPOINT_SECONDS,
    ) -> ContentProfile:
        """The generation's profile. With ``checkpoint_dir`` (a directory on local disk, used by
        one process at a time) the work is saved there as it goes and continued from there; the
        result is the same either way."""
        pacer = ReadPacer(self.busy_share)
        gen_dir = self.refined_dir(spec, generation.generation_id)
        if gen_dir is not None and not (gen_dir / "_SUCCESS.json").is_file():
            gen_dir = None
        zims = sorted(
            (
                generation_dir / str(a.relpath)
                for a in generation.artifacts
                if str(a.relpath).endswith(".zim")
            ),
            key=str,
        )
        if checkpoint_dir is not None:
            try:
                fingerprint = checkpoint_fingerprint(spec.id, generation, gen_dir, self.sample_rate)
            except OSError as exc:
                log.warning("profile checkpoint: off for this run, the input cannot be described: %r", exc)
                ckpt = ProfileCheckpoint(None)
            else:
                ckpt = ProfileCheckpoint.open(checkpoint_dir, fingerprint, every=checkpoint_seconds)
            return self._in_units(ckpt, gen_dir, zims, pacer)
        builder = ProfileBuilder(sample_rate=self.sample_rate)
        refined = 0
        if gen_dir is not None:
            refined = refinery_documents(gen_dir, builder, pacer)
        for z in zims:
            prefix = f"{z.stem}/" if len(zims) > 1 else ""
            self.zim_reader(z, builder, documents=refined == 0, prefix=prefix, pacer=pacer)
        return builder.build()

    def _in_units(
        self, ckpt: ProfileCheckpoint, gen_dir: Path | None, zims: Sequence[Path], pacer: ReadPacer
    ) -> ContentProfile:
        """The same profile as one builder over everything gives, made unit by unit (the Refinery,
        then each ZIM) and merged in that order: every count is a sum, and of a key sampled twice
        the later unit's entry wins in both. A unit saved as done is not read again."""
        first = ckpt.done(0)
        if first is None:
            builder = ProfileBuilder(sample_rate=self.sample_rate)
            if gen_dir is not None:
                unit = ckpt.unit(0, builder)
                refinery_documents(gen_dir, builder, pacer, resume=unit.resume, saved=unit.saved)
            first = ckpt.finish(0, builder)
        units = [first]
        documents = first.doc_count == 0  # the rule of the one-builder path: nothing refined
        for n, z in enumerate(zims, start=1):
            profile = ckpt.done(n)
            if profile is None:
                builder = ProfileBuilder(sample_rate=self.sample_rate)
                unit = ckpt.unit(n, builder)
                prefix = f"{z.stem}/" if len(zims) > 1 else ""
                self.zim_reader(
                    z,
                    builder,
                    documents=documents,
                    prefix=prefix,
                    pacer=pacer,
                    resume=unit.resume,
                    saved=unit.saved,
                )
                profile = ckpt.finish(n, builder)
            units.append(profile)
        return merge_profiles(units)


@dataclass
class IsolatedProfiler:
    """``IntegrityHooks.profile`` run in a child unit (:func:`profile_child`); the profile comes
    back through a file under the runner's work directory."""

    cfg: Mapping[str, Any]
    runner: IsolatedRunner
    timeout: float | None = 24 * 3600.0
    min_rate: float = PROFILE_MIN_RATE

    def timeout_for(self, generation: Generation) -> float | None:
        """The time budget, at least long enough to read the generation's ZIMs at ``min_rate``. A
        profile visits every entry of a ZIM, paced to a share of the NAS link: Spanish Wikipedia
        (204 GB) ran out of the fixed 24 hours three times and failed (2026-10-25)."""
        if self.timeout is None:
            return None
        zim_bytes = sum(a.size for a in generation.artifacts if str(a.relpath).endswith(".zim"))
        return max(self.timeout, zim_bytes / self.min_rate) if self.min_rate > 0 else self.timeout

    def __call__(self, spec: SourceSpec, generation: Generation, generation_dir: Path) -> ContentProfile:
        out = self.runner.workdir / "_profiles" / spec.id / f"{generation.generation_id}.json.gz"
        out.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.runner.call(
                "profile",
                "wsp_cli.integrity:profile_child",
                {
                    "config": dict(self.cfg),
                    "source_id": spec.id,
                    "generation_id": generation.generation_id,
                    "out": str(out),
                },
                key=f"{spec.id}/{generation.generation_id}",
                timeout=self.timeout_for(generation),
            )
            return ContentProfile.load(out)
        finally:
            out.unlink(missing_ok=True)


def profile_child(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Child unit: profile one sealed generation (``PROFILE`` lock, not ``HEAVY``: paced, it runs for
    hours with little memory, and must not hold up refines and the nightly index; reads the Refinery
    and the vault).

    Its progress is kept next to the result, in ``<generation>.ckpt`` (:class:`ProfileCheckpoint`),
    and is NOT removed when the profile is done: if the daemon dies before it has stored the result
    in its state directory, the next run puts the profile together from the finished units in
    seconds. :func:`sweep_checkpoints` removes it a month later. The directory is derived from
    ``out``, so a daemon running older code needs to send nothing new."""
    from wsp_cli.wiring import child_context

    p = bootstrap.paths(cast(Mapping[str, Any], payload["config"]))
    # The lock first, then the registry and manifest reads (audit 2026-10-12, L1).
    with exclusive(lock_dir_for(p.require_index()), PROFILE):
        ctx = child_context(payload)
        assert ctx.generation is not None
        sec = bootstrap.section(ctx.cfg, "integrity")
        profiler = Profiler(
            p.refinery if p.index_root is not None else None,
            float(sec.get("profile_sample_rate", DEFAULT_SAMPLE_RATE)),
            busy_share=float(sec.get("profile_busy_share", 1.0)),
        )
        out = Path(str(payload["out"]))
        checkpoint = out.with_name(f"{ctx.generation.generation_id}.ckpt")
        sweep_checkpoints(out.parent.parent, keep=checkpoint)
        prof = profiler(
            ctx.spec,
            ctx.generation,
            ctx.vault.generation_path(ctx.spec.id, ctx.generation.generation_id),
            checkpoint_dir=checkpoint,
            checkpoint_seconds=_checkpoint_seconds(sec),
        )
    prof.save(out)
    return {"out": str(payload["out"])}


def scrubbed_hook(vault: FileVault, scrub: Scrubber) -> Callable[[str, str], bool]:
    def scrubbed(source_id: str, generation_id: str) -> bool:
        try:
            manifest = vault.manifest(source_id, generation_id)
        except (SignatureError, OSError):
            return False
        keys = [f"{source_id}/{generation_id}/{e.path}" for e in manifest.entries]
        return bool(keys) and len(scrub.verified_ok(keys)) == len(keys)

    return scrubbed


def integrity_hooks(
    cfg: Mapping[str, Any],
    vault: FileVault,
    scrub: Scrubber | None,
    *,
    runner: IsolatedRunner | None = None,
    profile_timeout: float | None = 24 * 3600.0,
) -> IntegrityHooks:
    """The guard's hooks. With ``runner`` (the daemon) profiles are computed in child units."""
    sec = bootstrap.section(cfg, "integrity")
    p = bootstrap.paths(cfg)
    refinery = p.refinery if p.index_root is not None else None

    def attest(source_id: str, generation_id: str, name: str, body: Mapping[str, Any]) -> str:
        return write_attestation(vault, source_id, generation_id, name, body)

    profile: Callable[[SourceSpec, Generation, Path], ContentProfile]
    if runner is not None:
        profile = IsolatedProfiler(
            cfg, runner, profile_timeout, float(sec.get("profile_min_rate", PROFILE_MIN_RATE))
        )
    else:
        profile = Profiler(
            refinery,
            float(sec.get("profile_sample_rate", DEFAULT_SAMPLE_RATE)),
            busy_share=float(sec.get("profile_busy_share", 1.0)),
        )
    return IntegrityHooks(
        settings=IntegritySettings.from_config(cfg),
        attest=attest,
        profile=profile,
        scrubbed=scrubbed_hook(vault, scrub) if scrub is not None else None,
        profiles_dir=p.state_dir / "profiles" if p.state_dir is not None else None,
    )


# --- health reporting (wsp doctor, /v1/system) ------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Coverage:
    generations: int = 0
    with_provenance: int = 0
    invalid: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()

    @property
    def fraction(self) -> float:
        return self.with_provenance / self.generations if self.generations else 1.0


def provenance_coverage(vault: FileVault, source_ids: Iterable[str]) -> Coverage:
    """How many sealed generations carry a valid signed ``PROVENANCE.json``."""
    total = ok = 0
    invalid: list[str] = []
    missing: list[str] = []
    for sid in source_ids:
        for g in vault.generations(sid):
            if g.status is GenerationStatus.STAGING:
                continue
            total += 1
            state = attestation_state(vault, sid, g.generation_id, PROVENANCE_NAME)
            if state == "present":
                ok += 1
            elif state == "invalid":
                invalid.append(f"{sid}/{g.generation_id}")
            else:
                missing.append(f"{sid}/{g.generation_id}")
    return Coverage(total, ok, tuple(invalid), tuple(missing))


@dataclass
class IntegrityState:
    """What the orchestrator state DB says about source integrity (read-only)."""

    channels: dict[str, dict[str, Any]] = field(default_factory=dict[str, dict[str, Any]])
    held: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    quarantined: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    drift_alerts: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    pinned_hosts: int = 0


def read_integrity_state(state_db: Path | None) -> IntegrityState:
    if state_db is None or not state_db.is_file():
        return IntegrityState()
    out = IntegrityState()
    with (
        suppress(sqlite3.Error),
        closing(sqlite3.connect(f"file:{state_db}?mode=ro", uri=True, timeout=2)) as db,
    ):
        db.row_factory = sqlite3.Row
        for r in db.execute("SELECT key, value FROM meta WHERE key LIKE 'channel:%'"):
            with suppress(ValueError):
                out.channels[str(r["key"])[len("channel:") :]] = cast(
                    dict[str, Any], json.loads(str(r["value"]))
                )
        for r in db.execute(
            "SELECT source_id, version, state, hold_reason, last_error FROM releases"
            " WHERE (hold_reason IS NOT NULL AND state NOT IN ('promoted','quarantined'))"
            " OR (state='quarantined' AND last_error LIKE 'integrity:%') ORDER BY source_id, version"
        ):
            row = {"source_id": r["source_id"], "version": r["version"], "state": r["state"]}
            if r["state"] == "quarantined":
                out.quarantined.append({**row, "reason": r["last_error"]})
            else:
                out.held.append({**row, "reason": r["hold_reason"]})
        for r in db.execute(
            "SELECT key, severity, message, raised_at FROM alerts WHERE resolved_at IS NULL"
            " AND (key LIKE 'origin-drift:%' OR key LIKE 'integrity:%') ORDER BY raised_at DESC"
        ):
            out.drift_alerts.append(dict(r))
        with suppress(sqlite3.Error):
            row = db.execute("SELECT COUNT(*) FROM origin_pins").fetchone()
            out.pinned_hosts = int(row[0]) if row else 0
    return out


def channel_summary(state: IntegrityState, source_ids: Sequence[str]) -> dict[str, str]:
    """Per source: the weakest channel status of its last checked release (or ``never checked``)."""
    rank = {"mismatch": 0, "unavailable": 1, "same-origin": 2, "independent": 3}
    out: dict[str, str] = {}
    for sid in source_ids:
        info = state.channels.get(sid)
        statuses = cast(list[str], info.get("status", [])) if info else []
        out[sid] = min(statuses, key=lambda s: rank.get(s, 0)) if statuses else "never checked"
    return out
