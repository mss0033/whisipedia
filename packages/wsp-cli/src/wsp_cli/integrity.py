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

Everything runs on local disk except reading the vault; nothing here edits content.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import closing, suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from wsp_cli import bootstrap
from wsp_cli.locks import PROFILE, exclusive, lock_dir_for
from wsp_core.memory import drop_file_cache
from wsp_core.model import Generation, GenerationStatus, SourceSpec
from wsp_orchestrator.integrity import ContentProfile, IntegrityHooks, IntegritySettings, ProfileBuilder
from wsp_orchestrator.isolation import IsolatedRunner
from wsp_vault.attest import PROVENANCE_NAME, attestation_state, write_attestation
from wsp_vault.scrub import Scrubber
from wsp_vault.vault import FileVault, SignatureError

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


def refinery_documents(gen_dir: Path, builder: ProfileBuilder, pacer: ReadPacer = UNPACED) -> int:
    """Add every refined document of a generation (sealed Refinery Parquet) to ``builder``."""
    import pyarrow.compute as pc  # pyright: ignore[reportMissingTypeStubs]
    import pyarrow.parquet as pq  # pyright: ignore[reportMissingTypeStubs]

    n = 0
    utf8_length = cast(Callable[[Any], Any], pc.utf8_length)  # pyright: ignore[reportUnknownMemberType]
    for part in sorted(gen_dir.glob("part-*.parquet")):
        try:
            n += _profile_part(part, builder, pacer, utf8_length, pq)
        finally:
            # Read once: on the NAS its pages stay charged to the step's memory ceiling (a profile
            # step held 2.6 GB of cache against 94 MB of heap, 2026-10-14).
            drop_file_cache(part)
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
    path: Path, builder: ProfileBuilder, *, documents: bool, prefix: str = "", pacer: ReadPacer = UNPACED
) -> int:
    """Entries of a ZIM: MIME types for all, raw bytes scanned for the deterministic sample.

    With ``documents=True`` (no Refinery output to use) every non-redirect entry is also counted as
    a document; sizes come from the sample only (reading every body would decompress the archive).
    """
    from libzim.reader import Archive  # pyright: ignore[reportMissingTypeStubs]

    archive = Archive(path)
    try:
        return _zim_entries(archive, builder, documents=documents, prefix=prefix, pacer=pacer, path=path)
    finally:
        drop_file_cache(path)


ZIM_DROP_EVERY = 50_000
"""Entries between page-cache drops of a ZIM being profiled: often enough that a whole-archive read
does not sit in the step's memory, rarely enough that the API's pages of the same file (dropped too:
the cache is shared) are not evicted at every entry."""


def _zim_entries(
    archive: Any, builder: ProfileBuilder, *, documents: bool, prefix: str, pacer: ReadPacer, path: Path
) -> int:
    n = 0
    for i in range(int(archive.entry_count)):
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

    def __call__(self, spec: SourceSpec, generation: Generation, generation_dir: Path) -> ContentProfile:
        builder = ProfileBuilder(sample_rate=self.sample_rate)
        pacer = ReadPacer(self.busy_share)
        refined = 0
        gen_dir = self.refined_dir(spec, generation.generation_id)
        if gen_dir is not None and (gen_dir / "_SUCCESS.json").is_file():
            refined = refinery_documents(gen_dir, builder, pacer)
        zims = sorted(
            (
                generation_dir / str(a.relpath)
                for a in generation.artifacts
                if str(a.relpath).endswith(".zim")
            ),
            key=str,
        )
        for z in zims:
            prefix = f"{z.stem}/" if len(zims) > 1 else ""
            self.zim_reader(z, builder, documents=refined == 0, prefix=prefix, pacer=pacer)
        return builder.build()


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
    and the vault)."""
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
        prof = profiler(
            ctx.spec, ctx.generation, ctx.vault.generation_path(ctx.spec.id, ctx.generation.generation_id)
        )
    prof.save(Path(str(payload["out"])))
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
