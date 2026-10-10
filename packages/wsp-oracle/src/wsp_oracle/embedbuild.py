"""Embed Refinery generations into the passage vector store: resumable, incremental, with progress.

    uv run --extra gpu wsp-oracle-embed --sources wikipedia-en-zim
    uv run python packages/wsp-oracle/scripts/embed_sources.py --sources wikipedia-en-zim

For each source the current sealed Refinery generation under ``<index.root>/refinery`` is read in
batches; documents whose ``content_hash`` the store already holds are skipped (that is what makes
a re-run resume, and a new generation cost only its changed documents), changed documents are
re-chunked and replaced, and documents the new generation no longer has are deleted at the end.
The store is saved every ``save_every`` passages, so an interrupted run loses at most that much.

Reading and chunking run in a producer thread (and in ``--workers`` processes) while the GPU
embeds the previous batch.
"""

from __future__ import annotations

import argparse
import logging
import queue
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Executor, ProcessPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from wsp_core.ports import Embedder
from wsp_index import RefineryReader
from wsp_index.refinery import current_generation, generation_dir
from wsp_oracle.chunker import Chunk, chunk_text, make_chunk_id
from wsp_oracle.embedder import encode_passages
from wsp_oracle.vectors import ChunkVectorStore

log = logging.getLogger("wsp_oracle.embed")

_COLUMNS = ("doc_id", "source_id", "title", "text", "content_hash")


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
    replace: bool
    chunks: tuple[Chunk, ...]


def _chunk_one(args: tuple[str, str, str, int, int]) -> list[tuple[str, str]]:
    title, text, _doc_id, words, max_chunks = args
    return chunk_text(title, text, target_words=words, max_chunks=max_chunks) or [("", title)]


def _chunk_many(batch: list[tuple[str, str, str, int, int]]) -> list[list[tuple[str, str]]]:
    return [_chunk_one(a) for a in batch]


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
    pool: Executor | None,
    batch_docs: int,
) -> None:
    try:
        seen = 0
        for batch in reader.scan(_COLUMNS, batch_size=batch_docs):
            cols: dict[str, list[Any]] = {name: batch.column(name).to_pylist() for name in _COLUMNS}
            ids = [str(x) for x in cols["doc_id"]]
            if limit is not None:
                ids = ids[: max(0, limit - seen)]
            if not ids:
                break
            seen += len(ids)
            known = store.doc_hashes(ids)
            todo: list[int] = []
            unchanged: list[str] = []
            for i, doc_id in enumerate(ids):
                if known.get(doc_id) == cols["content_hash"][i]:
                    unchanged.append(doc_id)
                else:
                    todo.append(i)
            if unchanged:
                store.touch_docs(unchanged, generation_id)
            stats.docs_unchanged += len(unchanged)
            args = [(str(cols["title"][i]), str(cols["text"][i]), ids[i], words, max_chunks) for i in todo]
            if pool is not None and len(args) > 64:
                parts = [args[j : j + 64] for j in range(0, len(args), 64)]
                pieces = [p for chunked in pool.map(_chunk_many, parts) for p in chunked]
            else:
                pieces = [_chunk_one(a) for a in args]
            docs: list[_Doc] = []
            for i, pairs in zip(todo, pieces, strict=True):
                doc_id, title = ids[i], str(cols["title"][i])
                chunks = tuple(
                    Chunk(make_chunk_id(doc_id, n), doc_id, n, title, sec, txt)
                    for n, (sec, txt) in enumerate(pairs)
                )
                docs.append(
                    _Doc(
                        doc_id,
                        str(cols["source_id"][i]),
                        str(cols["content_hash"][i]),
                        doc_id in known,
                        chunks,
                    )
                )
            stats.docs_seen += len(unchanged)
            out.put(docs)
            if limit is not None and seen >= limit:
                break
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
    max_chunks_per_doc: int = 256,
    batch_chunks: int = 4096,
    batch_docs: int = 1024,
    save_every: int = 250_000,
    limit: int | None = None,
    workers: int = 0,
    progress: Callable[[EmbedStats], None] | None = None,
    progress_every_s: float = 10.0,
) -> EmbedStats:
    """Bring ``store`` up to date with one Refinery generation of ``source_id``.

    With ``limit`` only the first ``limit`` documents are considered and nothing is deleted.
    """
    stats = EmbedStats(source_id=source_id, generation_id=generation_id, total_docs=limit or len(reader))
    q: queue.Queue[list[_Doc] | BaseException | None] = queue.Queue(maxsize=4)
    pool = ProcessPoolExecutor(workers) if workers > 0 else None
    producer = threading.Thread(
        target=_produce,
        args=(reader, store, stats, q),
        kwargs={
            "generation_id": generation_id,
            "words": chunk_words,
            "max_chunks": max_chunks_per_doc,
            "limit": limit,
            "pool": pool,
            "batch_docs": batch_docs,
        },
        daemon=True,
        name="embed-producer",
    )
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
            (d.doc_id, d.source_id, d.content_hash, generation_id, len(d.chunks)) for d in pending
        )
        stats.docs_embedded += len(pending)
        stats.docs_seen += len(pending)
        stats.chunks += len(chunks)
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
            stale = store.stale_docs(source_id, generation_id)
            if stale:
                store.delete_docs(stale)
                stats.docs_removed = len(stale)
        store.save()
    finally:
        if pool is not None:
            pool.shutdown(cancel_futures=True)
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
    ap.add_argument("--batch-chunks", type=int, default=4096)
    ap.add_argument("--workers", type=int, default=6, help="chunking processes (0 = in the producer thread)")
    ap.add_argument("--device", default="", help="override oracle.embedder.device")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    raw = load_config(args.config_dir or _find_config_dir())
    section = raw.get("oracle", {})
    ocfg = OracleConfig.from_mapping(cast(Mapping[str, Any], section) if isinstance(section, Mapping) else {})
    index = raw.get("index", {})
    root_value = cast(Mapping[str, Any], index).get("root") if isinstance(index, Mapping) else None
    index_root = Path(str(root_value)).expanduser() if root_value else None
    refinery = args.refinery or (index_root / "refinery" if index_root else None)
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
                workers=args.workers,
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
