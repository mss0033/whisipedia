from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from wsp_core.ids import content_hash, make_doc_id
from wsp_core.model import ChangeKind, DocRecord
from wsp_index import refinery
from wsp_index.refinery import (
    RefineryError,
    RefineryReader,
    RefineryWriter,
    current_generation,
    diff_generations,
    generation_dir,
    inlink_counts,
    sealed_generations,
)
from wsp_index_helpers import doc


def write(
    root: Path,
    gen: str,
    docs: list[DocRecord],
    *,
    row_group_rows: int = refinery.DEFAULT_ROW_GROUP_ROWS,
    docs_per_file: int = refinery.DEFAULT_DOCS_PER_FILE,
) -> RefineryReader:
    with RefineryWriter(root, "src", gen, row_group_rows=row_group_rows, docs_per_file=docs_per_file) as w:
        w.write(docs)
    return RefineryReader(generation_dir(root, "src", gen))


def test_round_trip_across_files_and_row_groups(tmp_path: Path) -> None:
    docs = [doc("src", f"n{i:03d}", f"Title {i}", f"body {i}") for i in range(50)]
    reader = write(tmp_path, "g1", docs, row_group_rows=4, docs_per_file=16)
    assert len(reader) == 50
    assert len(reader.parts) == 4
    for d in docs:
        got = reader.get(d.doc_id)
        assert got is not None
        assert got.text == d.text
        assert got.content_hash == content_hash(d.text)  # filled in by the writer
    assert reader.get(make_doc_id("src", "missing")) is None
    assert make_doc_id("src", "n007") in reader
    assert sorted(r.doc_id for r in reader.records()) == sorted(d.doc_id for d in docs)


def test_files_are_zstd_with_small_row_groups(tmp_path: Path) -> None:
    reader = write(tmp_path, "g1", [doc("src", f"n{i}", "t") for i in range(10)], row_group_rows=3)
    meta = pq.ParquetFile(reader.parts[0]).metadata
    assert meta.num_row_groups == 4
    assert meta.row_group(0).column(0).compression == "ZSTD"


def test_timestamps_normalized_to_utc(tmp_path: Path) -> None:
    plus2 = timezone(timedelta(hours=2))
    naive = datetime(2026, 5, 1, 12, 0)  # naive input is documented as UTC
    a = doc("src", "a", "A")
    b = doc("src", "b", "B")
    reader = write(
        tmp_path,
        "g1",
        [
            replace(a, timestamp=datetime(2026, 5, 1, 12, 0, tzinfo=plus2)),
            replace(b, timestamp=naive),
        ],
    )
    ra, rb = reader.get(a.doc_id), reader.get(b.doc_id)
    assert ra is not None
    assert rb is not None
    assert ra.timestamp == datetime(2026, 5, 1, 10, 0, tzinfo=UTC)
    assert rb.timestamp == datetime(2026, 5, 1, 12, 0, tzinfo=UTC)


def test_duplicate_in_one_file_keeps_last(tmp_path: Path) -> None:
    w = RefineryWriter(tmp_path, "src", "g1")
    w.write([doc("src", "a", "A"), doc("src", "a", "A2")])
    w.close()
    r = RefineryReader(generation_dir(tmp_path, "src", "g1"))
    got = r.get(make_doc_id("src", "a"))
    assert got is not None
    assert got.title == "A2"
    assert [row["doc_id"] for b in r.scan(("doc_id",)) for row in b.to_pylist()] == [make_doc_id("src", "a")]


def test_duplicate_across_files_keeps_last_and_scan_skips_shadowed(tmp_path: Path) -> None:
    w = RefineryWriter(tmp_path, "src", "g1", docs_per_file=1)
    w.write([doc("src", "a", "A"), doc("src", "b", "B"), doc("src", "a", "A2")])
    w.close()
    r = RefineryReader(generation_dir(tmp_path, "src", "g1"))
    assert len(r) == 2
    got = r.get(make_doc_id("src", "a"))
    assert got is not None
    assert got.title == "A2"
    titles = sorted(row["title"] for b in r.scan(("doc_id", "title")) for row in b.to_pylist())
    assert titles == ["A2", "B"]
    assert r.read_columns(("title",)).num_rows == 2


def test_hash_collisions_resolved_by_stored_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def colliding(_doc_id: str) -> int:
        return 42

    monkeypatch.setattr(refinery, "id_hash", colliding)
    docs = [doc("src", f"n{i}", f"T{i}") for i in range(7)]
    reader = write(tmp_path, "g1", docs, docs_per_file=3, row_group_rows=2)
    for d in docs:
        got = reader.get(d.doc_id)
        assert got is not None
        assert got.title == d.title
    assert reader.get(make_doc_id("src", "zzz")) is None


def test_wrong_source_rejected(tmp_path: Path) -> None:
    with RefineryWriter(tmp_path, "src", "g1") as w, pytest.raises(RefineryError):
        w.write([doc("other", "a", "A")])


def test_unsealed_generation_invisible_and_sealed_is_immutable(tmp_path: Path) -> None:
    w = RefineryWriter(tmp_path, "src", "g2")
    w.write([doc("src", "a", "A")])
    write(tmp_path, "g1", [doc("src", "a", "A")])
    assert sealed_generations(tmp_path, "src") == ["g1"]
    with pytest.raises(RefineryError):
        RefineryReader(generation_dir(tmp_path, "src", "g2"))
    w.close()
    assert sealed_generations(tmp_path, "src") == ["g1", "g2"]
    with pytest.raises(RefineryError):
        RefineryWriter(tmp_path, "src", "g1")
    assert not list(generation_dir(tmp_path, "src", "g2").glob("*.partial"))


def test_current_generation_pointer(tmp_path: Path) -> None:
    write(tmp_path, "2026-01", [doc("src", "a", "A")])
    write(tmp_path, "2026-02", [doc("src", "a", "A")])
    assert current_generation(tmp_path, "src") == "2026-02"
    (tmp_path / "src" / "CURRENT.txt").write_text("2026-01\n")
    assert current_generation(tmp_path, "src") == "2026-01"
    assert current_generation(tmp_path, "nothing") is None


def test_empty_generation(tmp_path: Path) -> None:
    reader = write(tmp_path, "g1", [])
    assert len(reader) == 0
    assert reader.get(make_doc_id("src", "a")) is None
    assert list(diff_generations(None, reader)) == []


def test_diff_generations(tmp_path: Path) -> None:
    old = write(
        tmp_path, "g1", [doc("src", "keep", "K"), doc("src", "mod", "M", "v1"), doc("src", "gone", "G")]
    )
    new = write(
        tmp_path, "g2", [doc("src", "keep", "K"), doc("src", "mod", "M", "v2"), doc("src", "new", "N")]
    )
    changes = {c.doc_id: c for c in diff_generations(old, new)}
    assert set(changes) == {make_doc_id("src", n) for n in ("mod", "gone", "new")}
    assert changes[make_doc_id("src", "mod")].kind is ChangeKind.MODIFIED
    assert changes[make_doc_id("src", "mod")].old_hash == content_hash("v1")
    assert changes[make_doc_id("src", "mod")].new_hash == content_hash("v2")
    assert changes[make_doc_id("src", "gone")].kind is ChangeKind.REMOVED
    assert changes[make_doc_id("src", "gone")].new_hash is None
    assert changes[make_doc_id("src", "new")].kind is ChangeKind.ADDED
    assert all(c.generation_id == "g2" for c in changes.values())
    assert [c.kind for c in diff_generations(None, old)] == [ChangeKind.ADDED] * 3


def test_inlink_counts() -> None:
    a = make_doc_id("src", "a")
    counts = inlink_counts(
        [doc("src", "b", "B", links=(a, a)), doc("src", "c", "C", links=(a,)), doc("src", "a", "A")]
    )
    assert counts[a] == 2  # repeated links from one page count once


def test_concurrent_lookups_on_one_reader_are_safe(tmp_path: Path) -> None:
    # 2026-10-10/11: the API crashed (double free, SIGSEGV) when request threads read row groups of one
    # shared ParquetFile at the same time. Unlocked, this test crashed pyarrow 26 within seconds.
    import random
    import threading

    docs = [doc("src", f"n{i:04d}", f"Title {i}", "body " * 200 + str(i)) for i in range(3000)]
    reader = RefineryReader(
        write(tmp_path, "g1", docs, row_group_rows=8, docs_per_file=500).dir, cache_row_groups=2
    )
    errors: list[BaseException] = []

    def hammer(seed: int) -> None:
        rng = random.Random(seed)
        try:
            for _ in range(400):
                d = docs[rng.randrange(len(docs))]
                got = reader.get(d.doc_id)
                assert got is not None
                assert got.text == d.text
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=hammer, args=(s,)) for s in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors


def test_a_part_the_share_refuses_for_a_moment_is_opened_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """2026-10-28: EAGAIN opening one Refinery part on the NAS ended the nightly build."""
    import time

    import pyarrow as pa

    path = tmp_path / "part.parquet"
    pq.write_table(pa.table({"x": [1, 2]}), path)  # pyright: ignore[reportUnknownMemberType]
    real = pq.ParquetFile
    tries: list[int] = []

    def flaky(p: object) -> pq.ParquetFile:
        tries.append(1)
        if len(tries) < 3:
            raise BlockingIOError(11, "Resource temporarily unavailable")
        return real(p)  # type: ignore[arg-type]

    def no_wait(_s: float) -> None:
        return None

    monkeypatch.setattr(pq, "ParquetFile", flaky)
    monkeypatch.setattr(time, "sleep", no_wait)
    assert refinery.open_parquet(path).metadata.num_rows == 2
    assert len(tries) == 3

    tries.clear()
    monkeypatch.setattr(refinery, "OPEN_TRIES", 2)
    with pytest.raises(BlockingIOError):
        refinery.open_parquet(path)


def _no_waits(monkeypatch: pytest.MonkeyPatch) -> None:
    """Retries on the share without their 2-16 s pauses (``retrying`` binds ``time.sleep`` as a default)."""
    import time

    from wsp_core import nasretry

    def no_wait(_s: float) -> None:
        return None

    monkeypatch.setattr(time, "sleep", no_wait)
    monkeypatch.setattr(nasretry.retrying, "__kwdefaults__", {"sleep": no_wait})


def test_a_seal_the_share_refuses_for_a_moment_is_written_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """2026-11-04 trawl: only the parts were retried; one refusal writing ``_ids``, the shadow list or the
    seal (or sizing a published part) left a whole refine unsealed, and the next run refined it all again."""
    _no_waits(monkeypatch)
    real_replace = refinery._atomic_replace  # pyright: ignore[reportPrivateUsage]
    refused: list[str] = []

    def flaky_replace(partial: Path, final: Path) -> None:
        if final.name in (refinery.IDS_FILE, refinery.SHADOW_FILE) and final.name not in refused:
            refused.append(final.name)
            raise BlockingIOError(11, "Resource temporarily unavailable")
        if final.name == refinery.SUCCESS_FILE and final.name not in refused:
            refused.append(final.name)
            real_replace(partial, final)  # renamed, then the directory fsync refused: the .partial is gone
            raise ConnectionAbortedError(103, "Software caused connection abort")
        real_replace(partial, final)

    real_stat = Path.stat

    def flaky_stat(self: Path, *, follow_symlinks: bool = True) -> os.stat_result:
        if self.name == "part-00000.parquet" and self.name not in refused:
            refused.append(self.name)
            raise BlockingIOError(11, "Resource temporarily unavailable")
        return real_stat(self, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(refinery, "_atomic_replace", flaky_replace)
    monkeypatch.setattr(Path, "stat", flaky_stat)
    w = RefineryWriter(tmp_path, "src", "g1", docs_per_file=1)
    w.write([doc("src", "a", "A"), doc("src", "b", "B"), doc("src", "a", "A2")])  # "a" twice: a shadow list
    stats = w.close()
    assert sorted(refused) == sorted(
        ["part-00000.parquet", refinery.IDS_FILE, refinery.SHADOW_FILE, refinery.SUCCESS_FILE]
    )
    assert sealed_generations(tmp_path, "src") == ["g1"]
    assert stats.parts == 3
    assert stats.bytes > 0
    r = RefineryReader(generation_dir(tmp_path, "src", "g1"))
    assert len(r) == 2
    got = r.get(make_doc_id("src", "a"))
    assert got is not None
    assert got.title == "A2"
    assert not list(r.dir.glob("*.partial"))


def test_reads_the_share_refuses_for_a_moment_are_read_again_and_no_row_twice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """2026-11-04 trawl: only the open was retried; one refused read mid-scan ended a whole-Refinery phase
    (rank, spell, suggest) hours in. Here every other read is refused before it is served."""
    _no_waits(monkeypatch)
    refusals: list[str] = []

    def every_other_refused[**P, T](name: str, real: Callable[P, T]) -> Callable[P, T]:
        calls: list[int] = []

        def flaky(*args: P.args, **kwargs: P.kwargs) -> T:
            calls.append(1)
            if len(calls) % 2:
                refusals.append(name)
                raise BlockingIOError(11, "Resource temporarily unavailable")
            return real(*args, **kwargs)

        return flaky

    for name in ("read", "read_row_group", "read_row_groups"):
        monkeypatch.setattr(pq.ParquetFile, name, every_other_refused(name, getattr(pq.ParquetFile, name)))
    docs = [doc("src", f"n{i:03d}", f"T{i}") for i in range(40)]
    plain = write(tmp_path, "g1", docs, row_group_rows=4, docs_per_file=16)
    # A row written again in a later part: the writer reads ids back to shadow it, and scans are masked.
    with RefineryWriter(tmp_path, "src", "g2", row_group_rows=4, docs_per_file=16) as w:
        w.write([*docs, doc("src", "n005", "T5 again")])
    shadowed = RefineryReader(generation_dir(tmp_path, "src", "g2"))
    assert len(shadowed) == 40
    expected = {
        "g1": sorted(d.title for d in docs),
        "g2": sorted([*(d.title for d in docs if d.native_id != "n005"), "T5 again"]),
    }
    for reader in (plain, shadowed):
        titles = expected[reader.dir.name]
        batches = list(reader.scan(("doc_id", "title"), batch_size=10))
        assert sorted(row["title"] for b in batches for row in b.to_pylist()) == titles
        assert max(b.num_rows for b in batches) <= 10
        assert sorted(map(str, reader.read_columns(("title",)).column("title").to_pylist())) == titles
        table, positions = reader.part_table(0, ("title",))
        assert table.num_rows == len(positions)
        rows = reader.part_rows(0, positions[:3], ("title",))
        assert rows.column("title").to_pylist() == table.column("title").to_pylist()[:3]
    assert set(refusals) == {"read", "read_row_group", "read_row_groups"}
    # A run of row groups also stops at a size: long texts never fill one batch past a string's 2 GB.
    monkeypatch.setattr(refinery, "SCAN_RUN_BYTES", 1)
    batches = list(plain.scan(("doc_id", "title"), batch_size=10))
    assert [b.num_rows for b in batches] == [4] * 10
    assert sorted(row["title"] for b in batches for row in b.to_pylist()) == expected["g1"]
