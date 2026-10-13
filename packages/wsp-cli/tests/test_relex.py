"""``wsp index rebuild-lexical`` (``wsp_cli.relex``): a fresh search index from the indexed generations."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import shutil
from collections.abc import Generator
from pathlib import Path

import numpy as np
import pytest

from wsp_cli import ops
from wsp_cli.relex import CHECKPOINT, Relexer, relex
from wsp_core.ids import DocId, make_doc_id
from wsp_core.model import DocRecord
from wsp_index import TantivyConfig, TantivyIndex
from wsp_index.linkrank import RANK_FILE, fence_path, key64, write_rank_file
from wsp_index.refinery import RefineryWriter
from wsp_index.spelling import SPELL_FILE

SMALL = TantivyConfig(writer_heap_bytes=20_000_000, writer_threads=1)


def rec(source: str, native: str, title: str) -> DocRecord:
    return DocRecord(make_doc_id(source, native), source, native, title, "en", "CC0", f"{title} text")


def refine(root: Path, source: str, gen: str, docs: list[DocRecord], per_file: int = 5) -> None:
    with RefineryWriter(root, source, gen, docs_per_file=per_file) as w:
        w.write(docs)


def ids(index: TantivyIndex) -> list[str]:
    return sorted(h.doc_id for h in index.search_hits("text", limit=1000))


@pytest.fixture
def library(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    ref = tmp_path / "refinery"
    refine(ref, "alpha", "g1", [rec("alpha", f"a{i:02d}", f"Alpha {i}") for i in range(12)])
    refine(ref, "beta", "g1", [rec("beta", f"b{i:02d}", f"Beta {i}") for i in range(7)])
    return ref, {"alpha": "g1", "beta": "g1"}


def test_load_indexes_every_recorded_generation(library: tuple[Path, dict[str, str]], tmp_path: Path) -> None:
    ref, state = library
    with TantivyIndex(tmp_path / "new", SMALL) as idx:
        n = Relexer(idx, ref, lambda: state).load()
        assert n == 19
        assert idx.num_docs == 19
        mark = json.loads((tmp_path / "new" / CHECKPOINT).read_text(encoding="utf-8"))
        assert mark["sources"] == {
            "alpha": {"generation": "g1", "parts": 3},
            "beta": {"generation": "g1", "parts": 2},
        }


def test_a_stopped_load_resumes_without_adding_twice(
    library: tuple[Path, dict[str, str]], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ref, state = library
    real = TantivyIndex.commit
    calls = {"n": 0}

    def dies_after_second(self: TantivyIndex, *, rebuild_suggest: bool = True) -> None:
        calls["n"] += 1
        real(self, rebuild_suggest=rebuild_suggest)
        if calls["n"] == 2:
            raise KeyboardInterrupt  # stopped between a part's commit and its checkpoint

    with TantivyIndex(tmp_path / "new", SMALL) as idx:
        monkeypatch.setattr(TantivyIndex, "commit", dies_after_second)
        with pytest.raises(KeyboardInterrupt):
            Relexer(idx, ref, lambda: state).load()
        monkeypatch.setattr(TantivyIndex, "commit", real)
        Relexer(idx, ref, lambda: state).load()
        assert idx.num_docs == 19
        assert len(ids(idx)) == 19


@pytest.mark.parametrize(("commit_bytes", "commits"), [(1, 5), (1 << 30, 2)])
def test_load_commits_once_per_batch_of_parts(
    library: tuple[Path, dict[str, str]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    commit_bytes: int,
    commits: int,
) -> None:
    ref, state = library
    real = TantivyIndex.commit
    calls = {"n": 0}

    def counting(self: TantivyIndex, *, rebuild_suggest: bool = True) -> None:
        calls["n"] += 1
        real(self, rebuild_suggest=rebuild_suggest)

    with TantivyIndex(tmp_path / "new", SMALL) as idx:
        monkeypatch.setattr(TantivyIndex, "commit", counting)
        assert Relexer(idx, ref, lambda: state, commit_bytes=commit_bytes).load() == 19
        assert calls["n"] == commits  # 3 + 2 parts: one commit each, or one per source
        mark = json.loads((tmp_path / "new" / CHECKPOINT).read_text(encoding="utf-8"))
        assert mark["sources"]["alpha"] == {"generation": "g1", "parts": 3}


def test_a_load_stopped_mid_batch_resumes_from_the_batch_start(
    library: tuple[Path, dict[str, str]], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ref, state = library
    real = TantivyIndex.upsert
    calls = {"n": 0}

    def dies_on_third_part(self: TantivyIndex, docs: list[DocRecord], *, replace: bool = True) -> int:
        calls["n"] += 1
        if calls["n"] == 3:
            raise KeyboardInterrupt  # alpha's first two parts are written, not committed
        return real(self, docs, replace=replace)

    monkeypatch.setattr(TantivyIndex, "upsert", dies_on_third_part)
    with pytest.raises(KeyboardInterrupt), TantivyIndex(tmp_path / "new", SMALL) as idx:
        Relexer(idx, ref, lambda: state).load()  # the interrupt leaves the block: uncommitted writes dropped
    monkeypatch.setattr(TantivyIndex, "upsert", real)
    with TantivyIndex(tmp_path / "new", SMALL) as idx:  # reopened: the uncommitted parts are gone
        assert idx.num_docs == 0
        Relexer(idx, ref, lambda: state).load()
        assert idx.num_docs == 19
        assert len(ids(idx)) == 19


def test_catch_up_follows_what_the_live_index_did(
    library: tuple[Path, dict[str, str]], tmp_path: Path
) -> None:
    ref, state = library
    with TantivyIndex(tmp_path / "new", SMALL) as idx:
        relexer = Relexer(idx, ref, lambda: state)
        relexer.load()
        # Meanwhile the live index moved alpha to g2 (two dropped, one new), dropped beta, added gamma.
        refine(ref, "alpha", "g2", [rec("alpha", f"a{i:02d}", f"Alpha {i}") for i in range(2, 13)])
        refine(ref, "gamma", "g1", [rec("gamma", "c1", "Gamma one")])
        state.clear()
        state.update({"alpha": "g2", "gamma": "g1"})
        assert sorted(relexer.catch_up()) == ["alpha", "beta", "gamma"]
        expected = [make_doc_id("alpha", f"a{i:02d}") for i in range(2, 13)] + [make_doc_id("gamma", "c1")]
        assert ids(idx) == sorted(expected)
        assert relexer.catch_up() == []  # nothing left to do


def test_relex_carries_over_rank_and_spelling_and_builds_suggest(
    library: tuple[Path, dict[str, str]], tmp_path: Path
) -> None:
    ref, state = library
    live = tmp_path / "live"
    live.mkdir()
    keys = key64([make_doc_id("beta", "b03")])
    one = np.array([0.9], dtype=np.float32)
    write_rank_file(live / RANK_FILE, keys, np.zeros(1, dtype=np.int32), one, one, one)
    (live / SPELL_FILE).write_bytes(b"vocabulary")
    entered: list[str] = []

    @contextlib.contextmanager
    def lock() -> Generator[None]:
        entered.append("in")
        yield

    with TantivyIndex(tmp_path / "new", SMALL) as idx:
        report = relex(Relexer(idx, ref, lambda: state), live, lock)
        assert (report.sources, report.documents, report.caught_up) == (2, 19, ())
        assert entered == ["in"]
        for name in (RANK_FILE, fence_path(Path(RANK_FILE)).name, SPELL_FILE):
            assert (tmp_path / "new" / name).is_file()
        assert idx.suggest("beta")[0] == "Beta 3"  # the carried-over rank is the suggest prior


def _point(lexical: Path, target: Path) -> tuple[int, str]:
    out = io.StringIO()
    code = ops.cmd_index_point_tantivy(argparse.Namespace(lexical=str(lexical), target=str(target)), out)
    return code, out.getvalue()


def test_point_tantivy_switches_only_to_a_faithful_copy(tmp_path: Path) -> None:
    lexical = tmp_path / "lexical"
    with TantivyIndex(lexical, SMALL) as idx:
        idx.upsert([rec("alpha", "a1", "Alpha one")])
        idx.commit()
    copy = tmp_path / "nas" / "tantivy"
    shutil.copytree(lexical / "tantivy", copy)
    broken = tmp_path / "nas" / "broken"
    shutil.copytree(lexical / "tantivy", broken)
    victim = next(p for p in broken.iterdir() if p.suffix not in (".json", ".lock") and p.is_file())
    victim.write_bytes(victim.read_bytes()[:-1])
    code, text = _point(lexical, broken)
    assert code == 2
    assert "same files" in text
    code, _ = _point(lexical, copy)
    assert code == 0
    with TantivyIndex(lexical, SMALL) as idx:
        assert idx.tantivy_dir == copy
        assert idx.lookup(DocId(make_doc_id("alpha", "a1"))) is not None
