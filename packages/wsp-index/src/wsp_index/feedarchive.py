"""Sealed archive ranges of the change feed, as Parquet (ADR-0011).

The feed only grows, and clients read it as "everything after cursor N". Old ranges move out of the
local SQLite database into write-once files that may live on the NAS::

    <root>/<epoch>/<first seq:016x>-<last seq:016x>.parquet

One file per range of ``seq`` values: rows sorted by ``seq``, zstd, row groups of
:data:`ROW_GROUP_ROWS` (their min/max statistics let a reader skip to a cursor). The epoch directory
keeps a rebuilt feed's archive apart from the old one's. A file is written as ``*.partial``, read back
and checked, then renamed, so a reader never sees a torn range. Files are never rewritten.

Reads never memory-map: a page fault on a ``soft`` SMB mount is a SIGBUS, not an ``OSError``.
"""

from __future__ import annotations

import itertools
import os
import re
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import cast

import pyarrow as pa
import pyarrow.parquet as pq

ROW_GROUP_ROWS = 65_536
SCHEMA = pa.schema(
    [
        ("seq", pa.int64()),
        ("doc_id", pa.string()),
        ("kind", pa.string()),
        ("generation_id", pa.string()),
        ("old_hash", pa.string()),
        ("new_hash", pa.string()),
        ("renamed_from", pa.string()),
    ]
)
_NAME = re.compile(r"^([0-9a-f]{16})-([0-9a-f]{16})\.parquet$")


class ArchiveError(RuntimeError):
    """An archive range failed its read-back check, or ranges overlap."""


class ChangeArchive:
    """Archived ranges of one or more feeds (one directory per feed epoch)."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def ranges(self, epoch: str) -> list[tuple[int, int, Path]]:
        """``(first, last, path)`` of each sealed range, in order."""
        directory = self.root / epoch
        if not directory.is_dir():
            return []
        out: list[tuple[int, int, Path]] = []
        for p in directory.iterdir():
            m = _NAME.match(p.name)
            if m:
                out.append((int(m.group(1), 16), int(m.group(2), 16), p))
        out.sort()
        for (_, last, a), (first, _, b) in itertools.pairwise(out):
            if first <= last:
                raise ArchiveError(f"overlapping archive ranges: {a.name} and {b.name}")
        return out

    def last(self, epoch: str) -> int:
        """The highest archived ``seq`` (0 when nothing is archived)."""
        ranges = self.ranges(epoch)
        return ranges[-1][1] if ranges else 0

    def rows(self, epoch: str, after: int, through: int, limit: int) -> list[tuple[object, ...]]:
        """At most ``limit`` rows with ``after < seq <= through``, in ``seq`` order."""
        out: list[tuple[object, ...]] = []
        for first, last, path in self.ranges(epoch):
            if last <= after:
                continue
            if first > through or len(out) >= limit:
                break
            pf = pq.ParquetFile(path, memory_map=False)
            meta = pf.metadata
            for i in range(meta.num_row_groups):
                stats = meta.row_group(i).column(0).statistics
                if stats is not None and stats.has_min_max and int(cast(int, stats.max)) <= after:
                    continue
                table = pf.read_row_group(i)  # pyright: ignore[reportUnknownMemberType]  # pyarrow-stubs leave some parameter types unknown
                for row in zip(*(table.column(c).to_pylist() for c in SCHEMA.names), strict=True):
                    seq = int(cast(int, row[0]))
                    if seq <= after:
                        continue
                    if seq > through or len(out) >= limit:
                        return out
                    out.append(row)
        return out

    def write(self, epoch: str, first: int, last: int, batches: Iterable[Sequence[Sequence[object]]]) -> Path:
        """Seal the range ``first..last`` from row batches (``seq`` ascending, every ``seq`` in range)."""
        directory = self.root / epoch
        directory.mkdir(parents=True, exist_ok=True)
        if any(f <= last and first <= lst for f, lst, _ in self.ranges(epoch)):
            raise ArchiveError(f"range {first}-{last} overlaps an archived range in {directory}")
        final = directory / f"{first:016x}-{last:016x}.parquet"
        partial = final.with_name(final.name + ".partial")
        rows = 0
        prev = first - 1
        try:
            with pq.ParquetWriter(partial, SCHEMA, compression="zstd") as writer:
                for batch in batches:
                    if not batch:
                        continue
                    columns = list(zip(*batch, strict=True))
                    seqs = [int(str(s)) for s in columns[0]]
                    if seqs[0] <= prev or seqs[-1] > last or any(b <= a for a, b in itertools.pairwise(seqs)):
                        raise ArchiveError(f"rows for {final.name} are out of order or out of range")
                    prev = seqs[-1]
                    rows += len(seqs)
                    table = pa.Table.from_arrays(
                        [pa.array(seqs, pa.int64()), *(pa.array(c, pa.string()) for c in columns[1:])],
                        schema=SCHEMA,
                    )
                    writer.write_table(table, row_group_size=ROW_GROUP_ROWS)
            with partial.open("rb") as fh:
                os.fsync(fh.fileno())
            check = pq.ParquetFile(partial, memory_map=False).metadata
            if check.num_rows != rows:
                raise ArchiveError(f"{partial}: {check.num_rows} rows read back, {rows} written")
            os.replace(partial, final)
            fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        finally:
            partial.unlink(missing_ok=True)
        return final
