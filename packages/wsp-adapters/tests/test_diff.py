from __future__ import annotations

import random
from pathlib import Path
from typing import Any

import pytest
from adapters_testkit import build_zim, make_ctx, make_spec

from wsp_adapters._common import WorkerPool
from wsp_adapters.diff import (
    SnapshotDiff,
    apply_unified_diff,
    increment_path,
    read_segment,
    read_snapshot,
    snapshot_generation,
    unified_diff,
    write_snapshot,
)
from wsp_adapters.entries import Entry, Redirect
from wsp_adapters.kiwix import KiwixOpdsAdapter
from wsp_core.ids import content_hash, make_doc_id
from wsp_core.model import ChangeKind, ChangeRecord, DocRecord
from wsp_core.testing.adapter_contract import FakeFetcher

SOURCE = "wikipedia-en"


def doc(native_id: str, text: str) -> DocRecord:
    return DocRecord(
        doc_id=make_doc_id(SOURCE, native_id),
        source_id=SOURCE,
        native_id=native_id,
        title=native_id.replace("_", " "),
        lang="en",
        license="CC-BY-SA-4.0",
        text=text,
        content_hash=content_hash(text),
    )


LONG = "\n".join(f"Paragraph {i} about rotors." for i in range(20))

OLD: list[Entry] = [
    doc("Same", "Unchanged text."),
    doc("Edited", LONG),
    doc("Gone", "Deleted upstream."),
    doc("Old_title", "Page that moved.\nSecond line."),
    doc("Copy_source", "Identical body moved without a redirect."),
    Redirect("Alias", "Same"),
]
NEW: list[Entry] = [
    doc("Same", "Unchanged text."),
    doc("Edited", LONG.replace("Paragraph 7", "Paragraph seven") + "\nA new closing line."),
    doc("New_title", "Page that moved.\nSecond line, edited."),
    Redirect("Old_title", "New_title"),
    doc("Copy_target", "Identical body moved without a redirect."),
    doc("Fresh", "Brand new article."),
    Redirect("Alias", "Same"),
]


@pytest.fixture
def snapshots(tmp_path: Path) -> tuple[Path, Path]:
    old, new = tmp_path / "old.snap.zst", tmp_path / "new.snap.zst"
    write_snapshot(OLD, old)
    write_snapshot(reversed(NEW), new)  # input order must not matter
    return old, new


def test_snapshot_is_sorted_and_complete(snapshots: tuple[Path, Path]) -> None:
    rows = list(read_snapshot(snapshots[1]))
    ids = [r.native_id for r in rows]
    assert ids == sorted(ids)
    assert len(ids) == len(NEW)
    moved = next(r for r in rows if r.native_id == "Old_title")
    assert (moved.kind, moved.target) == ("redirect", "New_title")


def test_external_sort_with_tiny_runs_matches(tmp_path: Path, snapshots: tuple[Path, Path]) -> None:
    out = tmp_path / "tiny.snap.zst"
    stats = write_snapshot(reversed(NEW), out, run_bytes=1, fan_in=2)
    assert stats.runs == len(NEW)
    assert list(read_snapshot(out)) == list(read_snapshot(snapshots[1]))


def test_duplicates_are_dropped(tmp_path: Path) -> None:
    out = tmp_path / "d.snap.zst"
    stats = write_snapshot([doc("A", "x"), doc("A", "y")], out)
    assert stats.duplicates == 1
    assert [r.native_id for r in read_snapshot(out)] == ["A"]


def test_change_records(snapshots: tuple[Path, Path], tmp_path: Path) -> None:
    diff = SnapshotDiff(*snapshots, source_id=SOURCE, generation_id="2026-10-01", workdir=tmp_path)
    changes: dict[tuple[ChangeKind, str], ChangeRecord] = {(c.kind, str(c.doc_id)): c for c in diff}
    wsp = f"wsp:{SOURCE}/"
    assert set(changes) == {
        (ChangeKind.MODIFIED, wsp + "Edited"),
        (ChangeKind.RENAMED, wsp + "New_title"),
        (ChangeKind.RENAMED, wsp + "Copy_target"),
        (ChangeKind.REMOVED, wsp + "Gone"),
        (ChangeKind.ADDED, wsp + "Fresh"),
    }
    moved = changes[(ChangeKind.RENAMED, wsp + "New_title")]
    assert moved.renamed_from == wsp + "Old_title"
    assert moved.old_hash != moved.new_hash
    copied = changes[(ChangeKind.RENAMED, wsp + "Copy_target")]
    assert copied.renamed_from == wsp + "Copy_source"
    assert copied.old_hash == copied.new_hash
    assert changes[(ChangeKind.REMOVED, wsp + "Gone")].new_hash is None
    assert all(c.generation_id == "2026-10-01" for c in changes.values())
    assert (diff.stats.modified, diff.stats.renamed, diff.stats.unchanged) == (1, 2, 1)


def test_segment_reconstructs_new_generation(snapshots: tuple[Path, Path], tmp_path: Path) -> None:
    segment = increment_path(tmp_path / "wikipedia-en", "2026-10-01")
    assert segment.name == "2026-10.jsonl.zst"
    diff = SnapshotDiff(
        *snapshots,
        source_id=SOURCE,
        generation_id="2026-10-01",
        from_generation="2026-09-01",
        segment=segment,
    )
    list(diff)
    assert segment.is_file()
    assert diff.stats.segment_bytes == segment.stat().st_size
    header, *ops = read_segment(segment)
    assert header["format"] == "wsp-increment/1"
    assert header["from_generation"] == "2026-09-01"

    texts = {r.native_id: r.text for r in read_snapshot(snapshots[0]) if r.kind == "doc"}
    for op in ops:
        if op["op"] == "modified":
            texts[op["id"]] = apply_unified_diff(texts[op["id"]] or "", op["diff"])
        elif op["op"] == "renamed":
            old = texts.pop(op["from"]) or ""
            texts[op["id"]] = apply_unified_diff(old, op["diff"]) if "diff" in op else old
        elif op["op"] == "removed":
            del texts[op["id"]]
        else:
            texts[op["id"]] = op["text"]
    expected = {r.native_id: r.text for r in read_snapshot(snapshots[1]) if r.kind == "doc"}
    assert texts == expected
    assert all(content_hash(t or "") == content_hash(expected[k] or "") for k, t in texts.items())


def test_parallel_diff_matches(snapshots: tuple[Path, Path], tmp_path: Path) -> None:
    def run(workers: int) -> list[object]:
        seg = tmp_path / f"seg{workers}.jsonl.zst"
        changes = list(
            SnapshotDiff(*snapshots, source_id=SOURCE, generation_id="g", segment=seg, workers=workers)
        )
        return [changes, list(read_segment(seg))[1:]]

    assert run(2) == run(1)


def test_one_pool_for_every_flush(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Audit 2026-10-11 M9: the diff used to start a spawn pool per flush of modified pairs."""
    import wsp_adapters.diff as diff_module

    pools: list[object] = []
    maps: list[int] = []

    class CountingPool(WorkerPool):
        def __init__(self, workers: int) -> None:
            super().__init__(workers)
            pools.append(self)

        def map(self, fn: Any, items: Any, *, window: int | None = None) -> Any:
            maps.append(1)
            return super().map(fn, items, window=window)

    old, new = tmp_path / "old.snap.zst", tmp_path / "new.snap.zst"
    write_snapshot([doc(f"P{i}", f"old text {i}") for i in range(10)], old)
    write_snapshot([doc(f"P{i}", f"new text {i}") for i in range(10)], new)
    monkeypatch.setattr(diff_module, "WorkerPool", CountingPool)
    monkeypatch.setattr(diff_module, "_DIFF_BATCH", 1)  # a flush per two modified pairs (2 workers)
    seg = tmp_path / "seg.jsonl.zst"
    changes = list(SnapshotDiff(old, new, source_id=SOURCE, generation_id="g", segment=seg, workers=2))
    assert len(changes) == 10
    assert len(maps) == 5
    assert len(pools) == 1


def test_segment_needs_text(tmp_path: Path) -> None:
    old, new = tmp_path / "o", tmp_path / "n"
    write_snapshot([doc("A", "x")], old, keep_text=False)
    write_snapshot([doc("A", "y")], new, keep_text=False)
    assert [c.kind for c in SnapshotDiff(old, new, source_id=SOURCE, generation_id="g")] == [
        ChangeKind.MODIFIED
    ]
    with pytest.raises(ValueError, match="keep_text"):
        list(SnapshotDiff(old, new, source_id=SOURCE, generation_id="g", segment=tmp_path / "s.zst"))
    assert not (tmp_path / "s.zst").exists()
    assert not (tmp_path / "s.zst.partial").exists()


@pytest.mark.parametrize("seed", range(25))
def test_unified_diff_roundtrip(seed: int) -> None:
    rng = random.Random(seed)
    old = [rng.choice("abcdef") * rng.randint(0, 3) for _ in range(rng.randint(0, 30))]
    new = list(old)
    for _ in range(rng.randint(0, 8)):
        op = rng.choice(("ins", "del", "sub"))
        pos = rng.randint(0, len(new))
        if op == "ins" or not new:
            new.insert(pos, rng.choice("xyz"))
        elif op == "del":
            del new[min(pos, len(new) - 1)]
        else:
            new[min(pos, len(new) - 1)] = rng.choice("xyz")
    a, b = "\n".join(old), "\n".join(new)
    assert apply_unified_diff(a, unified_diff(a, b)) == b


def test_zim_generations_end_to_end(tmp_path: Path) -> None:
    """A page move inside a ZIM (old path left as a redirect) is reported as a rename."""
    spec = make_spec(SOURCE, {"adapter": "kiwix-opds", "selector": {"name": "t"}, "workers": 1})
    ctx = make_ctx(spec, FakeFetcher(), tmp_path)
    adapter = KiwixOpdsAdapter()
    g1 = tmp_path / "g1" / "upstream" / "t_en_all_2026-08.zim"
    g2 = tmp_path / "g2" / "upstream" / "t_en_all_2026-09.zim"
    build_zim(g1, name="t", articles=[("A", "A", "<p>Alpha</p>"), ("B", "B", "<p>Beta</p>")])
    build_zim(
        g2,
        name="t",
        articles=[("A", "A", "<p>Alpha!</p>"), ("Beta_(letter)", "Beta (letter)", "<p>Beta</p>")],
        redirects=[("B", "B", "Beta_(letter)")],
    )
    s1, s2 = tmp_path / "s1", tmp_path / "s2"
    snapshot_generation(adapter, ctx, g1.parent.parent, s1)
    snapshot_generation(adapter, ctx, g2.parent.parent, s2)
    kinds = sorted(
        (c.kind.value, c.doc_id) for c in SnapshotDiff(s1, s2, source_id=SOURCE, generation_id="g2")
    )
    assert kinds == [("modified", f"wsp:{SOURCE}/A"), ("renamed", f"wsp:{SOURCE}/Beta_(letter)")]
