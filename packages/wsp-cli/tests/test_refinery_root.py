"""``[index] refinery_root``: the Refinery elsewhere (the NAS, ADR-0011), its id maps kept local."""

from __future__ import annotations

from pathlib import Path

from wsp_cli import bootstrap
from wsp_core.ids import make_doc_id
from wsp_core.model import DocRecord
from wsp_index.refinery import IDS_MAP_H, RefineryWriter, generation_dir


def test_default_layout_is_unchanged(tmp_path: Path) -> None:
    p = bootstrap.paths({"index": {"root": str(tmp_path / "index")}})
    assert p.refinery == tmp_path / "index" / "refinery"
    assert p.refinery_maps is None


def test_refinery_root_moves_the_refinery_and_keeps_maps_local(tmp_path: Path) -> None:
    cfg = {"index": {"root": str(tmp_path / "index"), "refinery_root": str(tmp_path / "nas" / "refinery")}}
    p = bootstrap.paths(cfg)
    assert p.refinery == tmp_path / "nas" / "refinery"
    assert p.refinery_maps == tmp_path / "index" / "refinery-maps"

    a = DocRecord(make_doc_id("src", "a"), "src", "a", "Alpha", "en", "CC0", "alpha text")
    with RefineryWriter(p.refinery, "src", "g1") as w:
        w.write([a])
    store = bootstrap.open_documents(cfg)
    got = store.get(a.doc_id)
    assert got is not None
    assert got.title == "Alpha"
    assert not (generation_dir(p.refinery, "src", "g1") / IDS_MAP_H).exists()
    assert list((tmp_path / "index" / "refinery-maps" / "src").glob(f"g1-*/{IDS_MAP_H}"))
