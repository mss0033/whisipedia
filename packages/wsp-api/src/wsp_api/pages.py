"""Rendered document pages, cached by bytes (audit 2026-10-11 #8).

Rendering a ZIM page means reading the entry (over SMB, in a ZIM worker), rewriting its links (77 ms
for Alan_Turing, 221 ms for United_States in pure Python) and extracting its text. A page of a sealed
generation never changes, so each is rendered once per ``(source, generation, entry)`` and kept in a
least-recently-used cache bounded by size. Concurrent requests for the same page share one render.
"""

from __future__ import annotations

import html as htmllib
import re
import sys
import threading
from collections import OrderedDict
from collections.abc import Callable, Hashable
from concurrent.futures import Future
from dataclasses import dataclass

from wsp_core.ids import content_hash

_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_CODE_RE = re.compile(r"<(script|style|noscript)\b[^>]*>.*?</\1\s*>", re.IGNORECASE | re.DOTALL)
_WS_RE = re.compile(r"\s+")


def page_title(html: str) -> str:
    match = _TITLE_RE.search(html)
    return htmllib.unescape(match.group(1)).strip() if match else ""


def page_text(html: str) -> str:
    """Readable text of a page: no title, scripts or styles (E2E-9)."""
    body = _CODE_RE.sub(" ", _TITLE_RE.sub(" ", html))
    return _WS_RE.sub(" ", htmllib.unescape(_TAG_RE.sub(" ", body))).strip()


_MATH_RE = re.compile(r"<math\b[^>]*?\balttext=\"([^\"]*)\"[^>]*>.*?</math>", re.IGNORECASE | re.DOTALL)
_MATH_ANY_RE = re.compile(r"<math\b.*?</math>", re.IGNORECASE | re.DOTALL)
_BLOCK_RE = re.compile(
    r"<(?:/p|br\s*/?|/h[1-6]|/li|/tr|/dd|/dt|/blockquote|/pre|/div|/table|/caption)\b[^>]*>", re.IGNORECASE
)
_DISPLAY_RE = re.compile(r"^\{\\displaystyle\s*(.*)\}$", re.DOTALL)


def _tex(alttext: str) -> str:
    tex = htmllib.unescape(alttext).strip()
    m = _DISPLAY_RE.match(tex)
    return f" {(m.group(1) if m else tex).strip()} "


def rendered_text(html: str) -> str:
    """The text a reader sees on a rendered page, paragraphs kept, each formula as its TeX source
    (the alttext of the MathML). The text of the Wikipedia dumps drops formulas and measurement
    templates; the ZIM page that renders the article has them (2026-10-14 field exam)."""
    body = _CODE_RE.sub(" ", _TITLE_RE.sub(" ", html))
    body = _MATH_RE.sub(lambda m: _tex(m.group(1)), body)
    body = _MATH_ANY_RE.sub(" ", body)
    body = _BLOCK_RE.sub("\n", body)
    text = htmllib.unescape(_TAG_RE.sub(" ", body))
    lines = (_WS_RE.sub(" ", line).strip() for line in text.split("\n"))
    return "\n".join(line for line in lines if line)


@dataclass(frozen=True, slots=True)
class Rendered:
    """A page ready to serve: rewritten HTML plus what a live ZIM document needs (title, text, hash)."""

    html: str
    title: str
    text: str
    content_hash: str

    @property
    def nbytes(self) -> int:
        return sys.getsizeof(self.html) + sys.getsizeof(self.text) + sys.getsizeof(self.title) + 128


def render(original: str, rewrite: Callable[[str], str]) -> Rendered:
    text = page_text(original)
    return Rendered(rewrite(original), page_title(original), text, content_hash(text))


class ByteLRU[K: Hashable, V]:
    """Least-recently-used values, bounded by their total size; ``get_or_build`` is single-flight."""

    def __init__(self, max_bytes: int, size: Callable[[V], int]) -> None:
        self.max_bytes = max_bytes
        self._size = size
        self._items: OrderedDict[K, tuple[V, int]] = OrderedDict()
        self._building: dict[K, Future[V | None]] = {}
        self._bytes = 0
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    @property
    def nbytes(self) -> int:
        with self._lock:
            return self._bytes

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)

    def get(self, key: K) -> V | None:
        with self._lock:
            found = self._items.get(key)
            if found is None:
                return None
            self._items.move_to_end(key)
            self.hits += 1
            return found[0]

    def put(self, key: K, value: V) -> None:
        n = self._size(value)
        if n > self.max_bytes // 4:  # one huge page must not flush everything else
            return
        with self._lock:
            old = self._items.pop(key, None)
            if old is not None:
                self._bytes -= old[1]
            self._items[key] = (value, n)
            self._bytes += n
            while self._bytes > self.max_bytes and self._items:
                _, (_, freed) = self._items.popitem(last=False)
                self._bytes -= freed

    def get_or_build(self, key: K, build: Callable[[], V | None]) -> V | None:
        """The cached value, or ``build()`` once however many threads ask at the same time. A failed
        or ``None`` build is not cached (no negative entries)."""
        with self._lock:
            found = self._items.get(key)
            if found is not None:
                self._items.move_to_end(key)
                self.hits += 1
                return found[0]
            pending = self._building.get(key)
            mine = pending is None
            if pending is None:
                pending = self._building[key] = Future()
                self.misses += 1
        if not mine:
            return pending.result()
        try:
            value = build()
        except BaseException as exc:
            with self._lock:
                del self._building[key]
            pending.set_exception(exc)
            raise
        if value is not None:
            self.put(key, value)
        with self._lock:
            del self._building[key]
        pending.set_result(value)
        return value
