"""``wsp_core.atomicio``: a small state file is replaced whole or left as it was."""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pytest

from wsp_core import atomicio
from wsp_core.atomicio import write_text_atomic


def names(directory: Path) -> list[str]:
    return sorted(p.name for p in directory.iterdir())


def test_write_text_atomic_replaces_and_leaves_no_partial(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    write_text_atomic(path, "one")
    write_text_atomic(path, "two")
    assert path.read_text() == "two"
    assert names(tmp_path) == ["state.json"]


def test_failed_write_keeps_the_old_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "state.json"
    write_text_atomic(path, "old")

    def refuse(src: object, dst: object) -> None:
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(os, "replace", refuse)
    with pytest.raises(OSError, match="Input/output error"):
        write_text_atomic(path, "new")
    assert path.read_text() == "old"
    assert names(tmp_path) == ["state.json"], "the temporary file is removed"


def test_a_failed_fsync_fails_the_write_and_keeps_the_old_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "state.json"
    write_text_atomic(path, "old")

    def refuse(fd: int) -> None:
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(os, "fsync", refuse)
    with pytest.raises(OSError, match="Input/output error"):
        write_text_atomic(path, "new")
    assert path.read_text() == "old"
    assert names(tmp_path) == ["state.json"]


def test_two_threads_writing_one_path_each_write_a_whole_file(tmp_path: Path) -> None:
    """The temporary name is unique per call, not per process: with ``<name>.<pid>.partial`` two threads
    of one daemon wrote into one temporary file, or the second rename found it gone."""
    path = tmp_path / "state.json"
    texts = [str(i) * 4096 for i in range(8)]
    errors: list[BaseException] = []
    torn: list[str] = []
    start = threading.Barrier(len(texts) + 1)
    done = threading.Event()

    def writer(text: str) -> None:
        start.wait()
        try:
            for _ in range(40):
                write_text_atomic(path, text)
        except BaseException as exc:
            errors.append(exc)

    def reader() -> None:
        start.wait()
        while not done.is_set():
            if path.exists() and (seen := path.read_text()) not in texts:
                torn.append(seen[:40])

    threads = [threading.Thread(target=writer, args=(t,)) for t in texts]
    watch = threading.Thread(target=reader)
    for t in (*threads, watch):
        t.start()
    for t in threads:
        t.join()
    done.set()
    watch.join()
    assert errors == []
    assert torn == []
    assert path.read_text() in texts
    assert names(tmp_path) == ["state.json"]


def test_a_killed_writers_partial_is_removed_after_a_day(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    old = time.time() - atomicio.STALE_PARTIAL_S - 60
    stale = tmp_path / "state.json.wspatomic-k1ll3d_x.partial"
    recent = tmp_path / "state.json.wspatomic-runn1ng_.partial"  # may belong to a writer that is running
    download = tmp_path / "state.json.partial"  # another writer's name, not ours
    archive = tmp_path / "state.json.zip.partial"  # a download of state.json.zip
    other = tmp_path / "state.json.12345.partial"  # someone else's pid-named temporary file
    sibling = tmp_path / "state.json.bak.wspatomic-k1ll3d_x.partial"  # belongs to state.json.bak
    for leftover in (stale, recent, download, archive, other, sibling):
        leftover.write_text("{")
    for leftover in (stale, download, archive, other, sibling):
        os.utime(leftover, (old, old))
    write_text_atomic(path, "{}")
    kept = [recent.name, download.name, archive.name, other.name, sibling.name]
    assert names(tmp_path) == sorted(["state.json", *kept])


def test_the_file_is_readable_like_one_written_plainly(tmp_path: Path) -> None:
    """mkstemp creates 0600; a state file other users read must not become unreadable to them."""
    plain = tmp_path / "plain.json"
    plain.write_text("{}")
    path = tmp_path / "state.json"
    write_text_atomic(path, "{}")
    assert path.stat().st_mode & 0o777 == plain.stat().st_mode & 0o777
    path.chmod(0o640)
    write_text_atomic(path, "[]")
    assert path.stat().st_mode & 0o777 == 0o640, "an existing file keeps its mode"
