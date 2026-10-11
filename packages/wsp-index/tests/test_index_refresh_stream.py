"""Streaming, resumable refresh (wsp_index.sync / wsp_index.delta) and the Refinery bounds behind it."""

from __future__ import annotations

import shutil
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from wsp_core.ids import make_doc_id
from wsp_core.model import ChangeKind, DocRecord
from wsp_index import TantivyConfig, TantivyIndex
from wsp_index.changefeed import SqliteChangeFeed
from wsp_index.delta import KeyIndex, id_keys
from wsp_index.refinery import (
    RefineryReader,
    RefineryWriter,
    compose_set,
    generation_dir,
    part_dir,
    sealed_generations,
)
from wsp_index.sync import refresh
from wsp_index_helpers import doc

SMALL = TantivyConfig(writer_heap_bytes=50_000_000, writer_threads=1)


def gen(root: Path, gid: str, docs: list[DocRecord], *, per_file: int = 4) -> RefineryReader:
    with RefineryWriter(root, "enc", gid, docs_per_file=per_file, row_group_rows=2) as w:
        w.write(docs)
    return RefineryReader(generation_dir(root, "enc", gid))


def docs_v1() -> list[DocRecord]:
    return [doc("enc", f"d{i:02d}", f"Doc {i}", f"text number {i}") for i in range(20)]


def docs_v2() -> list[DocRecord]:
    out = [d for d in docs_v1() if d.native_id not in ("d03", "d17")]  # two removed
    out[0] = doc("enc", "d00", "Doc 0", "changed text zero")  # one modified
    out.append(doc("enc", "d99", "Doc 99", "brand new"))  # one added
    return out


def test_key_index_finds_every_live_document(tmp_path: Path) -> None:
    g = gen(tmp_path, "g1", docs_v1())
    keys = KeyIndex.build(g)
    assert len(keys) == 20
    q1, q2 = id_keys([make_doc_id("enc", "d07"), make_doc_id("enc", "nope")])
    found = keys.find(q1, q2)
    assert found[0] >= 0
    assert found[1] == -1


def test_part_rows_reads_only_what_is_asked(tmp_path: Path) -> None:
    g = gen(tmp_path, "g1", docs_v1(), per_file=10)
    table, pos = g.part_table(1, ["doc_id"])
    picked = g.part_rows(1, pos[[1, 4, 9]], ["doc_id", "text"])
    assert picked.column("doc_id").to_pylist() == [table.column("doc_id")[i].as_py() for i in (1, 4, 9)]


def test_refresh_applies_adds_changes_and_removals(tmp_path: Path) -> None:
    g1, g2 = gen(tmp_path, "g1", docs_v1()), gen(tmp_path, "g2", docs_v2())
    feed = SqliteChangeFeed(tmp_path / "feed.sqlite")
    with TantivyIndex(tmp_path / "ix", SMALL) as idx:
        s1 = refresh(idx, feed, None, g1)
        assert (s1.upserted, s1.deleted) == (20, 0)
        s2 = refresh(idx, feed, g1, g2)
        assert (s2.upserted, s2.deleted) == (2, 2)
        assert idx.num_docs == 19
        assert [h.doc_id for h in idx.search("brand")] == [make_doc_id("enc", "d99")]
        assert idx.search("number 3") == [] or all(
            h.doc_id != make_doc_id("enc", "d03") for h in idx.search("3")
        )
    changes, _ = feed.since(s1.cursor)
    kinds = {(c.doc_id.split("/")[-1], c.kind) for c in changes}
    assert kinds == {
        ("d00", ChangeKind.MODIFIED),
        ("d99", ChangeKind.ADDED),
        ("d03", ChangeKind.REMOVED),
        ("d17", ChangeKind.REMOVED),
    }
    modified = next(c for c in changes if c.kind is ChangeKind.MODIFIED)
    assert modified.old_hash
    assert modified.new_hash
    assert modified.old_hash != modified.new_hash


def test_a_crash_resumes_without_losing_or_repeating_feed_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    g1 = gen(tmp_path, "g1", docs_v1())  # 5 parts of 4 documents
    feed = SqliteChangeFeed(tmp_path / "feed.sqlite")
    calls = {"n": 0}
    real = TantivyIndex.commit

    def flaky(self: TantivyIndex, *, rebuild_suggest: bool = True) -> None:
        calls["n"] += 1
        if calls["n"] == 3:  # dies in the third part, after two parts were committed and published
            raise RuntimeError("killed")
        real(self, rebuild_suggest=rebuild_suggest)

    monkeypatch.setattr(TantivyIndex, "commit", flaky)
    idx = TantivyIndex(tmp_path / "ix", SMALL)
    with pytest.raises(RuntimeError):
        refresh(idx, feed, None, g1)
    idx.rollback()
    monkeypatch.setattr(TantivyIndex, "commit", real)
    stats = refresh(idx, feed, None, g1)
    idx.close()
    assert stats.upserted == 20
    published, _ = feed.since(None, limit=1000)
    assert sorted(c.doc_id for c in published) == sorted(d.doc_id for d in docs_v1())  # each exactly once
    with TantivyIndex(tmp_path / "ix", SMALL) as again:
        assert again.num_docs == 20


def test_without_the_old_generation_stale_documents_are_still_removed(tmp_path: Path) -> None:
    g1, g2 = gen(tmp_path, "g1", docs_v1()), gen(tmp_path, "g2", docs_v2())
    with TantivyIndex(tmp_path / "ix", SMALL) as idx:
        refresh(idx, None, None, g1)
        shutil.rmtree(g1.dir)  # the Refinery may be deleted; the index still holds g1
        stats = refresh(idx, None, None, g2)
        assert stats.deleted == 2
        assert idx.num_docs == 19
        assert not idx.has_source("other")
        assert idx.has_source("enc")


def test_long_documents_flush_early_into_small_row_groups(tmp_path: Path) -> None:
    books = [doc("enc", f"b{i}", f"Book {i}", "word " * 40_000) for i in range(6)]  # 200k chars each
    with RefineryWriter(tmp_path, "enc", "g1", buffer_chars=450_000) as w:
        w.write(books)
    reader = RefineryReader(generation_dir(tmp_path, "enc", "g1"))
    assert len(reader.parts) == 2  # flushed by size (3 books each), not after 50,000 documents
    meta = pq.ParquetFile(reader.parts[0]).metadata
    assert meta.row_group(0).num_rows < 256  # ~4M chars per row group


def test_set_numbers_never_repeat_after_a_set_is_deleted(tmp_path: Path) -> None:
    def part(gid: str, native: str) -> Path:
        out = part_dir(tmp_path, "src", gid)
        with RefineryWriter(tmp_path, "src", gid, directory=out) as w:
            w.write([doc("src", native, native)])
        return out

    ak, ca = part("2026-10-03.ak", "a"), part("2026-10-01.ca", "c")
    first = compose_set(tmp_path, "src", [ak])
    second = compose_set(tmp_path, "src", [ak, ca])
    shutil.rmtree(generation_dir(tmp_path, "src", first))
    ca2 = part("2026-10-02.ca", "c2")  # a backfill: the newest member id is unchanged
    third = compose_set(tmp_path, "src", [ak, ca2])
    assert third != second
    assert second in sealed_generations(tmp_path, "src")


def test_scans_never_load_the_id_index(tmp_path: Path) -> None:
    # Loading _ids costs ~20 bytes a document (1.4 GB for 32M); scans and counts must not need it.
    g = gen(tmp_path, "g1", docs_v1())
    reader = RefineryReader(g.dir)
    assert len(reader) == 20
    assert sum(b.num_rows for b in reader.scan(["doc_id"])) == 20
    assert reader._ids is None  # pyright: ignore[reportPrivateUsage]
    assert reader.get(make_doc_id("enc", "d05")) is not None
    assert reader._ids is not None  # pyright: ignore[reportPrivateUsage]
