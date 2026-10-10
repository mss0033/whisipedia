"""Snapshot-to-snapshot diffing of one source's generations (PLAN §6.4, §6.7).

Two steps, both with memory bounded independently of corpus size:

1. :func:`write_snapshot` turns a generation's entry stream (articles and redirects, in upstream
   order) into a *snapshot file* on local disk: one JSON line per native id, sorted by native id,
   zstd-compressed, holding the content hash and (by default) the normalized text. Sorting is an
   external merge sort: entries are buffered up to ``run_bytes``, sorted and spilled as runs, then
   k-way merged (at most ``fan_in`` runs open at once). Keep the previous generation's snapshot on
   local disk until the next cycle; for English Wikipedia it is a few GB.

2. :class:`SnapshotDiff` merge-joins two snapshot files in one sequential pass and yields
   ``ChangeRecord`` values. Modified documents stream out immediately. Added and removed candidates
   go to a temporary on-disk SQLite database so renames can be paired afterwards:

   * a removed title that became a redirect to an added title is a rename (a MediaWiki page move,
     or a ZIM redirect left behind);
   * otherwise a removed and an added document with the same, unique, content hash are a rename.

   Memory therefore holds one row per open run plus SQLite's page cache, whatever the corpus size.

Optionally the diff writes a **net-diff segment** (``increments/<YYYY-MM>.jsonl.zst``): a header line,
then one JSON line per change with a zero-context unified diff of the normalized text for modified
documents, the full text for added ones and a tombstone for removed ones. A generation's text is
the base generation's plus all segments in order (:func:`apply_unified_diff`).
"""

from __future__ import annotations

import difflib
import heapq
import io
import json
import re
import sqlite3
import tempfile
from collections.abc import Generator, Iterable, Iterator, Sequence
from contextlib import ExitStack, closing
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any, Literal, cast

import zstandard

from wsp_adapters._common import ordered_parallel_map
from wsp_adapters.entries import Entry, EntrySource, Redirect
from wsp_core.ids import content_hash, make_doc_id, normalize_text
from wsp_core.model import ChangeKind, ChangeRecord
from wsp_core.ports import Ctx, SourceAdapter

SNAPSHOT_FORMAT = "wsp-snapshot/1"
SEGMENT_FORMAT = "wsp-increment/1"
DEFAULT_RUN_BYTES = 256 * 1024 * 1024
DEFAULT_FAN_IN = 64
_RUN_LEVEL = 3
_SNAPSHOT_LEVEL = 6
_SEGMENT_LEVEL = 12
_DIFF_BATCH = 256
_EMPTY_HASH = content_hash("")
_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")

Kind = Literal["doc", "redirect"]


@dataclass(frozen=True, slots=True)
class SnapshotRow:
    native_id: str
    kind: Kind
    content_hash: str = ""
    title: str = ""
    target: str = ""  # redirect target (native id)
    text: str | None = None  # normalized text, when the snapshot keeps text


@dataclass(slots=True)
class SnapshotStats:
    docs: int = 0
    redirects: int = 0
    duplicates: int = 0
    runs: int = 0


# --- snapshot files ------------------------------------------------------------------------------


def _row_line(row: SnapshotRow) -> bytes:
    """``<json id>\\t<json record>\\n``. The id prefix lets merges compare without parsing the text."""
    body: list[Any] = [row.kind, row.content_hash, row.title, row.target, row.text]
    return (json.dumps(row.native_id) + "\t" + json.dumps(body, ensure_ascii=False) + "\n").encode()


def _line_id(line: bytes) -> str:
    return cast(str, json.loads(line[: line.index(b"\t")]))


def _parse_row(line: bytes) -> SnapshotRow:
    tab = line.index(b"\t")
    kind, chash, title, target, text = cast(list[Any], json.loads(line[tab + 1 :]))
    return SnapshotRow(cast(str, json.loads(line[:tab])), kind, chash, title, target, text)


def _to_row(entry: Entry, keep_text: bool) -> SnapshotRow:
    if isinstance(entry, Redirect):
        return SnapshotRow(entry.native_id, "redirect", target=entry.target)
    text = normalize_text(entry.text)
    return SnapshotRow(
        native_id=entry.native_id,
        kind="doc",
        content_hash=entry.content_hash or content_hash(text),
        title=entry.title,
        text=text if keep_text else None,
    )


def _write_lines(path: Path, lines: Iterable[bytes], level: int, header: bytes | None = None) -> None:
    cctx = zstandard.ZstdCompressor(level=level, threads=-1)
    with path.open("wb") as raw, cctx.stream_writer(raw) as out:
        if header is not None:
            out.write(header)
        for line in lines:
            out.write(line)


def _read_lines(path: Path) -> Generator[bytes]:
    with path.open("rb") as raw:
        reader = io.BufferedReader(zstandard.ZstdDecompressor().stream_reader(raw), 1024 * 1024)
        yield from reader


def _merge_sorted(paths: Sequence[Path]) -> Iterator[tuple[str, bytes]]:
    with ExitStack() as stack:
        streams = [stack.enter_context(closing(_read_lines(p))) for p in paths]
        keyed = [((_line_id(line), line) for line in s) for s in streams]
        yield from heapq.merge(*keyed, key=lambda kv: kv[0])


def write_snapshot(
    entries: Iterable[Entry],
    out: Path,
    *,
    keep_text: bool = True,
    run_bytes: int = DEFAULT_RUN_BYTES,
    fan_in: int = DEFAULT_FAN_IN,
    tmp_dir: Path | None = None,
) -> SnapshotStats:
    """Write the sorted snapshot file of one generation. Peak memory is about ``run_bytes``."""
    stats = SnapshotStats()
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="wsp-snap-", dir=tmp_dir or out.parent) as tmp:
        runs: list[Path] = []
        buf: list[tuple[str, bytes]] = []
        size = 0

        def spill() -> None:
            nonlocal buf, size
            buf.sort(key=lambda kv: kv[0])
            path = Path(tmp) / f"run-{len(runs):05d}.zst"
            _write_lines(path, (line for _, line in buf), _RUN_LEVEL)
            runs.append(path)
            buf, size = [], 0

        for entry in entries:
            row = _to_row(entry, keep_text)
            if row.kind == "doc":
                stats.docs += 1
            else:
                stats.redirects += 1
            line = _row_line(row)
            buf.append((row.native_id, line))
            size += len(line) + 100
            if size >= run_bytes:
                spill()
        if buf or not runs:
            spill()
        stats.runs = len(runs)
        # Reduce to at most fan_in runs so the final merge keeps few decoders open.
        generation = 0
        while len(runs) > fan_in:
            merged: list[Path] = []
            for i in range(0, len(runs), fan_in):
                group = runs[i : i + fan_in]
                path = Path(tmp) / f"merge-{generation}-{i:05d}.zst"
                _write_lines(path, (line for _, line in _merge_sorted(group)), _RUN_LEVEL)
                for p in group:
                    p.unlink()
                merged.append(path)
            runs, generation = merged, generation + 1

        def deduplicated() -> Iterator[bytes]:
            last: str | None = None
            for key, line in _merge_sorted(runs):
                if key == last:
                    stats.duplicates += 1
                    continue
                last = key
                yield line

        header = json.dumps({"format": SNAPSHOT_FORMAT, "keep_text": keep_text}) + "\n"
        partial = out.with_name(out.name + ".partial")
        _write_lines(partial, deduplicated(), _SNAPSHOT_LEVEL, header=header.encode())
        partial.replace(out)
    return stats


def read_snapshot(path: Path) -> Iterator[SnapshotRow]:
    """Rows of a snapshot file in native-id order."""
    lines = _read_lines(path)
    header = json.loads(next(lines, b"{}"))
    if header.get("format") != SNAPSHOT_FORMAT:
        raise ValueError(f"{path.name}: not a {SNAPSHOT_FORMAT} file")
    for line in lines:
        yield _parse_row(line)


def snapshot_generation(
    adapter: SourceAdapter, ctx: Ctx, generation_dir: Path, out: Path, **kwargs: Any
) -> SnapshotStats:
    """Snapshot a sealed generation, including redirects when the adapter provides them."""
    entries: Iterable[Entry] = (
        adapter.entries(ctx, generation_dir)
        if isinstance(adapter, EntrySource)
        else adapter.documents(ctx, generation_dir)
    )
    return write_snapshot(entries, out, **kwargs)


# --- diffing -------------------------------------------------------------------------------------


@dataclass(slots=True)
class DiffStats:
    added: int = 0
    modified: int = 0
    removed: int = 0
    renamed: int = 0
    unchanged: int = 0
    segment_bytes: int = 0


def unified_diff(old: str, new: str) -> str:
    """Zero-context unified diff (hunks only, no file headers) between two normalized texts."""
    lines = difflib.unified_diff(old.split("\n"), new.split("\n"), n=0, lineterm="")
    return "\n".join(line for i, line in enumerate(lines) if i >= 2)


def apply_unified_diff(old: str, diff: str) -> str:
    """Inverse of :func:`unified_diff`: ``apply_unified_diff(a, unified_diff(a, b)) == b``."""
    src = old.split("\n")
    out: list[str] = []
    pos = 0
    idx = 0
    for line in diff.split("\n") if diff else ():
        hunk = _HUNK_RE.match(line)
        if hunk:
            start, count = int(hunk.group(1)), int(hunk.group(2) or 1)
            idx = start - 1 if count else start
            out.extend(src[pos:idx])
            pos = idx
        elif line.startswith("-"):
            if src[pos] != line[1:]:
                raise ValueError(f"diff does not apply at line {pos + 1}")
            pos += 1
        elif line.startswith("+"):
            out.append(line[1:])
        elif line.startswith(" "):
            out.append(src[pos])
            pos += 1
    out.extend(src[pos:])
    return "\n".join(out)


def _join(
    old: Iterator[SnapshotRow], new: Iterator[SnapshotRow]
) -> Iterator[tuple[SnapshotRow | None, SnapshotRow | None]]:
    o, n = next(old, None), next(new, None)
    while o is not None or n is not None:
        if n is None or (o is not None and o.native_id < n.native_id):
            yield o, None
            o = next(old, None)
        elif o is None or n.native_id < o.native_id:
            yield None, n
            n = next(new, None)
        else:
            yield o, n
            o, n = next(old, None), next(new, None)


def _diff_batch(pairs: Sequence[tuple[str, str, str]]) -> list[str]:
    """Worker: unified diffs for ``(native_id, old_text, new_text)`` triples."""
    return [unified_diff(a, b) for _, a, b in pairs]


@dataclass
class SnapshotDiff:
    """Iterate to get the ``ChangeRecord`` values between two snapshot files of one source.

    ``stats`` is complete once iteration finishes. If ``segment`` is given it is written atomically
    when iteration completes.
    """

    old: Path
    new: Path
    source_id: str
    generation_id: str
    segment: Path | None = None
    from_generation: str | None = None
    workdir: Path | None = None
    workers: int = 1
    stats: DiffStats = field(default_factory=DiffStats)

    def __iter__(self) -> Iterator[ChangeRecord]:
        with ExitStack() as stack:
            tmp = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="wsp-diff-", dir=self.workdir)))
            db = stack.enter_context(closing(sqlite3.connect(tmp / "candidates.db")))
            db.executescript(
                "PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF; PRAGMA cache_size=-65536;"
                "CREATE TABLE removed(id TEXT PRIMARY KEY, hash TEXT, target TEXT, text TEXT,"
                " done INT DEFAULT 0);"
                "CREATE TABLE added(id TEXT PRIMARY KEY, hash TEXT, title TEXT, text TEXT,"
                " done INT DEFAULT 0);"
            )
            seg: IO[bytes] | None = None
            raw: IO[bytes] | None = None
            partial: Path | None = None
            if self.segment is not None:
                self.segment.parent.mkdir(parents=True, exist_ok=True)
                partial = self.segment.with_name(self.segment.name + ".partial")
                raw = stack.enter_context(partial.open("wb"))
                stack.callback(partial.unlink, missing_ok=True)  # no-op once renamed into place
                cctx = zstandard.ZstdCompressor(level=_SEGMENT_LEVEL, threads=-1)
                seg = cast(IO[bytes], cctx.stream_writer(raw, closefd=False))
                seg.write(self._header())
            yield from self._merge(db, seg)
            db.execute("CREATE INDEX added_hash ON added(hash)")
            db.execute("CREATE INDEX removed_hash ON removed(hash)")
            yield from self._pair(db, seg)
            if seg is not None and raw is not None and partial is not None and self.segment is not None:
                seg.close()  # ends the zstd frame
                raw.close()  # flush to disk before the atomic rename
                partial.replace(self.segment)
                self.stats.segment_bytes = self.segment.stat().st_size

    def _header(self) -> bytes:
        header = {
            "format": SEGMENT_FORMAT,
            "source_id": self.source_id,
            "from_generation": self.from_generation,
            "to_generation": self.generation_id,
            "created": datetime.now(UTC).isoformat(timespec="seconds"),
            "diff": "unified, zero context, normalized text split on LF",
        }
        return _jsonl(header)

    def _record(
        self,
        kind: ChangeKind,
        native_id: str,
        old_hash: str | None,
        new_hash: str | None,
        renamed_from: str | None = None,
    ) -> ChangeRecord:
        setattr(self.stats, kind.value, getattr(self.stats, kind.value) + 1)
        return ChangeRecord(
            doc_id=make_doc_id(self.source_id, native_id),
            kind=kind,
            generation_id=self.generation_id,
            old_hash=old_hash,
            new_hash=new_hash,
            renamed_from=make_doc_id(self.source_id, renamed_from) if renamed_from else None,
        )

    def _merge(self, db: sqlite3.Connection, seg: IO[bytes] | None) -> Iterator[ChangeRecord]:
        pending: list[tuple[SnapshotRow, SnapshotRow]] = []

        def flush() -> Iterator[ChangeRecord]:
            batch = list(pending)
            pending.clear()
            yield from self._emit_modified(batch, seg)

        for o, n in _join(read_snapshot(self.old), read_snapshot(self.new)):
            od = o if o is not None and o.kind == "doc" else None
            nd = n if n is not None and n.kind == "doc" else None
            if od is not None and nd is not None:
                if od.content_hash == nd.content_hash:
                    self.stats.unchanged += 1
                    continue
                pending.append((od, nd))
                if len(pending) >= _DIFF_BATCH * max(1, self.workers):
                    yield from flush()
            elif od is not None:
                target = n.target if n is not None and n.kind == "redirect" else ""
                db.execute(
                    "INSERT INTO removed(id, hash, target, text) VALUES (?,?,?,?)",
                    (od.native_id, od.content_hash, target, od.text),
                )
            elif nd is not None:
                db.execute(
                    "INSERT INTO added(id, hash, title, text) VALUES (?,?,?,?)",
                    (nd.native_id, nd.content_hash, nd.title, nd.text),
                )
        yield from flush()

    def _emit_modified(
        self, pairs: Sequence[tuple[SnapshotRow, SnapshotRow]], seg: IO[bytes] | None
    ) -> Iterator[ChangeRecord]:
        diffs: list[str] = []
        if seg is not None and pairs:
            triples = [(o.native_id, _need_text(o), _need_text(n)) for o, n in pairs]
            batches = [triples[i : i + _DIFF_BATCH] for i in range(0, len(triples), _DIFF_BATCH)]
            diffs = [d for chunk in ordered_parallel_map(_diff_batch, batches, self.workers) for d in chunk]
        for i, (o, n) in enumerate(pairs):
            if seg is not None:
                seg.write(
                    _jsonl(
                        {
                            "op": "modified",
                            "id": n.native_id,
                            "old_hash": o.content_hash,
                            "new_hash": n.content_hash,
                            "diff": diffs[i],
                        }
                    )
                )
            yield self._record(ChangeKind.MODIFIED, n.native_id, o.content_hash, n.content_hash)

    def _pair(self, db: sqlite3.Connection, seg: IO[bytes] | None) -> Iterator[ChangeRecord]:
        # Candidate pairs are id/hash tuples only; their number is bounded by monthly churn.
        # 1. Page moves: the old title is now a redirect to a newly added title.
        moves = db.execute(
            "SELECT r.id, r.hash, a.id, a.hash FROM removed r JOIN added a ON a.id = r.target"
            " WHERE r.target != '' ORDER BY a.id"
        ).fetchall()
        # 2. Same non-empty content under a new id, when the hash is unique on both sides.
        same = db.execute(
            "SELECT r.id, r.hash, a.id, a.hash FROM removed r JOIN added a ON a.hash = r.hash"
            " WHERE r.hash != ? "
            "   AND r.hash IN (SELECT hash FROM removed GROUP BY hash HAVING COUNT(*) = 1)"
            "   AND a.hash IN (SELECT hash FROM added GROUP BY hash HAVING COUNT(*) = 1)"
            " ORDER BY a.id",
            (_EMPTY_HASH,),
        ).fetchall()
        taken_old: set[str] = set()
        taken_new: set[str] = set()
        for old_id, old_hash, new_id, new_hash in [*moves, *same]:
            if old_id in taken_old or new_id in taken_new:
                continue
            taken_old.add(old_id)
            taken_new.add(new_id)
            db.execute("UPDATE removed SET done = 1 WHERE id = ?", (old_id,))
            db.execute("UPDATE added SET done = 1 WHERE id = ?", (new_id,))
            if seg is not None:
                rec: dict[str, Any] = {
                    "op": "renamed",
                    "id": new_id,
                    "from": old_id,
                    "old_hash": old_hash,
                    "new_hash": new_hash,
                }
                if old_hash != new_hash:
                    (old_text,) = db.execute("SELECT text FROM removed WHERE id = ?", (old_id,)).fetchone()
                    (new_text,) = db.execute("SELECT text FROM added WHERE id = ?", (new_id,)).fetchone()
                    rec["diff"] = unified_diff(_need_str(old_text), _need_str(new_text))
                seg.write(_jsonl(rec))
            yield self._record(ChangeKind.RENAMED, new_id, old_hash, new_hash, renamed_from=old_id)
        for old_id, old_hash in db.execute("SELECT id, hash FROM removed WHERE done = 0 ORDER BY id"):
            if seg is not None:
                seg.write(_jsonl({"op": "removed", "id": old_id, "old_hash": old_hash}))
            yield self._record(ChangeKind.REMOVED, old_id, old_hash, None)
        for new_id, new_hash, title, text in db.execute(
            "SELECT id, hash, title, text FROM added WHERE done = 0 ORDER BY id"
        ):
            if seg is not None:
                seg.write(
                    _jsonl(
                        {
                            "op": "added",
                            "id": new_id,
                            "new_hash": new_hash,
                            "title": title,
                            "text": _need_str(text),
                        }
                    )
                )
            yield self._record(ChangeKind.ADDED, new_id, None, new_hash)


def _need_text(row: SnapshotRow) -> str:
    return _need_str(row.text)


def _need_str(text: str | None) -> str:
    if text is None:
        raise ValueError("a net-diff segment needs snapshots written with keep_text=True")
    return text


def _jsonl(obj: object) -> bytes:
    return (json.dumps(obj, ensure_ascii=False) + "\n").encode()


def read_segment(path: Path) -> Iterator[dict[str, Any]]:
    """Header first, then one change per item."""
    for line in _read_lines(path):
        yield cast(dict[str, Any], json.loads(line))


def increment_path(source_dir: Path, generation_id: str) -> Path:
    """``<source_dir>/increments/<YYYY-MM>.jsonl.zst`` for a generation id starting ``YYYY-MM``."""
    return source_dir / "increments" / f"{generation_id[:7]}.jsonl.zst"
