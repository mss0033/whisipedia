"""ZIM-backed :class:`~wsp_api.seams.AssetStore`: original HTML and images straight from sealed ZIMs.

Paths are native ids. For a single-book source the native id is the ZIM entry path (``A/Alan_Turing``);
for a bundle of books it is prefixed with the book name (``ifixit_en_all/A/Foo``), exactly as the
``kiwix-opds`` adapter assigns them. Archives are opened read-only and kept in a small LRU.
"""

from __future__ import annotations

import html
import re
import threading
from collections import OrderedDict
from contextlib import suppress
from pathlib import Path

from libzim.reader import Archive, Entry

from wsp_core.model import Asset
from wsp_core.ports import Vault

_ZIM_DATE_RE = re.compile(r"_(\d{4}-\d{2})\.zim$")
_MAX_REDIRECTS = 5
BOOKS_PAGE = "_books"  # native id of the generated book list of a bundle source


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
        self._books: dict[tuple[str, str], dict[str, Path]] = {}
        self.names: dict[str, str] = {}  # source id -> display name, for generated pages
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
            # Bundles (e.g. 181 Stack Exchange sites): open books lazily, located by file name, because
            # opening every archive over SMB takes minutes. See ``_book``.
            with self._lock:
                self._books[key] = {_ZIM_DATE_RE.sub("", f.name).removesuffix(".zim"): f for f in files}
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
            archive = archives.get(book) or self._book(source_id, generation_id, archives, book)
            if archive is None:
                return None
        if not entry_path:  # "<book>/" or "": the book's front page
            if not archive.has_main_entry:
                return None
            entry = archive.main_entry
        elif not archive.has_entry_by_path(entry_path):
            return None
        else:
            entry = archive.get_entry_by_path(entry_path)
        for _ in range(_MAX_REDIRECTS):
            if not entry.is_redirect:
                return entry
            entry = entry.get_redirect_entry()
        return None

    def _book(
        self, source_id: str, generation_id: str, archives: dict[str, Archive], book: str
    ) -> Archive | None:
        """Open one book of a bundle on first use. File names normally match the ``Name`` metadata the
        adapter uses for native ids; if not, fall back to opening every book once to read it."""
        key = (source_id, generation_id)
        with self._lock:
            books = self._books.get(key, {})
        path = books.get(book)
        if path is not None:
            archive = Archive(path)
            if _book_name(archive, path) == book:
                with self._lock:
                    archives[book] = archive
                return archive
        for name, f in books.items():
            if name in archives:
                continue
            archive = Archive(f)
            with self._lock:
                archives[_book_name(archive, f)] = archive
        return archives.get(book)

    def home(self, source_id: str, generation_id: str) -> str | None:
        """Native id of the landing page: the ZIM's main entry for a single book, or
        :data:`BOOKS_PAGE` (a generated list of books) for a bundle."""
        archives = self._archives(source_id, generation_id)
        if "" not in archives:
            return BOOKS_PAGE if self.books(source_id, generation_id) else None
        archive = archives[""]
        if not archive.has_main_entry:
            return None
        entry = archive.main_entry
        for _ in range(_MAX_REDIRECTS):
            if not entry.is_redirect:
                return entry.path
            entry = entry.get_redirect_entry()
        return None

    def books(self, source_id: str, generation_id: str) -> list[str]:
        """Book names of a bundle (from file names; no archive is opened)."""
        self._archives(source_id, generation_id)
        with self._lock:
            return sorted(self._books.get((source_id, generation_id), {}))

    def _books_page(self, source_id: str, generation_id: str) -> str | None:
        """Generated index of a bundle: one link per book (``<book>/`` resolves to its front page)."""
        books = [] if "" in self._archives(source_id, generation_id) else self.books(source_id, generation_id)
        if not books:
            return None
        name = html.escape(self.names.get(source_id, source_id))
        items = "".join(
            f'<li><a href="{html.escape(b)}/">{html.escape(b.replace("_", " "))}</a></li>' for b in books
        )
        return f"<html><head><title>{name}</title></head><body><h1>{name}</h1><ul>{items}</ul></body></html>"

    def warm(self, source_ids: list[str]) -> None:
        """Open the current archives of single-book sources (first open of a large ZIM over SMB takes
        seconds); bundle books still open lazily."""
        for source_id in source_ids:
            with suppress(Exception):
                gen = self._vault.current(source_id)
                if gen is not None:
                    self._archives(source_id, gen.generation_id)

    def html(self, source_id: str, generation_id: str, html_ref: str) -> str | None:
        if not html_ref.startswith("zim:"):
            return None
        if html_ref == f"zim:{BOOKS_PAGE}":
            return self._books_page(source_id, generation_id)
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
