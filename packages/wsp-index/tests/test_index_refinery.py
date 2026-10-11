from __future__ import annotations

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


def write(root: Path, gen: str, docs: list[DocRecord], **kw: int) -> RefineryReader:
    with RefineryWriter(root, "src", gen, **kw) as w:
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
