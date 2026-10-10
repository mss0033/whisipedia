"""Evaluation harness (PLAN §9.5): nDCG@10, Recall@50, latency percentiles, summary faithfulness.

    uv run --extra gpu wsp-oracle-eval --modes lexical,hybrid,full [--index-root DIR] [--summaries 30]

Gold sets are JSON lines (``packages/wsp-oracle/eval/gold/queries.jsonl``)::

    {"id": "q001", "query": "...", "lang": "en", "domain": "science",
     "relevant": {"wsp:wikipedia-en-zim/Vaccine": 2, "wsp:wikipedia-en-zim/Immune_system": 1}}

Grades: 2 = the answer, 1 = also relevant. nDCG uses gains ``2^g - 1``; Recall@50 counts every
graded document. Documents missing from the index under test are still counted (a smaller index
cannot score higher by forgetting the answer). Faithfulness is claim-level: each summary sentence
must cite a passage, and the cited passage must support it (``lexical_judge`` for CI,
``LlmJudge`` against the local model for the nightly run).
"""

from __future__ import annotations

import argparse
import asyncio
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

from wsp_oracle.generator import Passage, Summarizer
from wsp_oracle.pipeline import OracleSearcher
from wsp_oracle.router import content_terms

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


# --- faithfulness ---------------------------------------------------------------------------------


def sentences(text: str) -> list[str]:
    return [s.strip() for s in _SPLIT.split(text) if s and s.strip()]


def lexical_judge(min_overlap: float = 0.5) -> Judge:
    """Supported when at least ``min_overlap`` of the claim's content words occur in the passage.

    Crude but deterministic (CI). Paraphrase and translation defeat it; use :class:`LlmJudge` for
    the real number.
    """

    def judge(claim: str, passage: str) -> bool:
        words = {w.casefold() for w in content_terms(_MARK.sub("", claim)) if len(w) > 2}
        if not words:
            return True
        hay = passage.casefold()
        return sum(1 for w in words if w[:6] in hay) / len(words) >= min_overlap

    return judge


@dataclass(frozen=True, slots=True)
class Faithfulness:
    sentences: int
    cited: int
    supported: int
    unsupported: tuple[str, ...] = ()

    @property
    def score(self) -> float:
        """Supported sentences over all sentences (uncited ones count as unsupported)."""
        return self.supported / self.sentences if self.sentences else 1.0


def faithfulness(summary: str, passages: Mapping[int, str], judge: Judge) -> Faithfulness:
    total = cited = supported = 0
    bad: list[str] = []
    for s in sentences(summary):
        total += 1
        marks = [int(m) for m in _MARK.findall(s) if int(m) in passages]
        if not marks:
            bad.append(s)
            continue
        cited += 1
        claim = _MARK.sub("", s).strip()
        if any(judge(claim, passages[m]) for m in marks):
            supported += 1
        else:
            bad.append(s)
    return Faithfulness(total, cited, supported, tuple(bad))


class LlmJudge:
    """Asks the local model whether a passage supports a claim (yes/no), via llama-server."""

    PROMPT = (
        "Passage:\n{passage}\n\nClaim: {claim}\n\n"
        "Is every fact in the claim stated or directly implied by the passage? Answer only yes or no."
    )

    def __init__(self, url: str) -> None:
        from wsp_oracle.generator import LlamaServerGenerator

        self.llm = LlamaServerGenerator(url, temperature=0.0, timeout_s=60)

    async def ask(self, claim: str, passage: str) -> bool:
        text = ""
        msg = [{"role": "user", "content": self.PROMPT.format(passage=passage[:3000], claim=claim)}]
        async for piece in self.llm.stream_chat(msg, max_tokens=4):
            text += piece
        return text.strip().casefold().startswith("yes")

    def __call__(self, claim: str, passage: str) -> bool:
        return asyncio.run(self.ask(claim, passage))


@dataclass(frozen=True, slots=True)
class SummaryEval:
    queries: int
    refused: int
    faithfulness: float
    cited_rate: float
    first_token_p50_ms: float
    first_token_p95_ms: float
    unsupported: tuple[str, ...] = ()


async def _summaries(
    summarizer: Summarizer, items: Sequence[tuple[str, list[Passage]]], judge: Judge
) -> SummaryEval:
    firsts: list[float] = []
    refused = 0
    reports: list[Faithfulness] = []
    for question, passages in items:
        t0 = time.perf_counter()
        first: float | None = None
        text = ""
        was_refused = False
        async for ev in summarizer.events(question, passages):
            kind = ev["type"]
            if first is None and kind in ("token", "citation", "notice"):
                first = (time.perf_counter() - t0) * 1000
            if kind == "token":
                text += str(ev["text"])
            elif kind == "citation":
                text += f"[{ev['index']}]"
            elif kind == "notice" and "no summary" in str(ev["text"]):
                was_refused = True
        if first is not None:
            firsts.append(first)
        if was_refused:
            refused += 1
            continue
        by_index = {p.index: p.text for p in passages}
        reports.append(await asyncio.to_thread(faithfulness, text, by_index, judge))
    total = sum(r.sentences for r in reports)
    return SummaryEval(
        queries=len(items),
        refused=refused,
        faithfulness=(sum(r.supported for r in reports) / total) if total else 1.0,
        cited_rate=(sum(r.cited for r in reports) / total) if total else 1.0,
        first_token_p50_ms=percentile(firsts, 50),
        first_token_p95_ms=percentile(firsts, 95),
        unsupported=tuple(s for r in reports for s in r.unsupported)[:20],
    )


def evaluate_summaries(
    summarizer: Summarizer, items: Sequence[tuple[str, list[Passage]]], judge: Judge
) -> SummaryEval:
    return asyncio.run(_summaries(summarizer, items, judge))


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
    ap.add_argument("--summaries", type=int, default=0, help="also summarize the top results of N queries")
    ap.add_argument("--judge", choices=("llm", "lexical"), default="llm")
    ap.add_argument("--out", type=Path, default=None, help="write the full JSON report here")
    args = ap.parse_args(argv)

    cfg: dict[str, Any] = dict(load_config(args.config_dir or _find_config_dir()))
    if args.index_root is not None:
        cfg["index"] = {**cast(dict[str, Any], cfg.get("index", {})), "root": str(args.index_root)}
    if args.summaries:
        oracle_cfg = dict(cast(dict[str, Any], cfg.get("oracle", {})))
        oracle_cfg["generator"] = {**cast(dict[str, Any], oracle_cfg.get("generator", {})), "enabled": True}
        cfg["oracle"] = oracle_cfg
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
    if args.summaries and oracle.summarizer is not None:
        items: list[tuple[str, list[Passage]]] = []
        for q in gold[: args.summaries]:
            res = base.run(q.query)
            passages = [
                Passage(i, h.title, res.passages.get(h.doc_id, ""), h.doc_id)
                for i, h in enumerate(res.hits[: oracle.config.generator.passages], start=1)
            ]
            items.append((q.query, passages))
        judge: Judge = LlmJudge(oracle.config.generator.url) if args.judge == "llm" else lexical_judge()
        se = evaluate_summaries(oracle.summarizer, items, judge)
        print(
            f"summaries n={se.queries} refused={se.refused} faithfulness={se.faithfulness:.3f} "
            f"cited={se.cited_rate:.3f} first-token p50={se.first_token_p50_ms:.0f}ms "
            f"p95={se.first_token_p95_ms:.0f}ms"
        )
        out["summaries"] = asdict(se)
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
