"""Id-map sidecars kept off the NAS (ADR-0011): a local map root, and no memory-mapping over SMB."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pyarrow as pa
import pyarrow.parquet as pq
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
    current_generation,
    ensure_id_map,
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


def test_ensure_id_map_writes_the_sidecar_once_for_the_index_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The index job maps a generation as it goes live; the API never builds a map in a request."""
    gen = _write(tmp_path / "nas")
    monkeypatch.setattr(refinery, "filesystem_type", _on_smb)
    maps = id_map_dir(tmp_path / "local", gen)
    assert ensure_id_map(gen, maps) is True
    assert _sidecars(maps) == {IDS_MAP_H, IDS_MAP_LOC}
    assert ensure_id_map(gen, maps) is False  # already there and matching
    reader = RefineryReader(gen, map_dir=maps)
    assert reader.get(DOCS[5].doc_id) is not None
    assert reader.id_index_bytes == 0


def test_the_document_store_asks_for_the_current_generation_once_in_a_while(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finding the current generation is three metadata round trips on the NAS; a search page asked
    per hit. It is remembered for a few seconds (a generation goes live once a night)."""
    from wsp_index.docstore import CURRENT_TTL

    _write(tmp_path / "nas")
    calls = {"n": 0}

    def counting(root: Path, source_id: str) -> str | None:
        calls["n"] += 1
        return current_generation(root, source_id)

    monkeypatch.setattr("wsp_index.docstore.current_generation", counting)
    clock = {"t": 1000.0}
    store = ParquetDocumentStore(tmp_path / "nas", map_root=tmp_path / "maps", monotonic=lambda: clock["t"])
    assert store.get(DOCS[1].doc_id) is not None
    assert store.texts([DOCS[2].doc_id, DOCS[3].doc_id]) != {}
    assert store.content_hashes([DOCS[4].doc_id]) != {}
    assert calls["n"] == 1
    clock["t"] += CURRENT_TTL + 1
    assert store.get(DOCS[1].doc_id) is not None
    assert calls["n"] == 2


def test_sparse_lookups_keep_the_last_decoded_id_row_groups(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without a map (older generations read for history) each lookup read and decoded a whole
    ``_ids.parquet`` row group over the NAS; the last two are kept."""
    gen = _write(tmp_path / "nas")
    monkeypatch.setattr(refinery, "filesystem_type", _on_smb)
    reads = {"n": 0}
    real = cast(Callable[..., pa.Table], pq.ParquetFile.read_row_group)  # pyright: ignore[reportUnknownMemberType]

    def counting(self: pq.ParquetFile, i: int, *args: Any, **kw: Any) -> pa.Table:
        reads["n"] += 1
        return real(self, i, *args, **kw)

    monkeypatch.setattr(pq.ParquetFile, "read_row_group", counting)
    reader = RefineryReader(gen, map_ids=False)
    before = reads["n"]
    for d in DOCS[:10]:
        assert reader.lookup(d.doc_id, ("content_hash",)) is not None
    id_reads = reads["n"] - before
    assert id_reads <= 10  # the part's row groups, read once each: not one id row group per lookup
    assert reader.id_rows_bytes > 0


def test_a_page_of_text_and_hash_lookups_runs_side_by_side(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fifty passages one by one over the NAS were 2-4 s of a query; the store reads a page's lookups
    with a bounded pool (:data:`wsp_index.docstore.LOOKUP_WORKERS`)."""
    import threading
    import time

    from wsp_index.docstore import LOOKUP_WORKERS

    _write(tmp_path / "nas")
    real = RefineryReader.lookup
    state = {"now": 0, "peak": 0}
    gate = threading.Lock()

    def slow(self: RefineryReader, doc_id: str, columns: Any = None) -> Any:
        with gate:
            state["now"] += 1
            state["peak"] = max(state["peak"], state["now"])
        try:
            time.sleep(0.02)
            return real(self, doc_id, columns)
        finally:
            with gate:
                state["now"] -= 1

    monkeypatch.setattr(RefineryReader, "lookup", slow)
    store = ParquetDocumentStore(tmp_path / "nas", map_root=tmp_path / "maps")
    ids = [d.doc_id for d in DOCS]
    texts = store.texts([*ids, ids[0], "not-a-doc-id"])  # repeats and junk asked once or not at all
    assert set(texts) == set(ids)
    assert 1 < state["peak"] <= LOOKUP_WORKERS
    hashes = store.content_hashes(ids)
    assert set(hashes) == set(ids)
    assert store.texts([ids[0]]) == {ids[0]: texts[ids[0]]}  # one lookup: no pool


def test_concurrent_cold_lookups_build_one_reader(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A cold page of hits asks from several threads at once: one reader is built, not one each."""
    import threading
    import time

    from wsp_index import docstore

    _write(tmp_path / "nas")
    store = ParquetDocumentStore(tmp_path / "nas", map_root=tmp_path / "local")
    built: list[int] = []
    real = refinery.RefineryReader

    def slow_reader(*args: Any, **kwargs: Any) -> refinery.RefineryReader:
        built.append(1)
        time.sleep(0.05)  # long enough for every thread to miss the cache together
        return real(*args, **kwargs)

    monkeypatch.setattr(docstore, "RefineryReader", slow_reader)
    start = threading.Barrier(8)
    got: list[object] = []

    def ask() -> None:
        start.wait()
        got.append(store.reader("src", "g1", current=True))

    threads = [threading.Thread(target=ask) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(built) == 1
    assert len({id(r) for r in got}) == 1
    assert got[0] is not None
    assert not store._building  # pyright: ignore[reportPrivateUsage]
