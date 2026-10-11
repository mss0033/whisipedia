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
from typing import Protocol, cast, runtime_checkable

import numpy as np

from wsp_core.ids import DocId
from wsp_core.model import ChangeKind, ChangeRecord, DocRecord
from wsp_core.ports import ChangeFeed, LexicalIndex
from wsp_index.delta import KeyIndex, OldHashes, id_keys, part_delta, removed_records
from wsp_index.refinery import RefineryReader

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

    def source_doc_ids(self, source_id: str, *, batch: int = 50_000) -> Iterator[list[str]]: ...


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
    but ``new`` lacks (the old Refinery generation was deleted) are found from the index and removed.
    """
    key = f"refresh:{new.source_id}"
    target = {"old": old.generation_id if old is not None else None, "new": new.generation_id}
    state = {"parts": 0, "removed": 0, "upserted": 0, "deleted": 0}
    checkpointing = cast(CheckpointingFeed, feed) if isinstance(feed, CheckpointingFeed) else None
    if checkpointing is not None and (saved := checkpointing.checkpoint(key)):
        data = json.loads(saved)
        if {k: data.get(k) for k in target} == target:
            state = {k: int(data.get(k, 0)) for k in state}
    resumed = state["parts"] > 0 or state["removed"] > 0
    bulk = cast(BulkIndex, index) if isinstance(index, BulkIndex) else None
    held_before = bulk is not None and old is None and bulk.has_source(new.source_id)
    # A first load of a source the index has never held needs no delete-before-add per document.
    add_only = bulk is not None and old is None and (resumed or not held_before)

    def publish(records: Sequence[ChangeRecord]) -> str | None:
        if feed is None:
            return None
        if checkpointing is not None:
            return checkpointing.publish(records, checkpoint=(key, json.dumps({**target, **state})))
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
            if add_only and bulk is not None:
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
        gone: Iterator[ChangeRecord] = removed_records(keys, matched, hashes, new.generation_id)
    elif held_before and bulk is not None:
        gone = _orphans(bulk, new)
    else:
        gone = iter(())
    for _ in itertools.islice(gone, state["removed"]):  # deleted and published before a crash
        pass
    while chunk := list(itertools.islice(gone, DELETE_CHUNK)):
        index.delete([c.doc_id for c in chunk])
        _commit(index)
        state["removed"] += len(chunk)
        state["deleted"] += len(chunk)
        cursor = publish(chunk) or cursor
    return SyncStats(state["upserted"], state["deleted"], cursor)


def _orphans(index: BulkIndex, new: RefineryReader) -> Iterator[ChangeRecord]:
    """Indexed documents of ``new``'s source that ``new`` does not hold (sorted by doc id)."""
    present = KeyIndex.build(new)
    for ids in index.source_doc_ids(new.source_id):
        q1, q2 = id_keys(ids)
        missing = np.flatnonzero(present.find(q1, q2) < 0)
        for i in missing.tolist():
            yield ChangeRecord(DocId(ids[i]), ChangeKind.REMOVED, new.generation_id, None, None)
