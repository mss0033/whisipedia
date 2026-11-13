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


def test_a_copy_the_share_refuses_for_a_moment_is_made_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """2026-11-04 trawl: on the NAS a set copies its members' parts with no retry, so one refused copy
    left the set unsealed and the next compose copied every part again."""
    import shutil
    import time

    from wsp_core import nasretry
    from wsp_index import refinery

    def no_wait(_s: float) -> None:
        return None

    monkeypatch.setattr(time, "sleep", no_wait)
    monkeypatch.setattr(
        nasretry.retrying, "__kwdefaults__", {"sleep": no_wait, "transient": nasretry.is_transient}
    )
    ak = write_part(
        tmp_path, "2026-10-01.ak", [doc("src", "a1", "A1"), doc("src", "a2", "A2")], docs_per_file=1
    )
    ca = write_part(tmp_path, "2026-10-02.ca", [doc("src", "c1", "C1")])

    def on_nas(_path: Path) -> bool:
        return True

    real_copy = shutil.copyfile
    refused: list[str] = []

    def flaky_copy(src: Path, dst: Path) -> Path:
        if dst.name not in refused:
            refused.append(dst.name)
            real_copy(src, dst)
            with open(dst, "r+b") as fh:
                fh.truncate(10)  # torn mid-write
            raise BlockingIOError(11, "Resource temporarily unavailable")
        return real_copy(src, dst)

    monkeypatch.setattr(refinery, "_on_network_mount", on_nas)
    monkeypatch.setattr(shutil, "copyfile", flaky_copy)
    set_id = compose_set(tmp_path, "src", [ak, ca])
    assert len(refused) == 3
    reader = RefineryReader(generation_dir(tmp_path, "src", set_id))
    assert {r.native_id for r in reader.records()} == {"a1", "a2", "c1"}
    assert not list(reader.dir.glob("*.partial"))


def test_members_must_be_sealed(tmp_path: Path) -> None:
    unsealed = part_dir(tmp_path, "src", "2026-10-01.ak")
    unsealed.mkdir(parents=True)
    with pytest.raises(RefineryError, match="not sealed"):
        compose_set(tmp_path, "src", [unsealed])
    with pytest.raises(RefineryError, match="at least one"):
        compose_set(tmp_path, "src", [])
