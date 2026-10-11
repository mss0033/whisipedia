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
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import closing, suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from wsp_cli import bootstrap
from wsp_cli.locks import HEAVY, exclusive, lock_dir_for
from wsp_core.model import Generation, GenerationStatus, SourceSpec
from wsp_orchestrator.integrity import ContentProfile, IntegrityHooks, IntegritySettings, ProfileBuilder
from wsp_orchestrator.isolation import IsolatedRunner
from wsp_vault.attest import PROVENANCE_NAME, attestation_state, write_attestation
from wsp_vault.scrub import Scrubber
from wsp_vault.vault import FileVault, SignatureError

DEFAULT_SAMPLE_RATE = 1 / 32
# Rows per batch when reading Refinery parts: the text column of 256 large documents (books) stays
# in the tens of MB, where 8,192 rows of Gutenberg could reach gigabytes.
PROFILE_BATCH_ROWS = 256


def _namespace(native_id: str) -> str:
    head, sep, _ = native_id.partition("/")
    return head if sep and len(head) <= 40 else "main"


def refinery_documents(gen_dir: Path, builder: ProfileBuilder) -> int:
    """Add every refined document of a generation (sealed Refinery Parquet) to ``builder``."""
    import pyarrow.compute as pc  # pyright: ignore[reportMissingTypeStubs]
    import pyarrow.parquet as pq  # pyright: ignore[reportMissingTypeStubs]

    n = 0
    utf8_length = cast(Callable[[Any], Any], pc.utf8_length)  # pyright: ignore[reportUnknownMemberType]
    for part in sorted(gen_dir.glob("part-*.parquet")):
        pf = pq.ParquetFile(part)
        batches = pf.iter_batches(  # pyright: ignore[reportUnknownMemberType]
            columns=["native_id", "title", "content_hash", "text"], batch_size=PROFILE_BATCH_ROWS
        )
        for batch in batches:
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


def zim_entries(path: Path, builder: ProfileBuilder, *, documents: bool, prefix: str = "") -> int:
    """Entries of a ZIM: MIME types for all, raw bytes scanned for the deterministic sample.

    With ``documents=True`` (no Refinery output to use) every non-redirect entry is also counted as
    a document; sizes come from the sample only (reading every body would decompress the archive).
    """
    from libzim.reader import Archive  # pyright: ignore[reportMissingTypeStubs]

    archive = Archive(path)
    n = 0
    for i in range(int(archive.entry_count)):
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
        refined = 0
        gen_dir = self.refined_dir(spec, generation.generation_id)
        if gen_dir is not None and (gen_dir / "_SUCCESS.json").is_file():
            refined = refinery_documents(gen_dir, builder)
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
            self.zim_reader(z, builder, documents=refined == 0, prefix=prefix)
        return builder.build()


@dataclass
class IsolatedProfiler:
    """``IntegrityHooks.profile`` run in a child unit (:func:`profile_child`); the profile comes
    back through a file under the runner's work directory."""

    cfg: Mapping[str, Any]
    runner: IsolatedRunner
    timeout: float | None = 6 * 3600.0

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
                timeout=self.timeout,
            )
            return ContentProfile.load(out)
        finally:
            out.unlink(missing_ok=True)


def profile_child(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Child unit: profile one sealed generation (HEAVY lock; reads the Refinery and the vault)."""
    from wsp_cli.wiring import child_context

    ctx = child_context(payload)
    assert ctx.generation is not None
    p = bootstrap.paths(ctx.cfg)
    sec = bootstrap.section(ctx.cfg, "integrity")
    profiler = Profiler(
        p.index_root / "refinery" if p.index_root is not None else None,
        float(sec.get("profile_sample_rate", DEFAULT_SAMPLE_RATE)),
    )
    with exclusive(lock_dir_for(p.require_index()), HEAVY):
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
    profile_timeout: float | None = 6 * 3600.0,
) -> IntegrityHooks:
    """The guard's hooks. With ``runner`` (the daemon) profiles are computed in child units."""
    sec = bootstrap.section(cfg, "integrity")
    p = bootstrap.paths(cfg)
    refinery = p.index_root / "refinery" if p.index_root is not None else None

    def attest(source_id: str, generation_id: str, name: str, body: Mapping[str, Any]) -> str:
        return write_attestation(vault, source_id, generation_id, name, body)

    profile: Callable[[SourceSpec, Generation, Path], ContentProfile]
    if runner is not None:
        profile = IsolatedProfiler(cfg, runner, profile_timeout)
    else:
        profile = Profiler(refinery, float(sec.get("profile_sample_rate", DEFAULT_SAMPLE_RATE)))
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
