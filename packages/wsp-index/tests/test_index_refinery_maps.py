"""Id-map sidecars kept off the NAS (ADR-0011): a local map root, and no memory-mapping over SMB."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from wsp_index import refinery
from wsp_index.docstore import ParquetDocumentStore
from wsp_index.refinery import (
    IDS_MAP_H,
    IDS_MAP_LOC,
    SUCCESS_FILE,
    RefineryReader,
    RefineryWriter,
    configured_root,
    generation_dir,
    id_map_dir,
    part_dir,
)
from wsp_index_helpers import doc

DOCS = [doc("src", f"d{i:03d}", f"Title {i}", f"text number {i}") for i in range(40)]


def _write(root: Path, source: str = "src", gen: str = "g1") -> Path:
    with RefineryWriter(root, source, gen, row_group_rows=8) as w:
        w.write([doc(source, d.doc_id.split("/", 1)[1], d.title, d.text) for d in DOCS])
    return generation_dir(root, source, gen)


def _on_smb(path: Path, mounts_file: Path | None = None) -> str:
    return "cifs"


def _sidecars(directory: Path) -> set[str]:
    return {p.name for p in directory.iterdir() if p.name in (IDS_MAP_H, IDS_MAP_LOC)}


def test_sidecar_goes_to_the_map_dir_not_the_generation(tmp_path: Path) -> None:
    gen = _write(tmp_path / "nas")
    maps = id_map_dir(tmp_path / "local", gen)
    reader = RefineryReader(gen, map_dir=maps)
    got = reader.get(DOCS[17].doc_id)
    assert got is not None
    assert got.title == "Title 17"
    assert _sidecars(maps) == {IDS_MAP_H, IDS_MAP_LOC}
    assert _sidecars(gen) == set()
    assert reader.id_index_bytes == 0  # memory-mapped: page cache, not anonymous memory
    # A second reader uses the existing map.
    assert RefineryReader(gen, map_dir=maps).get(DOCS[3].doc_id) is not None


def test_on_a_network_mount_without_a_map_dir_nothing_is_mapped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gen = _write(tmp_path / "nas")
    monkeypatch.setattr(refinery, "filesystem_type", _on_smb)
    reader = RefineryReader(gen)
    for d in (DOCS[0], DOCS[21], DOCS[39]):
        got = reader.get(d.doc_id)
        assert got is not None
        assert got.text == d.text
    assert reader.get("wsp:src/missing") is None
    assert _sidecars(gen) == set()


def test_on_a_network_mount_an_old_sidecar_there_is_not_memory_mapped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gen = _write(tmp_path / "nas")
    assert RefineryReader(gen).get(DOCS[5].doc_id) is not None  # local: writes a sidecar in gen
    assert _sidecars(gen) == {IDS_MAP_H, IDS_MAP_LOC}
    monkeypatch.setattr(refinery, "filesystem_type", _on_smb)
    reader = RefineryReader(gen)
    assert reader.get(DOCS[6].doc_id) is not None
    assert reader._ids is None  # pyright: ignore[reportPrivateUsage]  # row-group lookups, no mmap


def test_map_dir_changes_when_the_generation_is_refined_again(tmp_path: Path) -> None:
    gen = _write(tmp_path / "nas")
    first = id_map_dir(tmp_path / "local", gen)
    assert first == id_map_dir(tmp_path / "local", gen)  # stable
    seal = json.loads((gen / SUCCESS_FILE).read_text(encoding="utf-8"))
    (gen / SUCCESS_FILE).write_text(json.dumps({**seal, "refined": "again"}), encoding="utf-8")
    second = id_map_dir(tmp_path / "local", gen)
    assert second != first
    assert second.parent == tmp_path / "local" / "src"
    assert second.name.startswith("g1-")


def test_partition_generations_are_filed_under_their_source(tmp_path: Path) -> None:
    root = tmp_path / "nas"
    pdir = part_dir(root, "big", "p1")
    with RefineryWriter(root, "big", "p1", directory=pdir) as w:
        w.write([doc("big", "a", "A", "alpha")])
    assert (pdir / SUCCESS_FILE).is_file()
    assert id_map_dir(tmp_path / "local", pdir).parent == tmp_path / "local" / "big"


def test_document_store_files_maps_under_its_map_root(tmp_path: Path) -> None:
    _write(tmp_path / "nas")
    store = ParquetDocumentStore(tmp_path / "nas", map_root=tmp_path / "local")
    got = store.get(DOCS[9].doc_id)
    assert got is not None
    assert got.title == "Title 9"
    maps = list((tmp_path / "local" / "src").iterdir())
    assert len(maps) == 1
    assert _sidecars(maps[0]) == {IDS_MAP_H, IDS_MAP_LOC}
    assert _sidecars(generation_dir(tmp_path / "nas", "src", "g1")) == set()


def test_configured_root(tmp_path: Path) -> None:
    assert configured_root(tmp_path, None) == tmp_path / "refinery"
    assert configured_root(tmp_path, "  ") == tmp_path / "refinery"
    assert configured_root(tmp_path, "/mnt/z/whisipedia/index/refinery") == Path(
        "/mnt/z/whisipedia/index/refinery"
    )
