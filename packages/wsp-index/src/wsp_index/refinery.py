"""Refinery storage: ``DocRecord`` streams as Parquet, one directory per source and generation (PLAN §9.1).

Layout (readable without this code)::

    <root>/<source_id>/<generation_id>/
        part-00000.parquet   records sorted by doc_id, zstd, small row groups
        part-00001.parquet
        _ids.parquet         point-lookup index: blake2b-64(doc_id) -> (part, row_group, row), sorted
        _SUCCESS.json        seal marker, written last: {"format": 1, "docs": n, "parts": k}

A generation without ``_SUCCESS.json`` is incomplete and is ignored by readers. Every file is
written as ``*.partial`` and renamed after a read-back check, so a crash never leaves a torn file.

Point lookup: Parquet min/max statistics only help when a whole generation is globally sorted,
which a streaming writer cannot guarantee without an external sort. Instead each generation carries
a tiny sorted hash index (16 bytes per document; 112 MB for 7M articles) that is loaded into numpy
and binary-searched, so a lookup costs one ``searchsorted`` plus decoding one small row group.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from collections import Counter, OrderedDict
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Self, cast

import numpy as np
import numpy.typing as npt
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from wsp_core.ids import DocId, content_hash
from wsp_core.model import ChangeKind, ChangeRecord, DocRecord

FORMAT_VERSION = 1
SUCCESS_FILE = "_SUCCESS.json"
IDS_FILE = "_ids.parquet"
CURRENT_FILE = "CURRENT.txt"

DEFAULT_ROW_GROUP_ROWS = 256
DEFAULT_DOCS_PER_FILE = 50_000
DEFAULT_ZSTD_LEVEL = 6

SCHEMA = pa.schema(
    [
        pa.field("doc_id", pa.string(), nullable=False),
        pa.field("source_id", pa.string(), nullable=False),
        pa.field("native_id", pa.string(), nullable=False),
        pa.field("title", pa.string(), nullable=False),
        pa.field("lang", pa.string(), nullable=False),
        pa.field("license", pa.string(), nullable=False),
        pa.field("text", pa.string(), nullable=False),
        pa.field("revision", pa.string()),
        pa.field("timestamp", pa.timestamp("us", tz="UTC")),
        pa.field("url_native", pa.string()),
        pa.field("html_ref", pa.string()),
        pa.field("entities", pa.list_(pa.string()), nullable=False),
        pa.field("links", pa.list_(pa.string()), nullable=False),
        pa.field("content_hash", pa.string(), nullable=False),
    ]
)
COLUMNS: tuple[str, ...] = tuple(SCHEMA.names)

_IDS_SCHEMA = pa.schema(
    [("h", pa.uint64()), ("part", pa.uint32()), ("rg", pa.uint32()), ("row", pa.uint32())]
)


class RefineryError(RuntimeError):
    pass


class DuplicateDocIdError(RefineryError):
    pass


def id_hash(doc_id: str) -> int:
    """64-bit key used by the lookup index (collisions are resolved by comparing the stored id)."""
    return int.from_bytes(hashlib.blake2b(doc_id.encode("utf-8"), digest_size=8).digest(), "little")


def generation_dir(root: Path, source_id: str, generation_id: str) -> Path:
    return root / source_id / generation_id


def is_sealed(gen_dir: Path) -> bool:
    return (gen_dir / SUCCESS_FILE).is_file()


def sealed_generations(root: Path, source_id: str) -> list[str]:
    """Sealed generation ids of a source, oldest first (generation ids must sort chronologically)."""
    src = root / source_id
    if not src.is_dir():
        return []
    return sorted(p.name for p in src.iterdir() if p.is_dir() and is_sealed(p))


def current_generation(root: Path, source_id: str) -> str | None:
    """``CURRENT.txt`` if present (vault-style pointer), else the newest sealed generation."""
    pointer = root / source_id / CURRENT_FILE
    if pointer.is_file():
        gen = pointer.read_text(encoding="utf-8").strip()
        if gen and is_sealed(generation_dir(root, source_id, gen)):
            return gen
    gens = sealed_generations(root, source_id)
    return gens[-1] if gens else None


# --- record <-> row ----------------------------------------------------------------------------


def _utc(ts: datetime | None) -> datetime | None:
    if ts is None:
        return None
    return ts.replace(tzinfo=UTC) if ts.tzinfo is None else ts.astimezone(UTC)


def _to_row(rec: DocRecord) -> dict[str, object]:
    return {
        "doc_id": rec.doc_id,
        "source_id": rec.source_id,
        "native_id": rec.native_id,
        "title": rec.title,
        "lang": rec.lang,
        "license": rec.license,
        "text": rec.text,
        "revision": rec.revision,
        "timestamp": _utc(rec.timestamp),
        "url_native": rec.url_native,
        "html_ref": rec.html_ref,
        "entities": list(rec.entities),
        "links": list(rec.links),
        "content_hash": rec.content_hash,
    }


def record_from_row(row: Mapping[str, Any]) -> DocRecord:
    """Build a ``DocRecord`` from a row dict (timestamps come back timezone-aware, UTC)."""
    return DocRecord(
        doc_id=DocId(row["doc_id"]),
        source_id=row["source_id"],
        native_id=row["native_id"],
        title=row["title"],
        lang=row["lang"],
        license=row["license"],
        text=row["text"],
        revision=row["revision"],
        timestamp=row["timestamp"],
        url_native=row["url_native"],
        html_ref=row["html_ref"],
        entities=tuple(row["entities"] or ()),
        links=tuple(row["links"] or ()),
        content_hash=row["content_hash"],
    )


def _atomic_replace(partial: Path, final: Path) -> None:
    with partial.open("rb") as fh:
        os.fsync(fh.fileno())
    os.replace(partial, final)


# --- writer ------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class WriteStats:
    docs: int
    parts: int
    bytes: int


class RefineryWriter:
    """Streams ``DocRecord``s of one source generation to Parquet.

    Records are buffered, sorted by ``doc_id`` and flushed every ``docs_per_file`` records. A missing
    ``content_hash`` is filled in from the text. ``close()`` writes the id index and seals.
    """

    def __init__(
        self,
        root: Path,
        source_id: str,
        generation_id: str,
        *,
        row_group_rows: int = DEFAULT_ROW_GROUP_ROWS,
        docs_per_file: int = DEFAULT_DOCS_PER_FILE,
        zstd_level: int = DEFAULT_ZSTD_LEVEL,
    ) -> None:
        if row_group_rows < 1 or docs_per_file < 1:
            raise ValueError("row_group_rows and docs_per_file must be positive")
        self.dir = generation_dir(root, source_id, generation_id)
        if is_sealed(self.dir):
            raise RefineryError(f"generation already sealed: {self.dir}")
        self.dir.mkdir(parents=True, exist_ok=True)
        for stale in self.dir.glob("*.partial"):
            stale.unlink()
        self.source_id = source_id
        self.generation_id = generation_id
        self._rg_rows = row_group_rows
        self._per_file = docs_per_file
        self._zstd = zstd_level
        self._buf: list[DocRecord] = []
        self._parts = 0
        self._bytes = 0
        self._docs = 0
        self._idx_h: list[npt.NDArray[np.uint64]] = []
        self._idx_loc: list[npt.NDArray[np.uint32]] = []
        self._closed = False

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type: type[BaseException] | None, *_: object) -> None:
        if exc_type is None:
            self.close()

    def write(self, records: Iterable[DocRecord]) -> int:
        if self._closed:
            raise RefineryError("writer is closed")
        n = 0
        for rec in records:
            if rec.source_id != self.source_id:
                raise RefineryError(f"record {rec.doc_id} belongs to {rec.source_id}, not {self.source_id}")
            if not rec.content_hash:
                rec = replace(rec, content_hash=content_hash(rec.text))
            self._buf.append(rec)
            n += 1
            if len(self._buf) >= self._per_file:
                self._flush()
        return n

    def _flush(self) -> None:
        if not self._buf:
            return
        self._buf.sort(key=lambda r: r.doc_id)
        for a, b in zip(self._buf, self._buf[1:], strict=False):
            if a.doc_id == b.doc_id:
                raise DuplicateDocIdError(a.doc_id)
        table = pa.Table.from_pylist([_to_row(r) for r in self._buf], schema=SCHEMA)
        name = f"part-{self._parts:05d}.parquet"
        final = self.dir / name
        partial = self.dir / (name + ".partial")
        pq.write_table(  # pyright: ignore[reportUnknownMemberType]  # stubs: partially unknown params
            table,
            partial,
            row_group_size=self._rg_rows,
            compression="zstd",
            compression_level=self._zstd,
            # A list of columns is valid (pyarrow docs); the stubs only allow bool.
            use_dictionary=["source_id", "lang", "license"],  # pyright: ignore[reportArgumentType]
            write_statistics=["doc_id"],
        )
        meta = pq.ParquetFile(partial).metadata
        if meta.num_rows != len(self._buf):
            raise RefineryError(f"read-back mismatch in {partial}")
        # Row -> (row group, offset) from the actual row-group sizes written.
        rg_sizes = np.array([meta.row_group(i).num_rows for i in range(meta.num_row_groups)], dtype=np.int64)
        rg_of_row = np.repeat(np.arange(len(rg_sizes), dtype=np.uint32), rg_sizes)
        starts = np.concatenate(([0], np.cumsum(rg_sizes)[:-1]))
        row_in_rg = (np.arange(len(self._buf), dtype=np.int64) - starts[rg_of_row]).astype(np.uint32)
        loc = np.empty((len(self._buf), 3), dtype=np.uint32)
        loc[:, 0] = self._parts
        loc[:, 1] = rg_of_row
        loc[:, 2] = row_in_rg
        self._idx_h.append(np.fromiter((id_hash(r.doc_id) for r in self._buf), np.uint64, len(self._buf)))
        self._idx_loc.append(loc)
        _atomic_replace(partial, final)
        self._bytes += final.stat().st_size
        self._docs += len(self._buf)
        self._parts += 1
        self._buf.clear()

    def close(self) -> WriteStats:
        if self._closed:
            return WriteStats(self._docs, self._parts, self._bytes)
        self._flush()
        h = np.concatenate(self._idx_h) if self._idx_h else np.empty(0, np.uint64)
        loc = np.concatenate(self._idx_loc) if self._idx_loc else np.empty((0, 3), np.uint32)
        order = np.argsort(h, kind="stable")
        h, loc = h[order], loc[order]
        self._check_cross_file_duplicates(h, loc)
        ids = pa.Table.from_arrays(
            [pa.array(h), pa.array(loc[:, 0]), pa.array(loc[:, 1]), pa.array(loc[:, 2])],
            schema=_IDS_SCHEMA,
        )
        partial = self.dir / (IDS_FILE + ".partial")
        pq.write_table(ids, partial, compression="zstd", row_group_size=1 << 20)  # pyright: ignore[reportUnknownMemberType]  # pyarrow-stubs leave some parameter types unknown
        _atomic_replace(partial, self.dir / IDS_FILE)
        success = {"format": FORMAT_VERSION, "docs": self._docs, "parts": self._parts}
        partial = self.dir / (SUCCESS_FILE + ".partial")
        partial.write_text(json.dumps(success, sort_keys=True) + "\n", encoding="utf-8")
        _atomic_replace(partial, self.dir / SUCCESS_FILE)
        self._closed = True
        return WriteStats(self._docs, self._parts, self._bytes)

    def _check_cross_file_duplicates(self, h: npt.NDArray[np.uint64], loc: npt.NDArray[np.uint32]) -> None:
        if len(h) < 2:
            return
        dup = np.nonzero(h[1:] == h[:-1])[0]
        if len(dup) == 0:
            return
        files: dict[int, pq.ParquetFile] = {}

        def doc_id_at(i: int) -> str:
            part, rg, row = (int(x) for x in loc[i])
            pf = files.setdefault(part, pq.ParquetFile(self.dir / f"part-{part:05d}.parquet"))
            table = pf.read_row_group(rg, columns=["doc_id"])  # pyright: ignore[reportUnknownMemberType]  # pyarrow-stubs leave some parameter types unknown
            return cast(str, table.column(0)[row].as_py())

        for i in dup.tolist():
            a, b = doc_id_at(i), doc_id_at(i + 1)
            if a == b:
                raise DuplicateDocIdError(a)


# --- reader ------------------------------------------------------------------------------------


class RefineryReader:
    """Point lookups and scans over one sealed generation."""

    def __init__(self, gen_dir: Path, *, cache_row_groups: int = 32) -> None:
        if not is_sealed(gen_dir):
            raise RefineryError(f"not a sealed refinery generation: {gen_dir}")
        self.dir = gen_dir
        self.generation_id = gen_dir.name
        self.source_id = gen_dir.parent.name
        meta = json.loads((gen_dir / SUCCESS_FILE).read_text(encoding="utf-8"))
        if meta.get("format") != FORMAT_VERSION:
            raise RefineryError(f"unsupported refinery format {meta.get('format')!r} in {gen_dir}")
        self._n_parts = int(meta["parts"])
        ids = pq.read_table(gen_dir / IDS_FILE)  # pyright: ignore[reportUnknownMemberType]  # pyarrow-stubs leave some parameter types unknown
        self._h: npt.NDArray[np.uint64] = ids.column("h").to_numpy()
        self._part: npt.NDArray[np.uint32] = ids.column("part").to_numpy()
        self._rg: npt.NDArray[np.uint32] = ids.column("rg").to_numpy()
        self._row: npt.NDArray[np.uint32] = ids.column("row").to_numpy()
        self._files: dict[int, pq.ParquetFile] = {}
        self._cache: OrderedDict[tuple[int, int, tuple[str, ...]], pa.Table] = OrderedDict()
        self._cache_size = cache_row_groups
        self._lock = threading.Lock()

    def __len__(self) -> int:
        return len(self._h)

    @property
    def parts(self) -> list[Path]:
        return [self.dir / f"part-{i:05d}.parquet" for i in range(self._n_parts)]

    def _file(self, part: int) -> pq.ParquetFile:
        with self._lock:
            pf = self._files.get(part)
            if pf is None:
                # No memory_map: a page fault on a soft SMB mount would be a SIGBUS, not an OSError.
                pf = pq.ParquetFile(self.dir / f"part-{part:05d}.parquet")
                self._files[part] = pf
            return pf

    def _row_group(self, part: int, rg: int, columns: tuple[str, ...]) -> pa.Table:
        key = (part, rg, columns)
        with self._lock:
            hit = self._cache.get(key)
            if hit is not None:
                self._cache.move_to_end(key)
                return hit
        table = self._file(part).read_row_group(rg, columns=list(columns))  # pyright: ignore[reportUnknownMemberType]  # pyarrow-stubs leave some parameter types unknown
        with self._lock:
            self._cache[key] = table
            while len(self._cache) > self._cache_size:
                self._cache.popitem(last=False)
        return table

    def lookup(self, doc_id: str, columns: Sequence[str] = COLUMNS) -> dict[str, Any] | None:
        """Row of ``doc_id`` restricted to ``columns`` (``doc_id`` is always included), or ``None``."""
        cols = tuple(dict.fromkeys(("doc_id", *columns)))
        h = np.uint64(id_hash(doc_id))
        i = int(np.searchsorted(self._h, h))
        while i < len(self._h) and self._h[i] == h:
            table = self._row_group(int(self._part[i]), int(self._rg[i]), cols)
            row = int(self._row[i])
            if table.column(0)[row].as_py() == doc_id:
                return {name: table.column(name)[row].as_py() for name in cols}
            i += 1
        return None

    def get(self, doc_id: str) -> DocRecord | None:
        row = self.lookup(doc_id)
        return None if row is None else record_from_row(row)

    def content_hash(self, doc_id: str) -> str | None:
        row = self.lookup(doc_id, ("content_hash",))
        return None if row is None else cast(str, row["content_hash"])

    def __contains__(self, doc_id: object) -> bool:
        return isinstance(doc_id, str) and self.lookup(doc_id, ()) is not None

    def scan(self, columns: Sequence[str] = COLUMNS, *, batch_size: int = 4096) -> Iterator[pa.RecordBatch]:
        for path in self.parts:
            pf = pq.ParquetFile(path)
            yield from pf.iter_batches(batch_size=batch_size, columns=list(columns))  # pyright: ignore[reportUnknownMemberType]  # pyarrow-stubs leave some parameter types unknown

    def records(self, *, batch_size: int = 1024) -> Iterator[DocRecord]:
        for batch in self.scan(batch_size=batch_size):
            for row in batch.to_pylist():
                yield record_from_row(row)

    def read_columns(self, columns: Sequence[str]) -> pa.Table:
        parts = [pq.read_table(p, columns=list(columns)) for p in self.parts]  # pyright: ignore[reportUnknownMemberType]  # pyarrow-stubs leave some parameter types unknown
        if not parts:
            return SCHEMA.empty_table().select(list(columns))
        return pa.concat_tables(parts)


# --- derived views -----------------------------------------------------------------------------


def diff_generations(old: RefineryReader | None, new: RefineryReader) -> Iterator[ChangeRecord]:
    """Change records turning ``old`` into ``new`` (sorted by doc_id). Renames are not detected here.

    Uses an Arrow hash join over ``(doc_id, content_hash)``; memory is ~100 bytes per document.
    """
    new_t = new.read_columns(["doc_id", "content_hash"]).rename_columns(["doc_id", "new_hash"])
    if old is None:
        old_t = pa.schema([("doc_id", pa.string()), ("old_hash", pa.string())]).empty_table()
    else:
        old_t = old.read_columns(["doc_id", "content_hash"]).rename_columns(["doc_id", "old_hash"])
    joined = new_t.join(old_t, keys="doc_id", join_type="full outer")
    old_col, new_col = joined["old_hash"], joined["new_hash"]
    differs = pc.or_kleene(
        pc.or_kleene(pc.is_null(old_col), pc.is_null(new_col)),  # pyright: ignore[reportUnknownMemberType]  # stubs
        pc.not_equal(old_col, new_col),  # pyright: ignore[reportUnknownMemberType]  # stubs
    )
    changed = joined.filter(differs).sort_by("doc_id")  # pyright: ignore[reportUnknownMemberType]  # pyarrow-stubs leave some parameter types unknown
    gen = new.generation_id
    for row in changed.to_pylist():
        old_hash = cast(str | None, row["old_hash"])
        new_hash = cast(str | None, row["new_hash"])
        if old_hash is None:
            kind = ChangeKind.ADDED
        elif new_hash is None:
            kind = ChangeKind.REMOVED
        else:
            kind = ChangeKind.MODIFIED
        yield ChangeRecord(DocId(cast(str, row["doc_id"])), kind, gen, old_hash, new_hash)


def inlink_counts(records: Iterable[DocRecord]) -> Counter[str]:
    """Inbound link count per target (doc id or native url): the popularity prior for suggest."""
    counts: Counter[str] = Counter()
    for rec in records:
        counts.update(set(rec.links))
    return counts
