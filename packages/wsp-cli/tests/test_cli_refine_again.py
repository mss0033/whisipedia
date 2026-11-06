"""`wsp index build --source X --refine-again`: an adapter that learned to read more of the same files
(LibreTexts pages were app stubs, 2026-10-16 field exam) refines the current generation once more."""

from __future__ import annotations

from collections.abc import Callable, Generator, Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import patch

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


def test_spell_is_due_against_what_its_vocabulary_was_built_from(tmp_path: Path) -> None:
    """2026-11-06 review: the nightly build ran spell only when its own run changed documents, so a
    spell run that failed the night they changed was never tried again on the quiet nights after."""
    from wsp_index.spelling import SPELL_FILE, SPELL_INPUTS

    ix = indexer(tmp_path, Adapter())
    try:
        ix.build([SID])
        assert ix.spell_due() == (True, "no vocabulary yet")
        with (
            patch("wsp_cli.indexer.build_vocabulary", side_effect=OSError("the share went away")),
            pytest.raises(OSError, match="share went away"),
        ):
            ix.spell()
        assert not (ix.index.path / SPELL_INPUTS).exists()
        assert ix.spell_if_due()  # the failed run is tried again (nothing changed in between)
        assert (ix.index.path / SPELL_FILE).is_file()
        assert ix.spell_due() == (False, "built from the current generations")
        assert not ix.spell_if_due()
        # a vocabulary whose build stopped after it was written, before its inputs were: built again
        (ix.index.path / SPELL_INPUTS).unlink()
        assert ix.spell_due()[0]
        ix.spell()
        # the generation refined again under the same id (a new seal): due
        ix.refine_again(SID)
        assert ix.spell_due() == (True, "the indexed generations changed since it was built")
    finally:
        ix.close()


def test_the_nightly_build_finishes_an_interrupted_refine_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """2026-10-26 review #8: a refine-again killed after its new copy was in place left the index on the
    old copy; the nightly build saw the generation 'already indexed' and the new documents were never
    found. #9: the live copy is no longer moved aside before the new one is ready."""
    adapter = Adapter()
    ix = indexer(tmp_path, adapter)
    try:
        ix.build([SID])
        adapter.full = True
        real = ix.index_generation
        calls: list[int] = []

        def dies_once(*args: Any, **kw: Any) -> Any:
            calls.append(1)
            if len(calls) == 1:
                raise KeyboardInterrupt  # the unit was killed
            return real(*args, **kw)

        monkeypatch.setattr(ix, "index_generation", dies_once)
        with pytest.raises(KeyboardInterrupt):
            ix.refine_again(SID)
        source_dir = tmp_path / "index" / "refinery" / SID
        assert is_sealed(source_dir / "2026-01")  # the new copy is live, never missing
        assert (source_dir / REPLACED_DIR / PENDING_FILE).is_file()
        (report,) = ix.build([SID])  # the nightly build
        assert report.upserted == 1
        assert [h.doc_id for h in ix.index.search("moon")] == [make_doc_id(SID, "tides")]
        assert not (source_dir / REPLACED_DIR / PENDING_FILE).exists()
    finally:
        ix.close()


def _wire(monkeypatch: pytest.MonkeyPatch, make: Callable[[], Indexer]) -> None:
    """``wsp index build`` (cmd_index_build) on the indexer ``make`` returns: no config, no locks."""
    import contextlib

    from wsp_cli import ops

    @contextlib.contextmanager
    def opened(*_: object) -> Generator[Indexer]:
        ix = make()
        try:
            yield ix
        finally:
            ix.close()

    def context(_args: object) -> tuple[None, dict[str, Any], tuple[SourceSpec, ...]]:
        return None, {}, (spec(),)

    def unlocked(_cfg: object) -> contextlib.nullcontext[None]:
        return contextlib.nullcontext()

    monkeypatch.setattr(ops, "_context", context)
    monkeypatch.setattr(ops, "_index_writer", unlocked)
    monkeypatch.setattr(ops, "open_indexer", opened)


def _nightly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    adapter: Adapter,
    before: Callable[[Indexer], object] | None = None,
    **flags: Any,
) -> tuple[list[str], list[str], str]:
    """Run ``wsp index build`` on the fixture's indexer; returns the rank and suggest rebuilds it made
    (not counting the empty trie a new index opens with) and its output."""
    import argparse
    import io

    from wsp_cli import ops
    from wsp_index.lexical import TantivyIndex as Lexical

    events: list[str] = []
    out = io.StringIO()
    real_rank, real_rebuild = Indexer.rank, Lexical._rebuild_suggest  # pyright: ignore[reportPrivateUsage]

    def rank(self: Indexer, *a: Any, **k: Any) -> Any:
        events.append("rank")
        return real_rank(self, *a, **k)

    def rebuild(self: Lexical) -> None:
        events.append("suggest")
        real_rebuild(self)

    def make() -> Indexer:
        ix = indexer(tmp_path, adapter)
        ix.out = out  # the run log's progress lines
        assert ix.index is not None
        events.clear()
        if before is not None:
            before(ix)
        return ix

    monkeypatch.setattr(Indexer, "rank", rank)
    monkeypatch.setattr(Lexical, "_rebuild_suggest", rebuild)
    _wire(monkeypatch, make)
    args = argparse.Namespace(**{"source": None, "no_spell": True, **flags})
    assert ops.cmd_index_build(args, out) == 0
    monkeypatch.undo()
    return [e for e in events if e == "rank"], [e for e in events if e == "suggest"], out.getvalue()


def test_the_nightly_build_ranks_only_when_the_link_graph_can_have_changed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """2026-11-05 deep dive: PageRank ran 4-12 h a night whenever anything was indexed, and the
    suggest trie was rebuilt twice on those nights (in finish(), then for the rank's priors)."""
    adapter = Adapter()
    ranks, tries, said = _nightly(tmp_path, monkeypatch, adapter)
    assert ranks == ["rank"]  # no rank yet
    assert tries == ["suggest"]  # once, for the rank's priors: not also before it
    assert "rank: due (no rank yet)" in said
    # The source is refined again with more text but links nowhere: the kept rank stands.
    adapter.full = True
    ranks, _, said = _nightly(tmp_path, monkeypatch, adapter, refine_again=True, source=SID)
    assert ranks == []
    assert "rank: kept (only link-free sources changed (books)" in said
    ranks, _, said = _nightly(tmp_path, monkeypatch, adapter)
    assert ranks == []
    assert "rank: kept (only link-free sources changed (books)" in said

    # A kept rank, or --no-rank: finish() still folds the trie when it is due.
    def stale(ix: Indexer) -> None:
        ix.index.mark_suggest_stale()

    assert _nightly(tmp_path, monkeypatch, adapter, stale)[:2] == ([], ["suggest"])
    assert _nightly(tmp_path, monkeypatch, adapter, stale, no_rank=True)[:2] == ([], ["suggest"])


def test_a_failed_rank_still_folds_the_suggest_trie(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Without the rebuild in finish() before it, a rank that fails must not leave what was indexed out
    of the trie: finish() runs, then the failure is raised."""
    import argparse
    import io

    from wsp_cli import ops

    finished: list[bool] = []

    def make() -> Indexer:
        ix = indexer(tmp_path, Adapter())
        real_finish = ix.finish

        def refused(*_a: object, **_k: object) -> None:
            raise BlockingIOError(11, "refused")

        def finish(**k: Any) -> None:
            finished.append(True)
            real_finish(**k)

        ix.rank = refused  # type: ignore[method-assign]
        ix.finish = finish  # type: ignore[method-assign]
        return ix

    _wire(monkeypatch, make)
    with pytest.raises(BlockingIOError):
        ops.cmd_index_build(argparse.Namespace(source=None, no_spell=True), io.StringIO())
    assert finished == [True]
