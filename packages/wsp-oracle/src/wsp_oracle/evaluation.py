"""Evaluation harness (PLAN §9.5): nDCG@10, Recall@50, MRR, latency percentiles.

    uv run --extra gpu wsp-oracle-eval --modes lexical,hybrid,full [--index-root DIR]

Gold sets are JSON lines (``packages/wsp-oracle/eval/gold/queries.jsonl``)::

    {"id": "q001", "query": "...", "lang": "en", "domain": "science",
     "relevant": {"wsp:wikipedia-en-zim/Vaccine": 2, "wsp:wikipedia-en-zim/Immune_system": 1}}

Grades: 2 = the answer, 1 = also relevant. nDCG uses gains ``2^g - 1``; Recall@50 counts every
graded document. Documents missing from the index under test are still counted (a smaller index
cannot score higher by forgetting the answer).
"""

from __future__ import annotations

import argparse
import json
import math
import re
import statistics
import sys
import time
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, cast

from wsp_oracle.pipeline import OracleSearcher

Searcher = Callable[[str], Sequence[str]]
Judge = Callable[[str, str], bool]

DEFAULT_GOLD = Path(__file__).resolve().parents[2] / "eval" / "gold" / "queries.jsonl"
_MARK = re.compile(r"\[(\d{1,3})\]")
_SPLIT = re.compile(r"(?<=[.!?。\uff01\uff1f])(?:\s*\[\d{1,3}\])*\s+(?=[^\s\[a-z])|\n+")


@dataclass(frozen=True, slots=True)
class GoldQuery:
    id: str
    query: str
    relevant: Mapping[str, int]
    lang: str = "en"
    domain: str = ""


def load_gold(path: Path = DEFAULT_GOLD) -> list[GoldQuery]:
    out: list[GoldQuery] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = cast(dict[str, Any], json.loads(line))
        rel = {str(k): int(v) for k, v in cast(dict[str, Any], row["relevant"]).items()}
        out.append(
            GoldQuery(
                str(row["id"]), str(row["query"]), rel, str(row.get("lang", "en")), str(row.get("domain", ""))
            )
        )
    return out


def ndcg_at(ranked: Sequence[str], relevant: Mapping[str, int], k: int = 10) -> float:
    def gain(g: int) -> float:
        return float(2**g - 1)

    dcg = sum(gain(relevant.get(d, 0)) / math.log2(i + 2) for i, d in enumerate(ranked[:k]))
    ideal = sorted(relevant.values(), reverse=True)[:k]
    idcg = sum(gain(g) / math.log2(i + 2) for i, g in enumerate(ideal))
    return dcg / idcg if idcg > 0 else 0.0


def recall_at(ranked: Sequence[str], relevant: Mapping[str, int], k: int = 50) -> float:
    rel = {d for d, g in relevant.items() if g > 0}
    return len(rel & set(ranked[:k])) / len(rel) if rel else 0.0


def percentile(values: Sequence[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    idx = min(len(s) - 1, max(0, math.ceil(p / 100 * len(s)) - 1))
    return s[idx]


@dataclass(frozen=True, slots=True)
class QueryResult:
    id: str
    ndcg10: float
    recall50: float
    latency_ms: float
    top: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class EvalReport:
    name: str
    queries: int
    ndcg10: float
    recall50: float
    mrr: float
    p50_ms: float
    p95_ms: float
    p99_ms: float
    by_domain: Mapping[str, float] = field(default_factory=dict[str, float])
    results: tuple[QueryResult, ...] = ()

    def summary(self) -> str:
        return (
            f"{self.name:<10} n={self.queries:<4} nDCG@10={self.ndcg10:.3f} R@50={self.recall50:.3f} "
            f"MRR={self.mrr:.3f} p50={self.p50_ms:.1f}ms p95={self.p95_ms:.1f}ms p99={self.p99_ms:.1f}ms"
        )


def evaluate(name: str, search: Searcher, gold: Sequence[GoldQuery], *, warmup: int = 3) -> EvalReport:
    """Run every gold query through ``search`` (which returns ranked doc ids, at least 50 if it can)."""
    for q in gold[:warmup]:
        search(q.query)
    results: list[QueryResult] = []
    rr: list[float] = []
    domains: dict[str, list[float]] = defaultdict(list)
    for q in gold:
        t0 = time.perf_counter()
        ranked = list(search(q.query))
        ms = (time.perf_counter() - t0) * 1000
        n, r = ndcg_at(ranked, q.relevant), recall_at(ranked, q.relevant)
        first = next((i for i, d in enumerate(ranked[:50]) if q.relevant.get(d, 0) > 0), None)
        rr.append(0.0 if first is None else 1.0 / (first + 1))
        domains[q.domain].append(n)
        results.append(QueryResult(q.id, n, r, ms, tuple(ranked[:10])))
    lat = [r.latency_ms for r in results]
    return EvalReport(
        name=name,
        queries=len(results),
        ndcg10=statistics.fmean(r.ndcg10 for r in results) if results else 0.0,
        recall50=statistics.fmean(r.recall50 for r in results) if results else 0.0,
        mrr=statistics.fmean(rr) if rr else 0.0,
        p50_ms=percentile(lat, 50),
        p95_ms=percentile(lat, 95),
        p99_ms=percentile(lat, 99),
        by_domain={d: round(statistics.fmean(v), 4) for d, v in sorted(domains.items())},
        results=tuple(results),
    )


# --- CLI ------------------------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    from wsp_core.config import load_config
    from wsp_oracle.embedbuild import _find_config_dir  # pyright: ignore[reportPrivateUsage]
    from wsp_oracle.services import build_oracle

    ap = argparse.ArgumentParser(description="Evaluate the Oracle against a gold query set.")
    ap.add_argument("--config-dir", type=Path, default=None)
    ap.add_argument(
        "--index-root", type=Path, default=None, help="override index.root (e.g. a benchmark sample)"
    )
    ap.add_argument("--gold", type=Path, default=DEFAULT_GOLD)
    ap.add_argument(
        "--modes", default="lexical,hybrid,full", help="lexical, dense, hybrid, full (hybrid+rerank)"
    )
    ap.add_argument("--out", type=Path, default=None, help="write the full JSON report here")
    args = ap.parse_args(argv)

    cfg: dict[str, Any] = dict(load_config(args.config_dir or _find_config_dir()))
    if args.index_root is not None:
        cfg["index"] = {**cast(dict[str, Any], cfg.get("index", {})), "root": str(args.index_root)}
    gold = load_gold(args.gold)
    oracle = build_oracle(cfg)
    base = oracle.searcher
    variants = {
        "lexical": replace_searcher(base, dense=False, rerank=False),
        "dense": replace_searcher(base, dense=True, rerank=False, lexical_weight=0.0),
        "hybrid": replace_searcher(base, dense=True, rerank=False),
        "full": replace_searcher(base, dense=True, rerank=True),
    }
    reports: list[EvalReport] = []
    for mode in [m for m in args.modes.split(",") if m]:
        s = variants[mode]
        if mode != "lexical" and s.vectors is None and mode != "full":
            print(f"{mode}: skipped (no vector store)", file=sys.stderr)
            continue
        rep = evaluate(mode, lambda q, s=s: [h.doc_id for h in s.run(q).hits], gold)
        reports.append(rep)
        print(rep.summary())
    out: dict[str, Any] = {"gold": str(args.gold), "reports": [asdict(r) for r in reports]}
    if args.out is not None:
        args.out.write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    oracle.close()
    return 0


def replace_searcher(
    b: OracleSearcher, *, dense: bool, rerank: bool, lexical_weight: float | None = None
) -> OracleSearcher:
    """A copy of an ``OracleSearcher`` with stages switched off (for ablations)."""
    fusion = b.fusion if lexical_weight is None else replace(b.fusion, lexical_weight=lexical_weight)
    return OracleSearcher(
        b.lexical,
        router=b.router,
        embedder=b.embedder if dense else None,
        vectors=b.vectors if dense else None,
        reranker=b.reranker if rerank else None,
        fusion=fusion,
        rerank_top=b.rerank_top,
        resolve=b.resolve,
        passage_text=b.passage_text,
        budget_ms=1e9,
    )


if __name__ == "__main__":
    raise SystemExit(main())
