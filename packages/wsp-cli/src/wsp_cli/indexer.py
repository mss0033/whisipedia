"""Refine sealed vault generations into Parquet, then index them (PLAN §9.1-9.3), on local disk.

Layout under ``index.root`` (local disk only, ADR-0002)::

    refinery/<source_id>/<generation_id>/   Refinery Parquet (wsp_index.refinery), CURRENT.txt per source
    refinery/<source_id>/_parts/<gen_id>/   partitioned sources (ADR-0010): one per partition generation,
                                            composed into a set generation <newest>.setNNNNN that is indexed
    lexical/                                Tantivy + suggest tries (TantivyIndex), static_rank.bin,
                                            spelling.bin ("did you mean")
    changes.sqlite                          change feed (SqliteChangeFeed)
    index-state.json                        {source_id: generation indexed last}
    catalog/<source_id>.json                portal/directory/random data (wsp_index.catalog)
    work/<source_id>/<generation_id>/       redirects.sqlite (aliases), adapter scratch
    work/_rank/                             edge list while ``rank`` runs (deleted afterwards)

Every step is resumable: a refined generation is sealed by ``_SUCCESS.json`` and skipped next time
(an unsealed one is rebuilt from scratch); indexing diffs the last indexed generation against the
new one, so re-running after a crash re-applies at most one idempotent batch.

A partitioned source's content is the union of its partitions' current generations
(``Vault.current_set``): each partition generation is refined once, then a set generation hard-links
their Parquet parts and is indexed like any other generation, so search, documents, history and the
catalog see every partition, not only the last one ingested.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO, cast

from wsp_adapters.entries import EntrySource, Redirect
from wsp_core.errors import FetchError
from wsp_core.ids import InvalidDocIdError, make_doc_id
from wsp_core.model import DocRecord, FetchResult, Generation, GenerationStatus, RemoteFile, SourceSpec
from wsp_core.partitions import partition_of
from wsp_core.ports import ChangeFeed, Ctx, SourceAdapter, Vault
from wsp_index import RefineryReader, RefineryWriter, TantivyIndex, ensure_local, refresh
from wsp_index.catalog import (
    CATALOG_DIR,
    CatalogConfig,
    build_source_catalog,
    is_featured_source,
    refresh_featured,
)
from wsp_index.linkrank import RankConfig, RankStats, StaticRank, compute_static_rank
from wsp_index.refinery import (
    CURRENT_FILE,
    compose_set,
    current_generation,
    generation_dir,
    is_sealed,
    part_dir,
)
from wsp_index.spelling import SPELL_FILE, SpellConfig, VocabStats, build_vocabulary
from wsp_orchestrator.pipeline import RefineResult

PROGRESS_EVERY = 10_000
STATE_FILE = "index-state.json"


class OfflineFetcher:
    """Fetcher for steps that must not touch the network (refining reads only sealed files)."""

    def get_bytes(self, url: str, *, max_bytes: int = 64 * 1024 * 1024) -> bytes:
        raise FetchError(f"network access is not allowed while indexing: {url}")

    def download(self, remote: RemoteFile, dest: Path) -> FetchResult:
        raise FetchError(f"network access is not allowed while indexing: {remote.url}")


@dataclass(frozen=True, slots=True)
class SourceReport:
    source_id: str
    refined: tuple[tuple[str, int], ...] = ()  # (generation_id, documents) newly refined
    indexed: str | None = None  # generation now indexed
    upserted: int = 0
    deleted: int = 0
    skipped: str | None = None  # reason the source was skipped


_ID_PREFIX = re.compile(r"^[a-z][a-z0-9._-]*/")  # a language ("en/", "zh-yue/") or ZIM book prefix


def _alias_title(native_id: str, target: str = "") -> str:
    """The title of a redirect: its native id without the source's language/book prefix.

    The prefix is the first segment when the redirect and its ``target`` share it and it looks
    like a prefix (lower case: ``en/``, ``wikipedia_de_all/``); a title's own "/" is kept
    (``en/AC/DC_(album)`` -> ``AC/DC (album)``, not ``DC (album)``).
    """
    m = _ID_PREFIX.match(native_id)
    title = native_id[m.end() :] if m and target.startswith(m.group(0)) else native_id
    return title.replace("_", " ").strip()


class Indexer:
    """Owns the local index stores for one run (``close()`` when done)."""

    def __init__(
        self,
        vault: Vault,
        specs: Sequence[SourceSpec],
        adapters: Mapping[str, SourceAdapter],
        root: Path,
        index: TantivyIndex,
        feed: ChangeFeed,
        *,
        user_agent: str = "Whisipedia indexer",
        out: TextIO | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.vault = vault
        self.specs = {s.id: s for s in specs}
        self.adapters = adapters
        self.root = ensure_local(root)
        self.refinery = root / "refinery"
        self.work = root / "work"
        self.index = index
        self.feed = feed
        self.user_agent = user_agent
        self.out = out
        self.clock = clock

    # --- helpers -------------------------------------------------------------------------------

    def _say(self, text: str) -> None:
        if self.out is not None:
            self.out.write(text + "\n")
            self.out.flush()

    def adapter_for(self, spec: SourceSpec) -> SourceAdapter | None:
        for key in (spec.adapter, spec.id):
            if key is not None and key in self.adapters:
                return self.adapters[key]
        return None

    def ctx(self, spec: SourceSpec, generation_id: str) -> Ctx:
        work = self.work / spec.id / generation_id
        work.mkdir(parents=True, exist_ok=True)
        return Ctx(source=spec, fetcher=OfflineFetcher(), workdir=work, user_agent=self.user_agent)

    def _state(self) -> dict[str, str]:
        path = self.root / STATE_FILE
        return dict(json.loads(path.read_text(encoding="utf-8"))) if path.is_file() else {}

    def _save_state(self, state: Mapping[str, str]) -> None:
        path = self.root / STATE_FILE
        tmp = path.with_name(path.name + ".partial")
        tmp.write_text(json.dumps(dict(state), indent=1, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, path)

    # --- refine --------------------------------------------------------------------------------

    def refined_dir(self, spec: SourceSpec, generation_id: str) -> Path:
        """Where a vault generation is refined: ``_parts/<gid>`` for a partitioned source (ADR-0010)."""
        if spec.partitioned:
            return part_dir(self.refinery, spec.id, generation_id)
        return generation_dir(self.refinery, spec.id, generation_id)

    def refine(self, spec: SourceSpec, generation: Generation, generation_path: Path) -> RefineResult:
        """Write the generation's documents to Refinery Parquet (skipped if already sealed)."""
        gid = generation.generation_id
        out_dir = self.refined_dir(spec, gid)
        if is_sealed(out_dir):
            return RefineResult(len(RefineryReader(out_dir)))
        adapter = self.adapter_for(spec)
        if adapter is None:
            raise LookupError(f"no adapter installed for {spec.id} ({spec.adapter})")
        if out_dir.exists():
            shutil.rmtree(out_dir)  # derived, local and unsealed: rebuild from the vault
        ctx = self.ctx(spec, gid)
        redirects = ctx.workdir / "redirects.sqlite"
        redirects.unlink(missing_ok=True)
        started, n = self.clock(), 0
        with (
            closing(sqlite3.connect(redirects)) as db,
            RefineryWriter(self.refinery, spec.id, gid, directory=out_dir) as writer,
        ):
            db.execute("CREATE TABLE redirects (target TEXT NOT NULL, alias TEXT NOT NULL)")
            for doc in self._documents(adapter, ctx, generation_path, db):
                writer.write([doc])
                n += 1
                if n % PROGRESS_EVERY == 0:
                    rate = n / max(self.clock() - started, 1e-6)
                    self._say(f"  refine {spec.id}/{gid}: {n:,} documents ({rate:,.0f}/s)")
            db.commit()
        self._say(f"  refined {spec.id}/{gid}: {n:,} documents in {self.clock() - started:.1f}s")
        return RefineResult(n)

    @staticmethod
    def _documents(
        adapter: SourceAdapter, ctx: Ctx, generation_path: Path, db: sqlite3.Connection
    ) -> Iterator[DocRecord]:
        if not isinstance(adapter, EntrySource):
            yield from adapter.documents(ctx, generation_path)
            return
        batch: list[tuple[str, str]] = []
        for entry in adapter.entries(ctx, generation_path):
            if isinstance(entry, Redirect):
                batch.append((entry.target, _alias_title(entry.native_id, entry.target)))
                if len(batch) >= 5_000:
                    db.executemany("INSERT INTO redirects VALUES (?, ?)", batch)
                    batch.clear()
            else:
                yield entry
        db.executemany("INSERT INTO redirects VALUES (?, ?)", batch)

    # --- index ---------------------------------------------------------------------------------

    def index_generation(self, spec: SourceSpec, generation_id: str) -> SourceReport:
        """Bring the search index from the last indexed generation of ``spec`` to ``generation_id``."""
        state = self._state()
        previous = state.get(spec.id)
        new_dir = generation_dir(self.refinery, spec.id, generation_id)
        if previous == generation_id:
            if not (self.root / CATALOG_DIR / f"{spec.id}.json").is_file():
                self.build_catalog(spec, generation_id)
            return SourceReport(spec.id, indexed=generation_id)
        new = RefineryReader(new_dir)
        old_dir = generation_dir(self.refinery, spec.id, previous) if previous else None
        old = RefineryReader(old_dir) if old_dir is not None and is_sealed(old_dir) else None
        stats = refresh(self.index, self.feed, old, new)
        self._apply_redirect_aliases(spec, generation_id)
        self.index.commit()
        pointer = self.refinery / spec.id / CURRENT_FILE
        tmp = pointer.with_name(CURRENT_FILE + ".partial")
        tmp.write_text(generation_id + "\n", encoding="utf-8")
        os.replace(tmp, pointer)
        state[spec.id] = generation_id
        self._save_state(state)
        self.build_catalog(spec, generation_id)
        self._say(
            f"  indexed {spec.id}/{generation_id}: +{stats.upserted:,} upserted, -{stats.deleted:,} removed"
        )
        return SourceReport(spec.id, indexed=generation_id, upserted=stats.upserted, deleted=stats.deleted)

    def index_partitioned(self, spec: SourceSpec, include: str | None = None) -> SourceReport:
        """Index the union of a partitioned source's current partitions (ADR-0010).

        The members are the vault's current set; ``include`` (a sealed generation about to be
        promoted, from the release pipeline) replaces the current generation of its partition. Members
        not refined yet are refined first; the set generation is composed (or reused) and indexed.
        """
        ids = [g.generation_id for g in self.vault.current_set(spec.id)]
        if include is not None:
            ids = [g for g in ids if partition_of(g) != partition_of(include)] + [include]
        if not ids:
            return SourceReport(spec.id, skipped="no current generation")
        sealed = {g.generation_id: g for g in self.vault.generations(spec.id)}
        members: list[Path] = []
        for gid in sorted(ids):
            out_dir = part_dir(self.refinery, spec.id, gid)
            if not is_sealed(out_dir):
                gen = sealed.get(gid)
                if gen is None or gen.status is not GenerationStatus.SEALED:
                    raise LookupError(f"{spec.id}/{gid} is not a sealed generation")
                self.refine(spec, gen, self.vault.generation_path(spec.id, gid))
            members.append(out_dir)
        set_id = compose_set(self.refinery, spec.id, members)
        self._say(f"  {spec.id}: set {set_id} of {len(members)} partition(s)")
        report = self.index_generation(spec, set_id)
        for gid in ids:  # aliases of every member (index_generation only knows the set's own work dir)
            self._apply_redirect_aliases(spec, gid)
        self.index.commit()
        return report

    def build_catalog(self, spec: SourceSpec, generation_id: str) -> Path | None:
        """Precompute the portal/directory/random data of one indexed generation (derived, local)."""
        started = self.clock()
        cfg = CatalogConfig(featured=is_featured_source(spec), rank=self.index.static_rank())
        try:
            path = build_source_catalog(self.refinery, spec.id, generation_id, self.root / CATALOG_DIR, cfg)
        except Exception as exc:  # derived data: never fail an index build over it
            self._say(f"  catalog {spec.id}/{generation_id}: FAILED ({type(exc).__name__}: {exc})")
            return None
        self._say(f"  catalog {spec.id}/{generation_id}: built in {self.clock() - started:.1f}s")
        return path

    def _apply_redirect_aliases(self, spec: SourceSpec, generation_id: str) -> None:
        path = self.work / spec.id / generation_id / "redirects.sqlite"
        if not path.is_file():
            return
        with closing(sqlite3.connect(path)) as db:
            batch: dict[str, list[str]] = {}
            for target, alias in db.execute("SELECT target, alias FROM redirects ORDER BY target"):
                try:
                    doc_id = make_doc_id(spec.id, str(target))
                except InvalidDocIdError:
                    continue
                if doc_id not in batch and len(batch) >= 5_000:
                    self.index.set_aliases(batch)
                    batch = {}
                batch.setdefault(doc_id, []).append(str(alias))
            if batch:
                self.index.set_aliases(batch)

    # --- static rank (PageRank) ---------------------------------------------------------------

    def rank(self, config: RankConfig | None = None) -> RankStats | None:
        """PageRank over every indexed source's current generation (``wsp index rank``).

        Writes ``lexical/static_rank.bin``, then uses it for suggest priors and the portal's
        featured articles. Re-runnable on its own; it reads only the Refinery and the index's aliases.
        """
        readers = self._current_readers()
        if not readers:
            self._say("rank: nothing indexed")
            return None
        started = self.clock()
        stats = compute_static_rank(
            readers,
            self.index.rank_path,
            self.work / "_rank",
            aliases=self.index.iter_aliases(),
            config=config or RankConfig(),
            progress=self._say,
        )
        self._say(f"rank: {stats.line()}")
        rank = self.index.reload_static_rank()
        if rank is not None:
            self._apply_priors(readers, rank)
            for sid, reader in readers:
                spec = self.specs[sid]
                if is_featured_source(spec):
                    cfg = CatalogConfig(featured=True, rank=rank)
                    refresh_featured(self.refinery, sid, reader.generation_id, self.root / CATALOG_DIR, cfg)
        self._say(f"rank: done in {self.clock() - started:.1f}s")
        return stats

    # --- "did you mean" vocabulary ------------------------------------------------------------

    def _current_readers(self) -> list[tuple[str, RefineryReader]]:
        readers: list[tuple[str, RefineryReader]] = []
        for sid in sorted(self.specs):
            gen = current_generation(self.refinery, sid)
            if gen is not None and is_sealed(generation_dir(self.refinery, sid, gen)):
                readers.append((sid, RefineryReader(generation_dir(self.refinery, sid, gen))))
        return readers

    def spell(self, config: SpellConfig | None = None) -> VocabStats | None:
        """Rebuild the spelling vocabulary from every indexed source (``wsp index spell``)."""
        readers = self._current_readers()
        if not readers:
            self._say("spell: nothing indexed")
            return None
        path = self.index.path / SPELL_FILE
        stats = build_vocabulary(readers, path, config or SpellConfig(), progress=self._say)
        self._say(f"spell: {stats.line()}")
        return stats

    def _apply_priors(self, readers: Sequence[tuple[str, RefineryReader]], rank: StaticRank) -> None:
        """Suggest popularity priors from the static rank (streamed, 50k documents at a time)."""
        for _, reader in readers:
            for batch in reader.scan(["doc_id"], batch_size=50_000):
                ids = cast(list[str], batch.column(0).to_pylist())
                feats = rank.features(ids)
                self.index.set_priors({d: float(f) for d, f in zip(ids, feats, strict=True)})
        self.index.commit()

    # --- whole runs ----------------------------------------------------------------------------

    def build(self, source_ids: Sequence[str] | None = None) -> list[SourceReport]:
        """Refine every sealed generation, then index the vault's current one, per source."""
        reports: list[SourceReport] = []
        wanted = list(source_ids) if source_ids else sorted(self.specs)
        for sid in wanted:
            spec = self.specs.get(sid)
            if spec is None:
                reports.append(SourceReport(sid, skipped="not in the registry"))
                continue
            gens = [g for g in self.vault.generations(sid) if g.status is GenerationStatus.SEALED]
            if not gens:
                if source_ids:
                    reports.append(SourceReport(sid, skipped="no sealed generation"))
                continue
            if self.adapter_for(spec) is None:
                reports.append(SourceReport(sid, skipped=f"no adapter installed for {spec.adapter!r}"))
                continue
            self._say(f"{sid}: {len(gens)} sealed generation(s)")
            refined: list[tuple[str, int]] = []
            for g in gens:
                if is_sealed(self.refined_dir(spec, g.generation_id)):
                    continue
                result = self.refine(spec, g, self.vault.generation_path(sid, g.generation_id))
                refined.append((g.generation_id, result.doc_count or 0))
            if spec.partitioned:
                rep = self.index_partitioned(spec)
                reports.append(
                    SourceReport(sid, tuple(refined), rep.indexed, rep.upserted, rep.deleted, rep.skipped)
                )
                continue
            current = self.vault.current(sid)
            if current is None:
                reports.append(SourceReport(sid, tuple(refined), skipped="no current generation"))
                continue
            rep = self.index_generation(spec, current.generation_id)
            reports.append(
                SourceReport(sid, tuple(refined), rep.indexed, rep.upserted, rep.deleted, rep.skipped)
            )
        return reports
