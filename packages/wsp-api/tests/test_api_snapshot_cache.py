"""A restarted server answers from the last saved snapshot while the first fresh build runs."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from wsp_api.snapshot import LibrarySnapshot, SourceState, load_snapshot, save_snapshot
from wsp_api.testing.fakes import SAMPLE_SOURCES
from wsp_core.model import Generation, GenerationStatus, SourceSpec


def _spec(sid: str) -> SourceSpec:
    return replace(SAMPLE_SOURCES[0], id=sid, name=sid)


def test_a_saved_snapshot_loads_for_the_same_sources(tmp_path: Path) -> None:
    gen = Generation("a", "g1", GenerationStatus.SEALED)
    snap = LibrarySnapshot(1.0, {"a": SourceState(gen, (), (gen,), {"g1": tmp_path / "a" / "g1"})}, {"a": 3})
    path = tmp_path / "cache" / "library-snapshot.pickle"
    save_snapshot(path, snap)
    loaded = load_snapshot(path, [_spec("a")])
    assert loaded is not None
    assert loaded.source("a").current == gen
    assert loaded.doc_counts == {"a": 3}
    assert load_snapshot(path, [_spec("a"), _spec("new-source")]) is None  # the registry changed
    path.write_bytes(b"not a pickle")
    assert load_snapshot(path, [_spec("a")]) is None
    assert load_snapshot(tmp_path / "missing", [_spec("a")]) is None
