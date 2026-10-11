"""Generation deltas in bounded memory: which documents a new Refinery generation adds, changes or drops.

The old generation is held as a compact *key index*: per live document a 128-bit fingerprint of its
``doc_id`` (blake2b), the first 64 bits of its content hash and its storage position, 32 bytes a
document (1 GB for 32M) in numpy, sorted for binary search. The new generation is then read part by
part: first only ``doc_id`` and ``content_hash`` of a part, then, for the rows that changed, the full
rows of just the row groups that hold them. Nothing is materialized per document in Python beyond
one part's ids, so a 32M-document generation needs about 1.5 GB, and an unchanged part costs two
narrow column reads.

Exactness: a document is unchanged when its 128-bit id fingerprint and 64-bit content prefix both
match (a false match needs a blake2b-128 collision between two ids, or a 64-bit collision between
two versions of one document's blake3 hash). Equal 64-bit halves of different ids are handled.
"""

from __future__ import annotations

import hashlib
from collections import OrderedDict
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from typing import cast

import numpy as np
import numpy.typing as npt
import pyarrow as pa

from wsp_core.ids import DocId
from wsp_core.model import ChangeKind, ChangeRecord, DocRecord
from wsp_index.refinery import COLUMNS, RefineryReader, record_from_row

U64 = npt.NDArray[np.uint64]
I64 = npt.NDArray[np.int64]

KEY_COLUMNS = ("doc_id", "content_hash")


def id_keys(doc_ids: Sequence[str]) -> tuple[U64, U64]:
    """128-bit blake2b fingerprints of ``doc_ids`` as two uint64 arrays."""
    if not doc_ids:
        return np.zeros(0, np.uint64), np.zeros(0, np.uint64)
    blob = b"".join(hashlib.blake2b(d.encode("utf-8"), digest_size=16).digest() for d in doc_ids)
    pairs = np.frombuffer(blob, dtype="<u8").reshape(-1, 2)
    return pairs[:, 0].copy(), pairs[:, 1].copy()


def content_keys(hashes: Sequence[str]) -> U64:
    """First 64 bits of each hex content hash (``""`` maps to 0, which then never matches a real one)."""
    if not hashes:
        return np.zeros(0, np.uint64)
    blob = b"".join(bytes.fromhex(h[:16].ljust(16, "0")) for h in hashes)
    return np.frombuffer(blob, dtype=">u8").astype(np.uint64)


@dataclass(frozen=True, slots=True)
class KeyIndex:
    """Live documents of one generation, sorted by id fingerprint."""

    k1: U64
    k2: U64
    content: U64
    part: npt.NDArray[np.uint32]
    pos: npt.NDArray[np.uint32]  # position within the part file

    def __len__(self) -> int:
        return len(self.k1)

    @classmethod
    def empty(cls) -> KeyIndex:
        z = np.zeros(0, np.uint64)
        u = np.zeros(0, np.uint32)
        return cls(z, z, z, u, u)

    @classmethod
    def build(cls, reader: RefineryReader, progress: Callable[[str], None] | None = None) -> KeyIndex:
        k1s: list[U64] = []
        k2s: list[U64] = []
        cs: list[U64] = []
        parts: list[npt.NDArray[np.uint32]] = []
        poss: list[npt.NDArray[np.uint32]] = []
        for part in range(reader.n_parts):
            table, pos = reader.part_table(part, KEY_COLUMNS)
            a, b = id_keys(cast(list[str], table.column("doc_id").to_pylist()))
            k1s.append(a)
            k2s.append(b)
            cs.append(content_keys(cast(list[str], table.column("content_hash").to_pylist())))
            parts.append(np.full(len(a), part, np.uint32))
            poss.append(pos.astype(np.uint32))
            if progress and part % 64 == 63:
                progress(
                    f"  keys {reader.source_id}/{reader.generation_id}: {part + 1}/{reader.n_parts} parts"
                )
        if not k1s:
            return cls.empty()
        k1, k2 = np.concatenate(k1s), np.concatenate(k2s)
        order = np.lexsort((k2, k1))
        return cls(
            k1[order],
            k2[order],
            np.concatenate(cs)[order],
            np.concatenate(parts)[order],
            np.concatenate(poss)[order],
        )

    def find(self, q1: U64, q2: U64) -> I64:
        """Index of each query key in this index, -1 when absent."""
        out = np.full(len(q1), -1, np.int64)
        if not len(self.k1) or not len(q1):
            return out
        lo = np.searchsorted(self.k1, q1, side="left")
        hi = np.searchsorted(self.k1, q1, side="right")
        single = (hi - lo) == 1
        idx = np.flatnonzero(single)
        cand = lo[idx]
        ok = self.k2[cand] == q2[idx]
        out[idx[ok]] = cand[ok]
        for i in np.flatnonzero((hi - lo) > 1):  # equal 64-bit halves of different ids: vanishingly rare
            sub = np.flatnonzero(self.k2[lo[i] : hi[i]] == q2[i])
            if len(sub):
                out[i] = int(lo[i] + sub[0])
        return out


@dataclass(slots=True)
class PartDelta:
    """Changes of one part of the new generation (ADDED / MODIFIED, with their documents)."""

    part: int
    records: list[ChangeRecord]
    documents: list[DocRecord]
    matched: I64  # positions in the old KeyIndex that this part still holds


class OldHashes:
    """Full old content hashes by (part, pos), a few parts' columns cached (old and new usually align)."""

    def __init__(self, reader: RefineryReader | None, cache_parts: int = 8) -> None:
        self.reader = reader
        self._cache: OrderedDict[int, pa.Table] = OrderedDict()
        self._size = cache_parts

    def column(self, part: int) -> pa.Table:
        assert self.reader is not None
        hit = self._cache.get(part)
        if hit is None:
            positions = np.arange(_part_rows(self.reader, part), dtype=np.int64)
            hit = self.reader.part_rows(part, positions, KEY_COLUMNS)
            self._cache[part] = hit
            while len(self._cache) > self._size:
                self._cache.popitem(last=False)
        else:
            self._cache.move_to_end(part)
        return hit

    def get(self, part: int, pos: int) -> tuple[str, str]:
        t = self.column(part)
        return cast(str, t.column("doc_id")[pos].as_py()), cast(str, t.column("content_hash")[pos].as_py())


def _part_rows(reader: RefineryReader, part: int) -> int:
    import pyarrow.parquet as pq

    return int(pq.ParquetFile(reader.dir / f"part-{part:05d}.parquet").metadata.num_rows)


def part_delta(
    new: RefineryReader, part: int, old: KeyIndex, old_hashes: OldHashes, *, with_documents: bool = True
) -> PartDelta:
    """Classify one part of ``new`` against ``old``; full rows are read only for changed documents."""
    table, pos = new.part_table(part, KEY_COLUMNS)
    ids = cast(list[str], table.column("doc_id").to_pylist())
    hashes = cast(list[str], table.column("content_hash").to_pylist())
    q1, q2 = id_keys(ids)
    found = old.find(q1, q2)
    hit = found >= 0
    same = np.zeros(len(ids), np.bool_)
    same[hit] = old.content[found[hit]] == content_keys(hashes)[hit]
    changed = np.flatnonzero(~same)
    records: list[ChangeRecord] = []
    gen = new.generation_id
    for i in changed.tolist():
        if found[i] < 0:
            records.append(ChangeRecord(DocId(ids[i]), ChangeKind.ADDED, gen, None, hashes[i]))
        else:
            j = int(found[i])
            _, old_hash = old_hashes.get(int(old.part[j]), int(old.pos[j]))
            records.append(ChangeRecord(DocId(ids[i]), ChangeKind.MODIFIED, gen, old_hash, hashes[i]))
    documents: list[DocRecord] = []
    if with_documents and len(changed):
        rows = new.part_rows(part, pos[changed], COLUMNS)
        documents = [record_from_row(r) for r in rows.to_pylist()]
    return PartDelta(part, records, documents, found[hit])


def removed_records(
    old: KeyIndex, matched: npt.NDArray[np.bool_], old_hashes: OldHashes, generation_id: str
) -> Iterator[ChangeRecord]:
    """Documents of the old generation that the new one no longer has, in storage order."""
    gone = np.flatnonzero(~matched)
    if not len(gone):
        return
    order = np.lexsort((old.pos[gone], old.part[gone]))
    for j in gone[order].tolist():
        doc_id, old_hash = old_hashes.get(int(old.part[j]), int(old.pos[j]))
        yield ChangeRecord(DocId(doc_id), ChangeKind.REMOVED, generation_id, old_hash, None)


def diff(old: RefineryReader | None, new: RefineryReader) -> Iterator[ChangeRecord]:
    """Change records turning ``old`` into ``new`` (part order, then removals). Renames are not detected."""
    keys = KeyIndex.build(old) if old is not None else KeyIndex.empty()
    hashes = OldHashes(old)
    matched = np.zeros(len(keys), np.bool_)
    for part in range(new.n_parts):
        d = part_delta(new, part, keys, hashes, with_documents=False)
        matched[d.matched] = True
        yield from d.records
    yield from removed_records(keys, matched, hashes, new.generation_id)
