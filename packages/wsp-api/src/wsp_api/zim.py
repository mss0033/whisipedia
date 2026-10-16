"""ZIM-backed :class:`~wsp_core.ports.AssetStore`: original HTML and images straight from sealed ZIMs.

Paths are native ids. For a single-book source the native id is the ZIM entry path (``A/Alan_Turing``);
for a bundle of books it is prefixed with the book name (``ifixit_en_all/A/Foo``), exactly as the
``kiwix-opds`` adapter assigns them (``Name`` metadata, else the dated file name).

The store decides *which file* holds a page; a :class:`~wsp_api.zimread.ZimReader` reads it (worker
processes in production, see :mod:`wsp_api.zimpool`). Lookups never walk the NAS for unknown input:

* the generation id is checked against the vault's list of generations first (in the server that list
  is the in-memory library snapshot), so a bogus id costs nothing and evicts nothing (audit #6);
* a generation's file layout is computed once and kept in a bounded cache (sealed generations never
  change); failures are not cached;
* a bundle's book -> file map (``Name`` metadata of each of its ZIMs: one archive open per book) is
  built once, off the request path when ``sync_books`` is off, and persisted in ``book_index``.
"""

from __future__ import annotations

import html
import json
import logging
import os
import posixpath
import re
import threading
import time
from collections import OrderedDict
from collections.abc import Mapping
from concurrent.futures import Future
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote, urlsplit

from wsp_api.seams import VaultView
from wsp_api.zimread import (
    EntryData,
    InProcessZimReader,
    Want,
    ZimError,
    ZimReader,
    ZimUnavailable,
    file_book_name,
)
from wsp_core import mindtouch
from wsp_core.model import Asset, GenerationStatus

log = logging.getLogger(__name__)

BOOKS_PAGE = "_books"  # native id of the generated book list of a bundle source
_MAX_LAYOUTS = 512
_SERVED = (GenerationStatus.SEALED, GenerationStatus.DEGRADED)
_MAX_STUB_HOPS = 3
_MAX_PAGE_MAPS = 4  # MindTouch page lists kept (a few MB each for the largest LibreTexts library)
_STUB_MAX_BYTES = 4096
# mwoffliner writes a redirect to a *section* as a tiny page that only refreshes to it:
# `<meta http-equiv="refresh" content="0;URL='./IPv6_address#Stateless_address_autoconfiguration_(SLAAC)'" />`
_REFRESH = re.compile(
    r"""<meta\s+http-equiv=["']?refresh["']?\s+content=["']\s*\d+\s*;\s*url\s*=\s*'?([^'">]+)'?\s*["']""",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class ZimLayout:
    """The ZIM files of one generation: one book, or a bundle keyed by file-derived book name."""

    files: tuple[str, ...]
    by_file_name: Mapping[str, str] = field(default_factory=dict[str, str])

    @property
    def single(self) -> str | None:
        return self.files[0] if len(self.files) == 1 else None


@dataclass(frozen=True, slots=True)
class ZimPage:
    """An HTML page: ``native_id`` is the entry actually served (after redirects / the book's front
    page), which is what relative links in ``html`` resolve against."""

    native_id: str
    html: str
    book_root: str  # "" for a single-book source, "<book>/" in a bundle
    fragment: str = ""  # the section a followed section-redirect stub pointed at


def stub_target(page_html: str, path: str) -> tuple[str, str] | None:
    """``(entry path, fragment)`` a section-redirect stub at ``path`` refreshes to, or ``None`` if the
    page is not such a stub (big, or no refresh, or a refresh that leaves the archive)."""
    if len(page_html) > _STUB_MAX_BYTES:
        return None
    m = _REFRESH.search(page_html)
    if m is None:
        return None
    url = urlsplit(m[1].strip())
    if url.scheme or url.netloc or not url.path or url.fragment.startswith("/"):
        return None  # elsewhere, or a route of a JavaScript app (MindTouch: wsp_core.mindtouch)
    target = posixpath.normpath(posixpath.join(posixpath.dirname(path), unquote(url.path)))
    if target.startswith("../") or target in (".", ".."):
        return None
    return target.removeprefix("./"), unquote(url.fragment)


class BookIndex:
    """``(source, generation) -> {book name: file name}`` for bundles, persisted as one JSON file.

    Generations are immutable, so an entry never goes stale. ``path=None`` keeps it in memory only.
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._maps: dict[str, dict[str, str]] = {}
        if path is not None and path.is_file():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    for key, value in raw.items():  # pyright: ignore[reportUnknownVariableType]
                        if isinstance(value, dict):
                            self._maps[str(key)] = {str(k): str(v) for k, v in value.items()}  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
            except (OSError, ValueError):
                log.warning("ignoring unreadable book index %s", path)

    @staticmethod
    def _key(source_id: str, generation_id: str) -> str:
        return f"{source_id}/{generation_id}"

    def get(self, source_id: str, generation_id: str) -> dict[str, str] | None:
        with self._lock:
            return self._maps.get(self._key(source_id, generation_id))

    def put(self, source_id: str, generation_id: str, books: Mapping[str, str]) -> None:
        with self._lock:
            self._maps[self._key(source_id, generation_id)] = dict(books)
            snapshot = json.dumps(self._maps, sort_keys=True, ensure_ascii=False)
        if self.path is None:
            return
        try:  # atomic replace; a lost write only costs a rebuild
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(f"{self.path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
            tmp.write_text(snapshot, encoding="utf-8")
            tmp.replace(self.path)
        except OSError:
            log.warning("could not persist the book index to %s", self.path, exc_info=True)


class ZimAssetStore:
    def __init__(
        self,
        vault: VaultView,
        reader: ZimReader | None = None,
        *,
        book_index: BookIndex | None = None,
        sync_books: bool = True,
    ) -> None:
        """``vault`` answers ``generations``/``generation_path`` (the server passes its snapshot view).
        ``sync_books``: build a bundle's book index on the request that first needs it (tests, CLI);
        otherwise in a background thread, answering :class:`ZimUnavailable` meanwhile (server)."""
        self._vault = vault
        self.reader: ZimReader = reader or InProcessZimReader()
        self.book_index = book_index or BookIndex()
        self.sync_books = sync_books
        self.names: dict[str, str] = {}  # source id -> display name, for generated pages
        self._layouts: OrderedDict[tuple[str, str], ZimLayout] = OrderedDict()
        self._building: dict[tuple[str, str], Future[dict[str, str] | None]] = {}
        self._warm_seen: set[tuple[str, str]] = set()
        # Articles per generation (sum of each archive's article count), once all are open.
        self.article_counts: dict[tuple[str, str], int] = {}
        self._warm_queue: list[tuple[str, str]] = []
        self._warmer: threading.Thread | None = None
        self._page_maps: OrderedDict[str, dict[str, str]] = OrderedDict()  # MindTouch: file -> path -> id
        self._lock = threading.Lock()

    # --- where things are ---

    def layout(self, source_id: str, generation_id: str) -> ZimLayout | None:
        """The ZIM files of a known generation, or ``None`` (unknown generations are never looked up)."""
        key = (source_id, generation_id)
        with self._lock:
            hit = self._layouts.get(key)
            if hit is not None:
                self._layouts.move_to_end(key)
                return hit
        gen = next((g for g in self._vault.generations(source_id) if g.generation_id == generation_id), None)
        if gen is None or gen.status not in _SERVED:  # never a half-staged generation
            return None
        root = self._vault.generation_path(source_id, generation_id)
        files = [root / str(a.relpath) for a in gen.artifacts if a.relpath.suffix == ".zim"]
        if not files:  # older generations list no artifacts: the downloaded ZIMs under upstream/
            with suppress(OSError):
                files = sorted((root / "upstream").glob("*.zim"))
        if not files:
            return None
        layout = ZimLayout(
            files=tuple(str(f) for f in files),
            by_file_name={} if len(files) == 1 else {file_book_name(f): str(f) for f in files},
        )
        with self._lock:
            self._layouts[key] = layout
            while len(self._layouts) > _MAX_LAYOUTS:
                self._layouts.popitem(last=False)
        return layout

    def books(self, source_id: str, generation_id: str) -> list[str]:
        """Book names of a bundle: the ``Name`` of each ZIM once the book index is built, before that the
        names the file names imply. Empty for a single-book source."""
        layout = self.layout(source_id, generation_id)
        if layout is None or layout.single is not None:
            return []
        built = self.book_index.get(source_id, generation_id)
        if built is None and self.sync_books:
            built = self.build_books(source_id, generation_id)
        return sorted(built if built is not None else layout.by_file_name)

    def _book_file(self, source_id: str, generation_id: str, layout: ZimLayout, book: str) -> str | None:
        built = self.book_index.get(source_id, generation_id)
        if built is None and self.sync_books:
            built = self.build_books(source_id, generation_id)
        if built is not None:
            name = built.get(book)
            if name is None:
                return None
            return next((f for f in layout.files if Path(f).name == name), None)
        self.build_books_soon(source_id, generation_id)
        found = layout.by_file_name.get(book)  # usually the file name implies the book name
        if found is None:
            raise ZimUnavailable("the book list of this collection is still being built")
        return found

    def build_books(self, source_id: str, generation_id: str) -> dict[str, str] | None:
        """Read every book name of a bundle (one archive open each) and persist the map. Single-flight."""
        key = (source_id, generation_id)
        with self._lock:
            pending = self._building.get(key)
            mine = pending is None
            if pending is None:
                pending = self._building[key] = Future()
        if not mine:
            return pending.result()
        try:
            layout = self.layout(source_id, generation_id)
            books: dict[str, str] | None = None
            if layout is not None and layout.single is None:
                books = {}
                for f in layout.files:
                    books.setdefault(self.reader.book_name(f), Path(f).name)
                self.book_index.put(source_id, generation_id, books)
            pending.set_result(books)
            return books
        except BaseException as exc:
            pending.set_exception(exc)
            raise
        finally:
            with self._lock:
                del self._building[key]

    def random(self, source_id: str, generation_id: str) -> tuple[str, str] | None:
        """``(native id, title)`` of a random article of a single-archive generation (bundles have a
        Refinery catalog to pick from)."""
        layout = self.layout(source_id, generation_id)
        if layout is None or layout.single is None:
            return None
        return self.reader.random(layout.single)

    def warm_soon(self, source_id: str, generation_id: str) -> None:
        """Open every archive of a generation in the background, largest first, one at a time (a single
        thread for all generations: the NAS serves one cold open well, many badly)."""
        with self._lock:
            if (source_id, generation_id) in self._warm_seen:
                return
            self._warm_seen.add((source_id, generation_id))
            self._warm_queue.append((source_id, generation_id))
            if self._warmer is not None and self._warmer.is_alive():
                return
            self._warmer = threading.Thread(target=self._warm_loop, name="zim-warm", daemon=True)
            self._warmer.start()

    def _warm_loop(self) -> None:
        while True:
            with self._lock:
                if not self._warm_queue:
                    self._warmer = None
                    return
                source_id, generation_id = self._warm_queue.pop(0)
            layout = self.layout(source_id, generation_id)
            if layout is None:
                continue
            sized: list[tuple[int, str]] = []
            for f in layout.files:
                try:
                    sized.append((os.stat(f).st_size, f))
                except OSError:
                    sized.append((0, f))
            t0 = time.monotonic()
            total, known = 0, True
            for _, f in sorted(sized, reverse=True):
                try:
                    n = self.reader.warm(f)
                except Exception as exc:  # a bad archive must not stop the others
                    log.warning("could not open %s ahead of time: %s", f, exc)
                    n = None
                if n is None:
                    known = False
                else:
                    total += n
            if known:
                with self._lock:
                    self.article_counts[(source_id, generation_id)] = total
            log.info(
                "opened %d archive(s) of %s/%s in %.0f s",
                len(sized),
                source_id,
                generation_id,
                time.monotonic() - t0,
            )

    def build_books_soon(self, source_id: str, generation_id: str) -> None:
        with self._lock:
            if (source_id, generation_id) in self._building:
                return

        def run() -> None:
            try:
                self.build_books(source_id, generation_id)
            except Exception:
                log.warning("could not build the book list of %s/%s", source_id, generation_id, exc_info=True)

        threading.Thread(target=run, name="zim-books", daemon=True).start()

    def _locate(self, source_id: str, generation_id: str, native_id: str) -> tuple[str, str, str] | None:
        """``(file, entry path, book root)`` for a native id."""
        layout = self.layout(source_id, generation_id)
        if layout is None:
            return None
        if layout.single is not None:
            return layout.single, native_id, ""
        book, _, entry_path = native_id.partition("/")
        file = self._book_file(source_id, generation_id, layout, book) if book else None
        return None if file is None else (file, entry_path, f"{book}/")

    def file_of(self, source_id: str, generation_id: str, native_id: str) -> str | None:
        """The archive that holds ``native_id`` (no entry is read). ``None`` if unknown, or while a
        bundle's book list is still being built."""
        try:
            where = self._locate(source_id, generation_id, native_id)
        except ZimUnavailable:
            return None
        return None if where is None else where[0]

    def _read(self, file: str, path: str, want: Want) -> EntryData | None:
        try:
            return self.reader.read(file, path, want)
        except ZimUnavailable:
            raise
        except ZimError:  # unreadable archive or entry: "not here", logged for the operator
            log.warning("ZIM read failed: %s %s", file, path, exc_info=True)
            return None

    # --- pages and assets ---

    def page(self, source_id: str, generation_id: str, native_id: str) -> ZimPage | None:
        """The HTML page at ``native_id`` (one read: content and the entry actually served)."""
        if native_id == BOOKS_PAGE:
            text = self._books_page(source_id, generation_id)
            return None if text is None else ZimPage(BOOKS_PAGE, text, "")
        where = self._locate(source_id, generation_id, native_id)
        if where is None:
            return None
        file, path, root = where
        fragment = ""
        for _ in range(_MAX_STUB_HOPS + 1):
            data = self._read(file, path, Want.HTML)
            if data is None or data.content is None:
                return None
            text = data.content.decode("utf-8", errors="replace")
            body = self._mindtouch(file, data.path, text)
            if body is not None:
                return ZimPage(root + data.path, body, root, fragment)
            # A section redirect is served as the page it points at (a link to it was a dead end:
            # IPv6 -> IPv6_stateless_address_autoconfiguration, 2026-10-16 field exam).
            hop = stub_target(text, data.path)
            if hop is None or not self._read(file, hop[0], Want.NONE):
                return ZimPage(root + data.path, text, root, fragment)
            path, fragment = hop
        return None

    def _mindtouch(self, file: str, path: str, stub: str) -> str | None:
        """The page a MindTouch app stub stands for (``wsp_core.mindtouch``), or ``None``."""
        entry = mindtouch.content_entry(path, stub)
        if entry is None:
            return None
        content = self._read(file, entry, Want.ANY)
        if content is None or content.content is None:
            return None
        with self._lock:
            ids = self._page_maps.get(file)
            if ids is not None:
                self._page_maps.move_to_end(file)
        if ids is None:
            shared = self._read(file, mindtouch.SHARED, Want.ANY)
            ids = {} if shared is None or shared.content is None else mindtouch.page_ids(shared.content)
            with self._lock:
                self._page_maps[file] = ids
                while len(self._page_maps) > _MAX_PAGE_MAPS:
                    self._page_maps.popitem(last=False)
        return mindtouch.page_html(stub, content.content, ids)

    def exists(self, source_id: str, generation_id: str, native_id: str) -> bool:
        """An HTML page is there (no content is read)."""
        if native_id == BOOKS_PAGE:
            return bool(self.books(source_id, generation_id))
        where = self._locate(source_id, generation_id, native_id)
        if where is None:
            return False
        data = self._read(where[0], where[1], Want.NONE)
        return data is not None and data.is_html

    def home(self, source_id: str, generation_id: str) -> str | None:
        """Native id of the landing page: the ZIM's main entry for a single book, or
        :data:`BOOKS_PAGE` (a generated list of books) for a bundle. The server calls this from its
        snapshot refresher, so it also opens the archive / starts the bundle's book index there,
        off the request path."""
        layout = self.layout(source_id, generation_id)
        if layout is None:
            return None
        if layout.single is None:
            if not self.sync_books and self.book_index.get(source_id, generation_id) is None:
                self.build_books_soon(source_id, generation_id)
            return BOOKS_PAGE
        data = self._read(layout.single, "", Want.NONE)
        return None if data is None else data.path

    def _books_page(self, source_id: str, generation_id: str) -> str | None:
        """Generated index of a bundle: one link per book (``<book>/`` resolves to its front page)."""
        books = self.books(source_id, generation_id)
        if not books:
            return None
        name = html.escape(self.names.get(source_id, source_id))
        items = "".join(
            f'<li><a href="{html.escape(b)}/">{html.escape(b.replace("_", " "))}</a></li>' for b in books
        )
        return f"<html><head><title>{name}</title></head><body><h1>{name}</h1><ul>{items}</ul></body></html>"

    def html(self, source_id: str, generation_id: str, html_ref: str) -> str | None:
        if not html_ref.startswith("zim:"):
            return None
        found = self.page(source_id, generation_id, html_ref.removeprefix("zim:"))
        return None if found is None else found.html

    def asset(self, source_id: str, generation_id: str, path: str) -> Asset | None:
        where = self._locate(source_id, generation_id, path)
        if where is None:
            return None
        data = self._read(where[0], where[1], Want.ANY)
        if data is None or data.content is None:
            return None
        return Asset(content=data.content, mimetype=data.mimetype)

    def close(self) -> None:
        self.reader.close()
