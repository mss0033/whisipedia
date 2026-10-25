"""`wsp index build --source X --refine-again`: an adapter that learned to read more of the same files
(LibreTexts pages were app stubs, 2026-10-16 field exam) refines the current generation once more."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from wsp_cli.indexer import PENDING_FILE, REPLACED_DIR, STAGING_DIR, Indexer
from wsp_core.ids import content_hash, make_doc_id
from wsp_core.model import DocRecord, Generation, GenerationStatus, SourceSpec, SourceStatus, Tier
from wsp_index import SqliteChangeFeed, TantivyIndex
from wsp_index.refinery import is_sealed

SID = "books"
GEN = Generation(SID, "2026-01", GenerationStatus.SEALED)


class Vault:
    def __init__(self, root: Path) -> None:
        self.root = root

    def generations(self, source_id: str) -> list[Generation]:
        return [GEN] if source_id == SID else []

    def current(self, source_id: str) -> Generation | None:
        return GEN if source_id == SID else None

    def current_set(self, source_id: str) -> list[Generation]:
        return self.generations(source_id)

    def generation_path(self, source_id: str, generation_id: str) -> Path:
        return self.root / source_id / generation_id


class Adapter:
    """Reads "Tides" as its title alone until it learns to read the page (``full``)."""

    id = "fake"
    full = False

    def documents(self, ctx: Any, generation_path: Path) -> Iterator[DocRecord]:
        for native, body in (("tides", "Spring tides occur at new and full moon."), ("rocks", "Rocks.")):
            text = body if self.full or native == "rocks" else native.title()
            yield DocRecord(
                make_doc_id(SID, native),
                SID,
                native,
                native.title(),
                "en",
                "CC0-1.0",
                text,
                content_hash=content_hash(text),
            )


def spec() -> SourceSpec:
    return SourceSpec(
        id=SID,
        name="Books",
        tier=Tier.T1,
        status=SourceStatus.ACTIVE,
        license="CC0-1.0",
        legal_basis="open-license",
        domain=("books",),
        adapter="fake",
        raw={"access": {"adapter": "fake"}},
    )


def indexer(tmp_path: Path, adapter: Adapter, source: SourceSpec | None = None) -> Indexer:
    root = tmp_path / "index"
    return Indexer(
        Vault(tmp_path / "vault"),  # type: ignore[arg-type]
        [source or spec()],
        {"fake": adapter},  # type: ignore[dict-item]
        root,
        TantivyIndex(root / "lexical"),
        SqliteChangeFeed(root / "changes.sqlite"),
    )


def test_refine_again_indexes_what_the_adapter_now_reads(tmp_path: Path) -> None:
    adapter = Adapter()
    ix = indexer(tmp_path, adapter)
    try:
        ix.build([SID])
        assert ix.index.search("moon") == []
        (again,) = ix.build([SID])  # a plain build sees nothing new
        assert (again.refined, again.upserted) == ((), 0)
        adapter.full = True
        report = ix.refine_again(SID)
        assert (report.refined, report.indexed, report.upserted, report.deleted) == (
            (("2026-01", 2),),
            "2026-01",
            1,
            0,
        )
        assert [h.doc_id for h in ix.index.search("moon")] == [make_doc_id(SID, "tides")]
        source_dir = tmp_path / "index" / "refinery" / SID
        assert is_sealed(source_dir / "2026-01")
        (old,) = [p for p in (source_dir / REPLACED_DIR).iterdir() if p.is_dir()]
        assert is_sealed(old)  # kept: the operator removes it
        assert not (source_dir / REPLACED_DIR / PENDING_FILE).exists()
        assert ix.build([SID])[0].upserted == 0  # the replaced copy is not a generation
    finally:
        ix.close()


class Unread(Adapter):
    """Reads nothing until it learns to recognise the stored file (``full``): MedlinePlus, 2026-10-19."""

    def documents(self, ctx: Any, generation_path: Path) -> Iterator[DocRecord]:
        return super().documents(ctx, generation_path) if self.full else iter(())


def test_a_source_indexed_with_no_documents_is_refined_again(tmp_path: Path) -> None:
    """Indexed with none (its reader did not know the stored file), then refined again with two: the
    index-state check took the new copy for the indexed one and refused (2026-10-19)."""
    adapter = Unread()
    ix = indexer(tmp_path, adapter)
    try:
        ix.build([SID])
        assert not ix.index.has_source(SID)
        adapter.full = True
        report = ix.refine_again(SID)
        assert (report.indexed, report.upserted) == ("2026-01", 2)
        assert [h.doc_id for h in ix.index.search("rocks")] == [make_doc_id(SID, "rocks")]
    finally:
        ix.close()


def test_a_deferred_suggest_rebuild_is_left_for_a_later_run(tmp_path: Path) -> None:
    adapter = Adapter()
    ix = indexer(tmp_path, adapter)
    try:
        ix.build([SID])
        adapter.full = True
        ix.defer_suggest = True
        rebuilt: list[bool] = []
        ix.index.rebuild_suggest = lambda: rebuilt.append(True)  # type: ignore[method-assign]
        assert ix.refine_again(SID).upserted == 1
        assert rebuilt == []
        assert [h.doc_id for h in ix.index.search("moon")] == [make_doc_id(SID, "tides")]  # committed
    finally:
        ix.close()


def test_a_copy_in_use_is_staged_and_swapped_in_later(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 2026-10-16: the NAS share refused to rename Gutenberg's refined copy while the API held a file
    # of it open.
    adapter = Adapter()
    ix = indexer(tmp_path, adapter)
    source_dir = tmp_path / "index" / "refinery" / SID
    try:
        ix.build([SID])
        adapter.full = True
        real = Indexer._move_aside  # pyright: ignore[reportPrivateUsage]

        def in_use(*_a: object) -> Path:
            raise PermissionError(13, "Permission denied")

        monkeypatch.setattr(Indexer, "_move_aside", staticmethod(in_use))
        report = ix.refine_again(SID)
        assert (report.skipped or "").startswith("staged")
        assert is_sealed(source_dir / STAGING_DIR / "2026-01")
        assert ix.index.search("moon") == []  # nothing indexed yet; the old copy still serves
        assert (ix.refine_again(SID).skipped or "").startswith("staged")  # again: the staged copy is reused
        monkeypatch.setattr(Indexer, "_move_aside", staticmethod(real))
        assert (ix.refine_again(SID, swap=True).skipped or "").startswith("swapped")
        assert not (source_dir / STAGING_DIR / "2026-01").exists()
        assert (source_dir / REPLACED_DIR / PENDING_FILE).is_file()
        report = ix.refine_again(SID)  # indexes the swapped-in copy against the replaced one
        assert (report.upserted, report.deleted) == (1, 0)
        assert [h.doc_id for h in ix.index.search("moon")] == [make_doc_id(SID, "tides")]
        assert not (source_dir / REPLACED_DIR / PENDING_FILE).exists()
        assert ix.refine_again(SID, swap=True).skipped == "nothing staged to swap in"
    finally:
        ix.close()


def test_an_interrupted_refine_again_resumes_from_the_same_base(tmp_path: Path) -> None:
    adapter = Adapter()
    ix = indexer(tmp_path, adapter)
    try:
        ix.build([SID])
        adapter.full = True
        ix.index_generation = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("killed"))  # type: ignore[method-assign]
        with pytest.raises(RuntimeError, match="killed"):
            ix.refine_again(SID)
        del ix.index_generation  # the method again
        source_dir = tmp_path / "index" / "refinery" / SID
        assert (source_dir / REPLACED_DIR / PENDING_FILE).is_file()
        report = ix.refine_again(SID)  # the new copy is sealed: not replaced, not refined again
        assert report.upserted == 1
        assert len([p for p in (source_dir / REPLACED_DIR).iterdir() if p.is_dir()]) == 1
        assert [h.doc_id for h in ix.index.search("moon")] == [make_doc_id(SID, "tides")]
    finally:
        ix.close()


def test_refine_again_says_why_it_cannot(tmp_path: Path) -> None:
    ix = indexer(tmp_path, Adapter())
    try:
        assert ix.refine_again("nope").skipped == "not in the registry"
    finally:
        ix.close()


def test_reindex_writes_the_indexed_generation_again_without_change_records(tmp_path: Path) -> None:
    adapter = Adapter()
    adapter.full = True
    ix = indexer(tmp_path, adapter)
    try:
        ix.build([SID])
        before = ix.index.num_docs
        records, _ = ix.feed.since(None, limit=1000)
        report = ix.reindex(SID)
        assert (report.indexed, report.upserted, report.skipped) == ("2026-01", 2, None)
        assert ix.index.num_docs == before  # replaced, not added twice
        assert [h.doc_id for h in ix.index.search("moon")] == [make_doc_id(SID, "tides")]
        assert ix.feed.since(None, limit=1000)[0] == records  # no change records
        assert ix.reindex("nope").skipped == "not in the registry"
    finally:
        ix.close()


def test_the_spelling_vocabulary_leaves_out_catalogues(tmp_path: Path) -> None:
    # 2026-10-25: Open Library's records (book titles in 458 languages) made the vocabulary step read
    # 5.5 million row groups, and its titles outweighed the library's own text in every language.
    ix = indexer(tmp_path, Adapter())
    try:
        ix.build([SID])
        assert ix.spell() is not None
    finally:
        ix.close()
    ix = indexer(tmp_path, Adapter(), replace(spec(), domain=("catalog",)))
    try:
        assert ix.spell() is None
    finally:
        ix.close()
