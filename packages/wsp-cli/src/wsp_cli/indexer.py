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
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO, cast

from wsp_adapters.entries import EntrySource, Redirect
from wsp_core.errors import FetchError
from wsp_core.memory import memory_line
from wsp_core.model import (
    DocRecord,
    FetchResult,
    Generation,
    GenerationStatus,
    RemoteFile,
    SourceSpec,
    searchable,
)
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
from wsp_index.linkrank import RankConfig, RankStats, compute_static_rank, fence_path, write_fence
from wsp_index.refinery import (
    CURRENT_FILE,
    SUCCESS_FILE,
    compose_set,
    current_generation,
    ensure_id_map,
    generation_dir,
    id_map_dir,
    is_sealed,
    part_dir,
    set_members,
)
from wsp_index.spelling import SPELL_FILE, SpellConfig, VocabStats, build_vocabulary
from wsp_index.titles import IndexedSource, TitleSources
from wsp_orchestrator.pipeline import RefineResult
from wsp_vault.ratelimit import RateLimiter

PROGRESS_EVERY = 10_000
STATE_FILE = "index-state.json"
REPLACED_DIR = "_replaced"  # <refinery>/<source>/_replaced/: refined copies a refine-again replaced
PENDING_FILE = "PENDING"  # in REPLACED_DIR: the replaced copy a refine-again still diffs from


class IndexStateError(RuntimeError):
    """``index-state.json`` names a generation the lexical index does not hold."""


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


def _write_durably(path: Path, text: str) -> None:
    """Replace ``path`` atomically and durably (unique temp name, file and directory fsync)."""
    tmp = path.with_name(f"{path.name}.{os.getpid()}.partial")
    with tmp.open("w", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


# A language ("en/", "zh-yue/") or ZIM book prefix, or an old-style ZIM namespace ("A/").
_ID_PREFIX = re.compile(r"^(?:[a-z][a-z0-9._-]*|[A-Z])/")


def _alias_title(native_id: str, target: str = "") -> str:
    """The title of a redirect: its native id without the source's prefixes.

    A prefix is a leading segment the redirect and its ``target`` share that looks like one: lower
    case (``en/``, ``wikipedia_de_all/``) or a single capital letter (an old ZIM namespace,
    ``A/``); at most two (a book and its namespace). A title's own "/" is kept (``en/AC/DC_(album)``
    -> ``AC/DC (album)``, not ``DC (album)``). Ambiguous by construction: a title of a source
    without prefixes that starts like one ("A/B testing" redirecting to "A/B_testing" in a new-style
    ZIM) loses that start, as every title did before.
    """
    title, tgt = native_id, target
    for _ in range(2):
        m = _ID_PREFIX.match(title)
        if not m or not tgt.startswith(m.group(0)):
            break
        title, tgt = title[m.end() :], tgt[m.end() :]
    return title.replace("_", " ").strip()


def _has_rows(table: Path) -> bool:
    """Whether a generation's ``redirects.sqlite`` holds any redirect. :meth:`Indexer.refine` creates
    the table for every generation, so its mere existence marked the trie stale on every release
    (review 2026-10-14 #1b). Unreadable: assumed to hold some."""
    if not table.is_file():
        return False
    try:
        with closing(sqlite3.connect(f"file:{table}?mode=ro", uri=True)) as db:
            return db.execute("SELECT 1 FROM redirects LIMIT 1").fetchone() is not None
    except sqlite3.Error:
        return True


class Indexer:
    """Owns the local index stores for one run (``close()`` when done)."""

    def __init__(
        self,
        vault: Vault,
        specs: Sequence[SourceSpec],
        adapters: Mapping[str, SourceAdapter],
        root: Path,
        index: TantivyIndex | Callable[[], TantivyIndex],
        feed: ChangeFeed | Callable[[], ChangeFeed],
        *,
        user_agent: str = "Whisipedia indexer",
        out: TextIO | None = None,
        clock: Callable[[], float] = time.monotonic,
        refinery: Path | None = None,
        map_root: Path | None = None,
    ) -> None:
        self.vault = vault
        self.specs = {s.id: s for s in specs}
        self.adapters = adapters
        self.root = ensure_local(root)
        self.refinery = refinery or root / "refinery"  # [index] refinery_root: may be the NAS (ADR-0011)
        # Local id-map sidecars (ADR-0011): every reader here maps the id index from local disk, and a
        # generation's map is written as it goes live, so the API never builds one in a request.
        self.map_root = map_root
        self.work = root / "work"
        # Opened on first use: refining needs neither, and opening the index is not free.
        self._index = index if isinstance(index, TantivyIndex) else None
        self._open_index = None if isinstance(index, TantivyIndex) else index
        if hasattr(feed, "publish"):
            self._feed: ChangeFeed | None = cast(ChangeFeed, feed)
            self._open_feed: Callable[[], ChangeFeed] | None = None
        else:
            self._feed, self._open_feed = None, cast(Callable[[], ChangeFeed], feed)
        self._changed = False  # anything indexed this run (suggest is rebuilt once, in finish())
        self.user_agent = user_agent
        self.out = out
        self.clock = clock

    # --- stores --------------------------------------------------------------------------------

    @property
    def index(self) -> TantivyIndex:
        if self._index is None:
            assert self._open_index is not None
            self._index = self._open_index()
        return self._index

    @property
    def feed(self) -> ChangeFeed:
        if self._feed is None:
            assert self._open_feed is not None
            self._feed = self._open_feed()
        return self._feed

    def close(self) -> None:
        """Close whatever was opened (idempotent)."""
        try:
            if self._index is not None:
                self._index.close()
        finally:
            if self._feed is not None:
                self._feed.close()

    def finish(self) -> None:
        """Rebuild the suggest trie once (bulk commits defer it) when a rebuild is due
        (:meth:`TantivyIndex.suggest_rebuild_due`); a small overlay on a fresh trie waits for a later
        run. Checked even when this run changed nothing, so a nightly build that finds everything
        indexed still folds a dirty trie once it is old (review 2026-10-14 #1c)."""
        if self.index.suggest_rebuild_due():
            started = self.clock()
            self.index.rebuild_suggest()
            self._say(f"suggest: rebuilt in {self.clock() - started:.1f}s; {memory_line()}")
        elif self._changed:
            self.index.commit()
            self._say("suggest: rebuild not due (fresh trie, small overlay); left for a later run")
        self._changed = False

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
        _write_durably(self.root / STATE_FILE, json.dumps(dict(state), indent=1, sort_keys=True) + "\n")

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
            meta = json.loads((out_dir / SUCCESS_FILE).read_text(encoding="utf-8"))
            return RefineResult(int(meta.get("docs", 0)))
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
        self._say(
            f"  refined {spec.id}/{gid}: {n:,} documents in {self.clock() - started:.1f}s; {memory_line()}"
        )
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

    def index_generation(
        self,
        spec: SourceSpec,
        generation_id: str,
        *,
        alias_generations: Sequence[str] = (),
        since: Path | None = None,
    ) -> SourceReport:
        """Bring the search index from the last indexed generation of ``spec`` to ``generation_id``.

        Order (each step idempotent, so a crash anywhere re-runs into the same result):

        1. the Refinery ``CURRENT`` pointer moves first, so the document store serves every document
           search can return (until the refresh ends, a removed document may 404; an added one never);
        2. the resumable refresh (index commits and feed records, part by part);
        3. redirect aliases of this generation and of ``alias_generations`` (set members);
        4. the indexed-generation state, last: until it is saved, a re-run resumes the refresh.

        The suggest trie is rebuilt once per run, in :meth:`finish`.

        ``since``: the refined copy the index holds, when it is not the indexed generation's own
        directory (:meth:`refine_again` replaced it): diff from it even if the generation id is the same.
        """
        state = self._state()
        previous = state.get(spec.id)
        new_dir = generation_dir(self.refinery, spec.id, generation_id)
        if (
            previous is not None
            and self._documents_in(spec.id, previous)
            and not self.index.has_source(spec.id)
        ):
            # The state is trusted to diff from: with ``lexical/`` rebuilt alone, the run "indexed"
            # nothing (or only the changes) and reported success (audit 2026-10-12 #11).
            raise IndexStateError(
                f"{STATE_FILE} says {spec.id} is indexed at {previous}, but the lexical index holds none of "
                f"its documents (was {self.index.path} rebuilt alone?). Remove {self.root / STATE_FILE} "
                f"too (or only {spec.id!r} in it) to index again from the start "
                "(docs/runbooks/rebuild-indexes.md)"
            )
        if previous == generation_id and since is None:
            if not (self.root / CATALOG_DIR / f"{spec.id}.json").is_file():
                self.build_catalog(spec, generation_id)
            return SourceReport(spec.id, indexed=generation_id)
        new = self._reader(new_dir)
        old_dir = since or (generation_dir(self.refinery, spec.id, previous) if previous else None)
        old = self._reader(old_dir) if old_dir is not None and is_sealed(old_dir) else None
        if self.map_root is not None and ensure_id_map(new_dir, id_map_dir(self.map_root, new_dir)):
            self._say(f"  {spec.id}/{generation_id}: id map written")
        _write_durably(self.refinery / spec.id / CURRENT_FILE, generation_id + "\n")
        stats = refresh(self.index, self.feed, old, new, progress=self._say)
        if any(_has_rows(p) for p in self._redirect_tables(spec.id, (generation_id, *alias_generations))):
            # Its redirects join the trie at the next due rebuild (within a day), not at once: a full
            # scan per Kiwix release (they all carry redirects) took 1.5 h each (review 2026-10-14).
            self.index.mark_suggest_dirty()
        self.index.commit(rebuild_suggest=False)
        self._changed = True
        state[spec.id] = generation_id
        self._save_state(state)
        self.build_catalog(spec, generation_id)
        self._say(
            f"  indexed {spec.id}/{generation_id}: +{stats.upserted:,} upserted, -{stats.deleted:,} removed; "
            + memory_line()
        )
        return SourceReport(spec.id, indexed=generation_id, upserted=stats.upserted, deleted=stats.deleted)

    def _documents_in(self, source_id: str, generation_id: str) -> bool:
        """Whether a refined generation holds documents (unknown, e.g. deleted since: assumed so)."""
        path = generation_dir(self.refinery, source_id, generation_id) / SUCCESS_FILE
        try:
            return int(json.loads(path.read_text(encoding="utf-8")).get("docs", 1)) > 0
        except (OSError, ValueError):
            return True

    def purge(self, spec: SourceSpec) -> int:
        """Remove from the search index a source the registry no longer indexes (``index: false``).

        Its documents stay in the library (served as stored), so no change records are published; the
        source leaves ``index-state.json``. Idempotent: a crash leaves the rest for the next run.
        Before, such a source was only skipped, and what it had indexed stayed searchable for good
        (audit 2026-10-12 #13).
        """
        state = self._state()
        removed = 0
        if self.index.has_source(spec.id):
            removed = self._indexed_count(spec.id)
            self.index.delete_source(spec.id)
            self.index.commit(rebuild_suggest=False)
            self._changed = True
        if spec.id in state:
            del state[spec.id]
            self._save_state(state)
        if removed:
            self._say(
                f"{spec.id}: not indexed (registry index: false): {removed:,} documents removed from search"
            )
        return removed

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
        # Every member's aliases are applied before the state is saved (a crash cannot lose them).
        return self.index_generation(spec, set_id, alias_generations=sorted(ids))

    def build_catalog(self, spec: SourceSpec, generation_id: str) -> Path | None:
        """Precompute the portal/directory/random data of one indexed generation (derived, local)."""
        started = self.clock()
        cfg = CatalogConfig(featured=is_featured_source(spec), rank=self.index.static_rank())
        try:
            path = build_source_catalog(
                self.refinery, spec.id, generation_id, self.root / CATALOG_DIR, cfg, map_root=self.map_root
            )
        except Exception as exc:  # derived data: never fail an index build over it
            self._say(f"  catalog {spec.id}/{generation_id}: FAILED ({type(exc).__name__}: {exc})")
            return None
        self._say(f"  catalog {spec.id}/{generation_id}: built in {self.clock() - started:.1f}s")
        return path

    def _redirect_tables(self, source_id: str, generation_ids: Iterable[str]) -> list[Path]:
        """The ``redirects.sqlite`` of each refined generation (written by :meth:`refine`)."""
        return [self.work / source_id / gid / "redirects.sqlite" for gid in dict.fromkeys(generation_ids)]

    def _indexed_count(self, source_id: str) -> int:
        """Documents of ``source_id`` in its indexed generation (0 when unknown)."""
        gen = self._state().get(source_id)
        path = generation_dir(self.refinery, source_id, gen) / SUCCESS_FILE if gen else None
        try:
            return int(json.loads(path.read_text(encoding="utf-8")).get("docs", 0)) if path else 0
        except (OSError, ValueError):
            return 0

    def title_sources(self) -> TitleSources:
        """Titles and aliases of the indexed generations (the suggest trie's and the rank's inputs)."""
        return indexed_title_sources(self.refinery, self.work, self._state())

    # --- static rank (PageRank) ---------------------------------------------------------------

    def rank(self, config: RankConfig | None = None, *, reuse: bool = False) -> RankStats | None:
        """PageRank over every indexed source's current generation (``wsp index rank``).

        Writes ``lexical/static_rank.bin``, then uses it for suggest priors and the portal's
        featured articles. Re-runnable on its own; it reads only the Refinery and the index's aliases.
        """
        readers = self._current_readers()
        if not readers:
            self._say("rank: nothing indexed")
            return None
        started = self.clock()
        stats: RankStats | None = None
        if reuse and self.index.rank_path.is_file():
            self._say(f"rank: reusing {self.index.rank_path.name} (computed earlier)")
            if not fence_path(self.index.rank_path).is_file():
                write_fence(self.index.rank_path)
        else:
            stats = compute_static_rank(
                readers,
                self.index.rank_path,
                self.work / "_rank",
                aliases=self.title_sources().iter_aliases(),
                config=config or RankConfig(),
                progress=self._say,
            )
            self._say(f"rank: {stats.line()}")
        rank = self.index.reload_static_rank()
        if rank is not None:
            # Suggest priors are the rank's features, read from the rank file: one trie rebuild.
            started_suggest = self.clock()
            self.index.mark_suggest_stale()
            self.index.commit()
            self._say(f"rank: suggest rebuilt in {self.clock() - started_suggest:.1f}s")
            for sid, reader in readers:
                spec = self.specs[sid]
                if is_featured_source(spec):
                    cfg = CatalogConfig(featured=True, rank=rank)
                    refresh_featured(
                        self.refinery,
                        sid,
                        reader.generation_id,
                        self.root / CATALOG_DIR,
                        cfg,
                        map_root=self.map_root,
                    )
        self._say(f"rank: done in {self.clock() - started:.1f}s")
        return stats

    # --- "did you mean" vocabulary ------------------------------------------------------------

    def _current_readers(self) -> list[tuple[str, RefineryReader]]:
        readers: list[tuple[str, RefineryReader]] = []
        for sid in sorted(self.specs):
            gen = current_generation(self.refinery, sid)
            if gen is not None and is_sealed(generation_dir(self.refinery, sid, gen)):
                readers.append((sid, self._reader(generation_dir(self.refinery, sid, gen))))
        return readers

    def _reader(self, gen_dir: Path) -> RefineryReader:
        map_dir = None if self.map_root is None else id_map_dir(self.map_root, gen_dir)
        return RefineryReader(gen_dir, map_dir=map_dir)

    def spell(self, config: SpellConfig | None = None) -> VocabStats | None:
        """Rebuild the spelling vocabulary from every indexed source (``wsp index spell``)."""
        readers = self._current_readers()
        if not readers:
            self._say("spell: nothing indexed")
            return None
        path = self.index.path / SPELL_FILE
        stats = build_vocabulary(readers, path, config or SpellConfig(), progress=self._say)
        self._say(f"spell: {stats.line()}; {memory_line()}")
        return stats

    # --- whole runs ----------------------------------------------------------------------------

    def refine_again(self, source_id: str) -> SourceReport:
        """Refine the current generation of a source once more and index what changed: the adapter
        learned to read more of the same files (LibreTexts pages were app stubs, 2026-10-16).

        The sealed refined copy is moved to ``<source>/_replaced/<generation>-<stamp>`` (the diff base;
        never deleted here) and ``_replaced/PENDING`` names it until the index has the new copy, so an
        interrupted run resumes from the same base instead of replacing the new copy again."""
        spec = self.specs.get(source_id)
        if spec is None:
            return SourceReport(source_id, skipped="not in the registry")
        if spec.partitioned or not searchable(spec):
            return SourceReport(source_id, skipped="refine-again is for indexed, unpartitioned sources")
        current = self.vault.current(source_id)
        if current is None:
            return SourceReport(source_id, skipped="no current generation")
        gid = current.generation_id
        out_dir = self.refined_dir(spec, gid)
        replaced = out_dir.parent / REPLACED_DIR
        pending = replaced / PENDING_FILE
        old_dir: Path | None = None
        if pending.is_file():
            old_dir = replaced / pending.read_text(encoding="utf-8").strip()
            self._say(f"{source_id}: resuming the refine-again from {old_dir.name}")
        elif is_sealed(out_dir):
            replaced.mkdir(parents=True, exist_ok=True)
            old_dir = replaced / f"{gid}-{time.strftime('%Y%m%dT%H%M%S', time.gmtime())}"
            out_dir.rename(old_dir)
            _write_durably(pending, old_dir.name + "\n")
            self._say(f"{source_id}: {gid} moved to {REPLACED_DIR}/{old_dir.name}; refining again")
        result = self.refine(spec, current, self.vault.generation_path(source_id, gid))
        base = old_dir if old_dir is not None and is_sealed(old_dir) else None
        rep = self.index_generation(spec, gid, since=base)
        pending.unlink(missing_ok=True)
        self.finish()
        return SourceReport(
            source_id, ((gid, result.doc_count or 0),), rep.indexed, rep.upserted, rep.deleted
        )

    def build(self, source_ids: Sequence[str] | None = None) -> list[SourceReport]:
        """Refine and index the vault's current generation of each source (for a partitioned source,
        every current partition). Older generations are not refined: nothing reads them until the
        history view asks, and refining them costs as much as the current one. Ends with
        :meth:`finish` (one suggest rebuild)."""
        reports: list[SourceReport] = []
        wanted = list(source_ids) if source_ids else sorted(self.specs)
        for sid in wanted:
            spec = self.specs.get(sid)
            if spec is None:
                reports.append(SourceReport(sid, skipped="not in the registry"))
                continue
            if not searchable(spec):
                removed = self.purge(spec)
                if source_ids or removed:
                    reports.append(
                        SourceReport(sid, deleted=removed, skipped="not indexed (registry index: false)")
                    )
                continue
            gens = [g for g in self.vault.generations(sid) if g.status is GenerationStatus.SEALED]
            if not gens:
                if source_ids:
                    reports.append(SourceReport(sid, skipped="no sealed generation"))
                continue
            if self.adapter_for(spec) is None:
                reports.append(SourceReport(sid, skipped=f"no adapter installed for {spec.adapter!r}"))
                continue
            wanted_ids = {g.generation_id for g in self.vault.current_set(sid)}
            self._say(f"{sid}: {len(gens)} sealed generation(s), {len(wanted_ids)} current")
            refined: list[tuple[str, int]] = []
            for g in gens:
                if g.generation_id not in wanted_ids or is_sealed(self.refined_dir(spec, g.generation_id)):
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
        self.finish()
        return reports


def indexed_title_sources(
    refinery: Path, work: Path, state: Mapping[str, str], *, read_mb_per_sec: float = 0.0
) -> TitleSources:
    """:class:`TitleSources` of the generations ``state`` (``index-state.json``) records as indexed: each
    one's Refinery reader and redirect tables (a set generation's are its members').
    ``read_mb_per_sec`` > 0 paces the scans (the Refinery on the NAS shares the site's link)."""
    sources: list[IndexedSource] = []
    for sid in sorted(state):
        gen_dir = generation_dir(refinery, sid, state[sid])
        if not is_sealed(gen_dir):
            continue
        gens = (state[sid], *(set_members(gen_dir) or ()))
        tables = tuple(work / sid / g / "redirects.sqlite" for g in dict.fromkeys(gens))
        sources.append(IndexedSource(sid, RefineryReader(gen_dir, map_ids=False), tables))
    limiter = RateLimiter(read_mb_per_sec * 1e6, burst_seconds=5.0) if read_mb_per_sec > 0 else None
    return TitleSources(sources, limiter=limiter)
