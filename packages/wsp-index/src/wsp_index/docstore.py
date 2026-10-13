"""``DocumentStore`` over Refinery Parquet (backs ``/v1/documents`` and ``/history``, PLAN §11.1)."""

from __future__ import annotations

import bisect
import threading
from collections import OrderedDict
from collections.abc import Sequence
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
        + 3 * 1024 * 1024 * reader.max_open_files
        + 4 * 1024 * 1024 * reader.cache_row_groups
    )


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
    ) -> None:
        self.root = root
        self.map_root = map_root
        self._readers: OrderedDict[tuple[str, str, bool], RefineryReader] = OrderedDict()
        self._max = max_open_generations
        self._budget = memory_budget
        self._lock = threading.Lock()

    def reader(
        self, source_id: str, generation_id: str, *, current: bool | None = None
    ) -> RefineryReader | None:
        """A cached reader (``current``: whether ``generation_id`` is the source's current generation,
        looked up when not given). Only the current generation gets an id-map sidecar: older ones are
        read for history and dated requests, and wrote ~20 bytes a document each from a GET (audit
        2026-10-12 #12)."""
        if current is None:
            current = generation_id == current_generation(self.root, source_id)
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
            gen = current_generation(self.root, source_id)
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
        out: dict[str, str] = {}
        for doc_id in doc_ids:
            try:
                source_id, _ = parse_doc_id(DocId(doc_id))
            except InvalidDocIdError:
                continue
            gen = current_generation(self.root, source_id)
            reader = None if gen is None else self.reader(source_id, gen, current=True)
            row = None if reader is None else reader.lookup(doc_id, ("text",))
            text = None if row is None else row.get("text")
            if isinstance(text, str):
                out[doc_id] = text
        return out

    def content_hashes(self, doc_ids: Sequence[str]) -> dict[str, str]:
        """Current content hash of each document that has one (tells duplicate records of one text from
        distinct documents sharing a title). Reads only the ``content_hash`` column."""
        out: dict[str, str] = {}
        for doc_id in doc_ids:
            try:
                source_id, _ = parse_doc_id(DocId(doc_id))
            except InvalidDocIdError:
                continue
            gen = current_generation(self.root, source_id)
            reader = None if gen is None else self.reader(source_id, gen, current=True)
            row = None if reader is None else reader.lookup(doc_id, ("content_hash",))
            digest = None if row is None else row.get("content_hash")
            if isinstance(digest, str):
                out[doc_id] = digest
        return out

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
