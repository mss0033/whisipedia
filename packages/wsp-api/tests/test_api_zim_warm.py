"""Archives are opened ahead of time, largest first, one at a time, and kept open."""

from __future__ import annotations

import threading
import time
from pathlib import Path

from wsp_api.zim import ZimAssetStore
from wsp_api.zimread import EntryData, Want
from wsp_core.model import Generation, GenerationStatus


class _Reader:
    def __init__(self) -> None:
        self.warmed: list[str] = []
        self.active = 0
        self.max_active = 0
        self.lock = threading.Lock()

    def read(self, file: str, path: str, want: Want) -> EntryData | None:
        return None

    def book_name(self, file: str) -> str:
        return Path(file).stem

    def warm(self, file: str) -> None:
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        time.sleep(0.01)
        with self.lock:
            self.active -= 1
            self.warmed.append(Path(file).name)
        if file.endswith("bad.zim"):
            raise OSError("unreadable")

    def close(self) -> None: ...


class _Vault:
    def __init__(self, root: Path, names: list[str]) -> None:
        self.root = root
        self.names = names

    def generations(self, source_id: str) -> list[Generation]:
        return [Generation(source_id=source_id, generation_id="g1", status=GenerationStatus.SEALED)]

    def generation_path(self, source_id: str, generation_id: str) -> Path:
        return self.root


def test_every_archive_of_a_generation_is_opened_largest_first(tmp_path: Path) -> None:
    up = tmp_path / "upstream"
    up.mkdir()
    for name, size in (("small.zim", 10), ("big.zim", 1000), ("bad.zim", 500), ("mid.zim", 100)):
        (up / name).write_bytes(b"x" * size)
    reader = _Reader()
    store = ZimAssetStore(_Vault(tmp_path, []), reader)  # pyright: ignore[reportArgumentType]
    store.warm_soon("se", "g1")
    store.warm_soon("se", "g1")  # once per generation
    deadline = time.monotonic() + 5
    while len(reader.warmed) < 4 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert reader.warmed == ["big.zim", "bad.zim", "mid.zim", "small.zim"]  # a failure does not stop it
    assert reader.max_active == 1
