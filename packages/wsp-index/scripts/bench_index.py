# ruff: noqa: S311  # seeded PRNG for a reproducible synthetic corpus, not for security
"""Benchmark the wsp-index stack on a synthetic corpus (no real data, no network).

Generates documents from a Zipf word-frequency model (pseudo-words, log-normal lengths, power-law
link graph), writes them through the Refinery, indexes them with Tantivy, then measures:

* Refinery write rate, size and point-lookup latency
* index build rate (docs/s, MB/s of text) and index size
* search latency p50/p95/p99 (top 10 with highlighted snippets; also with a source filter)
* suggest latency for the chosen MARISA + heavy-prefix design versus two baselines

Usage (outputs go to a gitignored directory)::

    uv run python packages/wsp-index/scripts/bench_index.py --docs 200000 --out data/bench
"""

from __future__ import annotations

import argparse
import bisect
import json
import math
import random
import shutil
import statistics
import sys
import time
import tracemalloc
from collections.abc import Callable, Iterator
from pathlib import Path

import numpy as np

from wsp_core.ids import make_doc_id
from wsp_core.model import DocRecord
from wsp_index import RefineryReader, RefineryWriter, TantivyConfig, TantivyIndex, index_generation
from wsp_index.refinery import generation_dir
from wsp_index.suggest import SuggestEntry, Suggester, TitleTrie
from wsp_index.text import normalize_key

SOURCES: tuple[str, ...] = ("encyclopedia", "books", "papers", "law", "qa")
SYLLABLES = [c + v for c in "bcdfghjklmnprstvwz" for v in ("a", "e", "i", "o", "u", "ai", "ou")]


def make_vocab(n: int, rng: random.Random) -> list[str]:
    words: dict[str, None] = {}
    while len(words) < n:
        k = min(1 + int(rng.expovariate(0.6)), 5)
        words.setdefault("".join(rng.choice(SYLLABLES) for _ in range(k)), None)
    out = list(words)
    out.sort(key=len)  # frequent words are short, as in natural language
    return out


def corpus(n_docs: int, vocab: list[str], seed: int) -> Iterator[DocRecord]:
    nrng = np.random.default_rng(seed)
    ranks = np.arange(1, len(vocab) + 1, dtype=np.float64)
    p = 1.0 / ranks**1.07
    p /= p.sum()
    cdf = np.cumsum(p)
    popularity = 1.0 / np.arange(1, n_docs + 1) ** 0.8  # inbound-link popularity, power law
    pop_cdf = np.cumsum(popularity / popularity.sum())
    perm = nrng.permutation(n_docs)
    vocab_arr = np.array(vocab, dtype=object)
    for i in range(n_docs):
        length = int(min(max(nrng.lognormal(math.log(300), 0.8), 20), 4000))
        words = vocab_arr[np.searchsorted(cdf, nrng.random(length))]
        title_words = vocab_arr[
            nrng.integers(min(100, len(vocab) - 1), min(30_000, len(vocab)), size=int(nrng.integers(1, 4)))
        ]
        title = " ".join(w.capitalize() for w in title_words)
        # Paragraph breaks every ~80 words keep text realistic for snippets.
        text = "\n\n".join(" ".join(words[j : j + 80]) for j in range(0, length, 80))
        src = SOURCES[i % len(SOURCES)]
        targets = perm[np.searchsorted(pop_cdf, nrng.random(int(nrng.integers(0, 30))))]
        links = tuple(make_doc_id(SOURCES[t % len(SOURCES)], f"d{t}") for t in map(int, targets))
        yield DocRecord(
            doc_id=make_doc_id(src, f"d{i}"),
            source_id=src,
            native_id=f"d{i}",
            title=f"{title} {i % 97}" if i % 5 == 0 else title,
            lang="en",
            license="CC0-1.0",
            text=text,
            links=links,
        )


def pct(samples: list[float], q: float) -> float:
    s = sorted(samples)
    return s[min(len(s) - 1, round(q / 100 * (len(s) - 1)))]


def summary(lat_ms: list[float]) -> dict[str, float]:
    return {
        "n": len(lat_ms),
        "mean_ms": round(statistics.fmean(lat_ms), 3),
        "p50_ms": round(pct(lat_ms, 50), 3),
        "p95_ms": round(pct(lat_ms, 95), 3),
        "p99_ms": round(pct(lat_ms, 99), 3),
        "max_ms": round(max(lat_ms), 3),
    }


def dir_bytes(path: Path) -> int:
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


def run_latency(fn: Callable[[str], object], inputs: list[str], warmup: int = 50) -> dict[str, float]:
    for q in inputs[:warmup]:
        fn(q)
    lat: list[float] = []
    for q in inputs:
        t = time.perf_counter()
        fn(q)
        lat.append((time.perf_counter() - t) * 1000)
    return summary(lat)


class BisectSuggest:
    """Baseline: sorted Python list + bisect, rank the whole prefix range on every query."""

    def __init__(self, entries: list[SuggestEntry]) -> None:
        pairs = sorted((normalize_key(e.title), e.score, e.title) for e in entries)
        self.keys = [k for k, _, _ in pairs]
        self.scores = np.array([s for _, s, _ in pairs], dtype=np.float32)
        self.titles = [t for _, _, t in pairs]

    def suggest(self, prefix: str, limit: int = 10) -> list[str]:
        p = normalize_key(prefix)
        lo = bisect.bisect_left(self.keys, p)
        hi = bisect.bisect_left(self.keys, p + "\U0010ffff")
        if hi - lo > limit:
            idx = lo + np.argpartition(-self.scores[lo:hi], limit)[:limit]
        else:
            idx = np.arange(lo, hi)
        idx = idx[np.argsort(-self.scores[idx])]
        return [self.titles[i] for i in idx.tolist()]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--docs", type=int, default=200_000)
    ap.add_argument("--vocab", type=int, default=60_000)
    ap.add_argument("--queries", type=int, default=2_000)
    ap.add_argument("--seed", type=int, default=20261009)
    ap.add_argument("--out", type=Path, default=Path("data/bench"))
    ap.add_argument("--heap-mb", type=int, default=512)
    ap.add_argument("--row-group-rows", type=int, default=256)
    args = ap.parse_args()

    out: Path = args.out
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    rng = random.Random(args.seed)
    vocab = make_vocab(args.vocab, rng)
    results: dict[str, object] = {"docs": args.docs, "vocab": args.vocab, "seed": args.seed}

    # 1. Refinery ---------------------------------------------------------------------------
    t0 = time.perf_counter()
    text_bytes = 0
    titles: list[SuggestEntry] = []
    inlinks: dict[str, int] = {}
    refinery_root = out / "refinery"
    writers = {s: RefineryWriter(refinery_root, s, "g1", row_group_rows=args.row_group_rows) for s in SOURCES}
    for d in corpus(args.docs, vocab, args.seed):
        text_bytes += len(d.text.encode("utf-8"))
        titles.append(SuggestEntry(d.doc_id, d.title))
        for t in set(d.links):
            inlinks[t] = inlinks.get(t, 0) + 1
        writers[d.source_id].write([d])
    for w in writers.values():
        w.close()
    gen_and_write_s = time.perf_counter() - t0
    readers = [RefineryReader(generation_dir(refinery_root, s, "g1")) for s in SOURCES]
    by_source = dict(zip(SOURCES, readers, strict=True))
    sample_ids = [titles[rng.randrange(len(titles))].doc_id for _ in range(2000)]
    lat: list[float] = []
    for doc_id in sample_ids:
        reader = by_source[doc_id[4:].split("/", 1)[0]]
        t = time.perf_counter()
        got = reader.get(doc_id)
        lat.append((time.perf_counter() - t) * 1000)
        assert got is not None
    results["refinery"] = {
        "generate_and_write_s": round(gen_and_write_s, 1),
        "text_mb": round(text_bytes / 1e6, 1),
        "avg_doc_bytes": round(text_bytes / args.docs),
        "parquet_mb": round(dir_bytes(refinery_root) / 1e6, 1),
        "row_group_rows": args.row_group_rows,
        "point_lookup": summary(lat),
    }
    print("refinery", results["refinery"], flush=True)

    # 2. Index build -------------------------------------------------------------------------
    cfg = TantivyConfig(writer_heap_bytes=args.heap_mb * 1024 * 1024)
    index_dir = out / "index"
    idx = TantivyIndex(index_dir, cfg)
    t0 = time.perf_counter()
    n = sum(index_generation(idx, r) for r in readers)
    build_s = time.perf_counter() - t0
    t0 = time.perf_counter()
    idx.set_priors({k: math.log1p(v) for k, v in inlinks.items()})
    idx.commit()
    prior_s = time.perf_counter() - t0
    results["index"] = {
        "docs": n,
        "build_s_incl_read_commit_suggest": round(build_s, 1),
        "docs_per_s": round(n / build_s),
        "text_mb_per_s": round(text_bytes / 1e6 / build_s, 1),
        "set_priors_and_rebuild_suggest_s": round(prior_s, 1),
        "tantivy_mb": round(dir_bytes(index_dir / "tantivy") / 1e6, 1),
        "suggest_mb": round(dir_bytes(index_dir / "suggest") / 1e6, 1),
        "writer_heap_mb": args.heap_mb,
    }
    print("index", results["index"], flush=True)

    # 3. Search latency ------------------------------------------------------------------------
    qrng = random.Random(args.seed + 1)
    queries: list[str] = []
    for _ in range(args.queries):
        k = qrng.choice((1, 2, 2, 3, 3, 4))
        words = [vocab[min(int(qrng.paretovariate(0.6)) + 30, len(vocab) - 1)] for _ in range(k)]
        if qrng.random() < 0.2:
            words.append(vocab[qrng.randrange(10)])  # a stopword-like frequent term
        queries.append(" ".join(words))
    results["search_top10_snippets"] = run_latency(lambda q: idx.search(q, limit=10), queries)
    print("search", results["search_top10_snippets"], flush=True)
    results["search_page2"] = run_latency(lambda q: idx.search(q, limit=10, offset=10), queries[:500])
    results["search_source_filter"] = run_latency(
        lambda q: idx.search(q, limit=10, sources=["books", "law"]), queries[:500]
    )
    print("search filter", results["search_source_filter"], flush=True)

    idx.rebuild_suggest()  # measure the steady state (overlay folded into the trie)

    # 4. Suggest -------------------------------------------------------------------------------
    srng = random.Random(args.seed + 2)
    prefixes: list[str] = []
    for _ in range(5000):
        t = titles[srng.randrange(len(titles))].title
        prefixes.append(t[: srng.choice((1, 1, 2, 2, 3, 3, 4, 5, 6, 8, 10))])
    results["suggest_marisa_heavy"] = run_latency(lambda p: idx.suggest(p, limit=10), prefixes)
    print("suggest", results["suggest_marisa_heavy"], flush=True)

    entries = [SuggestEntry(e.doc_id, e.title, prior=math.log1p(inlinks.get(e.doc_id, 0))) for e in titles]
    tracemalloc.start()
    bis = BisectSuggest(entries)
    bisect_mem = tracemalloc.get_traced_memory()[0]
    tracemalloc.stop()
    results["suggest_bisect_rank_range"] = {
        **run_latency(lambda p: bis.suggest(p, 10), prefixes),
        "python_heap_mb": round(bisect_mem / 1e6, 1),
    }
    nh_dir = out / "suggest-noheavy"
    nh = Suggester(TitleTrie.build(entries, nh_dir, heavy_threshold=len(entries) + 1))
    results["suggest_marisa_no_heavy"] = run_latency(lambda p: nh.suggest(p, limit=10), prefixes[:1000])
    print("suggest baselines", results["suggest_bisect_rank_range"], results["suggest_marisa_no_heavy"])

    idx.close()
    (out / "results.json").write_text(json.dumps(results, indent=2) + "\n")
    print(json.dumps(results, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
