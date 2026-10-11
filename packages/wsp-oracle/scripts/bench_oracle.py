"""Benchmarks behind ADR-0007: embedding models, ANN designs, rerankers (GPU, local models).

    uv sync --all-packages --extra gpu
    uv run python packages/wsp-oracle/scripts/bench_oracle.py models --root <sample index root>
    uv run python packages/wsp-oracle/scripts/bench_oracle.py ann --root <root> [--with-faiss]
    uv run python packages/wsp-oracle/scripts/bench_oracle.py rerank --root <root>

``<root>`` is an index root holding ``refinery/<source>/<generation>`` (``sample_zim.py`` writes
one). ``models`` chunks every document, embeds the passages with each candidate (throughput), and
scores dense-only retrieval on the gold set (nDCG@10, Recall@50, overall and for the non-English
queries). ``ann`` compares the binary-HNSW + int8 store with exact search and other index types on
the chosen model's vectors. ``rerank`` times top-50 cross-encoder scoring. Results go to
``<root>/bench/*.json``.
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import numpy as np
import numpy.typing as npt

from wsp_index import RefineryReader
from wsp_index.refinery import current_generation, generation_dir
from wsp_oracle import devices
from wsp_oracle.chunker import Chunk, chunk_document
from wsp_oracle.config import DEFAULT_MODELS_DIR
from wsp_oracle.embedder import TransformerEmbedder
from wsp_oracle.evaluation import load_gold, ndcg_at, percentile, recall_at
from wsp_oracle.vectors import ChunkVectorStore, DocVectorIndex

F32 = npt.NDArray[np.float32]
MODELS = Path(DEFAULT_MODELS_DIR).expanduser()
CANDIDATES = (
    "ibm-granite--granite-embedding-97m-multilingual-r2",
    "intfloat--multilingual-e5-small",
    "ibm-granite--granite-embedding-311m-multilingual-r2",
    "intfloat--multilingual-e5-base",
    "BAAI--bge-m3",
    "Qwen--Qwen3-Embedding-0.6B",
)


def load_chunks(root: Path, source: str, *, gold: Path | None = None, limit: int = 0) -> list[Chunk]:
    """Every passage of the sample; with ``limit``, the gold documents' plus the first others up to it."""
    gen = current_generation(root / "refinery", source)
    if gen is None:
        raise SystemExit(f"no Refinery generation for {source} under {root / 'refinery'}")
    reader = RefineryReader(generation_dir(root / "refinery", source, gen))
    chunks = [c for d in reader.records() for c in chunk_document(d)]
    if not limit or len(chunks) <= limit:
        return chunks
    wanted = {d for g in load_gold(gold) for d in g.relevant} if gold else set[str]()
    keep = [c for c in chunks if c.doc_id in wanted]
    rest = [c for c in chunks if c.doc_id not in wanted]
    return keep + rest[: max(0, limit - len(keep))]


def sync_cuda() -> None:
    if devices.cuda_available():
        devices.torch().cuda.synchronize()


def dense_eval(
    emb: TransformerEmbedder, chunks: Sequence[Chunk], mat: F32, gold_path: Path
) -> dict[str, float]:
    gold = load_gold(gold_path)
    q = emb.encode_queries([g.query for g in gold])
    sims = q @ mat.T
    nd: list[float] = []
    r50: list[float] = []
    nd_ml: list[float] = []
    for i, g in enumerate(gold):
        order: list[int] = [int(x) for x in np.argsort(-sims[i])[:400]]
        docs: list[str] = []
        for j in order:
            d = chunks[j].doc_id
            if d not in docs:
                docs.append(d)
        nd.append(ndcg_at(docs, g.relevant))
        r50.append(recall_at(docs, g.relevant))
        if g.lang != "en":
            nd_ml.append(ndcg_at(docs, g.relevant))
    return {
        "ndcg10": statistics.fmean(nd),
        "recall50": statistics.fmean(r50),
        "ndcg10_non_english": statistics.fmean(nd_ml) if nd_ml else 0.0,
    }


def cmd_models(args: argparse.Namespace) -> dict[str, Any]:
    chunks = load_chunks(args.root, args.source, gold=args.gold, limit=args.max_chunks)
    texts = [c.embed_text() for c in chunks]
    words = sum(len(c.text.split()) for c in chunks) / len(chunks)
    print(f"{len(chunks):,} chunks, {words:.0f} words on average")
    out: dict[str, Any] = {"chunks": len(chunks), "avg_words": words, "models": {}}
    for name in args.models.split(","):
        path = MODELS / name
        if not path.is_dir():
            print(f"{name}: not downloaded, skipped")
            continue
        emb = TransformerEmbedder(path, batch_size=args.batch, max_tokens=args.max_tokens)
        emb.encode_passages(texts[:512])  # warm-up (kernels, allocator)
        sync_cuda()
        t0 = time.perf_counter()
        mat = emb.encode_passages(texts)
        sync_cuda()
        secs = time.perf_counter() - t0
        quality = dense_eval(emb, chunks, mat, args.gold)
        row = {"dims": emb.dimensions, "chunks_per_s": len(texts) / secs, "seconds": secs, **quality}
        out["models"][name] = row
        print(
            f"{name}: {json.dumps({k: round(v, 4) if isinstance(v, float) else v for k, v in row.items()})}"
        )
        if name == args.keep:
            np.save(args.root / "bench" / "vectors.npy", mat)
        del emb
        if devices.cuda_available():
            devices.torch().cuda.empty_cache()
    return out


def _latency(fn: Any, queries: F32) -> tuple[float, float]:
    times: list[float] = []
    for q in queries:
        t0 = time.perf_counter()
        fn(q)
        times.append((time.perf_counter() - t0) * 1000)
    return percentile(times, 50), percentile(times, 95)


def cmd_ann(args: argparse.Namespace) -> dict[str, Any]:
    mat: F32 = np.asarray(np.load(args.root / "bench" / "vectors.npy"), dtype=np.float32)
    n, d = int(mat.shape[0]), int(mat.shape[1])
    rng = np.random.default_rng(0)
    # queries: held-in vectors perturbed (stand-ins for real query embeddings)
    pick = np.asarray(rng.permutation(n)[: args.queries], dtype=np.int64)
    noise = rng.standard_normal((args.queries, d)).astype(np.float32) * 0.03
    raw: F32 = (mat[pick] + noise).astype(np.float32)
    queries: F32 = (raw / np.linalg.norm(raw, axis=1, keepdims=True)).astype(np.float32)
    truth = np.argsort(-(queries @ mat.T), axis=1)[:, :10]
    out: dict[str, Any] = {"vectors": n, "dims": d, "queries": args.queries, "designs": {}}

    def recall(found: list[list[int]]) -> float:
        return statistics.fmean(
            len(set(f[:10]) & set(t.tolist())) / 10 for f, t in zip(found, truth, strict=True)
        )

    store_dir = args.root / "bench" / "ann-store"
    if store_dir.exists():
        import shutil

        shutil.rmtree(store_dir)
    t0 = time.perf_counter()
    store = ChunkVectorStore(store_dir, d, oversample=args.oversample)
    for s in range(0, n, 50_000):
        store.upsert_matrix([str(i) for i in range(s, min(n, s + 50_000))], mat[s : s + 50_000])
    store.save()
    build = time.perf_counter() - t0
    for over in (2, 4, 8, 16):
        store.oversample = over
        found = [[int(i) for i, _ in store.search_array(q, limit=10)] for q in queries]
        p50, p95 = _latency(lambda q: store.search_array(q, limit=10), queries)  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
        out["designs"][f"usearch-b1+int8 x{over}"] = {"recall10": recall(found), "p50_ms": p50, "p95_ms": p95}
    docs = DocVectorIndex(store)
    p50, p95 = _latency(lambda q: docs.search_docs(q, limit=50), queries)  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    out["designs"]["pipeline: 50 docs (150 chunks, x8)"] = {"p50_ms": p50, "p95_ms": p95}
    st = store.stats()
    out["store"] = {"build_s": build, "graph_ram_bytes": st["memory_bytes"], "int8_bytes": n * d}
    store.close()

    from usearch.index import Index

    for dtype in ("i8", "f16"):
        idx = Index(
            ndim=d, metric="cos", dtype=dtype, connectivity=16, expansion_add=128, expansion_search=64
        )
        idx.add(np.arange(n, dtype=np.uint64), mat)
        res: Any = idx.search(queries, 10)
        found = [list(map(int, k)) for k in res.keys]
        p50, p95 = _latency(lambda q, idx=idx: idx.search(q, 10), queries)  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
        out["designs"][f"usearch-hnsw-{dtype}"] = {
            "recall10": recall(found),
            "p50_ms": p50,
            "p95_ms": p95,
            "ram_bytes": int(idx.memory_usage),
        }
    if args.with_faiss:
        import importlib

        faiss: Any = importlib.import_module("faiss")  # benchmark-only: `uv run --with faiss-cpu`

        bits = np.packbits(mat > 0, axis=1)
        fidx = faiss.IndexBinaryHNSW(d, 16)
        fidx.add(bits)
        qb = np.packbits(queries > 0, axis=1)

        def faiss_search(qi: int) -> list[int]:
            found_ids: Any = fidx.search(qb[qi : qi + 1], 80)[1]
            c = np.asarray(found_ids[0], dtype=np.int64)
            sims = mat[c] @ queries[qi]
            return [int(c[j]) for j in np.argsort(-sims)[:10]]

        found = [faiss_search(i) for i in range(len(queries))]
        times: list[float] = []
        for i in range(len(queries)):
            t0 = time.perf_counter()
            faiss_search(i)
            times.append((time.perf_counter() - t0) * 1000)
        out["designs"]["faiss-binaryhnsw+float rescoring x8"] = {
            "recall10": recall(found),
            "p50_ms": percentile(times, 50),
            "p95_ms": percentile(times, 95),
        }
    for k, v in out["designs"].items():
        print(f"{k:<40} {json.dumps({a: round(b, 4) for a, b in v.items()})}")
    return out


def cmd_rerank(args: argparse.Namespace) -> dict[str, Any]:
    from wsp_oracle.reranker import CrossEncoderReranker

    chunks = load_chunks(args.root, args.source)
    gold = load_gold(args.gold)
    rng = np.random.default_rng(1)
    out: dict[str, Any] = {}
    for name in args.rerankers.split(","):
        rr = CrossEncoderReranker(MODELS / name, max_tokens=args.max_tokens)
        rr.score("warm up", [c.text for c in chunks[:50]])
        times: list[float] = []
        for g in gold:
            sample = [int(i) for i in rng.choice(len(chunks), size=50, replace=False)]
            passages = [f"{chunks[i].title}\n{chunks[i].text}" for i in sample]
            sync_cuda()
            t0 = time.perf_counter()
            rr.score(g.query, passages)
            sync_cuda()
            times.append((time.perf_counter() - t0) * 1000)
        out[name] = {"top50_p50_ms": percentile(times, 50), "top50_p95_ms": percentile(times, 95)}
        print(name, out[name])
    return out


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=("models", "ann", "rerank"))
    ap.add_argument("--root", type=Path, required=True)
    ap.add_argument("--source", default="wikipedia-en-zim")
    ap.add_argument(
        "--gold", type=Path, default=Path(__file__).resolve().parents[1] / "eval" / "gold" / "queries.jsonl"
    )
    ap.add_argument("--models", default=",".join(CANDIDATES))
    ap.add_argument("--keep", default=CANDIDATES[0], help="model whose vectors `ann` uses")
    ap.add_argument(
        "--rerankers", default="BAAI--bge-reranker-v2-m3,ibm-granite--granite-embedding-reranker-english-r2"
    )
    ap.add_argument("--batch", type=int, default=256)
    ap.add_argument("--max-tokens", type=int, default=512)
    ap.add_argument("--queries", type=int, default=1000)
    ap.add_argument("--max-chunks", type=int, default=0, help="models: cap the corpus (gold documents kept)")
    ap.add_argument("--oversample", type=int, default=8)
    ap.add_argument("--with-faiss", action="store_true")
    args = ap.parse_args(argv)
    (args.root / "bench").mkdir(parents=True, exist_ok=True)
    result = {"models": cmd_models, "ann": cmd_ann, "rerank": cmd_rerank}[args.command](args)
    gpu = devices.torch().cuda.get_device_name(0) if devices.cuda_available() else "cpu"
    result["device"] = gpu
    (args.root / "bench" / f"{args.command}.json").write_text(
        json.dumps(result, indent=2) + "\n", encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
