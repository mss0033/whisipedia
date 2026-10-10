"""Refine sealed vault generations into Parquet, then index them (PLAN §9.1-9.3), on local disk.

Layout under ``index.root`` (local disk only, ADR-0002)::

    refinery/<source_id>/<generation_id>/   Refinery Parquet (wsp_index.refinery), CURRENT.txt per source
    lexical/                                Tantivy + suggest tries (TantivyIndex)
    changes.sqlite                          change feed (SqliteChangeFeed)
    index-state.json                        {source_id: generation indexed last}
    work/<source_id>/<generation_id>/       redirects.sqlite (aliases), adapter scratch

Every step is resumable: a refined generation is sealed by ``_SUCCESS.json`` and skipped next time
(an unsealed one is rebuilt from scratch); indexing diffs the last indexed generation against the
new one, so re-running after a crash re-applies at most one idempotent batch.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from wsp_adapters.entries import EntrySource, Redirect
from wsp_core.errors import FetchError
from wsp_core.ids import InvalidDocIdError, make_doc_id
from wsp_core.model import DocRecord, FetchResult, Generation, GenerationStatus, RemoteFile, SourceSpec
from wsp_core.ports import ChangeFeed, Ctx, SourceAdapter, Vault
from wsp_index import RefineryReader, RefineryWriter, TantivyIndex, ensure_local, refresh
from wsp_index.refinery import CURRENT_FILE, generation_dir, is_sealed
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


def _alias_title(native_id: str) -> str:
    return native_id.rsplit("/", 1)[-1].replace("_", " ").strip()


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

    def refine(self, spec: SourceSpec, generation: Generation, generation_path: Path) -> RefineResult:
        """Write the generation's documents to Refinery Parquet (skipped if already sealed)."""
        gid = generation.generation_id
        out_dir = generation_dir(self.refinery, spec.id, gid)
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
        with closing(sqlite3.connect(redirects)) as db, RefineryWriter(self.refinery, spec.id, gid) as writer:
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
                batch.append((entry.target, _alias_title(entry.native_id)))
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
        self._say(
            f"  indexed {spec.id}/{generation_id}: +{stats.upserted:,} upserted, -{stats.deleted:,} removed"
        )
        return SourceReport(spec.id, indexed=generation_id, upserted=stats.upserted, deleted=stats.deleted)

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
                if is_sealed(generation_dir(self.refinery, sid, g.generation_id)):
                    continue
                result = self.refine(spec, g, self.vault.generation_path(sid, g.generation_id))
                refined.append((g.generation_id, result.doc_count or 0))
            current = self.vault.current(sid)
            if current is None:
                reports.append(SourceReport(sid, tuple(refined), skipped="no current generation"))
                continue
            rep = self.index_generation(spec, current.generation_id)
            reports.append(
                SourceReport(sid, tuple(refined), rep.indexed, rep.upserted, rep.deleted, rep.skipped)
            )
        return reports
