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

import hashlib
import html
import itertools
import json
import logging
import os
import posixpath
import re
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterable, Mapping, Sequence
from concurrent.futures import Future
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote, urlsplit

from wsp_api.encoded import decoded, html_as_image
from wsp_api.outside import Site
from wsp_api.pdftext import pdf_text
from wsp_api.seams import VaultView, full_generation
from wsp_api.zimread import (
    EntryData,
    InProcessZimReader,
    Want,
    ZimError,
    ZimReader,
    ZimUnavailable,
    file_book_name,
)
from wsp_core import fcc, mindtouch, nautilus, youtubezim
from wsp_core.model import Asset, Generation, GenerationStatus

log = logging.getLogger(__name__)

BOOKS_PAGE = "_books"  # native id of the generated book list of a bundle source
_MAX_LAYOUTS = 512
_SERVED = (GenerationStatus.SEALED, GenerationStatus.DEGRADED)
_MAX_STUB_HOPS = 3
_MAX_PAGE_MAPS = 4  # MindTouch page lists kept (a few MB each for the largest LibreTexts library)
_STUB_MAX_BYTES = 4096
# Release ids and file names as the Kiwix adapter writes them (wsp_adapters.kiwix): a bundle release is
# "<date>-<digest of its file names>", a file "<book>_<YYYY-MM>[a-z].zim".
_BUNDLE_ID = re.compile(r"^(?P<date>\d{4}-\d{2}[a-z]?)[-+](?P<digest>[0-9a-f]{8})$")
_ZIM_DATE = re.compile(r"_(\d{4}-\d{2})([a-z]?)\.zim$")
_MAX_DROPPED_BOOKS = 2


def _names_digest(names: Iterable[str]) -> str:
    return hashlib.sha256("\n".join(sorted(names)).encode()).hexdigest()[:8]


def file_release_date(file_name: str) -> str:
    m = _ZIM_DATE.search(file_name)
    return m.group(1) + m.group(2) if m else ""


def book_key(file_name: str) -> str:
    return _ZIM_DATE.sub("", file_name).removesuffix(".zim")


def _release_order(gen: Generation) -> tuple[str, str, str]:
    """Release order (``wsp_adapters._common.generation_order_key``): the date the id starts with,
    then the seal time, then the id."""
    m = _BUNDLE_ID.match(gen.generation_id)
    sealed = gen.sealed_at.isoformat() if gen.sealed_at is not None else ""
    return (m.group("date") if m else gen.generation_id, sealed, gen.generation_id)


# mwoffliner writes a redirect to a *section* as a tiny page that only refreshes to it:
# `<meta http-equiv="refresh" content="0;URL='./IPv6_address#Stateless_address_autoconfiguration_(SLAAC)'" />`
_REFRESH = re.compile(
    r"""<meta\s+http-equiv=["']?refresh["']?\s+content=["']\s*\d+\s*;\s*url\s*=\s*'?([^'">]+)'?\s*["']""",
    re.IGNORECASE,
)


NAUTILUS_TEXT_SECONDS = 10.0  # all of one item page's PDF text, below the 20 s NAS deadline


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
    # Where else a root-relative URL of the page may be (htmlrewrite): its host folder in a crawled
    # site, and the folder the archive's main page is in.
    site_roots: tuple[str, ...] = ()


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
    # A web archive keeps a directory's trailing slash in its paths ("…/introduction/"): normpath
    # dropped it and the refresh led nowhere (getbootstrap.com/docs/5.2, 2026-10-16 crawl).
    slash = "/" if url.path.endswith("/") else ""
    return target.removeprefix("./") + slash, unquote(url.fragment)


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

    def items(self) -> list[tuple[str, str, dict[str, str]]]:
        """Every entry as ``(source, generation, map)``."""
        with self._lock:
            return [(*k.split("/", 1), dict(v)) for k, v in self._maps.items() if "/" in k]  # pyright: ignore[reportReturnType]

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
        site_index: BookIndex | None = None,
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
        self._page_maps: OrderedDict[str, mindtouch.Pages] = OrderedDict()  # MindTouch: file -> page list
        self._main_dirs: dict[str, str | None] = {}  # file -> folder of its main page (site_roots)
        self._catalogues: dict[str, list[nautilus.Item] | None] = {}  # file -> nautilus catalogue
        # The websites the current archives copy (wsp_api.outside), found while warming them;
        # ``sites_version`` changes with every addition (rendered pages are cached by it).
        self._sites: dict[tuple[str, str], list[Site]] = {}
        self.sites_version = 0
        # ... and remembered across restarts (``file name -> kind, host, book`` per generation): a site
        # was unknown until its archive was warmed again, and links to English Wikipedia read as "not
        # in the library" for the first hour after each start (2026-10-28, gap proposal 101).
        self._site_index = site_index
        for sid, gid, files in site_index.items() if site_index is not None else ():
            for value in files.values():
                kind, _, rest = value.partition("\t")
                host, _, book = rest.partition("\t")
                if kind and host:
                    self._sites.setdefault((sid, gid), []).append(Site(kind, host, sid, book))
                    self.sites_version += 1
        self._roots: dict[str, str | None] = {}  # MindTouch: file -> home page entry
        self._fronts: dict[str, str | None] = {}  # YouTube scraper: file -> generated front page
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
        gens = [g for g in self._vault.generations(source_id) if g.status in _SERVED]
        gen = next((g for g in gens if g.generation_id == generation_id), None)
        if gen is None:  # never a half-staged generation
            return None
        files = self._content(source_id, gen, gens)
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

    def _own_files(self, source_id: str, gen: Generation) -> dict[str, Path]:
        """A generation's own ZIMs by book key (the newest build of a book wins)."""
        gen = full_generation(self._vault, gen)  # the snapshot's lists leave artifacts out
        root = self._vault.generation_path(source_id, gen.generation_id)
        files = [root / str(a.relpath) for a in gen.artifacts if a.relpath.suffix == ".zim"]
        if not files:  # older generations list no artifacts: the downloaded ZIMs under upstream/
            with suppress(OSError):
                files = sorted((root / "upstream").glob("*.zim"))
        own: dict[str, Path] = {}
        for f in sorted(files, key=lambda f: (file_release_date(f.name), f.name)):
            own[book_key(f.name)] = f
        return own

    def _content(self, source_id: str, gen: Generation, gens: Sequence[Generation]) -> list[Path]:
        """The ZIMs of a generation's release. A bundle release (id ``<date>-<digest>``) stores only its
        new and changed books and reads the rest from earlier generations, the way the indexer builds
        it (``wsp_adapters.kiwix.bundle_content``): the Kiwix bundle's October release held Wikiquote
        alone, and iFixit, Wikibooks and nine more books lost their pages (2026-10-17 census)."""
        if not _BUNDLE_ID.match(gen.generation_id):
            return list(self._own_files(source_id, gen).values())
        me = _release_order(gen)
        content: dict[str, Path] = {}
        for g in sorted((g for g in gens if _release_order(g) <= me), key=_release_order):
            own = self._own_files(source_id, g)
            carried = {k: p for k, p in content.items() if k not in own}
            m = _BUNDLE_ID.match(g.generation_id)
            content = {**carried, **own}
            if m is None:
                continue
            names = [p.name for p in own.values()]
            for drops in range(min(_MAX_DROPPED_BOOKS, len(carried)) + 1):
                hit = next(
                    (
                        gone
                        for gone in itertools.combinations(sorted(carried), drops)
                        if _names_digest([*names, *(carried[k].name for k in carried if k not in gone)])
                        == m.group("digest")
                    ),
                    None,
                )
                if hit is not None:
                    content = {**{k: p for k, p in carried.items() if k not in hit}, **own}
                    break
            else:
                log.warning("%s/%s: its books do not match the release digest", source_id, g.generation_id)
        return sorted(content.values())

    def _note_site(self, source_id: str, generation_id: str, layout: ZimLayout, file: str) -> None:
        try:
            found = self.reader.site(file)
            book = ""
            if found is not None and layout.single is None:
                built = self.book_index.get(source_id, generation_id) or self.build_books(
                    source_id, generation_id
                )
                name = next((b for b, f in (built or {}).items() if f == Path(file).name), None)
                if name is None:
                    return
                book = f"{name}/"
        except Exception as exc:  # an archive whose metadata cannot be read is simply not linked
            log.warning("could not read which site %s copies: %s", file, exc)
            return
        if found is None:
            return
        site = Site(found[0], found[1], source_id, book)
        with self._lock:
            known = self._sites.setdefault((source_id, generation_id), [])
            if site in known:
                return
            known.append(site)
            self.sites_version += 1
        if self._site_index is not None:
            files = self._site_index.get(source_id, generation_id) or {}
            files[Path(file).name] = f"{site.kind}\t{site.host}\t{site.book}"
            self._site_index.put(source_id, generation_id, files)

    def sites(self, current: Callable[[str], str | None]) -> list[Site]:
        """The sites of each source's current generation (``current``: source id -> generation id)."""
        with self._lock:
            return [s for (sid, gid), found in self._sites.items() if current(sid) == gid for s in found]

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

    def open_landing(self, source_id: str, generation_id: str) -> None:
        """Open the archive a single-book generation's landing page is read from, waiting as long as
        that takes (the snapshot refresher, before :meth:`home`). A landing-page read that timed out
        left its worker thread opening the archive while the refresher went on to the next one: on
        2026-10-19, 15 minutes after a restart over a busy NAS, every worker thread was opening an
        archive no one waited for and page views failed as busy. This way the refresher holds one."""
        layout = self.layout(source_id, generation_id)
        if layout is None or layout.single is None:
            return  # a bundle's landing page is its book list: nothing is read
        try:
            self.reader.warm(layout.single)
        except Exception as exc:  # the landing-page read reports it
            log.debug("could not open %s ahead of its landing page: %s", layout.single, exc)

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
                if n is not None:
                    self._note_site(source_id, generation_id, layout, f)
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
        if (catalogue := self._nautilus(file)) is not None and (
            path in ("", nautilus.HOME, f"{nautilus.HOME}.html") or path.startswith(nautilus.ITEM_PREFIX)
        ):
            return self._nautilus_page(source_id, file, path, root, catalogue)
        if path == mindtouch.APP_SHELL:
            if (home := self._mindtouch_root(file)) is not None:
                path = home  # a link to the app ("../index.html", the main entry) is its home page
            elif (front := self._youtube_home(file) or self._fcc_home(source_id, file)) is not None:
                return ZimPage(root + path, front, root)
        fragment = ""
        for _ in range(_MAX_STUB_HOPS + 1):
            data = self._read(file, path, Want.HTML)
            if data is None or data.content is None:
                return None
            text = data.content.decode("utf-8", errors="replace")
            body = (
                self._mindtouch(file, data.path, text)
                or self._youtube(file, data.path, text)
                or self._fcc(file, data.path, text)
            )
            if body is not None:
                return ZimPage(root + data.path, body, root, fragment)
            # A section redirect is served as the page it points at (a link to it was a dead end:
            # IPv6 -> IPv6_stateless_address_autoconfiguration, 2026-10-16 field exam).
            hop = stub_target(text, data.path)
            if hop is not None and not self._read(file, hop[0], Want.NONE):
                other = hop[0].rstrip("/") if hop[0].endswith("/") else hop[0] + "/"
                hop = (other, hop[1]) if self._read(file, other, Want.NONE) else None
            if hop is None:
                return ZimPage(
                    root + data.path, text, root, fragment, self._site_roots(file, data.path, root)
                )
            path, fragment = hop
        return None

    def _site_roots(self, file: str, path: str, root: str) -> tuple[str, ...]:
        """``<host>/`` of a crawled page (``example.org/a/b``), then the main page's folder."""
        out: list[str] = []
        first = path.split("/", 1)[0]
        if "/" in path and "." in first and " " not in first:
            out.append(f"{root}{first}/")
        with self._lock:
            known = file in self._main_dirs
            main_dir = self._main_dirs.get(file)
        if not known:
            data = self._read(file, "", Want.NONE)
            main_dir = (
                None if data is None or "/" not in data.path else data.path[: data.path.rindex("/") + 1]
            )
            with self._lock:
                self._main_dirs[file] = main_dir
        if main_dir and f"{root}{main_dir}" not in out:
            out.append(f"{root}{main_dir}")
        return tuple(out)

    def find(self, source_id: str, generation_id: str, native_id: str) -> str | None:
        """The native id of the entry at ``native_id``, its other slash form or its folder's
        ``index.html`` (a crawled site's "/opsec/x" is "…/opsec/x/index.html"), any type; else None."""
        try:
            where = self._locate(source_id, generation_id, native_id)
        except ZimUnavailable:
            return None
        if where is None:
            return None
        file, path, root = where
        bare = path.rstrip("/")
        for candidate in (path, bare if path.endswith("/") else path + "/", f"{bare}/index.html"):
            if candidate and self._read(file, candidate, Want.NONE) is not None:
                return root + candidate
        return None

    def is_image(self, source_id: str, generation_id: str, native_id: str) -> bool:
        """Whether the entry at ``native_id`` is a picture (no content is read)."""
        try:
            where = self._locate(source_id, generation_id, native_id)
            data = self._read(where[0], where[1], Want.NONE) if where is not None else None
        except ZimUnavailable:
            return True  # busy: keep the picture rather than drop it
        return data is not None and data.mimetype.startswith("image/")

    def _nautilus(self, file: str) -> list[nautilus.Item] | None:
        """The catalogue of a nautilus archive (``wsp_core.nautilus``), ``None`` for any other."""
        with self._lock:
            if file in self._catalogues:
                return self._catalogues[file]
        found = self._read(file, nautilus.DATABASE, Want.ANY)
        catalogue = nautilus.items(found.content) if found is not None and found.content else []
        result = catalogue or None
        with self._lock:
            self._catalogues[file] = result
        return result

    def _nautilus_page(
        self, source_id: str, file: str, path: str, root: str, catalogue: list[nautilus.Item]
    ) -> ZimPage | None:
        """A nautilus archive's front page (``home``), or a document's page with its files' text."""
        if not path.startswith(nautilus.ITEM_PREFIX):
            name = self.names.get(source_id, source_id).removesuffix(" (Kiwix ZIM)")
            return ZimPage(root + nautilus.HOME, nautilus.home_html(name, "", catalogue), root)
        item = next((i for i in catalogue if i.path == path), None)
        if item is None:
            return None
        texts: dict[str, str] = {}
        # One budget for all the item's files, well inside the request's NAS deadline: five scanned
        # PDFs at 60 s each held NAS slots for minutes and other pages answered "busy" (review #113).
        deadline = time.monotonic() + NAUTILUS_TEXT_SECONDS
        for name in item.files:
            left = deadline - time.monotonic()
            if left <= 0:
                break
            data = self._read(file, nautilus.FILES + name, Want.ANY)
            if data is not None and data.content is not None and data.mimetype.startswith("application/pdf"):
                texts[name] = pdf_text(data.content, seconds=left)
        return ZimPage(root + item.path, nautilus.item_html(item, texts), root)

    def _mindtouch(self, file: str, path: str, stub: str) -> str | None:
        """The page a MindTouch app stub stands for (``wsp_core.mindtouch``), or ``None``."""
        entry = mindtouch.content_entry(path, stub)
        if entry is None:
            return None
        content = self._read(file, entry, Want.ANY)
        if content is None or content.content is None:
            return None
        pages = self._mindtouch_maps(file)[0]

        def load(page_id: str) -> bytes | None:
            found = self._read(file, mindtouch.content_path(page_id), Want.ANY)
            return None if found is None else found.content

        return mindtouch.page_html(stub, content.content, pages.ids, pages=pages, load=load)

    def _youtube(self, file: str, path: str, stub: str) -> str | None:
        """The page a YouTube-scraper app stub stands for (``wsp_core.youtubezim``), or ``None``."""
        entry = youtubezim.content_entry(path, stub)
        if entry is None:
            return None
        content = self._read(file, entry, Want.ANY)
        return None if content is None or content.content is None else youtubezim.page_html(content.content)

    def _fcc(self, file: str, path: str, stub: str) -> str | None:
        """The challenge a freeCodeCamp-scraper app stub stands for (``wsp_core.fcc``), or ``None``."""
        entry = fcc.content_entry(path, stub)
        if entry is None:
            return None
        content = self._read(file, entry, Want.ANY)
        return None if content is None or content.content is None else fcc.page_html(content.content)

    def _fcc_home(self, source_id: str, file: str) -> str | None:
        """The front page of a freeCodeCamp-scraper archive: every challenge (its app shell is empty)."""
        index = self._read(file, fcc.INDEX, Want.ANY)
        if index is None or index.content is None:
            return None
        return fcc.home_html(self.names.get(source_id, source_id).removesuffix(" (Kiwix ZIM)"), index.content)

    def _youtube_home(self, file: str) -> str | None:
        """The front page of a YouTube-scraper archive (its app shell is empty), or ``None``."""
        with self._lock:
            if file in self._fronts:
                return self._fronts[file]
        home = self._read(file, youtubezim.HOME, Want.ANY)
        front = None
        if home is not None and home.content is not None:
            channel = self._read(file, "channel.json", Want.ANY)
            front = youtubezim.home_html(home.content, None if channel is None else channel.content)
        with self._lock:
            self._fronts[file] = front
        return front

    def _mindtouch_maps(self, file: str) -> tuple[mindtouch.Pages, str | None]:
        """``(page list, home page entry)`` of a MindTouch archive (both empty for any other archive),
        from ``content/shared.json``, read once per archive."""
        with self._lock:
            pages = self._page_maps.get(file)
            if pages is not None:
                self._page_maps.move_to_end(file)
                return pages, self._roots.get(file)
        shared = self._read(file, mindtouch.SHARED, Want.ANY)
        raw = None if shared is None else shared.content
        pages = mindtouch.Pages({} if raw is None else mindtouch.page_ids(raw))
        home = None if raw is None else mindtouch.root_page(raw)
        with self._lock:
            self._page_maps[file] = pages
            self._roots[file] = home
            while len(self._page_maps) > _MAX_PAGE_MAPS:
                gone, _ = self._page_maps.popitem(last=False)
                self._roots.pop(gone, None)
        return pages, home

    def _mindtouch_root(self, file: str) -> str | None:
        return self._mindtouch_maps(file)[1]

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
        if data is not None and data.path == mindtouch.APP_SHELL:
            return self._mindtouch_root(layout.single) or data.path
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
        if data is None or data.content is None or html_as_image(data.content, data.mimetype):
            return None
        return Asset(content=decoded(data.content, data.mimetype), mimetype=data.mimetype)

    def close(self) -> None:
        self.reader.close()
