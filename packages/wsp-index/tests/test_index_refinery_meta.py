"""``DocRecord.meta`` in the Refinery: stored as an optional column, absent in older files."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pyarrow.parquet as pq

from wsp_index.refinery import RefineryReader, RefineryWriter, generation_dir
from wsp_index_helpers import doc


def test_meta_round_trip_and_files_without_the_column(tmp_path: Path) -> None:
    a = replace(doc("src", "a", "A", "alpha"), meta={"currency": "historical", "historical": "true"})
    b = doc("src", "b", "B", "beta")
    with RefineryWriter(tmp_path, "src", "g1") as w:
        w.write([a, b])
    reader = RefineryReader(generation_dir(tmp_path, "src", "g1"))
    got = reader.get(a.doc_id)
    assert got is not None
    assert got.meta == {"currency": "historical", "historical": "true"}
    got_b = reader.get(b.doc_id)
    assert got_b is not None
    assert got_b.meta == {}

    # A generation written before the column existed: drop it from every part file.
    for part in reader.parts:
        table = pq.read_table(part)  # pyright: ignore[reportUnknownMemberType]
        pq.write_table(table.drop(["meta"]), part)  # pyright: ignore[reportUnknownMemberType]
    old = RefineryReader(generation_dir(tmp_path, "src", "g1"))
    old_a = old.get(a.doc_id)
    assert old_a is not None
    assert old_a.meta == {}
    assert old_a.text == a.text
    assert sorted(r.doc_id for r in old.records()) == sorted([a.doc_id, b.doc_id])
    assert old.read_columns(["doc_id", "meta"]).column_names == ["doc_id"]
