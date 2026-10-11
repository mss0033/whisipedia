"""Set generations of partitioned sources (ADR-0010): the union of partition generations."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from wsp_core.ids import make_doc_id
from wsp_core.model import DocRecord
from wsp_index.docstore import ParquetDocumentStore
from wsp_index.refinery import (
    RefineryError,
    RefineryReader,
    RefineryWriter,
    compose_set,
    current_generation,
    diff_generations,
    generation_dir,
    part_dir,
    sealed_generations,
    set_members,
)
from wsp_index_helpers import doc


def write_part(root: Path, gid: str, docs: list[DocRecord], *, docs_per_file: int = 50_000) -> Path:
    out = part_dir(root, "src", gid)
    with RefineryWriter(root, "src", gid, directory=out, docs_per_file=docs_per_file) as w:
        w.write(docs)
    return out


def test_set_is_the_union_of_its_members(tmp_path: Path) -> None:
    ak = write_part(
        tmp_path, "2026-10-01.ak", [doc("src", "a1", "A1"), doc("src", "a2", "A2")], docs_per_file=1
    )
    ca = write_part(tmp_path, "2026-10-02.ca", [doc("src", "c1", "C1")])
    set_id = compose_set(tmp_path, "src", [ca, ak])
    assert set_id == "2026-10-02.ca.set00001"
    reader = RefineryReader(generation_dir(tmp_path, "src", set_id))
    assert len(reader) == 3
    assert {r.native_id for r in reader.records()} == {"a1", "a2", "c1"}
    got = reader.get(make_doc_id("src", "c1"))
    assert got is not None
    assert got.title == "C1"
    assert set_members(reader.dir) == ("2026-10-01.ak", "2026-10-02.ca")
    linked = reader.dir / "part-00000.parquet"
    assert os.stat(linked).st_ino == os.stat(ak / "part-00000.parquet").st_ino, "parts are hard links"
    assert sealed_generations(tmp_path, "src") == [set_id], "_parts is not a generation"
    assert current_generation(tmp_path, "src") == set_id


def test_same_members_reuse_the_set_and_new_members_make_a_later_one(tmp_path: Path) -> None:
    ak = write_part(tmp_path, "2026-10-01.ak", [doc("src", "a1", "A1", "v1")])
    ca = write_part(tmp_path, "2026-10-02.ca", [doc("src", "c1", "C1")])
    first = compose_set(tmp_path, "src", [ak, ca])
    assert compose_set(tmp_path, "src", [ca, ak]) == first
    ak2 = write_part(tmp_path, "2026-10-01.ak2", [doc("src", "a1", "A1", "v2"), doc("src", "a3", "A3")])
    second = compose_set(tmp_path, "src", [ak2, ca])
    assert second > first, "set ids sort in the order they were made"
    old = RefineryReader(generation_dir(tmp_path, "src", first))
    new = RefineryReader(generation_dir(tmp_path, "src", second))
    changes = {c.doc_id: c.kind.value for c in diff_generations(old, new)}
    assert changes == {make_doc_id("src", "a1"): "modified", make_doc_id("src", "a3"): "added"}
    store = ParquetDocumentStore(tmp_path)
    assert [g for g, _ in store.history(make_doc_id("src", "a1"))] == [first, second]
    assert store.get(make_doc_id("src", "c1")) is not None


def test_a_document_in_two_members_keeps_the_later_member(tmp_path: Path) -> None:
    b1 = write_part(tmp_path, "2026-10-01.b0001", [doc("src", "x", "X", "old"), doc("src", "y", "Y")])
    b2 = write_part(tmp_path, "2026-10-01.b0002", [doc("src", "x", "X", "new")])
    reader = RefineryReader(generation_dir(tmp_path, "src", compose_set(tmp_path, "src", [b2, b1])))
    assert len(reader) == 2
    got = reader.get(make_doc_id("src", "x"))
    assert got is not None
    assert got.text == "new"
    assert sorted(r.text for r in reader.records()) == ["Y text", "new"], "the shadowed row is skipped"


def test_members_must_be_sealed(tmp_path: Path) -> None:
    unsealed = part_dir(tmp_path, "src", "2026-10-01.ak")
    unsealed.mkdir(parents=True)
    with pytest.raises(RefineryError, match="not sealed"):
        compose_set(tmp_path, "src", [unsealed])
    with pytest.raises(RefineryError, match="at least one"):
        compose_set(tmp_path, "src", [])
