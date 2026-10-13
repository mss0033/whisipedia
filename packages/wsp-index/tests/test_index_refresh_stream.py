"""Streaming, resumable refresh (wsp_index.sync / wsp_index.delta) and the Refinery bounds behind it."""

from __future__ import annotations

import shutil
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq
import pytest

from wsp_core.ids import DocId, make_doc_id
from wsp_core.model import ChangeKind, ChangeRecord, DocRecord
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


def test_without_the_old_generation_stale_documents_are_found_in_the_others(tmp_path: Path) -> None:
    # Refreshed without ``old`` (state lost): stale documents are found in the source's other Refinery
    # generations (ADR-0011), here g1, and removed.
    g1, g2 = gen(tmp_path, "g1", docs_v1()), gen(tmp_path, "g2", docs_v2())
    with TantivyIndex(tmp_path / "ix", SMALL) as idx:
        refresh(idx, None, None, g1)
        stats = refresh(idx, None, None, g2)
        assert stats.deleted == 2
        assert idx.num_docs == 19
        assert not idx.has_source("other")
        assert idx.has_source("enc")
        assert g1.dir.is_dir()


def zebras(n: int) -> list[DocRecord]:
    return [doc("enc", f"z{i:03d}", f"Zebra {i}", f"zebra number {i}") for i in range(n)]


def indexed(idx: TantivyIndex) -> tuple[int, int]:
    """(documents, distinct doc ids) in the index."""
    found = [h.doc_id for h in idx.search_hits("zebra", limit=1000)]
    return len(found), len(set(found))


class KilledError(RuntimeError):
    pass


def kill_publish(monkeypatch: pytest.MonkeyPatch, when: Callable[[int, list[ChangeRecord]], bool]) -> None:
    """Make ``SqliteChangeFeed.publish`` fail once, on the call (counted from 1) ``when`` picks."""
    real = SqliteChangeFeed.publish
    calls = {"n": 0, "killed": False}

    def flaky(self: SqliteChangeFeed, changes: Iterable[ChangeRecord], **kw: Any) -> str:
        records = list(changes)
        calls["n"] += 1
        if not calls["killed"] and when(calls["n"], records):
            calls["killed"] = True
            raise KilledError("killed")
        return real(self, records, **kw)

    monkeypatch.setattr(SqliteChangeFeed, "publish", flaky)


def test_a_resumed_first_load_never_adds_a_document_twice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Audit 2026-10-12 #3: part 2 was committed but not checkpointed; the resume added it again.
    g1 = gen(tmp_path, "g1", zebras(30), per_file=10)
    feed = SqliteChangeFeed(tmp_path / "feed.sqlite")
    kill_publish(monkeypatch, lambda n, _: n == 2)  # after part 2's commit
    with TantivyIndex(tmp_path / "ix", SMALL) as idx:
        with pytest.raises(KilledError):
            refresh(idx, feed, None, g1)
        refresh(idx, feed, None, g1)
        assert indexed(idx) == (30, 30)
        assert idx.num_docs == 30


def test_a_resumed_reload_of_a_held_source_replaces_its_documents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Audit 2026-10-12 #3: the source was held from g1 but refreshed without it (old=None); after a
    # resume every remaining part was added without deleting its documents first.
    g1, g2 = gen(tmp_path, "g1", zebras(30), per_file=10), gen(tmp_path, "g2", zebras(30), per_file=10)
    feed = SqliteChangeFeed(tmp_path / "feed.sqlite")
    with TantivyIndex(tmp_path / "ix", SMALL) as idx:
        refresh(idx, feed, None, g1)
        kill_publish(monkeypatch, lambda n, _: n == 2)  # after g2's part 2 commit (part 1 checkpointed)
        with pytest.raises(KilledError):
            refresh(idx, feed, None, g2)
        refresh(idx, feed, None, g2)
        assert indexed(idx) == (30, 30)
        assert idx.num_docs == 30


@pytest.mark.parametrize("stage", ["publish", "delete"])
def test_a_resumed_removal_of_orphans_removes_every_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    # Audit 2026-10-12 #4: removals found from the index were skipped by count on resume, but the index
    # had already forgotten the deleted ones: as many orphans as were done stayed searchable for good.
    import wsp_index.sync as sync

    monkeypatch.setattr(sync, "DELETE_CHUNK", 10)
    g1, g2 = gen(tmp_path, "g1", zebras(60), per_file=10), gen(tmp_path, "g2", zebras(30), per_file=10)
    feed = SqliteChangeFeed(tmp_path / "feed.sqlite")
    with TantivyIndex(tmp_path / "ix", SMALL) as idx:
        refresh(idx, feed, None, g1)
        head = feed.head()
        removals = {"n": 0}
        if stage == "publish":  # the second chunk is deleted and committed, its records not published

            def second_removal(_n: int, records: list[ChangeRecord]) -> bool:
                removals["n"] += any(r.kind is ChangeKind.REMOVED for r in records)
                return removals["n"] == 2

            kill_publish(monkeypatch, second_removal)
        else:  # the second chunk is recorded as pending, then the delete fails
            real = TantivyIndex.delete

            def flaky(self: TantivyIndex, doc_ids: Iterable[DocId]) -> int:
                removals["n"] += 1
                if removals["n"] == 2:
                    raise KilledError("killed")
                return real(self, doc_ids)

            monkeypatch.setattr(TantivyIndex, "delete", flaky)
        with pytest.raises(KilledError):
            refresh(idx, feed, None, g2)
        idx.rollback()
        stats = refresh(idx, feed, None, g2)
        assert indexed(idx) == (30, 30)
        kept = sorted(h.doc_id for h in idx.search_hits("zebra", limit=100))
        assert kept == sorted(d.doc_id for d in zebras(30))
        published, _ = feed.since(head, limit=1000)
        removed = [c.doc_id for c in published if c.kind is ChangeKind.REMOVED]
        assert sorted(removed) == sorted(d.doc_id for d in zebras(60)[30:])  # each exactly once
        assert stats.deleted == 30


def test_a_held_source_without_any_other_generation_is_reloaded_with_a_warning(tmp_path: Path) -> None:
    # Its earlier documents can only be listed from another Refinery generation (ADR-0011: the index
    # keeps no table of them). The run says so, and still loads the new generation.
    g1, g2 = gen(tmp_path, "g1", zebras(30), per_file=10), gen(tmp_path, "g2", zebras(20), per_file=10)
    feed = SqliteChangeFeed(tmp_path / "feed.sqlite")
    lines: list[str] = []
    with TantivyIndex(tmp_path / "ix", SMALL) as idx:
        refresh(idx, feed, None, g1)
        shutil.rmtree(g1.dir)
        refresh(idx, feed, None, g2, progress=lines.append)
        assert any("rebuild-lexical" in line for line in lines)
        assert len(idx.present([d.doc_id for d in zebras(30)])) == 30  # g1's extra 10 cannot be found


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


def test_document_store_evicts_by_memory_not_just_count(tmp_path: Path) -> None:
    from wsp_index.docstore import ParquetDocumentStore

    for gid in ("g1", "g2", "g3"):
        gen(tmp_path, gid, docs_v1())
    store = ParquetDocumentStore(tmp_path, max_open_generations=16, memory_budget=1)
    for gid in ("g1", "g2", "g3"):
        assert store.reader("enc", gid) is not None
    assert len(store._readers) == 1  # pyright: ignore[reportPrivateUsage]  # the newest stays usable


def test_the_id_index_is_memory_mapped_from_a_sidecar(tmp_path: Path) -> None:
    import numpy as np

    from wsp_index.refinery import IDS_MAP_H, IDS_MAP_LOC

    g = gen(tmp_path, "g1", docs_v1())
    assert g.id_index_bytes == 20 * 20  # no sidecar yet: counted as anonymous memory
    got = g.get(make_doc_id("enc", "d05"))
    assert got is not None
    assert got.title == "Doc 5"
    assert (g.dir / IDS_MAP_H).is_file()
    assert (g.dir / IDS_MAP_LOC).is_file()
    assert isinstance(g._id_index()[0], np.memmap)  # pyright: ignore[reportPrivateUsage]
    assert g.id_index_bytes == 0
    assert not list(g.dir.glob("*.tmp"))
    # every document resolves through the map, and a fresh reader reuses it
    again = RefineryReader(g.dir)
    assert all(again.get(make_doc_id("enc", f"d{i:02d}")) is not None for i in range(20))
    assert again.get(make_doc_id("enc", "nope")) is None


def test_a_torn_id_map_is_rewritten(tmp_path: Path) -> None:
    import numpy as np

    from wsp_index.refinery import IDS_MAP_H

    g = gen(tmp_path, "g1", docs_v1())
    assert g.get(make_doc_id("enc", "d01")) is not None
    np.save(g.dir / IDS_MAP_H, np.arange(3, dtype=np.uint64))  # wrong length
    fresh = RefineryReader(g.dir)
    assert fresh.get(make_doc_id("enc", "d07")) is not None
    assert np.load(g.dir / IDS_MAP_H, mmap_mode="r").shape == (20,)


def test_an_unwritable_generation_falls_back_to_memory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import wsp_index.refinery as refinery

    g = gen(tmp_path, "g1", docs_v1())

    def refuse(gen_dir: Path, n: int, out_dir: Path) -> None:
        raise PermissionError("read-only")

    monkeypatch.setattr(refinery, "_write_id_map", refuse)
    assert g.get(make_doc_id("enc", "d09")) is not None
    assert g.id_index_bytes == 20 * 20


def test_a_truncated_id_map_is_rewritten(tmp_path: Path) -> None:
    # Audit 2026-10-12 #15: np.load raised ValueError ("mmap length is greater than file size") for a
    # truncated sidecar, and every lookup in the generation failed instead of rewriting it.
    import numpy as np

    from wsp_index.refinery import IDS_MAP_LOC

    g = gen(tmp_path, "g1", docs_v1())
    assert g.get(make_doc_id("enc", "d01")) is not None
    path = g.dir / IDS_MAP_LOC
    path.write_bytes(path.read_bytes()[:-40])
    fresh = RefineryReader(g.dir)
    assert fresh.get(make_doc_id("enc", "d07")) is not None
    assert np.load(path, mmap_mode="r").shape == (20, 3)


def test_history_writes_no_id_map_for_older_generations(tmp_path: Path) -> None:
    # Audit 2026-10-12 #12: a history request wrote a 20-byte-a-document sidecar for every generation.
    from wsp_index.docstore import ParquetDocumentStore
    from wsp_index.refinery import IDS_MAP_H

    gen(tmp_path, "g1", docs_v1())
    gen(tmp_path, "g2", docs_v2())
    store = ParquetDocumentStore(tmp_path)
    versions = store.history(make_doc_id("enc", "d00"))
    assert [g for g, _ in versions] == ["g1", "g2"]  # changed in g2
    assert store.get(make_doc_id("enc", "d03"), at_generation="g1") is not None  # removed in g2
    assert store.get(make_doc_id("enc", "d03")) is None
    assert not (generation_dir(tmp_path, "enc", "g1") / IDS_MAP_H).exists()
    assert (generation_dir(tmp_path, "enc", "g2") / IDS_MAP_H).exists()  # the current one has its map
    sparse = RefineryReader(generation_dir(tmp_path, "enc", "g1"), map_ids=False)
    assert all(sparse.get(d.doc_id) is not None for d in docs_v1())
    assert sparse.get(make_doc_id("enc", "nope")) is None
    assert sparse.id_index_bytes == 0
    assert sparse._ids is None  # pyright: ignore[reportPrivateUsage]
