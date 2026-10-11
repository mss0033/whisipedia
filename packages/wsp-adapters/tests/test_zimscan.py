"""Cluster-ordered ZIM reading equals libzim's path-ordered reading (audit 2026-10-11 C2)."""

from __future__ import annotations

import hashlib
import itertools
import struct
from pathlib import Path

import pytest
from libzim.reader import Archive
from libzim.writer import Compression, Creator, Hint, Item, StringProvider

from wsp_adapters import zimscan
from wsp_adapters._common import WorkerPool
from wsp_adapters.html_text import html_to_text
from wsp_adapters.kiwix import read_zim, zim_params
from wsp_core.model import DocRecord

Row = tuple[str, str, str, str]


class _Item(Item):
    def __init__(self, path: str, title: str, body: str, mimetype: str) -> None:
        super().__init__()
        self._path, self._title, self._body, self._mime = path, title, body, mimetype

    def get_path(self) -> str:
        return self._path

    def get_title(self) -> str:
        return self._title

    def get_mimetype(self) -> str:
        return self._mime

    def get_contentprovider(self) -> StringProvider:
        return StringProvider(self._body)

    def get_hints(self) -> dict[Hint, int]:
        return {Hint.FRONT_ARTICLE: self._mime.startswith("text/html")}


def _build(path: Path, compression: Compression) -> Path:
    """~600 articles in many small clusters, assets, empty titles, redirects (also to redirects)."""
    with (
        Creator(path)
        .config_indexing(False, "eng")
        .config_clustersize(4096)
        .config_compression(compression)
        .config_verbose(False) as creator
    ):
        creator.set_mainpath("A0")
        for i in range(600):
            body = f"<html><body><h1>Article {i}</h1><p>{f'word{i} ' * (i % 37 + 1)}</p></body></html>"
            title = "" if i % 50 == 0 else f"Article {i}"  # empty title: libzim reports the path
            creator.add_item(_Item(f"A{i}", title, body, "text/html; charset=utf-8"))
            if i % 7 == 0:
                creator.add_item(_Item(f"img/{i}.png", "", "\x89PNG" + "x" * (i * 13), "image/png"))
        creator.add_item(_Item(" padded", "Padded", "<p>skipped: not a stable id</p>", "text/html"))
        for i in range(0, 600, 3):
            creator.add_redirection(f"R{i}", f"Redirect {i}", f"A{i}", {Hint.FRONT_ARTICLE: True})
        creator.add_redirection("RR", "Chain", "R0", {Hint.FRONT_ARTICLE: True})
        creator.add_metadata("Name", "testbook")
        creator.add_metadata("Language", "eng")
        creator.add_metadata("Date", "2026-08-01")
    return path


def _oracle(path: Path) -> set[Row]:
    """The pre-2026-10-11 reader: libzim in path order (``_get_entry_by_id``)."""
    archive = Archive(path)
    out: set[Row] = set()
    for i in range(archive.entry_count):
        entry = archive._get_entry_by_id(i)  # pyright: ignore[reportPrivateUsage]
        if not entry.path or entry.path != entry.path.strip():
            continue
        if entry.is_redirect:
            out.add(("R", entry.path, entry.get_redirect_entry().path, ""))
            continue
        item = entry.get_item()
        if item.mimetype.startswith("text/html"):
            text = html_to_text(bytes(item.content))
            out.add(("D", entry.path, entry.title or entry.path, hashlib.sha256(text.encode()).hexdigest()))
    return out


def _new(path: Path, workdir: Path, workers: int) -> list[Row]:
    rows: list[Row] = []
    with WorkerPool(workers) as pool:
        for e in read_zim(path, zim_params(path, "t", "CC0-1.0", False), workdir=workdir, pool=pool):
            if isinstance(e, DocRecord):
                rows.append(("D", e.native_id, e.title, hashlib.sha256(e.text.encode()).hexdigest()))
            else:
                rows.append(("R", e.native_id, e.target, ""))
    return rows


@pytest.fixture(scope="module", params=[Compression.zstd, Compression.none], ids=["zstd", "none"])
def zim(request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory) -> Path:
    return _build(tmp_path_factory.mktemp("zim") / "testbook_en_all_2026-08.zim", request.param)


def test_cluster_order_equals_path_order_as_a_set(zim: Path, tmp_path: Path) -> None:
    rows = _new(zim, tmp_path, workers=1)
    assert len(rows) == len(set(rows))
    assert set(rows) == _oracle(zim)
    assert sum(r[0] == "D" for r in rows) == 600
    assert ("R", "RR", "R0", "") in rows  # a redirect to a redirect names its immediate target
    assert not any(r[1].startswith(" ") for r in rows)
    assert not list(tmp_path.iterdir())  # the scan files are removed


def test_output_is_deterministic_and_independent_of_task_sizes(
    zim: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    baseline = _new(zim, tmp_path, workers=1)
    assert _new(zim, tmp_path, workers=2) == baseline
    monkeypatch.setattr(zimscan, "WINDOW_ENTRIES", 97)  # many scan windows
    monkeypatch.setattr(zimscan, "TASK_ENTRIES", 13)  # many content tasks
    monkeypatch.setattr(zimscan, "REDIRECT_TASK", 11)
    monkeypatch.setattr(zimscan, "_SEGMENT_GAP", 64)  # many dirent segments per window
    monkeypatch.setattr(zimscan, "_TAIL", 1)  # every segment re-read with a longer tail
    small = _new(zim, tmp_path, workers=1)
    assert set(small) == set(baseline)
    docs = [r for r in small if r[0] == "D"]
    assert docs == [r for r in baseline if r[0] == "D"]  # cluster order does not depend on tasking


def test_content_tasks_are_bounded_runs_of_clusters(zim: Path, tmp_path: Path) -> None:
    scan, tasks = zimscan.scan_tasks(zim, tmp_path / "scan", window=101)
    for t in tasks:
        zimscan.scan_window(t)
    with zim.open("rb") as fh:
        offsets = zimscan.cluster_offsets(fh, scan.header)
    ctasks = zimscan.content_tasks(scan, offsets, task_bytes=2048, task_entries=40)
    assert len(ctasks) > 3
    for a, b in itertools.pairwise(ctasks):
        assert a.stop_cluster <= b.first_cluster  # disjoint, in cluster order
    seen = [blob.index for t in ctasks for blob in zimscan.read_clusters(t)]
    assert len(seen) == len(set(seen)) == 601  # every HTML entry exactly once (incl. " padded")
    zimscan.remove_scan(tmp_path / "scan")


def test_header_errors(tmp_path: Path, zim: Path) -> None:
    bad = tmp_path / "bad.zim"
    bad.write_bytes(b"\0" * 80)
    with bad.open("rb") as fh, pytest.raises(zimscan.ZimFormatError, match="magic"):
        zimscan.read_header(fh)
    old = tmp_path / "old.zim"
    data = bytearray(zim.read_bytes())
    struct.pack_into("<H", data, 6, 0)  # minor version 0: the old namespace scheme
    old.write_bytes(bytes(data))
    with old.open("rb") as fh, pytest.raises(zimscan.ZimFormatError, match="old namespace scheme"):
        zimscan.user_range(fh, zimscan.read_header(fh))
