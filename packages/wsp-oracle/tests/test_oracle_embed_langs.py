"""Embed runs (rank-oracle #15, #18, #19, #20): language filters, per-language staleness, the graph
built once, and only changed documents' text read."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from oracle_helpers import doc

from wsp_index import HashingEmbedder, RefineryReader, RefineryWriter
from wsp_index.refinery import generation_dir
from wsp_oracle import embedbuild
from wsp_oracle.embedbuild import count_docs, embed_generation
from wsp_oracle.vectors import ChunkVectorStore

DIMS = 64


def library(root: Path, gen: str, *, de_text: str = "Wort " * 50) -> RefineryReader:
    docs = [
        replace(doc("w", "d1", "D1", de_text), lang="de"),
        replace(doc("w", "d2", "D2", de_text), lang="de"),
        doc("w", "e1", "E1", "word " * 50),
        replace(doc("w", "d3", "D3", de_text), lang="de"),
        doc("w", "e2", "E2", "word " * 50),
    ]
    with RefineryWriter(root, "w", gen, row_group_rows=2) as w:
        w.write(docs)
    return RefineryReader(generation_dir(root, "w", gen))


def test_language_filter_and_per_language_staleness(tmp_path: Path) -> None:
    root = tmp_path / "ref"
    g1 = library(root, "g1")
    s = ChunkVectorStore(tmp_path / "v", DIMS)
    emb = HashingEmbedder(DIMS)
    both = embed_generation(g1, s, emb, source_id="w", generation_id="g1")
    assert both.docs_embedded == 5
    assert len(s) == 5  # the graph was built at the end
    assert count_docs(g1, frozenset({"en"})) == 2
    g2 = library(root, "g2")
    en = embed_generation(g2, s, emb, source_id="w", generation_id="g2", langs=frozenset({"en"}))
    assert (en.docs_seen, en.total_docs, en.docs_unchanged, en.docs_removed) == (2, 2, 2, 0)
    assert s.doc_count("w") == 5  # the German vectors were not removed as stale
    assert sorted(s.stale_docs("w", "g2", ["de"])) == ["wsp:w/d1", "wsp:w/d2", "wsp:w/d3"]
    assert s.stale_docs("w", "g2", ["en"]) == []
    s.close()


def test_only_changed_documents_are_read(tmp_path: Path, monkeypatch: object) -> None:
    root = tmp_path / "ref"
    g1 = library(root, "g1")
    s = ChunkVectorStore(tmp_path / "v", DIMS)
    emb = HashingEmbedder(DIMS)
    embed_generation(g1, s, emb, source_id="w", generation_id="g1")
    g2 = library(root, "g2", de_text="Neu " * 50)
    reads: list[int] = []
    original = RefineryReader.part_rows

    def counting(self: RefineryReader, part: int, positions: object, columns: object = ()) -> object:
        reads.append(len(positions))  # type: ignore[arg-type]
        return original(self, part, positions, columns)  # type: ignore[arg-type]

    import pytest

    mp = pytest.MonkeyPatch()
    mp.setattr(RefineryReader, "part_rows", counting)
    try:
        upd = embed_generation(g2, s, emb, source_id="w", generation_id="g2")
    finally:
        mp.undo()
    assert (upd.docs_embedded, upd.docs_unchanged) == (3, 2)
    assert sum(reads) == 3  # the two unchanged English documents' text was never read
    assert not hasattr(embedbuild, "ProcessPoolExecutor")  # no fork after CUDA
    s.close()
