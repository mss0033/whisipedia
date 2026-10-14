"""``DocumentStore`` over Refinery Parquet (backs ``/v1/documents`` and ``/history``, PLAN §11.1)."""

from __future__ import annotations

import bisect
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import cast

from wsp_core.ids import DocId, InvalidDocIdError, parse_doc_id
from wsp_core.model import DocRecord
from wsp_index.refinery import (
    RefineryReader,
    current_generation,
    generation_dir,
    id_map_dir,
    is_sealed,
    sealed_generations,
)


def _reader_cost(reader: RefineryReader) -> int:
    """Upper estimate of a reader's anonymous memory: the id index (20 B/doc unless memory-mapped), open
    file footers (~2.5 MB each) and the row-group cache (~4 MB of text each, by the writer's sizing)."""
    return (
        reader.id_index_bytes
        + reader.id_rows_bytes
        + 3 * 1024 * 1024 * reader.max_open_files
        + 4 * 1024 * 1024 * reader.cache_row_groups
    )


LOOKUP_WORKERS = 8
"""Lookups of one search page run side by side (:meth:`ParquetDocumentStore.texts`)."""

CURRENT_TTL = 5.0


class ParquetDocumentStore:
    """Reads documents from ``<root>/<source_id>/<generation_id>/`` Refinery generations.

    * ``get(doc_id)`` reads the source's current generation (``CURRENT.txt`` or the newest sealed
      one). A document tombstoned upstream is absent there and returns ``None``; its content stays
      reachable with ``at_generation``, which selects the newest generation at or before it.
    * ``history(doc_id)`` lists content versions oldest first: one entry per generation in which the
      content hash differs from the previous generation (a re-appearance after removal counts).

    Readers are cached (LRU) because loading a generation's id index costs ~20 bytes per document.
    The cache is bounded by an estimate of their memory (``memory_budget`` bytes: id index, open file
    footers, cached row groups), not just by count: one wikipedia-top10 reader is ~0.8 GB, a small
    source's a few MB, so a count bound alone let the API reach 5 GB (audit api-cli #3).

    ``map_root``: a local directory for the id-map sidecars (:func:`~wsp_index.refinery.id_map_dir`),
    for a Refinery on the NAS (ADR-0011). Without it they live next to each generation.
    """

    def __init__(
        self,
        root: Path,
        *,
        max_open_generations: int = 16,
        memory_budget: int = 1536 * 1024 * 1024,
        map_root: Path | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.root = root
        self.map_root = map_root
        self._monotonic = monotonic
        self._current: dict[str, tuple[float, str | None]] = {}  # source -> (asked at, generation)
        self._readers: OrderedDict[tuple[str, str, bool], RefineryReader] = OrderedDict()
        self._max = max_open_generations
        self._budget = memory_budget
        self._lock = threading.Lock()

    def current(self, source_id: str) -> str | None:
        """The source's current generation, remembered for :data:`CURRENT_TTL` seconds: finding it is
        three metadata round trips on the NAS, and a search page asked per hit (texts, fields,
        content hashes). A generation goes live at most once a night."""
        now = self._monotonic()
        with self._lock:
            hit = self._current.get(source_id)
            if hit is not None and now - hit[0] < CURRENT_TTL:
                return hit[1]
        gen = current_generation(self.root, source_id)
        with self._lock:
            self._current[source_id] = (now, gen)
        return gen

    def reader(
        self, source_id: str, generation_id: str, *, current: bool | None = None
    ) -> RefineryReader | None:
        """A cached reader (``current``: whether ``generation_id`` is the source's current generation,
        looked up when not given). Only the current generation gets an id-map sidecar: older ones are
        read for history and dated requests, and wrote ~20 bytes a document each from a GET (audit
        2026-10-12 #12)."""
        if current is None:
            current = generation_id == self.current(source_id)
        key = (source_id, generation_id, current)
        with self._lock:
            cached = self._readers.get(key)
            if cached is not None:
                self._readers.move_to_end(key)
                return cached
        gen_dir = generation_dir(self.root, source_id, generation_id)
        if not is_sealed(gen_dir):
            return None
        map_dir = None if self.map_root is None else id_map_dir(self.map_root, gen_dir)
        reader = RefineryReader(gen_dir, map_ids=current, map_dir=map_dir)
        with self._lock:
            self._readers[key] = reader
            while len(self._readers) > 1 and (
                len(self._readers) > self._max
                or sum(map(_reader_cost, self._readers.values())) > self._budget
            ):
                self._readers.popitem(last=False)
        return reader

    def generations(self, source_id: str) -> list[str]:
        return sealed_generations(self.root, source_id)

    def get(self, doc_id: DocId, *, at_generation: str | None = None) -> DocRecord | None:
        try:
            source_id, _ = parse_doc_id(doc_id)
        except InvalidDocIdError:
            return None
        if at_generation is None:
            gen = self.current(source_id)
        else:
            gens = self.generations(source_id)
            i = bisect.bisect_right(gens, at_generation)
            gen = gens[i - 1] if i else None
        if gen is None:
            return None
        reader = self.reader(source_id, gen, current=True if at_generation is None else None)
        return None if reader is None else reader.get(doc_id)

    def texts(self, doc_ids: Sequence[str]) -> dict[str, str]:
        """Current text of each document that has one (search snippets when the lexical index stores no
        text). Reads only the ``text`` column of each document's row group (cached per reader)."""
        return {d: t for d, t in self._column_many(doc_ids, "text").items() if isinstance(t, str)}

    def _column_many(self, doc_ids: Sequence[str], column: str) -> dict[str, object]:
        """One column of the current version of each document, the lookups run side by side.

        A search page asks for fifty passages and the content hashes of repeated titles; one by one
        over the NAS that was 2-4 s of a query (each lookup a row-group read of 30-80 ms, measured
        2026-10-13). Readers are safe to share between threads; the pool is bounded so a page cannot
        flood the link."""
        wanted = list(dict.fromkeys(doc_ids))
        if not wanted:
            return {}

        def one(doc_id: str) -> tuple[str, object] | None:
            try:
                source_id, _ = parse_doc_id(DocId(doc_id))
            except InvalidDocIdError:
                return None
            gen = self.current(source_id)
            reader = None if gen is None else self.reader(source_id, gen, current=True)
            row = None if reader is None else reader.lookup(doc_id, (column,))
            return None if row is None else (doc_id, row.get(column))

        if len(wanted) == 1:
            found = one(wanted[0])
            return {} if found is None else {found[0]: found[1]}
        with ThreadPoolExecutor(min(LOOKUP_WORKERS, len(wanted)), thread_name_prefix="wsp-lookup") as pool:
            return dict(f for f in pool.map(one, wanted) if f is not None)

    def content_hashes(self, doc_ids: Sequence[str]) -> dict[str, str]:
        """Current content hash of each document that has one (tells duplicate records of one text from
        distinct documents sharing a title). Reads only the ``content_hash`` column."""
        return {d: h for d, h in self._column_many(doc_ids, "content_hash").items() if isinstance(h, str)}

    def history(self, doc_id: DocId) -> Sequence[tuple[str, str]]:
        try:
            source_id, _ = parse_doc_id(doc_id)
        except InvalidDocIdError:
            return []
        out: list[tuple[str, str]] = []
        previous: str | None = None  # hash in the immediately preceding generation (None if absent)
        for gen in self.generations(source_id):
            reader = self.reader(source_id, gen)
            row = None if reader is None else reader.lookup(doc_id, ("content_hash",))
            current = None if row is None else cast(str, row["content_hash"])
            if current is not None and current != previous:
                out.append((gen, current))
            previous = current
        return out
