import queue
from pathlib import Path
from typing import Any, cast

import pyarrow as pa

from wsp_oracle import embedbuild


class _Reader:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows

    def scan(self, columns: Any, *, batch_size: int = 2) -> Any:
        for i in range(0, len(self.rows), batch_size):
            part = self.rows[i : i + batch_size]
            yield pa.RecordBatch.from_pylist([{c: r[c] for c in columns} for r in part])


class _Store:
    def doc_hashes(self, ids: list[str]) -> dict[str, str]:
        return {}

    def touch_docs(self, ids: list[str], generation_id: str) -> None:
        pass


def row(doc_id: str, lang: str) -> dict[str, Any]:
    return {
        "doc_id": doc_id,
        "source_id": "w",
        "title": doc_id,
        "text": "word " * 50,
        "content_hash": doc_id,
        "lang": lang,
    }


def test_language_filter_skips_whole_batches_without_stopping(tmp_path: Path) -> None:
    rows = [row("d1", "de"), row("d2", "de"), row("e1", "en"), row("d3", "de"), row("e2", "en")]
    out: queue.Queue[Any] = queue.Queue()
    stats = embedbuild.EmbedStats(source_id="w", generation_id="g", total_docs=len(rows))
    embedbuild._produce(  # pyright: ignore[reportPrivateUsage]
        cast(Any, _Reader(rows)),
        cast(Any, _Store()),
        stats,
        out,
        generation_id="g",
        words=200,
        max_chunks=4,
        limit=None,
        pool=None,
        batch_docs=2,
        langs=frozenset({"en"}),
    )
    docs: list[str] = []
    while (item := out.get()) is not None:
        docs += [d.doc_id for d in item]
    assert docs == ["e1", "e2"]
