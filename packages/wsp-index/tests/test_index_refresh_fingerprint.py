"""Index fingerprints in the refresh diff: a change the search index holds besides the text (a title
repaired by a refine-again, 2026-11-06) is upserted; rows refined before the fingerprint existed do not
make the next build upsert everything."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from wsp_core.ids import make_doc_id
from wsp_core.model import BYLINE_META, ChangeKind, DocRecord
from wsp_index import TantivyConfig, TantivyIndex
from wsp_index.changefeed import SqliteChangeFeed
from wsp_index.delta import KeyIndex, diff, index_keys
from wsp_index.refinery import (
    INDEX_HASH,
    RefineryReader,
    RefineryWriter,
    generation_dir,
    index_fingerprint,
)
from wsp_index.sync import refresh
from wsp_index_helpers import doc

SMALL = TantivyConfig(writer_heap_bytes=50_000_000, writer_threads=1)
N = 12


def gen(root: Path, gid: str, docs: list[DocRecord]) -> RefineryReader:
    with RefineryWriter(root, "enc", gid, docs_per_file=5, row_group_rows=2) as w:
        w.write(docs)
    return RefineryReader(generation_dir(root, "enc", gid))


def cut_titles() -> list[DocRecord]:
    return [doc("enc", f"d{i:02d}", "Encyclopédie de l", f"article body {i}") for i in range(N)]


def repaired(first: int = N) -> list[DocRecord]:
    """The same documents, the first ``first`` with a repaired title and the same text."""
    return [
        replace(d, title="Encyclopédie de l'environnement") if i < first else d
        for i, d in enumerate(cut_titles())
    ]


def without_fingerprints(reader: RefineryReader) -> RefineryReader:
    """The generation as code before 2026-11-06 wrote it: no ``index_hash`` column."""
    for part in reader.parts:
        table = pq.read_table(part)  # pyright: ignore[reportUnknownMemberType]
        pq.write_table(table.drop([INDEX_HASH]), part, row_group_size=2)  # pyright: ignore[reportUnknownMemberType]
    return RefineryReader(reader.dir)


def test_writer_stores_a_fingerprint_per_document(tmp_path: Path) -> None:
    g = gen(tmp_path, "g1", cut_titles())
    table = g.read_columns(["doc_id", INDEX_HASH])
    keys = index_keys(table)
    assert len(keys) == N
    assert bool(keys.all())


def test_title_only_change_is_upserted(tmp_path: Path) -> None:
    g1, g2 = gen(tmp_path, "g1", cut_titles()), gen(tmp_path, "g2", repaired(first=3))
    feed = SqliteChangeFeed(tmp_path / "feed.sqlite")
    with TantivyIndex(tmp_path / "ix", SMALL) as idx:
        s1 = refresh(idx, feed, None, g1)
        assert s1.upserted == N
        s2 = refresh(idx, feed, g1, g2)
        assert (s2.upserted, s2.deleted) == (3, 0)
        hits = idx.search("environnement")
        assert sorted(h.doc_id for h in hits) == sorted(make_doc_id("enc", f"d{i:02d}") for i in range(3))
        assert {h.title for h in hits} == {"Encyclopédie de l'environnement"}
    changes, _ = feed.since(s1.cursor)
    assert {c.kind for c in changes} == {ChangeKind.MODIFIED}
    assert all(c.old_hash == c.new_hash for c in changes)  # the text did not change


def test_unchanged_documents_are_not_upserted(tmp_path: Path) -> None:
    g1, g2 = gen(tmp_path, "g1", cut_titles()), gen(tmp_path, "g2", cut_titles())
    assert list(diff(g1, g2)) == []
    with TantivyIndex(tmp_path / "ix", SMALL) as idx:
        refresh(idx, None, None, g1)
        assert refresh(idx, None, g1, g2).upserted == 0


def test_old_rows_without_fingerprints_do_not_upsert_everything(tmp_path: Path) -> None:
    g1 = without_fingerprints(gen(tmp_path, "g1", cut_titles()))
    assert len(KeyIndex.build(g1).fp) == 0  # no memory spent on a generation without them
    g2 = gen(tmp_path, "g2", repaired())  # every title differs, no text does
    assert list(diff(g1, g2)) == []
    with TantivyIndex(tmp_path / "ix", SMALL) as idx:
        refresh(idx, None, None, g1)
        assert refresh(idx, None, g1, g2).upserted == 0
    # A new generation without them (an old one from current code's view) compares by content too.
    g3 = without_fingerprints(gen(tmp_path, "g3", repaired()))
    g4 = gen(tmp_path, "g4", cut_titles())
    assert list(diff(g4, g3)) == []
    # Text changes are still found.
    g5 = gen(
        tmp_path, "g5", [replace(d, text="new body") if i == 0 else d for i, d in enumerate(cut_titles())]
    )
    assert [c.doc_id for c in diff(g1, g5)] == [make_doc_id("enc", "d00")]


def test_refine_again_derives_the_old_copys_fingerprints(tmp_path: Path) -> None:
    g1 = without_fingerprints(gen(tmp_path, "g1", cut_titles()))
    same = gen(tmp_path, "same", cut_titles())
    assert list(diff(g1, same, derive_old=True)) == []  # derived exactly as the writer stores them
    g2 = gen(tmp_path, "g2", repaired(first=4))
    with TantivyIndex(tmp_path / "ix", SMALL) as idx:
        refresh(idx, None, None, g1)
        s = refresh(idx, None, g1, g2, derive_old=True)
        assert s.upserted == 4
        assert len(idx.search("environnement")) == 4


def test_fingerprint_covers_what_the_index_holds_only() -> None:
    base = index_fingerprint("T", "en", ("a",), {BYLINE_META: "X", "search_chars": "10"})
    assert base == index_fingerprint("T", "en", ["a"], {BYLINE_META: "X", "search_chars": "10", "other": "1"})
    assert base != index_fingerprint("T2", "en", ("a",), {BYLINE_META: "X", "search_chars": "10"})
    assert base != index_fingerprint("T", "fr", ("a",), {BYLINE_META: "X", "search_chars": "10"})
    assert base != index_fingerprint("T", "en", ("b",), {BYLINE_META: "X", "search_chars": "10"})
    assert base != index_fingerprint("T", "en", ("a",), {BYLINE_META: "Y", "search_chars": "10"})
    assert base != index_fingerprint("T", "en", ("a",), {BYLINE_META: "X"})
    assert index_fingerprint("T", "en", ("a", "b"), None) == index_fingerprint(
        "T", "en", ("b", "a", "a"), None
    )
    assert index_fingerprint("", "", (), None) != 0


def test_index_keys_reads_nulls_and_missing_columns_as_zero() -> None:
    t = pa.Table.from_arrays([pa.array(["a", "b"]), pa.array([5, None], pa.uint64())], ["doc_id", INDEX_HASH])
    assert index_keys(t).tolist() == [5, 0]
    assert index_keys(pa.Table.from_arrays([pa.array(["a"])], ["doc_id"])).tolist() == [0]
    assert index_keys(t).dtype == np.uint64
