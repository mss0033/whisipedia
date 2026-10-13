"""Refinery storage: ``DocRecord`` streams as Parquet, one directory per source and generation (PLAN §9.1).

Layout (readable without this code)::

    <root>/<source_id>/<generation_id>/
        part-00000.parquet   records sorted by doc_id, zstd, small row groups
        part-00001.parquet
        _ids.parquet         point-lookup index: blake2b-64(doc_id) -> (part, row_group, row), sorted
        _SUCCESS.json        seal marker, written last: {"format": 1, "docs": n, "parts": k}

A generation without ``_SUCCESS.json`` is incomplete and is ignored by readers. Every file is
written as ``*.partial`` and renamed after a read-back check, so a crash never leaves a torn file.

**Partitioned sources** (ADR-0010)::

    <root>/<source_id>/_parts/<generation_id>/   one vault generation (one partition), refined once
    <root>/<source_id>/<cycle>.<p>.setNNNNN/     a *set* generation: the union of the current
                                                 partitions (``_SUCCESS.json`` lists its ``members``)

A set generation hard-links its members' part files (copies them where the filesystem cannot link)
and merges their id indexes, so it costs O(documents) of index work and no text extraction; every
reader (search index refresh, documents, history, catalog, rank, spelling) sees an ordinary sealed
generation. A ``doc_id`` present in several members keeps the row of the member with the highest
generation id (the others are shadowed). ``_parts`` is never sealed itself, so listings skip it.

Point lookup: Parquet min/max statistics only help when a whole generation is globally sorted,
which a streaming writer cannot guarantee without an external sort. Instead each generation carries
a tiny sorted hash index (16 bytes per document; 112 MB for 7M articles) that is loaded into numpy
and binary-searched, so a lookup costs one ``searchsorted`` plus decoding one small row group.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
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
import pyarrow.parquet as pq

from wsp_core.ids import DocId, content_hash
from wsp_core.model import ChangeRecord, DocRecord
from wsp_index.localfs import REMOTE_FS_TYPES, filesystem_type

log = logging.getLogger(__name__)

FORMAT_VERSION = 2  # 2: ``aliases`` column (port revision v0.2); older generations are rebuilt
SUCCESS_FILE = "_SUCCESS.json"
IDS_FILE = "_ids.parquet"
SHADOW_FILE = "_shadowed.parquet"  # rows superseded by a later row with the same doc_id
# The id index as memory-mapped arrays: sorted hashes and (part, rg, row). Written on a sealed
# generation's first point lookup, so a long-lived reader (the API) holds it as reclaimable page cache
# instead of ~20 bytes of anonymous memory per document. Next to the generation when the Refinery is on
# local disk; under a local map root (:func:`id_map_dir`) when it is on the NAS (ADR-0011): a page fault
# on a ``soft`` SMB mount is a SIGBUS, not an OSError.
IDS_MAP_H = "_ids.h.npy"
IDS_MAP_LOC = "_ids.loc.npy"
CURRENT_FILE = "CURRENT.txt"
PARTS_DIR = "_parts"  # partitioned sources: refined vault generations, composed into set generations
SET_MARK = ".set"  # set generation ids: <newest member id>.setNNNNN
_SET_NUMBER = re.compile(r"\.set(\d+)$")

DEFAULT_ROW_GROUP_ROWS = 256
DEFAULT_DOCS_PER_FILE = 50_000
DEFAULT_BUFFER_CHARS = 64_000_000
"""Flush a part file once this much text is buffered, whatever the document count: 50,000 Gutenberg
books are ~20 GB of text (~77 GB of Python objects at flush), 50,000 Wikipedia articles ~0.2 GB."""
ROW_GROUP_TARGET_CHARS = 4_000_000  # long documents get fewer rows per row group (a point lookup
# decodes a whole row group: 256 books would be ~100 MB)
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
        pa.field("aliases", pa.list_(pa.string()), nullable=False),
        # Optional (v0.3, additive): DocRecord.meta as a JSON object string, null when empty. Files
        # written before it existed lack the column; readers treat it as absent (no format bump).
        pa.field("meta", pa.string()),
    ]
)
COLUMNS: tuple[str, ...] = tuple(SCHEMA.names)
OPTIONAL_COLUMNS = frozenset({"meta"})

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


def part_dir(root: Path, source_id: str, generation_id: str) -> Path:
    """Where one partition generation of a partitioned source is refined (ADR-0010)."""
    return root / source_id / PARTS_DIR / generation_id


def is_sealed(gen_dir: Path) -> bool:
    return (gen_dir / SUCCESS_FILE).is_file()


def id_map_dir(map_root: Path, gen_dir: Path) -> Path:
    """Where the id-map sidecar of the sealed ``gen_dir`` lives under a local ``map_root``.

    ``<map_root>/<source>/<generation>-<stamp>``: the stamp hashes the generation's path and seal
    marker, so a generation refined again under the same id (a format bump) gets a fresh map instead
    of a stale one that happens to have the same length.
    """
    seal = (gen_dir / SUCCESS_FILE).read_bytes()
    stamp = hashlib.blake2b(str(gen_dir.resolve()).encode("utf-8") + b"\0" + seal, digest_size=8).hexdigest()
    source = gen_dir.parent.parent.name if gen_dir.parent.name == PARTS_DIR else gen_dir.parent.name
    return map_root / source / f"{gen_dir.name}-{stamp}"


def configured_root(index_root: Path, value: object) -> Path:
    """The Refinery root: ``[index] refinery_root`` when set (it may be on the NAS, ADR-0011), else
    ``<index root>/refinery``."""
    text = str(value).strip() if value is not None else ""
    return Path(text).expanduser() if text else index_root / "refinery"


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
        "aliases": list(rec.aliases),
        "meta": json.dumps(dict(rec.meta), sort_keys=True) if rec.meta else None,
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
        aliases=tuple(row.get("aliases") or ()),
        meta=_meta(row.get("meta")),
    )


def _meta(raw: object) -> dict[str, str]:
    if not isinstance(raw, str) or not raw:
        return {}
    data = json.loads(raw)
    return (
        {str(k): str(v) for k, v in cast(dict[str, object], data).items()} if isinstance(data, dict) else {}
    )


def _atomic_replace(partial: Path, final: Path) -> None:
    with partial.open("rb") as fh:
        os.fsync(fh.fileno())
    os.replace(partial, final)
    _fsync_dir(final.parent)


def _fsync_dir(directory: Path) -> None:
    """Make a rename in ``directory`` durable (ext4, and SMB3, where it is a server flush)."""
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


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
        directory: Path | None = None,
        buffer_chars: int = DEFAULT_BUFFER_CHARS,
    ) -> None:
        """``directory`` overrides ``<root>/<source_id>/<generation_id>`` (partition generations)."""
        if row_group_rows < 1 or docs_per_file < 1:
            raise ValueError("row_group_rows and docs_per_file must be positive")
        self.dir = directory if directory is not None else generation_dir(root, source_id, generation_id)
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
        self._buf_chars = 0
        self._max_chars = buffer_chars
        self._parts = 0
        self._bytes = 0
        self._docs = 0
        self._dropped = 0  # duplicate ids dropped within one part file
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
            self._buf_chars += len(rec.text)
            n += 1
            if len(self._buf) >= self._per_file or self._buf_chars >= self._max_chars:
                self._flush()
        return n

    def _flush(self) -> None:
        if not self._buf:
            return
        self._buf.sort(key=lambda r: r.doc_id)  # stable: equal ids stay in arrival order
        deduped = [
            r
            for r, nxt in zip(self._buf, [*self._buf[1:], None], strict=True)
            if nxt is None or nxt.doc_id != r.doc_id
        ]
        self._dropped += len(self._buf) - len(deduped)  # an id repeated within the file: last arrival wins
        self._buf = deduped
        table = pa.Table.from_pylist([_to_row(r) for r in self._buf], schema=SCHEMA)
        per_doc = max(1, self._buf_chars // max(1, len(self._buf)))
        rg_rows = max(1, min(self._rg_rows, ROW_GROUP_TARGET_CHARS // per_doc))
        self._buf_chars = 0
        name = f"part-{self._parts:05d}.parquet"
        final = self.dir / name
        partial = self.dir / (name + ".partial")
        pq.write_table(  # pyright: ignore[reportUnknownMemberType]  # stubs: partially unknown params
            table,
            partial,
            row_group_size=rg_rows,
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
        keep = _duplicates_mask(self.dir, h, loc)
        shadowed = int(len(h) - int(keep.sum()))
        if shadowed:
            _write_shadow(self.dir, loc[~keep])
        h, loc = h[keep], loc[keep]
        _write_ids(self.dir, h, loc)
        _write_success(
            self.dir,
            {
                "format": FORMAT_VERSION,
                "docs": self._docs - shadowed,
                "parts": self._parts,
                "shadowed": shadowed + self._dropped,
            },
        )
        self._closed = True
        return WriteStats(self._docs, self._parts, self._bytes)


def _write_ids(directory: Path, h: npt.NDArray[np.uint64], loc: npt.NDArray[np.uint32]) -> None:
    ids = pa.Table.from_arrays(
        [pa.array(h), pa.array(loc[:, 0]), pa.array(loc[:, 1]), pa.array(loc[:, 2])],
        schema=_IDS_SCHEMA,
    )
    partial = directory / (IDS_FILE + ".partial")
    pq.write_table(ids, partial, compression="zstd", row_group_size=1 << 20)  # pyright: ignore[reportUnknownMemberType]  # pyarrow-stubs leave some parameter types unknown
    _atomic_replace(partial, directory / IDS_FILE)


def _write_success(directory: Path, success: Mapping[str, object]) -> None:
    partial = directory / (SUCCESS_FILE + ".partial")
    partial.write_text(json.dumps(dict(success), sort_keys=True) + "\n", encoding="utf-8")
    _atomic_replace(partial, directory / SUCCESS_FILE)


def _write_shadow(directory: Path, shadow: npt.NDArray[np.uint32]) -> None:
    table = pa.Table.from_arrays(
        [pa.array(shadow[:, 0]), pa.array(shadow[:, 1]), pa.array(shadow[:, 2])],
        names=["part", "rg", "row"],
    )
    partial = directory / (SHADOW_FILE + ".partial")
    pq.write_table(table, partial)  # pyright: ignore[reportUnknownMemberType]  # pyarrow-stubs leave some parameter types unknown
    _atomic_replace(partial, directory / SHADOW_FILE)


def _duplicates_mask(
    directory: Path, h: npt.NDArray[np.uint64], loc: npt.NDArray[np.uint32]
) -> npt.NDArray[np.bool_]:
    """Mask of (hash-sorted) index rows to keep. A doc_id written more than once keeps its LAST row in
    stream order, i.e. the max (part, rg, row) (dumps can list a page twice when it is moved mid-dump;
    the later entry is the newer page); callers record the others in ``_shadowed.parquet`` so scans
    skip them. Hash collisions between different ids are kept."""
    keep = np.ones(len(h), dtype=np.bool_)
    if len(h) < 2:
        return keep
    dup = np.nonzero(h[1:] == h[:-1])[0]
    if len(dup) == 0:
        return keep
    # Every row in a run of equal hashes, its doc_id read with one column read per (part, row group).
    in_run = np.zeros(len(h), dtype=np.bool_)
    in_run[dup] = True
    in_run[dup + 1] = True
    rows_needed = np.flatnonzero(in_run)
    ids: dict[int, str] = {}
    by_part: dict[int, dict[int, list[int]]] = {}
    for i in rows_needed.tolist():
        by_part.setdefault(int(loc[i, 0]), {}).setdefault(int(loc[i, 1]), []).append(i)
    for part, groups_of_part in sorted(by_part.items()):
        pf = pq.ParquetFile(directory / f"part-{part:05d}.parquet")
        for rg, members in sorted(groups_of_part.items()):
            column = pf.read_row_group(rg, columns=["doc_id"]).column(0)  # pyright: ignore[reportUnknownMemberType]  # pyarrow-stubs leave some parameter types unknown
            for i in members:
                ids[i] = cast(str, column[int(loc[i, 2])].as_py())

    def doc_id_at(i: int) -> str:
        return ids[i]

    # Runs of equal hashes; within a run, rows with the same id: keep the max (part, rg, row).
    for i in sorted(set(dup.tolist())):
        if i > 0 and h[i - 1] == h[i]:
            continue  # not the start of a run
        j = i + 1
        while j < len(h) and h[j] == h[i]:
            j += 1
        groups: dict[str, list[int]] = {}
        for k in range(i, j):
            groups.setdefault(doc_id_at(k), []).append(k)
        for rows in groups.values():
            if len(rows) > 1:
                latest = max(rows, key=lambda k: tuple(int(x) for x in loc[k]))
                for k in rows:
                    if k != latest:
                        keep[k] = False
    return keep


# --- set generations (partitioned sources, ADR-0010) --------------------------------------------


def set_members(gen_dir: Path) -> tuple[str, ...] | None:
    """Member generation ids of a sealed set generation, ``None`` for an ordinary generation."""
    meta = json.loads((gen_dir / SUCCESS_FILE).read_text(encoding="utf-8"))
    members = meta.get("members")
    return tuple(str(m) for m in cast(list[object], members)) if isinstance(members, list) else None


def _link_or_copy(src: Path, dst: Path) -> None:
    try:
        os.link(src, dst)
    except OSError:
        import shutil

        partial = dst.with_name(dst.name + ".partial")
        shutil.copyfile(src, partial)
        _atomic_replace(partial, dst)


def compose_set(root: Path, source_id: str, members: Sequence[Path]) -> str:
    """Seal (or reuse) the set generation that is the union of sealed ``members``; returns its id.

    ``members`` are sealed refinery generations of ``source_id`` (normally ``_parts/<gid>``). A set
    with exactly these member ids that is already sealed is reused. The new id is
    ``<newest member id>.setNNNNN`` with a running number, so set ids sort in the order they were
    made. Members are taken in generation-id order; a doc_id in several members keeps the row of the
    last one.
    """
    if not members:
        raise RefineryError(f"{source_id}: a set generation needs at least one member")
    ordered = sorted(members, key=lambda m: m.name)
    names = tuple(m.name for m in ordered)
    for m in ordered:
        if not is_sealed(m):
            raise RefineryError(f"set member is not sealed: {m}")
    existing = [g for g in sealed_generations(root, source_id) if SET_MARK in g]
    for gid in reversed(existing):
        if set_members(generation_dir(root, source_id, gid)) == names:
            return gid
    # The running number is max+1 over every set ever made (sealed or not), never len(): after a set is
    # deleted a len()-based number could repeat the newest set's id and remove it while it is served.
    numbers = (
        [int(m.group(1)) for d in (root / source_id).iterdir() if (m := _SET_NUMBER.search(d.name))]
        if (root / source_id).is_dir()
        else []
    )
    set_id = f"{names[-1]}{SET_MARK}{max(numbers, default=0) + 1:05d}"
    out = generation_dir(root, source_id, set_id)
    if is_sealed(out):
        raise RefineryError(f"set generation {out} already exists and is sealed")
    if out.exists():
        import shutil

        shutil.rmtree(out)  # an interrupted compose: derived and unsealed, start again
    out.mkdir(parents=True)
    hs: list[npt.NDArray[np.uint64]] = []
    locs: list[npt.NDArray[np.uint32]] = []
    shadows: list[npt.NDArray[np.uint32]] = []
    offset = docs = shadowed = 0
    for m in ordered:
        meta = json.loads((m / SUCCESS_FILE).read_text(encoding="utf-8"))
        if meta.get("format") != FORMAT_VERSION:
            raise RefineryError(f"unsupported refinery format {meta.get('format')!r} in {m}")
        n_parts = int(meta["parts"])
        for i in range(n_parts):
            _link_or_copy(m / f"part-{i:05d}.parquet", out / f"part-{offset + i:05d}.parquet")
        ids = pq.read_table(m / IDS_FILE)  # pyright: ignore[reportUnknownMemberType]  # pyarrow-stubs leave some parameter types unknown
        loc = np.empty((ids.num_rows, 3), dtype=np.uint32)
        loc[:, 0] = ids.column("part").to_numpy().astype(np.uint32) + offset
        loc[:, 1] = ids.column("rg").to_numpy()
        loc[:, 2] = ids.column("row").to_numpy()
        hs.append(ids.column("h").to_numpy().astype(np.uint64))
        locs.append(loc)
        if (m / SHADOW_FILE).is_file():
            sh = pq.read_table(m / SHADOW_FILE)  # pyright: ignore[reportUnknownMemberType]  # pyarrow-stubs leave some parameter types unknown
            rows = np.empty((sh.num_rows, 3), dtype=np.uint32)
            rows[:, 0] = sh.column("part").to_numpy().astype(np.uint32) + offset
            rows[:, 1] = sh.column("rg").to_numpy()
            rows[:, 2] = sh.column("row").to_numpy()
            shadows.append(rows)
        docs += int(meta.get("docs", ids.num_rows))
        shadowed += int(meta.get("shadowed", 0))
        offset += n_parts
    h = np.concatenate(hs) if hs else np.empty(0, np.uint64)
    loc = np.concatenate(locs) if locs else np.empty((0, 3), np.uint32)
    order = np.argsort(h, kind="stable")
    h, loc = h[order], loc[order]
    keep = _duplicates_mask(out, h, loc)
    dropped = int(len(h) - int(keep.sum()))
    shadows.append(loc[~keep])
    shadow = np.concatenate(shadows)
    if len(shadow):
        _write_shadow(out, shadow)
    _write_ids(out, h[keep], loc[keep])
    _write_success(
        out,
        {
            "format": FORMAT_VERSION,
            "docs": docs - dropped,
            "parts": offset,
            "shadowed": shadowed + dropped,
            "members": list(names),
        },
    )
    return set_id


def _write_id_map(gen_dir: Path, n: int, out_dir: Path) -> None:
    """Write ``out_dir/_ids.{h,loc}.npy`` from ``gen_dir/_ids.parquet`` batch by batch (bounded memory),
    atomically."""
    out_dir.mkdir(parents=True, exist_ok=True)
    tag = f".{os.getpid()}.{threading.get_ident()}.tmp"
    hp, lp = out_dir / (IDS_MAP_H + tag), out_dir / (IDS_MAP_LOC + tag)
    try:
        h = np.lib.format.open_memmap(hp, mode="w+", dtype=np.uint64, shape=(n,))
        loc = np.lib.format.open_memmap(lp, mode="w+", dtype=np.uint32, shape=(n, 3))
        i = 0
        source = pq.ParquetFile(gen_dir / IDS_FILE)
        batches = source.iter_batches(batch_size=1 << 20, columns=["h", "part", "rg", "row"])  # pyright: ignore[reportUnknownMemberType]  # pyarrow-stubs leave some parameter types unknown
        for batch in batches:
            k = batch.num_rows
            h[i : i + k] = batch.column(0).to_numpy()
            loc[i : i + k, 0] = batch.column(1).to_numpy()
            loc[i : i + k, 1] = batch.column(2).to_numpy()
            loc[i : i + k, 2] = batch.column(3).to_numpy()
            i += k
        if i != n:
            raise RefineryError(f"{gen_dir / IDS_FILE}: {i} rows read, {n} expected")
        h.flush()
        loc.flush()
        del h, loc
        for tmp in (hp, lp):
            with tmp.open("rb") as f:
                os.fsync(f.fileno())
        os.replace(lp, out_dir / IDS_MAP_LOC)  # loc first: h appearing last completes the pair
        os.replace(hp, out_dir / IDS_MAP_H)
        _fsync_dir(out_dir)
    finally:
        for tmp in (hp, lp):
            tmp.unlink(missing_ok=True)


def ensure_id_map(gen_dir: Path, map_dir: Path) -> bool:
    """Write the id-map sidecar of the sealed ``gen_dir`` under ``map_dir`` unless a matching one is
    there. For the index job, right after a generation goes live: the API otherwise built it on the
    first lookup, streaming the whole ``_ids.parquet`` over the NAS inside a request (ADR-0011).
    Returns True when a map was written."""
    reader = RefineryReader(gen_dir, map_ids=True, map_dir=map_dir)
    n = int(pq.ParquetFile(gen_dir / IDS_FILE).metadata.num_rows)  # pyright: ignore[reportUnknownMemberType]  # pyarrow-stubs leave some parameter types unknown
    if reader._mapped_ids(n) is not None:  # pyright: ignore[reportPrivateUsage]
        return False
    _write_id_map(gen_dir, n, map_dir)
    return True


ID_ROW_GROUPS_CACHED = 2


# --- reader ------------------------------------------------------------------------------------


class RefineryReader:
    """Point lookups and scans over one sealed generation.

    ``map_ids=False``: never write the id-map sidecar (``_ids.{h,loc}.npy``, 20 bytes a document) for
    this generation. Lookups then use the sidecar if one exists, else read the one row group of the
    hash-sorted ``_ids.parquet`` whose statistics cover the id (~20 MB decoded). For generations read
    rarely, from a request: the history of a document opened every older generation and wrote a
    sidecar for each, 720 MB for one PubMed set (audit 2026-10-12 #12).

    ``map_dir``: where the sidecar lives (:func:`id_map_dir`). Without one it lives in ``gen_dir``,
    unless ``gen_dir`` is on a network filesystem: then no sidecar is written or memory-mapped there,
    and lookups read ``_ids.parquet`` row groups as with ``map_ids=False`` (ADR-0011).
    """

    def __init__(
        self,
        gen_dir: Path,
        *,
        cache_row_groups: int = 32,
        map_ids: bool = True,
        map_dir: Path | None = None,
    ) -> None:
        if not is_sealed(gen_dir):
            raise RefineryError(f"not a sealed refinery generation: {gen_dir}")
        self.dir = gen_dir
        self.generation_id = gen_dir.name
        self.source_id = gen_dir.parent.name
        meta = json.loads((gen_dir / SUCCESS_FILE).read_text(encoding="utf-8"))
        if meta.get("format") != FORMAT_VERSION:
            raise RefineryError(f"unsupported refinery format {meta.get('format')!r} in {gen_dir}")
        self._n_parts = int(meta["parts"])
        # Live documents (the id index has one row per live document); loading that index costs
        # ~20 bytes a document (1.4 GB peak for 32M), so it is read on the first point lookup only:
        # scans (rank, spelling, catalog, diff) never need it.
        self._docs = int(meta.get("docs", -1))
        self._ids: (
            tuple[
                npt.NDArray[np.uint64], npt.NDArray[np.uint32], npt.NDArray[np.uint32], npt.NDArray[np.uint32]
            ]
            | None
        ) = None
        # Shadowed rows per part as (rg, row) pairs in numpy (a set of tuples costs ~150 bytes a row;
        # a pubmed set generation shadows millions).
        self._shadow: dict[int, npt.NDArray[np.int64]] = {}
        if (gen_dir / SHADOW_FILE).is_file():
            sh = pq.read_table(gen_dir / SHADOW_FILE)  # pyright: ignore[reportUnknownMemberType]  # pyarrow-stubs leave some parameter types unknown
            s_part = sh.column("part").to_numpy().astype(np.int64)
            s_rg = sh.column("rg").to_numpy().astype(np.int64)
            s_row = sh.column("row").to_numpy().astype(np.int64)
            order = np.argsort(s_part, kind="stable")
            s_part, s_rg, s_row = s_part[order], s_rg[order], s_row[order]
            bounds = np.flatnonzero(np.diff(s_part)) + 1
            for chunk_part, chunk_rg, chunk_row in zip(
                np.split(s_part, bounds), np.split(s_rg, bounds), np.split(s_row, bounds), strict=True
            ):
                if len(chunk_part):
                    self._shadow[int(chunk_part[0])] = np.stack([chunk_rg, chunk_row], axis=1)
        self._files: OrderedDict[int, tuple[pq.ParquetFile, threading.Lock]] = OrderedDict()
        self._max_files = 16  # each open ParquetFile keeps its footer (~2.5 MB with 256-row groups)
        self._cache: OrderedDict[tuple[int, int, tuple[str, ...]], pa.Table] = OrderedDict()
        self._cache_size = cache_row_groups
        self._lock = threading.Lock()  # the open-file and row-group LRUs (never held across I/O)
        self._ids_lock = threading.Lock()  # loading the id index: seconds on the NAS, alone
        if map_dir is None and filesystem_type(gen_dir) in REMOTE_FS_TYPES:
            self._map_dir: Path | None = None
            self._map_ids = False
        else:
            self._map_dir = gen_dir if map_dir is None else map_dir
            self._map_ids = map_ids
        self._id_groups: tuple[pq.ParquetFile, list[tuple[int, int]]] | None = None
        self._id_groups_lock = threading.Lock()  # a ParquetFile is not safe for concurrent reads
        # Decoded id row groups of the last sparse lookups (~20 B a row, a row group is ~1M rows): a
        # history page asks the same generations for one document, a diff for neighbouring hashes.
        self._id_rows: OrderedDict[int, tuple[npt.NDArray[np.uint64], pa.Table]] = OrderedDict()

    def _sparse(self) -> bool:
        """Lookups read ``_ids.parquet`` row groups instead of loading (or writing) the id index."""
        return not self._map_ids and self._ids is None and not self._has_map()

    def _has_map(self) -> bool:
        return self._map_dir is not None and (self._map_dir / IDS_MAP_H).is_file()

    def _locations(self, h: np.uint64) -> list[tuple[int, int, int]]:
        """``(part, rg, row)`` of each live row whose id hashes to ``h``."""
        if self._sparse():
            found = self._sparse_locations(h)
            if found is not None:
                return found
        hs, parts, rgs, rows = self._id_index()
        i = int(np.searchsorted(hs, h))
        out: list[tuple[int, int, int]] = []
        while i < len(hs) and hs[i] == h:
            out.append((int(parts[i]), int(rgs[i]), int(rows[i])))
            i += 1
        return out

    def _sparse_locations(self, h: np.uint64) -> list[tuple[int, int, int]] | None:
        """From the row groups of ``_ids.parquet`` whose min/max statistics cover ``h`` (the file is
        sorted by hash); ``None`` when it has no statistics."""
        with self._id_groups_lock:
            if self._id_groups is None:
                pf = pq.ParquetFile(self.dir / IDS_FILE)
                bounds: list[tuple[int, int]] = []
                for i in range(pf.metadata.num_row_groups):  # pyright: ignore[reportUnknownMemberType]  # pyarrow-stubs leave some parameter types unknown
                    stats = pf.metadata.row_group(i).column(0).statistics  # pyright: ignore[reportUnknownMemberType]  # pyarrow-stubs leave some parameter types unknown
                    if stats is None or not stats.has_min_max:
                        return None
                    bounds.append((int(cast(int, stats.min)), int(cast(int, stats.max))))
                self._id_groups = (pf, bounds)
            pf, bounds = self._id_groups
            out: list[tuple[int, int, int]] = []
            for i, (lo, hi) in enumerate(bounds):
                if lo <= int(h) <= hi:
                    cached = self._id_rows.get(i)
                    if cached is None:
                        t = pf.read_row_group(i, columns=["h", "part", "rg", "row"])  # pyright: ignore[reportUnknownMemberType]  # pyarrow-stubs leave some parameter types unknown
                        cached = (t.column(0).to_numpy(), t)
                        self._id_rows[i] = cached
                        while len(self._id_rows) > ID_ROW_GROUPS_CACHED:
                            self._id_rows.popitem(last=False)
                    else:
                        self._id_rows.move_to_end(i)
                    hs, t = cached
                    j = int(np.searchsorted(hs, h))
                    while j < len(hs) and hs[j] == h:
                        out.append(
                            (
                                int(t.column(1)[j].as_py()),
                                int(t.column(2)[j].as_py()),
                                int(t.column(3)[j].as_py()),
                            )
                        )
                        j += 1
            return out

    def _id_index(
        self,
    ) -> tuple[
        npt.NDArray[np.uint64], npt.NDArray[np.uint32], npt.NDArray[np.uint32], npt.NDArray[np.uint32]
    ]:
        ids = self._ids
        if ids is None:
            with self._ids_lock:  # not self._lock: readers of other row groups must not wait for this
                if self._ids is None:
                    self._ids = self._load_id_index()
                ids = self._ids
        return ids

    def _load_id_index(
        self,
    ) -> tuple[
        npt.NDArray[np.uint64], npt.NDArray[np.uint32], npt.NDArray[np.uint32], npt.NDArray[np.uint32]
    ]:
        """The id index, memory-mapped from its sidecar (written on first use); in memory if it cannot be."""
        n = int(pq.ParquetFile(self.dir / IDS_FILE).metadata.num_rows)  # pyright: ignore[reportUnknownMemberType]  # pyarrow-stubs leave some parameter types unknown
        try:
            mapped = self._mapped_ids(n)
            if mapped is None and self._map_ids and self._map_dir is not None:
                _write_id_map(self.dir, n, self._map_dir)
                mapped = self._mapped_ids(n)
            if mapped is not None:
                h, loc = mapped
                return h, loc[:, 0], loc[:, 1], loc[:, 2]
        except OSError as exc:  # read-only or full disk: the old in-memory index still works
            log.warning(
                "refinery %s: id map unavailable (%s); loading the id index into memory", self.dir, exc
            )
        table = pq.read_table(self.dir / IDS_FILE)  # pyright: ignore[reportUnknownMemberType]  # pyarrow-stubs leave some parameter types unknown
        return (
            table.column("h").to_numpy(),
            table.column("part").to_numpy(),
            table.column("rg").to_numpy(),
            table.column("row").to_numpy(),
        )

    def _mapped_ids(self, n: int) -> tuple[npt.NDArray[np.uint64], npt.NDArray[np.uint32]] | None:
        if self._map_dir is None:
            return None
        hp, lp = self._map_dir / IDS_MAP_H, self._map_dir / IDS_MAP_LOC
        if not (hp.is_file() and lp.is_file()):
            return None
        try:
            h = np.load(hp, mmap_mode="r")
            loc = np.load(lp, mmap_mode="r")
        except ValueError:  # truncated ("mmap length is greater than file size"; audit 2026-10-12 #15)
            return None  # rewritten
        if h.dtype != np.uint64 or loc.dtype != np.uint32 or h.shape != (n,) or loc.shape != (n, 3):
            return None  # torn or from another index: rewritten
        return cast(npt.NDArray[np.uint64], h), cast(npt.NDArray[np.uint32], loc)

    @property
    def id_rows_bytes(self) -> int:
        """Memory of the decoded id row groups kept for sparse lookups (:data:`ID_ROW_GROUPS_CACHED`)."""
        return sum(t.nbytes + h.nbytes for h, t in self._id_rows.values())

    @property
    def id_index_bytes(self) -> int:
        """Anonymous memory the id index holds or will hold: 0 when memory-mapped (page cache)."""
        ids = self._ids
        if ids is not None:
            return 0 if isinstance(ids[0], np.memmap) else 20 * len(ids[0])
        return 0 if self._has_map() or not self._map_ids else 20 * len(self)

    def __len__(self) -> int:
        if self._docs >= 0:
            return self._docs
        return len(self._id_index()[0])

    @property
    def parts(self) -> list[Path]:
        return [self.dir / f"part-{i:05d}.parquet" for i in range(self._n_parts)]

    def _file(self, part: int) -> tuple[pq.ParquetFile, threading.Lock]:
        """The part's open file and the lock that serializes reads of it.

        A ``ParquetFile`` is not safe for concurrent reads: two threads in ``read_row_group`` on one
        file race in Arrow's pre-buffer cache (double free / SIGSEGV; the API crashed on it three
        times on 2026-10-10/11). Open files are bounded (an LRU), each with its own lock.
        """
        with self._lock:
            entry = self._files.get(part)
            if entry is not None:
                self._files.move_to_end(part)
                return entry
        # Opened outside the lock: the footer read is a network round trip on the NAS, and the lock
        # also guards the row-group cache every other reader of this generation needs.
        # No memory_map: a page fault on a soft SMB mount would be a SIGBUS, not an OSError.
        opened = (pq.ParquetFile(self.dir / f"part-{part:05d}.parquet"), threading.Lock())
        with self._lock:
            entry = self._files.get(part)
            if entry is None:
                entry = opened
                self._files[part] = entry
                while len(self._files) > self._max_files:
                    self._files.popitem(last=False)
            else:
                self._files.move_to_end(part)
            return entry

    def _present(self, pf: pq.ParquetFile, columns: Sequence[str]) -> list[str]:
        """``columns`` minus optional ones this (older) file was written without."""
        names = set(pf.schema_arrow.names)
        return [c for c in columns if c in names or c not in OPTIONAL_COLUMNS]

    def _row_group(self, part: int, rg: int, columns: tuple[str, ...]) -> pa.Table:
        key = (part, rg, columns)
        with self._lock:
            hit = self._cache.get(key)
            if hit is not None:
                self._cache.move_to_end(key)
                return hit
        pf, file_lock = self._file(part)
        with file_lock:  # also single-flight: a second reader of this row group waits, then hits the cache
            with self._lock:
                hit = self._cache.get(key)
            if hit is not None:
                return hit
            table = pf.read_row_group(rg, columns=self._present(pf, columns))  # pyright: ignore[reportUnknownMemberType]  # pyarrow-stubs leave some parameter types unknown
        with self._lock:
            self._cache[key] = table
            while len(self._cache) > self._cache_size:
                self._cache.popitem(last=False)
        return table

    def lookup(self, doc_id: str, columns: Sequence[str] = COLUMNS) -> dict[str, Any] | None:
        """Row of ``doc_id`` restricted to ``columns`` (``doc_id`` is always included), or ``None``."""
        cols = tuple(dict.fromkeys(("doc_id", *columns)))
        for part, rg, row in self._locations(np.uint64(id_hash(doc_id))):
            table = self._row_group(part, rg, cols)
            if table.column(0)[row].as_py() == doc_id:
                return {name: table.column(name)[row].as_py() for name in table.column_names}
        return None

    def doc_id_at(self, i: int) -> str:
        """The doc id of the ``i``-th live document (``0 <= i < len(self)``, in id-hash order: a uniform
        ``i`` is a uniform document). One row group read; the id index is memory-mapped."""
        _, parts, rgs, rows = self._id_index()
        table = self._row_group(int(parts[i]), int(rgs[i]), ("doc_id",))
        return cast(str, table.column(0)[int(rows[i])].as_py())

    def get(self, doc_id: str) -> DocRecord | None:
        row = self.lookup(doc_id)
        return None if row is None else record_from_row(row)

    def content_hash(self, doc_id: str) -> str | None:
        row = self.lookup(doc_id, ("content_hash",))
        return None if row is None else cast(str, row["content_hash"])

    def __contains__(self, doc_id: object) -> bool:
        return isinstance(doc_id, str) and self.lookup(doc_id, ()) is not None

    @property
    def n_parts(self) -> int:
        return self._n_parts

    @property
    def max_open_files(self) -> int:
        return self._max_files

    @property
    def cache_row_groups(self) -> int:
        return self._cache_size

    def _live(self, part: int, pf: pq.ParquetFile) -> npt.NDArray[np.bool_] | None:
        """Mask of the part's rows that are not shadowed, or ``None`` when every row is live."""
        shadowed = self._shadow.get(part)
        if shadowed is None or not len(shadowed):
            return None
        meta = pf.metadata
        sizes = np.array([meta.row_group(i).num_rows for i in range(meta.num_row_groups)], dtype=np.int64)
        starts = np.concatenate(([0], np.cumsum(sizes)[:-1]))
        live = np.ones(int(sizes.sum()), dtype=np.bool_)
        live[starts[shadowed[:, 0]] + shadowed[:, 1]] = False
        return live

    def part_table(self, part: int, columns: Sequence[str]) -> tuple[pa.Table, npt.NDArray[np.int64]]:
        """Live rows of one part (``columns`` only) and each row's position within the part file."""
        pf = pq.ParquetFile(self.dir / f"part-{part:05d}.parquet")
        table = pf.read(columns=self._present(pf, columns))  # pyright: ignore[reportUnknownMemberType]  # pyarrow-stubs leave some parameter types unknown
        live = self._live(part, pf)
        if live is None:
            return table, np.arange(table.num_rows, dtype=np.int64)
        return table.filter(pa.array(live)), np.flatnonzero(live).astype(np.int64)

    def part_rows(
        self, part: int, positions: npt.NDArray[np.int64], columns: Sequence[str] = COLUMNS
    ) -> pa.Table:
        """Rows at ``positions`` (sorted positions within the part file), reading only their row groups."""
        pf = pq.ParquetFile(self.dir / f"part-{part:05d}.parquet")
        meta = pf.metadata
        sizes = np.array([meta.row_group(i).num_rows for i in range(meta.num_row_groups)], dtype=np.int64)
        ends = np.cumsum(sizes)
        rgs = np.unique(np.searchsorted(ends, positions, side="right"))
        table = pf.read_row_groups([int(r) for r in rgs], columns=self._present(pf, columns))  # pyright: ignore[reportUnknownMemberType]  # pyarrow-stubs leave some parameter types unknown
        # Position in the concatenation of the chosen row groups.
        starts = ends - sizes
        local_start = np.concatenate(([0], np.cumsum(sizes[rgs])[:-1]))
        which = np.searchsorted(rgs, np.searchsorted(ends, positions, side="right"))
        local = local_start[which] + (positions - starts[rgs[which]])
        return table.take(pa.array(local.astype(np.int64), type=pa.int64()))  # pyright: ignore[reportArgumentType]  # stubs: Int64Array is valid indices

    def scan(self, columns: Sequence[str] = COLUMNS, *, batch_size: int = 4096) -> Iterator[pa.RecordBatch]:
        """All live rows (shadowed duplicates skipped)."""
        for part, path in enumerate(self.parts):
            pf = pq.ParquetFile(path)
            present = self._present(pf, columns)
            live = self._live(part, pf)
            if live is None:
                yield from pf.iter_batches(batch_size=batch_size, columns=present)  # pyright: ignore[reportUnknownMemberType]  # pyarrow-stubs leave some parameter types unknown
                continue
            offset = 0
            for rg in range(pf.num_row_groups):
                table = pf.read_row_group(rg, columns=present)  # pyright: ignore[reportUnknownMemberType]  # pyarrow-stubs leave some parameter types unknown
                mask = live[offset : offset + table.num_rows]
                offset += table.num_rows
                yield from table.filter(pa.array(mask)).to_batches(max_chunksize=batch_size)

    def records(self, *, batch_size: int = 1024) -> Iterator[DocRecord]:
        for batch in self.scan(batch_size=batch_size):
            for row in batch.to_pylist():
                yield record_from_row(row)

    def read_columns(self, columns: Sequence[str]) -> pa.Table:
        if self._shadow:
            batches = list(self.scan(columns))
            if not batches:
                return SCHEMA.empty_table().select(list(columns))
            return pa.Table.from_batches(batches)
        parts = [
            pq.read_table(p, columns=self._present(pq.ParquetFile(p), columns))  # pyright: ignore[reportUnknownMemberType]  # pyarrow-stubs leave some parameter types unknown
            for p in self.parts
        ]
        if not parts:
            return SCHEMA.empty_table().select(list(columns))
        return pa.concat_tables(parts)


# --- derived views -----------------------------------------------------------------------------


def diff_generations(old: RefineryReader | None, new: RefineryReader) -> Iterator[ChangeRecord]:
    """Change records turning ``old`` into ``new``: streamed, ~32 bytes of memory per old document
    (see :mod:`wsp_index.delta`). Renames are not detected here."""
    from wsp_index.delta import diff

    return diff(old, new)


def inlink_counts(records: Iterable[DocRecord]) -> Counter[str]:
    """Inbound link count per target (doc id or native url): the popularity prior for suggest."""
    counts: Counter[str] = Counter()
    for rec in records:
        counts.update(set(rec.links))
    return counts
