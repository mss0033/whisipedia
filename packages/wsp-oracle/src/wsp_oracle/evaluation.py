"""Evaluation harness (PLAN §9.5): nDCG@10, Recall@50, MRR, latency percentiles.

    uv run wsp-oracle-eval --modes lexical,hybrid,full [--index-root DIR]
        [--rank-weights 0/0,1/0.1] [--typos]

``--rank-weights`` reruns the first mode with the link-analysis static rank at each weight pair
(lexical/fusion); ``--typos`` measures one-typo variants of every gold query with and without the
"did you mean" correction.

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
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Protocol, cast

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


# The library holds English Wikipedia twice (the ZIM and the top-10 dumps) and lesser copies of many
# articles (Simple English, MDWiki). A judged article found in another full copy is the same answer;
# found in a lesser copy it counts as "also relevant". Without this, the live library scored a judged
# article's twin as a miss (2026-10-14).
FULL_WIKIPEDIA = ("wsp:wikipedia-en-zim/", "wsp:wikipedia-top10/en/")
LESSER_COPIES = ("wsp:kiwix-wikipedia-en-simple-all/", "wsp:mdwiki/")


def _title(doc_id: str, prefixes: Sequence[str]) -> str | None:
    for p in prefixes:
        if doc_id.startswith(p):
            return doc_id[len(p) :]
    return None


def canonical(doc_id: str) -> str:
    """One key for every full copy of a Wikipedia article; any other doc id is its own key."""
    title = _title(doc_id, FULL_WIKIPEDIA)
    return f"wikipedia:{title}" if title is not None else doc_id


def graded(
    ranked: Sequence[str], relevant: Mapping[str, int]
) -> tuple[list[str], dict[str, int], dict[str, int]]:
    """(ranked keys, judged grades, grades for scoring), all by :func:`canonical` key.

    Copies of one article count once, at the first place one appears. A lesser copy of a judged
    Wikipedia article scores 1 unless that copy was judged itself; it never raises the ideal ranking."""
    judged: dict[str, int] = {}
    titles: set[str] = set()
    for d, g in relevant.items():
        key = canonical(d)
        judged[key] = max(judged.get(key, 0), g)
        title = _title(d, FULL_WIKIPEDIA)
        if title is not None:
            titles.add(title)
    scoring = dict(judged)
    keys: list[str] = []
    seen: set[str] = set()
    for d in ranked:
        key = canonical(d)
        lesser = _title(d, LESSER_COPIES)
        if key not in judged and lesser is not None and lesser in titles:
            scoring.setdefault(key, 1)
        if key not in seen:
            seen.add(key)
            keys.append(key)
    return keys, judged, scoring


def ndcg_at(
    ranked: Sequence[str],
    relevant: Mapping[str, int],
    k: int = 10,
    *,
    ideal_from: Mapping[str, int] | None = None,
) -> float:
    def gain(g: int) -> float:
        return float(2**g - 1)

    dcg = sum(gain(relevant.get(d, 0)) / math.log2(i + 2) for i, d in enumerate(ranked[:k]))
    ideal = sorted((relevant if ideal_from is None else ideal_from).values(), reverse=True)[:k]
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
        raw = list(search(q.query))
        ms = (time.perf_counter() - t0) * 1000
        ranked, judged, scoring = graded(raw, q.relevant)
        n, r = ndcg_at(ranked, scoring, ideal_from=judged), recall_at(ranked, judged)
        first = next((i for i, d in enumerate(ranked[:50]) if scoring.get(d, 0) > 0), None)
        rr.append(0.0 if first is None else 1.0 / (first + 1))
        domains[q.domain].append(n)
        results.append(QueryResult(q.id, n, r, ms, tuple(raw[:10])))
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


# --- ablations: static rank and spelling ------------------------------------------------------

_LETTERS = re.compile(r"[^\W\d_]{5,}")


def typo(query: str, seed: int) -> str:
    """A deterministic one-edit misspelling of the query's longest word (5+ letters).

    The edit cycles with ``seed``: swap two inner letters, drop one, double one. The first letter is
    kept (people rarely mistype it). Queries without such a word are returned unchanged.
    """
    words = list(_LETTERS.finditer(query))
    if not words:
        return query
    m = max(words, key=lambda x: (len(x.group(0)), -x.start()))
    w = m.group(0)
    i = 1 + seed % (len(w) - 2)
    kind = seed % 3
    if kind == 0 and w[i] != w[i + 1]:
        bad = w[:i] + w[i + 1] + w[i] + w[i + 2 :]
    elif kind == 1:
        bad = w[:i] + w[i + 1 :]
    else:
        bad = w[:i] + w[i] + w[i:]
    return query[: m.start()] + bad + query[m.end() :]


class Corrector(Protocol):
    def correct(self, query: str, *, only: Iterable[str] | None = None) -> str | None: ...

    def unknown(self, query: str) -> Sequence[str]: ...


def corrected(speller: Corrector, query: str) -> str:
    """What the search page would run after "did you mean": unknown words corrected, else the query."""
    unknown = list(speller.unknown(query))
    if not unknown:
        return query
    return speller.correct(query, only=unknown) or query


@dataclass(frozen=True, slots=True)
class SpellingReport:
    queries: int
    changed_by_typo: int
    restored: int
    """Typo'd queries the corrector turned back into exactly the original."""
    clean: EvalReport
    typos: EvalReport
    typos_corrected: EvalReport

    def summary(self) -> str:
        return (
            f"spelling n={self.queries} typo'd={self.changed_by_typo} restored={self.restored} "
            f"nDCG@10 clean={self.clean.ndcg10:.3f} typos={self.typos.ndcg10:.3f} "
            f"corrected={self.typos_corrected.ndcg10:.3f}"
        )


def spelling_ablation(
    name: str, search: Searcher, gold: Sequence[GoldQuery], speller: Corrector
) -> SpellingReport:
    """nDCG@10 of the gold queries, of one-typo variants, and of those variants after correction."""
    bad = [replace(q, query=typo(q.query, k)) for k, q in enumerate(gold)]
    fixed = [replace(q, query=corrected(speller, q.query)) for q in bad]
    changed = sum(1 for a, b in zip(gold, bad, strict=True) if a.query != b.query)
    restored = sum(
        1
        for a, b, c in zip(gold, bad, fixed, strict=True)
        if a.query != b.query and c.query.casefold() == a.query.casefold()
    )
    return SpellingReport(
        queries=len(gold),
        changed_by_typo=changed,
        restored=restored,
        clean=evaluate(f"{name}", search, gold),
        typos=evaluate(f"{name}+typos", search, bad),
        typos_corrected=evaluate(f"{name}+fixed", search, fixed),
    )


def with_static_rank(b: OracleSearcher, *, lexical_weight: float, fusion_weight: float) -> OracleSearcher:
    """The searcher with the static-rank weights changed (the lexical index's config is updated in
    place, so run ablations one after another)."""
    lex = b.lexical
    cfg = getattr(lex, "config", None)
    if cfg is not None and hasattr(cfg, "rank_weight"):
        lex.config = replace(cfg, rank_weight=lexical_weight)  # type: ignore[attr-defined]
    s = replace_searcher(b, dense=b.vectors is not None, rerank=b.reranker is not None)
    s.fusion = replace(b.fusion, static_weight=fusion_weight)
    return s


def rank_ablation(
    name: str, base: OracleSearcher, gold: Sequence[GoldQuery], weights: Sequence[tuple[float, float]]
) -> list[EvalReport]:
    """One report per ``(lexical rank weight, fusion rank weight)`` pair; (0, 0) is "no PageRank"."""
    out: list[EvalReport] = []
    for lw, fw in weights:
        s = with_static_rank(base, lexical_weight=lw, fusion_weight=fw)
        out.append(
            evaluate(f"{name} pr={lw:g}/{fw:g}", lambda q, s=s: [h.doc_id for h in s.run(q).hits], gold)
        )
    return out


def parse_weights(text: str) -> list[tuple[float, float]]:
    """``"0/0,1/0.1"`` -> ``[(0, 0), (1, 0.1)]`` (a lone number is used for both)."""
    out: list[tuple[float, float]] = []
    for part in (x for x in text.split(",") if x.strip()):
        a, _, b = part.partition("/")
        out.append((float(a), float(b or a)))
    return out


# --- CLI ------------------------------------------------------------------------------------------


def api_searcher(base_url: str, *, depth: int = 50, timeout: float = 180.0) -> Searcher:
    """Search the running library over HTTP (no models or index loaded here). Requests are marked as a
    background probe, so they never count as the owner's use; the user agent is generic."""
    import urllib.parse
    import urllib.request

    base = base_url.rstrip("/")

    def search(query: str) -> list[str]:
        url = f"{base}/v1/search?" + urllib.parse.urlencode({"q": query, "limit": depth})
        req = urllib.request.Request(  # noqa: S310 - a loopback URL given on the command line
            url, headers={"X-Whisipedia-Probe": "check", "User-Agent": "Whisipedia-eval/1"}
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - see above
            data = cast(dict[str, Any], json.load(resp))
        return [str(h["doc_id"]) for h in cast(list[dict[str, Any]], data.get("hits") or [])]

    return search


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
    ap.add_argument(
        "--rank-weights",
        default="",
        help="static-rank ablation, lexical/fusion weight pairs, e.g. 0/0,0.5/0.05,1/0.1,2/0.2",
    )
    ap.add_argument("--typos", action="store_true", help="spelling ablation: one-typo queries, corrected")
    ap.add_argument("--out", type=Path, default=None, help="write the full JSON report here")
    ap.add_argument(
        "--api", default=None, help="evaluate the running library at this URL (e.g. http://127.0.0.1:8737)"
    )
    args = ap.parse_args(argv)
    if args.api:
        report = evaluate("api", api_searcher(args.api), load_gold(args.gold), warmup=0)
        print(report.summary())
        for domain, score in sorted(report.by_domain.items()):
            print(f"  {domain:<14} nDCG@10={score:.3f}")
        if args.out is not None:
            payload = {"gold": str(args.gold), "api": args.api, "reports": [asdict(report)]}
            args.out.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return 0

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
    first = next((variants[m] for m in args.modes.split(",") if m in variants), variants["lexical"])
    if args.rank_weights:
        ranked = rank_ablation("rank", first, gold, parse_weights(args.rank_weights))
        for rep in ranked:
            print(rep.summary())
        out["rank_ablation"] = [asdict(r) for r in ranked]
    if args.typos:
        from wsp_index.spelling import SPELL_FILE, Speller

        root = Path(str(cast(dict[str, Any], cfg.get("index", {})).get("root", "")))
        speller = Speller.open(root / "lexical" / SPELL_FILE)
        if speller is None:
            print("typos: skipped (no spelling vocabulary; run `wsp index spell`)", file=sys.stderr)
        else:
            sp = spelling_ablation(
                "spell", lambda q, s=first: [h.doc_id for h in s.run(q).hits], gold, speller
            )
            print(sp.summary())
            out["spelling"] = asdict(sp)
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
        content_hashes=b.content_hashes,
    )


if __name__ == "__main__":
    raise SystemExit(main())
