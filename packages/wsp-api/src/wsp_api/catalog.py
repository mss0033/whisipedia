"""Default :class:`~wsp_api.seams.Catalog`: derived by replaying the change feed.

It follows the feed incrementally (each call first catches up from the last cursor), keeps the set of
live documents per source and a bounded tail of recent changes. The first call replays the whole feed,
which is fine for the sample library and small installs. TODO(integration): replace with an
index-backed catalog (doc counts, random pages and categories straight from the index).
"""

from __future__ import annotations

import random
import threading
from collections import deque
from collections.abc import Sequence

from wsp_core.ids import DocId
from wsp_core.model import Category, ChangeKind, ChangeRecord
from wsp_core.ports import ChangeFeed

_BATCH = 5000


class FeedCatalog:
    def __init__(self, feed: ChangeFeed, *, recent_size: int = 2000) -> None:
        self._feed = feed
        self._cursor: str | None = None
        self._live: dict[str, list[DocId]] = {}
        self._pos: dict[DocId, int] = {}
        self._recent: deque[ChangeRecord] = deque(maxlen=recent_size)
        self._lock = threading.Lock()

    def _catch_up(self) -> None:
        while True:
            records, cursor = self._feed.since(self._cursor, limit=_BATCH)
            self._cursor = cursor
            for rec in records:
                self._apply(rec)
            if len(records) < _BATCH:
                return

    def _apply(self, rec: ChangeRecord) -> None:
        self._recent.append(rec)
        if rec.kind is ChangeKind.REMOVED:
            self._remove(rec.doc_id)
            return
        if rec.kind is ChangeKind.RENAMED and rec.renamed_from is not None:
            self._remove(rec.renamed_from)
        if rec.doc_id not in self._pos:
            ids = self._live.setdefault(_source_of(rec.doc_id), [])
            self._pos[rec.doc_id] = len(ids)
            ids.append(rec.doc_id)

    def _remove(self, doc_id: DocId) -> None:
        i = self._pos.pop(doc_id, None)
        if i is None:
            return
        ids = self._live[_source_of(doc_id)]
        last = ids.pop()
        if last != doc_id:  # swap-remove keeps it O(1)
            ids[i] = last
            self._pos[last] = i

    # --- Catalog ---

    def doc_count(self, source_id: str) -> int | None:
        with self._lock:
            self._catch_up()
            return len(self._live.get(source_id, ()))

    def random_doc(self, *, sources: Sequence[str] | None, rng: random.Random) -> DocId | None:
        with self._lock:
            self._catch_up()
            pools = (
                [self._live.get(s, []) for s in sorted(sources)]
                if sources
                else [self._live[s] for s in sorted(self._live)]
            )
            total = sum(len(p) for p in pools)
            if total == 0:
                return None
            n = rng.randrange(total)
            for pool in pools:
                if n < len(pool):
                    return pool[n]
                n -= len(pool)
            return None  # pragma: no cover - unreachable

    def recent(
        self, *, limit: int, sources: Sequence[str] | None = None
    ) -> tuple[Sequence[ChangeRecord], str | None]:
        with self._lock:
            self._catch_up()
            wanted = set(sources) if sources else None
            out: list[ChangeRecord] = []
            for rec in reversed(self._recent):
                if wanted is None or _source_of(rec.doc_id) in wanted:
                    out.append(rec)
                    if len(out) >= limit:
                        break
            return out, self._cursor

    def categories(self, source_id: str) -> Sequence[Category]:
        return ()

    def category_docs(self, source_id: str, slug: str, *, limit: int) -> Sequence[DocId]:
        return ()


def _source_of(doc_id: str) -> str:
    return doc_id.removeprefix("wsp:").partition("/")[0]
