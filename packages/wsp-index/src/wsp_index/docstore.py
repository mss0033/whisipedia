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
    is_sealed,
    sealed_generations,
)


class ParquetDocumentStore:
    """Reads documents from ``<root>/<source_id>/<generation_id>/`` Refinery generations.

    * ``get(doc_id)`` reads the source's current generation (``CURRENT.txt`` or the newest sealed
      one). A document tombstoned upstream is absent there and returns ``None``; its content stays
      reachable with ``at_generation``, which selects the newest generation at or before it.
    * ``history(doc_id)`` lists content versions oldest first: one entry per generation in which the
      content hash differs from the previous generation (a re-appearance after removal counts).

    Readers are cached (LRU) because loading a generation's id index costs ~16 bytes per document.
    """

    def __init__(self, root: Path, *, max_open_generations: int = 16) -> None:
        self.root = root
        self._readers: OrderedDict[tuple[str, str], RefineryReader] = OrderedDict()
        self._max = max_open_generations
        self._lock = threading.Lock()

    def reader(self, source_id: str, generation_id: str) -> RefineryReader | None:
        key = (source_id, generation_id)
        with self._lock:
            cached = self._readers.get(key)
            if cached is not None:
                self._readers.move_to_end(key)
                return cached
        gen_dir = generation_dir(self.root, source_id, generation_id)
        if not is_sealed(gen_dir):
            return None
        reader = RefineryReader(gen_dir)
        with self._lock:
            self._readers[key] = reader
            while len(self._readers) > self._max:
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
        reader = self.reader(source_id, gen)
        return None if reader is None else reader.get(doc_id)

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
