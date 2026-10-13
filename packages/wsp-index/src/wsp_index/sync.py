"""Glue between Refinery generations, the lexical index and the change feed.

:func:`refresh` brings the index from one generation of a source to the next in bounded memory and
resumably (see :mod:`wsp_index.delta` for the diff):

* the new generation is applied part by part; after each part the index commits and the part's
  change records are published, **index first**, so a feed consumer never sees a change the search
  engine cannot serve yet;
* with a checkpointing feed (``SqliteChangeFeed``) the progress marker is written in the same
  transaction as the part's change records, so a crash anywhere resumes at the first part not yet
  published, without losing or repeating a feed record (re-applying an upsert to the index is
  idempotent);
* documents the new generation dropped are deleted last, in committed chunks, the same way.

Peak memory is the old generation's key index (~32 bytes a document) plus one part's changed rows.
"""

from __future__ import annotations

import itertools
import json
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast, runtime_checkable

import numpy as np

from wsp_core.ids import DocId
from wsp_core.model import ChangeKind, ChangeRecord, DocRecord
from wsp_core.ports import ChangeFeed, LexicalIndex
from wsp_index.delta import KeyIndex, OldHashes, id_keys, part_delta, removed_records
from wsp_index.refinery import PARTS_DIR, RefineryReader, is_sealed

DELETE_CHUNK = 50_000


@dataclass(frozen=True, slots=True)
class SyncStats:
    upserted: int
    deleted: int
    cursor: str | None = None


@runtime_checkable
class CheckpointingFeed(Protocol):
    """A change feed that stores a progress marker atomically with published records."""

    def publish(
        self, changes: Iterable[ChangeRecord], *, checkpoint: tuple[str, str] | None = None
    ) -> str: ...

    def checkpoint(self, key: str) -> str | None: ...


@runtime_checkable
class BulkIndex(Protocol):
    """Index extras used by bulk loads (``TantivyIndex``): add without delete, deferred suggest."""

    def upsert(self, docs: Iterable[DocRecord], *, replace: bool = True) -> int: ...

    def commit(self, *, rebuild_suggest: bool = True) -> None: ...

    def has_source(self, source_id: str) -> bool: ...

    def present(self, doc_ids: Sequence[str]) -> set[str]: ...


def index_generation(index: LexicalIndex, reader: RefineryReader, *, batch_size: int = 5_000) -> int:
    """Full load of a generation into ``index`` (commits once at the end)."""
    n = 0
    records = reader.records()
    while batch := list(itertools.islice(records, batch_size)):
        n += index.upsert(batch)
    index.commit()
    return n


def apply_changes(
    index: LexicalIndex, changes: Iterable[ChangeRecord], new: RefineryReader, *, batch_size: int = 1_000
) -> SyncStats:
    """Apply change records to ``index`` using documents from the new generation, then commit.

    Point lookups per document: for small change sets only (``refresh`` streams whole generations).
    Tombstones (``REMOVED``) delete from the index; history stays in the Refinery.
    """
    upserted = deleted = 0
    to_upsert: list[DocId] = []
    to_delete: list[DocId] = []

    def flush() -> None:
        nonlocal upserted, deleted
        if to_delete:
            deleted += index.delete(to_delete)
            to_delete.clear()
        if to_upsert:
            docs = [d for d in (new.get(i) for i in to_upsert) if d is not None]
            upserted += index.upsert(docs)
            to_upsert.clear()

    for ch in changes:
        if ch.kind is ChangeKind.REMOVED:
            to_delete.append(ch.doc_id)
        else:
            if ch.kind is ChangeKind.RENAMED and ch.renamed_from is not None:
                to_delete.append(ch.renamed_from)
            to_upsert.append(ch.doc_id)
        if len(to_upsert) + len(to_delete) >= batch_size:
            flush()
    flush()
    index.commit()
    return SyncStats(upserted, deleted)


def _commit(index: LexicalIndex) -> None:
    if isinstance(index, BulkIndex):
        cast(BulkIndex, index).commit(rebuild_suggest=False)  # one suggest rebuild at the end, not per part
    else:
        index.commit()


def refresh(
    index: LexicalIndex,
    feed: ChangeFeed | None,
    old: RefineryReader | None,
    new: RefineryReader,
    *,
    progress: Callable[[str], None] | None = None,
) -> SyncStats:
    """Diff two generations and apply the difference to ``index`` and ``feed``, part by part.

    Resumable with a :class:`CheckpointingFeed`; the caller rebuilds derived structures (suggest)
    once afterwards. Without an ``old`` generation, documents of this source that the index still holds
    but ``new`` lacks are found among the source's other Refinery generations and removed (a document
    of a generation deleted from the Refinery by hand is not found: ``wsp index rebuild-lexical``).

    Resuming is idempotent (audit 2026-10-12 #3, #4): whether the index held the source before the run
    began is recorded with the first checkpoint, since by a resume the run's own parts are in it; the
    part after the checkpoint may be committed already and is always replaced, never added twice; and
    the ids of a chunk of removals found from the index are recorded before it is deleted (the index
    forgets them once deleted), so their change records are published even after a crash.
    """
    key = f"refresh:{new.source_id}"
    target = {"old": old.generation_id if old is not None else None, "new": new.generation_id}
    state = {"parts": 0, "removed": 0, "upserted": 0, "deleted": 0}
    held: bool | None = None  # the index held this source before the run began (``old is None`` only)
    pending: list[str] = []  # removals found from the index, being deleted when the run stopped
    checkpointing = cast(CheckpointingFeed, feed) if isinstance(feed, CheckpointingFeed) else None
    if checkpointing is not None and (saved := checkpointing.checkpoint(key)):
        data = json.loads(saved)
        if {k: data.get(k) for k in target} == target:
            state = {k: int(data.get(k, 0)) for k in state}
            held = bool(data["held"]) if "held" in data else None
            pending = [str(d) for d in data.get("pending", ())]
    bulk = cast(BulkIndex, index) if isinstance(index, BulkIndex) else None
    if state["upserted"] > 0 and bulk is not None and not bulk.has_source(new.source_id):
        # The index lost what the checkpoint says was loaded (it was rebuilt since): start again.
        state, held, pending = dict.fromkeys(state, 0), None, []
    resumed = state["parts"] > 0 or state["removed"] > 0 or bool(pending)
    if held is None:  # a checkpoint from before ``held`` was recorded: assume the worst
        held = bulk is not None and old is None and (resumed or bulk.has_source(new.source_id))
    if old is None and held and bulk is not None and progress and not _other_generations(new):
        # Then the index can only hold this generation's documents, unless a generation was deleted from
        # the Refinery by hand: its leftovers cannot be listed (ADR-0011).
        progress(
            f"  {new.source_id}: no other Refinery generation to find leftover documents in; if one was "
            "deleted by hand, `wsp index rebuild-lexical` clears what it left in the search index"
        )
    # A first load of a source the index has never held needs no delete-before-add per document.
    add_only = bulk is not None and old is None and not held
    resume_part = state["parts"] if resumed else -1  # committed perhaps, but not checkpointed

    def publish(records: Sequence[ChangeRecord]) -> str | None:
        if feed is None:
            return None
        if checkpointing is not None:
            mark = {**target, **state, "held": int(held), "pending": pending}
            return checkpointing.publish(records, checkpoint=(key, json.dumps(mark)))
        return feed.publish(records)

    keys = KeyIndex.build(old, progress) if old is not None else KeyIndex.empty()
    hashes = OldHashes(old)
    matched = np.zeros(len(keys), np.bool_)
    cursor: str | None = None
    for part in range(new.n_parts):
        if part < state["parts"]:  # done before a crash: only re-mark what it matched
            d = part_delta(new, part, keys, hashes, with_documents=False)
            matched[d.matched] = True
            continue
        d = part_delta(new, part, keys, hashes)
        matched[d.matched] = True
        if d.documents:
            if add_only and bulk is not None and part != resume_part:
                bulk.upsert(d.documents, replace=False)
            else:
                index.upsert(d.documents)
        _commit(index)
        state["parts"] = part + 1
        state["upserted"] += len(d.documents)
        cursor = publish(d.records) or cursor
        if progress and (part % 16 == 15 or part == new.n_parts - 1):
            progress(
                f"  index {new.source_id}/{new.generation_id}: part {part + 1}/{new.n_parts}, "
                f"{state['upserted']:,} upserted"
            )
    if old is not None:
        # Deterministic order: the first ``removed`` were deleted and published before a crash.
        gone: Iterator[ChangeRecord] = itertools.islice(
            removed_records(keys, matched, hashes, new.generation_id), state["removed"], None
        )
    elif held and bulk is not None:
        # A chunk recorded, then perhaps deleted, but not published goes first (deleting again is
        # harmless); the scan resumes past it.
        redo = [_removal(d, new) for d in pending]
        gone = itertools.chain(redo, _orphans(bulk, new))
    else:
        gone = iter(())
    while chunk := list(itertools.islice(gone, DELETE_CHUNK)):
        ids = [c.doc_id for c in chunk]
        if old is None and checkpointing is not None:
            # Found from the index, which forgets them once deleted: recorded before the delete, so a
            # crash between the delete and the publish below cannot lose their change records.
            pending = [str(d) for d in ids]
            publish([])
        index.delete(ids)
        _commit(index)
        state["removed"] += len(chunk)
        state["deleted"] += len(chunk)
        if old is None:
            pending = []
        cursor = publish(chunk) or cursor
    return SyncStats(state["upserted"], state["deleted"], cursor)


def _removal(doc_id: str, new: RefineryReader) -> ChangeRecord:
    return ChangeRecord(DocId(doc_id), ChangeKind.REMOVED, new.generation_id, None, None)


def _orphans(index: BulkIndex, new: RefineryReader) -> Iterator[ChangeRecord]:
    """Indexed documents of ``new``'s source that ``new`` does not hold: every document of the source's
    other sealed generations that ``new`` lacks and the index still has, each once. A resumed run
    scans again; what it deleted before is no longer in the index."""
    present = KeyIndex.build(new)
    seen: set[str] = set()
    for gen_dir in _other_generations(new):
        reader = RefineryReader(gen_dir, map_ids=False)
        for batch in reader.scan(["doc_id"], batch_size=DELETE_CHUNK):
            ids = cast(list[str], batch.column(0).to_pylist())
            q1, q2 = id_keys(ids)
            missing = [ids[i] for i in np.flatnonzero(present.find(q1, q2) < 0).tolist()]
            if not missing:
                continue
            alive = index.present(missing)
            for doc_id in missing:
                if doc_id in alive and doc_id not in seen:
                    seen.add(doc_id)
                    yield _removal(doc_id, new)


def _other_generations(new: RefineryReader) -> list[Path]:
    """The sealed generations of ``new``'s source other than ``new`` (sets and partitions included)."""
    source_dir = new.dir.parent.parent if new.dir.parent.name == PARTS_DIR else new.dir.parent
    found = [p for p in source_dir.iterdir() if p.is_dir() and p != new.dir and is_sealed(p)]
    parts = source_dir / PARTS_DIR
    if parts.is_dir():
        found += [p for p in parts.iterdir() if p.is_dir() and p != new.dir and is_sealed(p)]
    return sorted(found)
