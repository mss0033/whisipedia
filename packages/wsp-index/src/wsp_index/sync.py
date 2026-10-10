"""Glue between Refinery generations, the lexical index and the change feed."""

from __future__ import annotations

import itertools
from collections.abc import Iterable
from dataclasses import dataclass

from wsp_core.ids import DocId
from wsp_core.model import ChangeKind, ChangeRecord
from wsp_core.ports import ChangeFeed, LexicalIndex
from wsp_index.refinery import RefineryReader, diff_generations


@dataclass(frozen=True, slots=True)
class SyncStats:
    upserted: int
    deleted: int
    cursor: str | None = None


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


def refresh(
    index: LexicalIndex, feed: ChangeFeed | None, old: RefineryReader | None, new: RefineryReader
) -> SyncStats:
    """Diff two generations, update the index, then publish the changes (index first, so a feed
    consumer never sees a change the search engine cannot serve yet)."""
    changes = list(diff_generations(old, new))
    stats = apply_changes(index, changes, new)
    cursor = feed.publish(changes) if feed is not None else None
    return SyncStats(stats.upserted, stats.deleted, cursor)
