"""ZIM-backed :class:`~wsp_api.seams.AssetStore`: original HTML and images straight from sealed ZIMs.

Paths are native ids. For a single-book source the native id is the ZIM entry path (``A/Alan_Turing``);
for a bundle of books it is prefixed with the book name (``ifixit_en_all/A/Foo``), exactly as the
``kiwix-opds`` adapter assigns them. Archives are opened read-only and kept in a small LRU.
"""

from __future__ import annotations

import re
import threading
from collections import OrderedDict
from pathlib import Path

from libzim.reader import Archive, Entry

from wsp_api.seams import Asset
from wsp_core.ports import Vault

_ZIM_DATE_RE = re.compile(r"_(\d{4}-\d{2})\.zim$")
_MAX_REDIRECTS = 5


def _book_name(archive: Archive, path: Path) -> str:
    """Same rule as ``wsp_adapters.kiwix.book_name`` (``Name`` metadata, else the dated file name)."""
    if "Name" in archive.metadata_keys:
        name = archive.get_metadata("Name").decode("utf-8", errors="replace").strip()
        if name:
            return name
    return _ZIM_DATE_RE.sub("", path.name).removesuffix(".zim")


class ZimAssetStore:
    def __init__(self, vault: Vault, *, max_open: int = 16) -> None:
        self._vault = vault
        self._max_open = max_open
        self._open: OrderedDict[tuple[str, str], dict[str, Archive]] = OrderedDict()
        self._lock = threading.Lock()

    def _zim_files(self, source_id: str, generation_id: str) -> list[Path]:
        gen = next((g for g in self._vault.generations(source_id) if g.generation_id == generation_id), None)
        if gen is None:  # also rejects path tricks in ids: only known generations are opened
            return []
        root = self._vault.generation_path(source_id, generation_id)
        files = [root / str(a.relpath) for a in gen.artifacts if a.relpath.suffix == ".zim"]
        if not files and (root / "upstream").is_dir():
            files = sorted((root / "upstream").glob("*.zim"))
        return [f for f in files if f.is_file()]

    def _archives(self, source_id: str, generation_id: str) -> dict[str, Archive]:
        key = (source_id, generation_id)
        with self._lock:
            if key in self._open:
                self._open.move_to_end(key)
                return self._open[key]
        files = self._zim_files(source_id, generation_id)
        archives: dict[str, Archive] = {}
        if len(files) == 1:
            archives[""] = Archive(files[0])
        else:
            for f in files:
                archive = Archive(f)
                archives[_book_name(archive, f)] = archive
        with self._lock:
            self._open[key] = archives
            while len(self._open) > self._max_open:
                self._open.popitem(last=False)
        return archives

    def _entry(self, source_id: str, generation_id: str, path: str) -> Entry | None:
        archives = self._archives(source_id, generation_id)
        if "" in archives:
            archive, entry_path = archives[""], path
        else:
            book, _, entry_path = path.partition("/")
            archive = archives.get(book)
            if archive is None:
                return None
        if not entry_path or not archive.has_entry_by_path(entry_path):
            return None
        entry = archive.get_entry_by_path(entry_path)
        for _ in range(_MAX_REDIRECTS):
            if not entry.is_redirect:
                return entry
            entry = entry.get_redirect_entry()
        return None

    def html(self, source_id: str, generation_id: str, html_ref: str) -> str | None:
        if not html_ref.startswith("zim:"):
            return None
        entry = self._entry(source_id, generation_id, html_ref.removeprefix("zim:"))
        if entry is None:
            return None
        item = entry.get_item()
        if not item.mimetype.startswith("text/html"):
            return None
        return bytes(item.content).decode("utf-8", errors="replace")

    def asset(self, source_id: str, generation_id: str, path: str) -> Asset | None:
        entry = self._entry(source_id, generation_id, path)
        if entry is None:
            return None
        item = entry.get_item()
        return Asset(content=bytes(item.content), mimetype=item.mimetype)
