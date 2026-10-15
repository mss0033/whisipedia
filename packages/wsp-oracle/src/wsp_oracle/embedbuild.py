"""Embed Refinery generations into the passage vector store: resumable, incremental, with progress.

    uv run --extra gpu wsp-oracle-embed --sources wikipedia-en-zim
    uv run python packages/wsp-oracle/scripts/embed_sources.py --sources wikipedia-en-zim

For each source the current sealed Refinery generation under ``<index.root>/refinery`` is read in
batches; documents whose ``content_hash`` the store already holds are skipped (that is what makes
a re-run resume, and a new generation cost only its changed documents), changed documents are
re-chunked and replaced, and documents the new generation no longer has are deleted at the end.
Rows and ids are saved every ``save_every`` passages, so an interrupted run loses at most that much.

Reading and chunking run in a producer thread while the GPU embeds the previous batch (the chunker
does ~48k chunks/s on one core; the old process pool forked after CUDA had started). The producer
reads only ids, hashes and languages first and reads the text of changed documents only. The HNSW
graph is built once at the end of a run.
"""

from __future__ import annotations

import argparse
import logging
import queue
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import numpy as np
import numpy.typing as npt
import pyarrow as pa
import pyarrow.compute as _pc

from wsp_core.ports import Embedder
from wsp_index import RefineryReader
from wsp_index.refinery import configured_root, current_generation, generation_dir
from wsp_index.text import language_parts
from wsp_oracle.chunker import Chunk, chunk_text, is_wiki_like, make_chunk_id
from wsp_oracle.embedder import encode_passages
from wsp_oracle.vectors import ChunkVectorStore

log = logging.getLogger("wsp_oracle.embed")
pc: Any = _pc

_STATE_COLUMNS = ("doc_id", "content_hash", "lang")
_DOC_COLUMNS = ("doc_id", "source_id", "title", "text", "content_hash", "lang")


@dataclass(slots=True)
class EmbedStats:
    source_id: str = ""
    generation_id: str = ""
    docs_seen: int = 0
    docs_embedded: int = 0
    docs_unchanged: int = 0
    docs_removed: int = 0
    chunks: int = 0
    total_docs: int = 0
    started: float = field(default_factory=time.monotonic)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def add(self, *, seen: int = 0, embedded: int = 0, unchanged: int = 0, chunks: int = 0) -> None:
        """Counters are updated from the producer and the consumer thread."""
        with self._lock:
            self.docs_seen += seen
            self.docs_embedded += embedded
            self.docs_unchanged += unchanged
            self.chunks += chunks

    @property
    def seconds(self) -> float:
        return time.monotonic() - self.started

    @property
    def chunks_per_s(self) -> float:
        return self.chunks / self.seconds if self.seconds > 0 else 0.0

    def line(self) -> str:
        pct = f"{100 * self.docs_seen / self.total_docs:5.1f}%" if self.total_docs else "  ?  "
        eta = ""
        if self.docs_seen and self.total_docs:
            rest = self.seconds * (self.total_docs - self.docs_seen) / self.docs_seen
            eta = f" eta {rest / 3600:.1f}h"
        return (
            f"{self.source_id} {pct} docs {self.docs_seen:,}/{self.total_docs:,} "
            f"(embedded {self.docs_embedded:,}, unchanged {self.docs_unchanged:,}) "
            f"chunks {self.chunks:,} @ {self.chunks_per_s:,.0f}/s{eta}"
        )


@dataclass(frozen=True, slots=True)
class _Doc:
    doc_id: str
    source_id: str
    content_hash: str
    lang: str
    replace: bool
    chunks: tuple[Chunk, ...]


PROSE_LETTER_SHARE = 0.45


def is_prose(text: str) -> bool:
    """A passage worth a vector: letters are at least :data:`PROSE_LETTER_SHARE` of its visible
    characters. Tables of numbers (sight reduction, tide and almanac volumes) embed near every query
    and crowded out real passages: 337,216 NGA table chunks matched "pressure canning green beans" at
    0.78 above every USDA guide (2026-10-14)."""
    visible = [ch for ch in text if not ch.isspace()]
    if not visible:
        return False
    return sum(ch.isalpha() for ch in visible) >= PROSE_LETTER_SHARE * len(visible)


def _chunk_one(title: str, text: str, words: int, max_chunks: int, wiki: bool) -> list[tuple[str, str]]:
    return chunk_text(title, text, target_words=words, max_chunks=max_chunks, stop_at_references=wiki) or [
        ("", title)
    ]


def _wanted(langs: pa.Array[Any], wanted: frozenset[str] | None) -> npt.NDArray[np.bool_]:
    if wanted is None:
        return np.ones(len(langs), dtype=bool)
    mask = pc.is_in(langs, value_set=pa.array(sorted(wanted), type=pa.string()))
    return np.asarray(mask.to_numpy(zero_copy_only=False), dtype=bool)


def count_docs(reader: RefineryReader, langs: frozenset[str] | None) -> int:
    """Documents an embed run will look at (the ETA's denominator; reads only the ``lang`` column)."""
    if langs is None:
        return len(reader)
    return sum(r for lang, parts in language_parts(reader).items() if lang in langs for _, r in parts)


def _produce(
    reader: RefineryReader,
    store: ChunkVectorStore,
    stats: EmbedStats,
    out: queue.Queue[list[_Doc] | BaseException | None],
    *,
    generation_id: str,
    words: int,
    max_chunks: int,
    limit: int | None,
    batch_docs: int,
    langs: frozenset[str] | None = None,
) -> None:
    """Per part: read only ids, hashes and languages; skip unchanged documents; read and chunk the
    rest (title and text of their row groups only) in this thread (~48k chunks/s on one core)."""
    try:
        seen = 0
        wiki = is_wiki_like(reader.source_id)
        for part in range(reader.n_parts):
            if limit is not None and seen >= limit:
                break
            state, positions = reader.part_table(part, _STATE_COLUMNS)
            keep = _wanted(state.column("lang").combine_chunks(), langs)
            ids_all = cast(list[str], state.column("doc_id").to_pylist())
            hashes_all = cast(list[str], state.column("content_hash").to_pylist())
            rows = np.flatnonzero(keep)
            if limit is not None:
                rows = rows[: max(0, limit - seen)]
            for start in range(0, len(rows), batch_docs):
                sel = cast(list[int], rows[start : start + batch_docs].tolist())
                ids = [ids_all[i] for i in sel]
                seen += len(ids)
                known = store.doc_hashes(ids)
                todo = [i for i, d in zip(sel, ids, strict=True) if known.get(d) != hashes_all[i]]
                unchanged = [d for i, d in zip(sel, ids, strict=True) if known.get(d) == hashes_all[i]]
                if unchanged:
                    store.touch_docs(unchanged, generation_id)
                stats.add(seen=len(unchanged), unchanged=len(unchanged))
                if not todo:
                    continue
                table = reader.part_rows(part, positions[np.asarray(todo, dtype=np.int64)], _DOC_COLUMNS)
                docs: list[_Doc] = []
                for r in table.to_pylist():
                    doc_id, title = str(r["doc_id"]), str(r["title"])
                    pairs = _chunk_one(title, str(r["text"]), words, max_chunks, wiki)
                    numbered = [
                        Chunk(make_chunk_id(doc_id, n), doc_id, n, title, sec, txt)
                        for n, (sec, txt) in enumerate(pairs)
                    ]
                    # Numbers are kept by position (passage ids stay stable); tables go, one stays.
                    chunks = tuple(c for c in numbered if is_prose(c.text)) or tuple(numbered[:1])
                    docs.append(
                        _Doc(
                            doc_id,
                            str(r["source_id"]),
                            str(r["content_hash"]),
                            str(r["lang"]),
                            doc_id in known,
                            chunks,
                        )
                    )
                out.put(docs)
        out.put(None)
    except BaseException as exc:  # handed to the consumer, which re-raises
        out.put(exc)


def embed_generation(
    reader: RefineryReader,
    store: ChunkVectorStore,
    embedder: Embedder,
    *,
    source_id: str,
    generation_id: str,
    chunk_words: int = 200,
    max_chunks_per_doc: int = 0,
    batch_chunks: int = 4096,
    batch_docs: int = 1024,
    save_every: int = 250_000,
    limit: int | None = None,
    langs: frozenset[str] | None = None,
    progress: Callable[[EmbedStats], None] | None = None,
    progress_every_s: float = 10.0,
) -> EmbedStats:
    """Bring ``store`` up to date with one Refinery generation of ``source_id``.

    The graph is built once at the end (``ChunkVectorStore.build_graph``); ``save_every`` passages
    only make rows and ids durable, so an interrupted run loses at most that much and resumes.
    With ``limit`` only the first ``limit`` documents are considered and nothing is deleted; with
    ``langs`` only documents of those languages are embedded or removed.
    """
    total = limit or count_docs(reader, langs)
    stats = EmbedStats(source_id=source_id, generation_id=generation_id, total_docs=total)
    q: queue.Queue[list[_Doc] | BaseException | None] = queue.Queue(maxsize=4)
    producer = threading.Thread(
        target=_produce,
        args=(reader, store, stats, q),
        kwargs={
            "generation_id": generation_id,
            "words": chunk_words,
            "max_chunks": max_chunks_per_doc,
            "limit": limit,
            "batch_docs": batch_docs,
            "langs": langs,
        },
        daemon=True,
        name="embed-producer",
    )
    deferred = store.defer_graph
    store.defer_graph = True
    producer.start()
    pending: list[_Doc] = []
    pending_chunks = 0
    since_save = 0
    last_report = time.monotonic()

    def flush() -> None:
        nonlocal pending_chunks, since_save
        if not pending:
            return
        replaced = [d.doc_id for d in pending if d.replace]
        if replaced:
            store.delete_docs(replaced)
        chunks = [c for d in pending for c in d.chunks]
        mat = encode_passages(embedder, [c.embed_text() for c in chunks], store.dimensions)
        store.add_chunks(chunks, mat)
        store.record_docs(
            (d.doc_id, d.source_id, d.content_hash, generation_id, len(d.chunks), d.lang) for d in pending
        )
        stats.add(seen=len(pending), embedded=len(pending), chunks=len(chunks))
        since_save += len(chunks)
        pending.clear()
        pending_chunks = 0
        if since_save >= save_every:
            store.save()
            since_save = 0

    try:
        while True:
            item = q.get()
            if item is None:
                break
            if isinstance(item, BaseException):
                raise item
            for doc in item:
                pending.append(doc)
                pending_chunks += len(doc.chunks)
                if pending_chunks >= batch_chunks:
                    flush()
            if progress is not None and time.monotonic() - last_report >= progress_every_s:
                progress(stats)
                last_report = time.monotonic()
        flush()
        if limit is None:
            stale = store.stale_docs(source_id, generation_id, langs)
            if stale:
                store.delete_docs(stale)
                stats.docs_removed = len(stale)
        store.save()
    finally:
        store.defer_graph = deferred
    store.build_graph()
    if progress is not None:
        progress(stats)
    return stats


# --- CLI ------------------------------------------------------------------------------------------


def _find_config_dir() -> Path:
    here = Path.cwd().resolve()
    for parent in (here, *here.parents):
        if (parent / "config" / "default.toml").is_file():
            return parent / "config"
    return here / "config"


def main(argv: Sequence[str] | None = None) -> int:
    from wsp_core.config import load_config
    from wsp_oracle.config import OracleConfig
    from wsp_oracle.services import open_embedder, open_vector_store

    ap = argparse.ArgumentParser(description="Embed Refinery documents into the Oracle's vector store.")
    ap.add_argument("--config-dir", type=Path, default=None)
    ap.add_argument(
        "--sources", default="", help="comma-separated source ids (default: oracle.dense_sources)"
    )
    ap.add_argument("--generation", default="", help="Refinery generation (default: the current one)")
    ap.add_argument(
        "--refinery", type=Path, default=None, help="Refinery root (default: <index.root>/refinery)"
    )
    ap.add_argument(
        "--vectors", type=Path, default=None, help="store directory (default: oracle.vectors.path)"
    )
    ap.add_argument("--limit", type=int, default=None, help="only the first N documents (benchmarks)")
    ap.add_argument("--langs", default="", help="comma-separated language codes to embed (default: all)")
    ap.add_argument("--batch-chunks", type=int, default=4096)
    ap.add_argument("--device", default="", help="override oracle.embedder.device")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    raw = load_config(args.config_dir or _find_config_dir())
    section = raw.get("oracle", {})
    ocfg = OracleConfig.from_mapping(cast(Mapping[str, Any], section) if isinstance(section, Mapping) else {})
    index = raw.get("index", {})
    root_value = cast(Mapping[str, Any], index).get("root") if isinstance(index, Mapping) else None
    index_root = Path(str(root_value)).expanduser() if root_value else None
    refinery_value = (
        cast(Mapping[str, Any], index).get("refinery_root") if isinstance(index, Mapping) else None
    )
    refinery = args.refinery or (configured_root(index_root, refinery_value) if index_root else None)
    if refinery is None or not refinery.is_dir():
        print(f"no Refinery at {refinery} (set index.root or --refinery)", file=sys.stderr)
        return 2
    sources = [s for s in (args.sources.split(",") if args.sources else ocfg.dense_sources) if s]
    if not sources:
        print("no sources: pass --sources or set oracle.dense_sources", file=sys.stderr)
        return 2
    embedder = open_embedder(ocfg, device=args.device or None)
    store = open_vector_store(ocfg, index_root, dimensions=embedder.dimensions, path=args.vectors)
    try:
        for source_id in sources:
            gen = args.generation or current_generation(refinery, source_id)
            if gen is None:
                print(f"{source_id}: no sealed Refinery generation under {refinery}", file=sys.stderr)
                continue
            reader = RefineryReader(generation_dir(refinery, source_id, gen))
            log.info(
                "%s: embedding generation %s (%d documents) into %s", source_id, gen, len(reader), store.path
            )
            stats = embed_generation(
                reader,
                store,
                embedder,
                source_id=source_id,
                generation_id=gen,
                chunk_words=ocfg.chunk_words,
                max_chunks_per_doc=ocfg.max_chunks_per_doc,
                batch_chunks=args.batch_chunks,
                limit=args.limit,
                langs=frozenset(x for x in args.langs.split(",") if x) or None,
                progress=lambda s: log.info(s.line()),
            )
            log.info(
                "%s done: %s; removed %d; %.0f s", source_id, stats.line(), stats.docs_removed, stats.seconds
            )
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
