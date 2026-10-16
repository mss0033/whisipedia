"""Reading single entries from ZIM archives: the code that runs where libzim runs.

The API reads ZIMs through a :class:`ZimReader`. In production that is
:class:`wsp_api.zimpool.ZimProcessPool`, which runs this module in worker processes: libzim
memory-maps the archive, and on a soft-mounted CIFS share an I/O error inside a mapped page is a
``SIGBUS``, which kills the process that touched it (audit 2026-10-11 #2). Tests, the sample backend and
the in-process CLI use :class:`InProcessZimReader`, which runs the same code in the calling process.

Everything here takes a ZIM *file* and an entry path. Which file holds a page (single book or bundle,
which generation) is decided by :class:`wsp_api.zim.ZimAssetStore` from the library snapshot.
"""

from __future__ import annotations

import re
import threading
from collections import OrderedDict
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from libzim.reader import Archive, Entry

MAX_REDIRECTS = 5
ZIM_DATE_RE = re.compile(r"_(\d{4}-\d{2})[a-z]?\.zim$")  # "_2026-07", "_2026-07a" (a re-release)


class Want(StrEnum):
    """How much of an entry to send back."""

    NONE = "none"  # where it is and what it is (existence checks, link bases)
    HTML = "html"  # the bytes only if the entry is an HTML page
    ANY = "any"  # the bytes whatever the type (images)


@dataclass(frozen=True, slots=True)
class EntryData:
    """One entry after redirects: ``path`` is the entry actually served (``""`` asked for the main page)."""

    path: str
    mimetype: str
    content: bytes | None = None

    @property
    def is_html(self) -> bool:
        return self.mimetype.startswith("text/html")


class ZimError(RuntimeError):
    """A ZIM read could not be done (the archive is unreadable, or the reader is unavailable)."""


class ZimUnavailable(ZimError):  # noqa: N818 - "unavailable" reads better than "...Error" at call sites
    """The reader is overloaded, timed out or lost its worker: try again later (HTTP 503)."""


class ZimReader(Protocol):
    def read(self, file: str, path: str, want: Want) -> EntryData | None:
        """The entry at ``path`` (``""``: the main page) of ``file``, following redirects."""
        ...

    def book_name(self, file: str) -> str:
        """The book's ``Name`` metadata, else the dated file name (``wsp_adapters.kiwix.book_name``)."""
        ...

    def random(self, file: str) -> tuple[str, str] | None:
        """``(path, title)`` of a random article of ``file`` (:func:`random_article`)."""
        ...

    def warm(self, file: str) -> int | None:
        """Open ``file`` and keep it open (no request waits for it; a cold open over SMB can take
        minutes for the largest archives). Returns its article count."""
        ...

    def close(self) -> None: ...


def open_archive(file: str) -> Archive:
    return Archive(Path(file))


def file_book_name(path: Path | str) -> str:
    """The book name a file name implies (``askubuntu.com_en_all_2026-07a.zim``: ``askubuntu.com_en_all``)."""
    return ZIM_DATE_RE.sub("", Path(path).name).removesuffix(".zim")


def book_name_of(archive: Archive, path: Path | str) -> str:
    """Same rule as ``wsp_adapters.kiwix.book_name`` (``Name`` metadata, else the dated file name)."""
    if "Name" in archive.metadata_keys:
        name = archive.get_metadata("Name").decode("utf-8", errors="replace").strip()
        if name:
            return name
    return file_book_name(path)


def read_entry(archive: Archive, path: str, want: Want) -> EntryData | None:
    """``path`` (or the main entry for ``""``) with redirects followed; ``None`` if absent or a loop."""
    entry: Entry
    if not path:
        if not archive.has_main_entry:
            return None
        entry = archive.main_entry
    elif not archive.has_entry_by_path(path):
        return None
    else:
        entry = archive.get_entry_by_path(path)
    for _ in range(MAX_REDIRECTS):
        if entry.is_redirect:
            entry = entry.get_redirect_entry()
            continue
        item = entry.get_item()
        mimetype = str(item.mimetype)
        send = want is Want.ANY or (want is Want.HTML and mimetype.startswith("text/html"))
        return EntryData(entry.path, mimetype, bytes(item.content) if send else None)
    return None


def page_count(archive: Archive) -> int:
    """HTML pages in an archive: the ``text/html`` figure of its ``Counter`` metadata (``article_count``
    also counts redirects: 21.7M for Wikipedia English's ~7M pages), else ``article_count``."""
    if "Counter" in archive.metadata_keys:
        counter = archive.get_metadata("Counter").decode("utf-8", errors="replace")
        for part in counter.split(";"):
            mime, _, n = part.partition("=")
            if mime.strip() == "text/html" and n.strip().isdigit():
                return int(n)
    return int(archive.article_count)


RANDOM_TRIES = 20  # random entries drawn until one is an article (most are, after the front matter)


def random_article(archive: Archive) -> tuple[str, str] | None:
    """``(path, title)`` of a random HTML entry that is not a redirect, or ``None`` after a few tries."""
    for _ in range(RANDOM_TRIES):
        entry = archive.get_random_entry()
        if entry.is_redirect:
            continue
        if str(entry.get_item().mimetype).startswith("text/html"):
            return entry.path, entry.title
    return None


class ArchiveCache:
    """Open archives by file, least recently used first out, opened once even when asked concurrently.

    Opening a large ZIM over SMB takes seconds; two requests for a cold archive must not open it
    twice (audit #6), and a failed open is not remembered (the next request tries again).
    """

    def __init__(self, max_open: int, opener: Callable[[str], Archive] = open_archive) -> None:
        self._max = max(1, max_open)
        self._open = opener
        self._archives: OrderedDict[str, Archive] = OrderedDict()
        self._opening: dict[str, Future[Archive]] = {}
        self._lock = threading.Lock()

    def __len__(self) -> int:
        with self._lock:
            return len(self._archives)

    def get(self, file: str) -> Archive:
        got = self.get_or_wait(file)
        return got.result() if isinstance(got, Future) else got

    def get_or_wait(self, file: str) -> Archive | Future[Archive]:
        """The archive, opened by this call if nobody is opening it; else the future of the open in
        progress, which the caller must not block a thread on (``ZimWorker``: a cold open of a large
        archive over SMB takes minutes, and each request waiting on it held one of the worker's few
        threads, so every other archive of that worker timed out: 2026-10-16 field exam)."""
        with self._lock:
            hit = self._archives.get(file)
            if hit is not None:
                self._archives.move_to_end(file)
                return hit
            pending = self._opening.get(file)
            if pending is not None:
                return pending
            pending = self._opening[file] = Future()
        try:
            archive = self._open(file)
        except BaseException as exc:
            with self._lock:
                del self._opening[file]
            pending.set_exception(exc)
            raise
        with self._lock:
            del self._opening[file]
            self._archives[file] = archive
            while len(self._archives) > self._max:
                self._archives.popitem(last=False)
        pending.set_result(archive)
        return archive


class InProcessZimReader:
    """:class:`ZimReader` in the calling process (tests, the sample backend, the in-process CLI)."""

    def __init__(self, *, max_open: int = 16, opener: Callable[[str], Archive] = open_archive) -> None:
        self._cache = ArchiveCache(max_open, opener)
        self._opener = opener

    def read(self, file: str, path: str, want: Want) -> EntryData | None:
        return read_entry(self._cache.get(file), path, want)

    def book_name(self, file: str) -> str:
        # Not cached: a bundle has hundreds of books and only their names are needed here.
        return book_name_of(self._opener(file), file)

    def warm(self, file: str) -> int | None:
        return page_count(self._cache.get(file))

    def random(self, file: str) -> tuple[str, str] | None:
        return random_article(self._cache.get(file))

    def close(self) -> None:
        pass
