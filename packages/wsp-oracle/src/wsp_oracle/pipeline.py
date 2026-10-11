"""The query pipeline (PLAN §9.3): router → lexical + dense → RRF with priors → rerank top 50.

:class:`OracleSearcher` runs one query end to end and returns an :class:`OracleResult` with timings
and a confidence signal. :class:`OracleIndex` wraps it as a ``LexicalIndex`` so the API's search
route uses it unchanged; it caches recent result lists so paging and facet queries reuse one run.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import cast

import numpy as np

from wsp_core.ids import DocId, InvalidDocIdError, parse_doc_id
from wsp_core.model import DocRecord, SearchHit
from wsp_core.ports import Embedder, LexicalIndex, Reranker
from wsp_index import FusionConfig, HybridSearcher
from wsp_oracle.embedder import encode_queries
from wsp_oracle.reranker import plain
from wsp_oracle.router import Intent, Route, Router, content_terms
from wsp_oracle.vectors import DocVectorIndex

log = logging.getLogger("wsp_oracle")

Resolver = Callable[[str], SearchHit | None]
PREFER_BOOST = 1.5


def source_of(doc_id: str) -> str:
    try:
        return parse_doc_id(doc_id)[0]
    except InvalidDocIdError:
        return ""


@dataclass(frozen=True, slots=True)
class Scope:
    """Where a query that names a site was searched (``rfc 9110`` -> the ``rfc`` sources)."""

    site: str
    """The site word as the router read it (``rfc``)."""
    sources: tuple[str, ...]
    """The sources that word names."""
    fallback: bool = False
    """True when those sources had nothing for the query and the whole library was searched."""

    def searched(self) -> tuple[str, ...] | None:
        return None if self.fallback else self.sources


@dataclass(frozen=True, slots=True)
class OracleResult:
    query: str
    route: Route
    hits: tuple[SearchHit, ...]
    passages: Mapping[str, str] = field(default_factory=dict[str, str])
    """Best passage text per doc id (what the reranker scored)."""
    confidence: float | None = None
    """Best reranker probability; None without a reranker."""
    low_confidence: bool = False
    timings_ms: Mapping[str, float] = field(default_factory=dict[str, float])
    scope: Scope | None = None


class _StaticDense:
    """A ``VectorIndex`` answering with precomputed results (one query's dense retrieval)."""

    def __init__(self, results: Sequence[tuple[str, float]]) -> None:
        self._results = list(results)

    def upsert(self, ids: Sequence[str], vectors: Sequence[Sequence[float]]) -> None:
        raise NotImplementedError

    def delete(self, ids: Sequence[str]) -> None:
        raise NotImplementedError

    def search(self, vector: Sequence[float], *, limit: int = 50) -> Sequence[tuple[str, float]]:
        return self._results[:limit]


class _NoEmbed:
    dimensions = 1

    def embed(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        return [[0.0] for _ in texts]


def _title_key(title: str) -> str:
    return " ".join(title.replace("_", " ").casefold().split())


class OracleSearcher:
    def __init__(
        self,
        lexical: LexicalIndex,
        *,
        router: Router | None = None,
        embedder: Embedder | None = None,
        vectors: DocVectorIndex | None = None,
        reranker: Reranker | None = None,
        fusion: FusionConfig | None = None,
        rerank_top: int = 50,
        resolve: Resolver | None = None,
        passage_text: Callable[[Sequence[str]], Mapping[str, str]] | None = None,
        low_confidence: float = 0.05,
        budget_ms: float = 100.0,
    ) -> None:
        if (embedder is None) != (vectors is None):
            raise ValueError("embedder and vectors must be given together")
        self.lexical = lexical
        self.router = router or Router()
        self.embedder = embedder
        self.vectors = vectors
        self.reranker = reranker
        self.fusion = fusion or FusionConfig()
        self.rerank_top = rerank_top
        self.resolve = resolve
        self.passage_text = passage_text
        self.low_confidence = low_confidence
        self.budget_ms = budget_ms

    # --- stages --------------------------------------------------------------------------------

    def _dense(
        self, route: Route, depth: int, sources: Sequence[str] | None
    ) -> tuple[list[tuple[str, float]], dict[str, str]]:
        if self.embedder is None or self.vectors is None or not route.dense:
            return [], {}
        q = encode_queries(self.embedder, [route.dense], self.vectors.dimensions)[0]
        fetch = depth if sources is None else depth * 4
        docs = self.vectors.search_docs(q, limit=fetch)
        return [(d, s) for d, s, _ in docs], {d: c for d, _, c in docs}

    def _exact_title(self, title: str, sources: Sequence[str] | None) -> SearchHit | None:
        key = _title_key(title)
        if not key:
            return None
        for h in self.lexical.search(title, limit=10, sources=sources):
            if _title_key(h.title) == key:
                return h
        return None

    def _pins(self, route: Route, fused: list[SearchHit], sources: Sequence[str] | None) -> list[SearchHit]:
        """Hits that must lead: the exact title for page-name intents, each side of a comparison."""
        wanted: list[str] = []
        if route.intent in (Intent.NAVIGATE, Intent.LOOKUP, Intent.DEFINE, Intent.LOCATE) and route.title:
            wanted = [route.title]
        elif route.intent is Intent.COMPARE:
            wanted = list(route.parts)
        pins: list[SearchHit] = []
        for title in wanted:
            key = _title_key(title)
            hit = next((h for h in fused if _title_key(h.title) == key), None) or self._exact_title(
                title, sources
            )
            if hit is not None and all(p.doc_id != hit.doc_id for p in pins):
                pins.append(hit)
        return pins

    def _boost(self, route: Route, fused: list[SearchHit]) -> list[SearchHit]:
        if not route.prefer:
            return fused
        boosted = [
            replace(h, score=h.score * PREFER_BOOST)
            if any(p in (h.source_id or source_of(h.doc_id)) for p in route.prefer)
            else h
            for h in fused
        ]
        return sorted(boosted, key=lambda h: -h.score)

    def _passages(self, hits: Sequence[SearchHit], best_chunk: Mapping[str, str]) -> dict[str, str]:
        out: dict[str, str] = {}
        if self.passage_text is not None:
            ids = [best_chunk[h.doc_id] for h in hits if h.doc_id in best_chunk]
            leads = [h.doc_id for h in hits if h.doc_id not in best_chunk]
            texts = self.passage_text([*ids, *(f"{d}#0" for d in leads)])
            for h in hits:
                chunk = best_chunk.get(h.doc_id, f"{h.doc_id}#0")
                if chunk in texts:
                    out[h.doc_id] = texts[chunk]
        for h in hits:
            out.setdefault(h.doc_id, plain(h.snippet))
        return out

    # --- scope ---------------------------------------------------------------------------------

    def _any_hit(self, query: str, sources: Sequence[str]) -> bool:
        fast = getattr(self.lexical, "search_hits", None)
        if callable(fast):
            return bool(cast(Sequence[SearchHit], fast(query, limit=1, sources=sources)))
        return bool(self.lexical.search(query, limit=1, sources=sources))

    def scoped(self, query: str, sources: Sequence[str] | None) -> tuple[Route, Scope | None]:
        """The route and, for a query naming a site, where to search.

        An explicit ``sources`` filter always wins. A named site with nothing for the query (not
        indexed, or no match there) falls back to the whole library with the full query text, so
        ``rfc 9110`` still finds pages about RFC 9110 when the RFC site is missing.
        """
        route = self.router.route(query)
        if not route.site or sources is not None:
            return route, None
        named = self.router.sources_for(route.site)
        if not named:
            return route, None
        if (route.lexical or route.dense) and self._any_hit(route.lexical or route.dense, named):
            return route, Scope(route.site, tuple(named))
        whole = " ".join(content_terms(route.query)) or route.query
        return replace(route, lexical=whole, dense=route.query), Scope(route.site, tuple(named), True)

    # --- the pipeline --------------------------------------------------------------------------

    def run(
        self, query: str, *, sources: Sequence[str] | None = None, depth: int | None = None
    ) -> OracleResult:
        t0 = time.perf_counter()
        timings: dict[str, float] = {}

        def lap(name: str, since: float) -> float:
            now = time.perf_counter()
            timings[name] = round((now - since) * 1000, 3)
            return now

        route, scope = self.scoped(query, sources)
        if scope is not None:
            searched = scope.searched()
            sources = None if searched is None else list(searched)
        t = lap("route", t0)
        if not route.lexical and not route.dense:
            return OracleResult(query, route, (), low_confidence=True, timings_ms=timings, scope=scope)
        depth = max(depth or self.fusion.depth, self.rerank_top)
        dense, best_chunk = self._dense(route, depth, sources)
        t = lap("dense", t)
        hybrid = HybridSearcher(
            self.lexical,
            _StaticDense(dense) if self.vectors is not None else None,
            _NoEmbed() if self.vectors is not None else None,
            config=replace(self.fusion, depth=depth),
            resolve=self.resolve,
        )
        fused = list(hybrid.search(route.lexical or route.dense, limit=depth, sources=sources))
        fused = self._boost(route, fused)
        t = lap("lexical+fusion", t)
        pins = self._pins(route, fused, sources)
        pinned = {p.doc_id for p in pins}
        rest = [h for h in fused if h.doc_id not in pinned]
        t = lap("titles", t)

        head, tail = rest[: self.rerank_top], rest[self.rerank_top :]
        passages = self._passages([*pins, *head], best_chunk)
        confidence: float | None = None
        if self.reranker is not None and head:
            display = {h.doc_id: h.snippet for h in head}
            probe = [replace(h, snippet=passages[h.doc_id]) for h in [*pins, *head]]
            ranked = list(self.reranker.rerank(route.dense or query, probe))
            confidence = max((h.score for h in ranked), default=None)
            scores = {h.doc_id: h.score for h in ranked}
            pins = [replace(p, score=scores.get(p.doc_id, p.score)) for p in pins]
            head = [replace(h, snippet=display[h.doc_id]) for h in ranked if h.doc_id in display]
            t = lap("rerank", t)
        hits = tuple([*pins, *head, *tail])
        total = (time.perf_counter() - t0) * 1000
        timings["total"] = round(total, 3)
        if total > self.budget_ms:
            log.info("oracle query over budget: %.1f ms (%s)", total, timings)
        low = not hits or (confidence is not None and confidence < self.low_confidence)
        return OracleResult(query, route, hits, passages, confidence, low, timings, scope)

    def search(self, query: str, *, limit: int = 10, sources: Sequence[str] | None = None) -> list[SearchHit]:
        return list(self.run(query, sources=sources).hits[:limit])


class OracleIndex:
    """``LexicalIndex`` port over :class:`OracleSearcher`; writes go to the wrapped lexical index.

    Vectors are maintained by ``embed_generation`` (a batch job), never by the API process.
    """

    def __init__(self, searcher: OracleSearcher, *, cache_size: int = 256) -> None:
        self.searcher = searcher
        self.lexical = searcher.lexical
        self._cache: OrderedDict[tuple[str, tuple[str, ...] | None], OracleResult] = OrderedDict()
        self._cache_size = cache_size
        self._lock = threading.Lock()

    def result(self, query: str, *, sources: Sequence[str] | None = None, depth: int = 0) -> OracleResult:
        key = (" ".join(query.split()), None if sources is None else tuple(sorted(sources)))
        with self._lock:
            hit = self._cache.get(key)
            if hit is not None and (depth <= len(hit.hits) or len(hit.hits) < self.searcher.fusion.depth):
                self._cache.move_to_end(key)
                return hit
        res = self.searcher.run(query, sources=sources, depth=depth or None)
        with self._lock:
            self._cache[key] = res
            while len(self._cache) > self._cache_size:
                self._cache.popitem(last=False)
        return res

    def search(
        self, query: str, *, limit: int = 10, offset: int = 0, sources: Sequence[str] | None = None
    ) -> Sequence[SearchHit]:
        if limit <= 0 or offset < 0 or not query.strip():
            return []
        if sources is not None and not sources:
            return []
        res = self.result(query, sources=sources, depth=offset + limit)
        return list(res.hits[offset : offset + limit])

    def suggest(self, prefix: str, *, limit: int = 10) -> Sequence[str]:
        return self.lexical.suggest(prefix, limit=limit)

    def scope(self, query: str, sources: Sequence[str] | None = None) -> Scope | None:
        """Where :meth:`search` looks for this query (a named site, or the whole library)."""
        return self.result(query, sources=sources).scope

    def search_hits(
        self, query: str, *, limit: int = 10, offset: int = 0, sources: Sequence[str] | None = None
    ) -> Sequence[SearchHit]:
        """Cheap, snippet-free lexical window for totals and facets (the ranked page uses :meth:`search`).

        It is routed exactly like :meth:`search`: a query naming a site counts that site's matches
        for the rewritten query (or the whole library's, after a fallback), so totals and facets
        agree with the results shown.
        """
        if limit <= 0 or offset < 0 or not query.strip() or (sources is not None and not sources):
            return []
        route, scope = self.searcher.scoped(query, sources)
        if scope is not None:
            searched = scope.searched()
            sources = None if searched is None else list(searched)
        text = route.lexical or route.dense
        if not text:
            return []
        fast = getattr(self.lexical, "search_hits", None)
        if callable(fast):
            return cast(Sequence[SearchHit], fast(text, limit=limit, offset=offset, sources=sources))
        return self.lexical.search(text, limit=limit, offset=offset, sources=sources)

    def lookup(self, doc_id: str) -> SearchHit | None:
        find = getattr(self.lexical, "lookup", None)
        return cast(SearchHit | None, find(doc_id)) if callable(find) else None

    def _invalidate(self) -> None:
        with self._lock:
            self._cache.clear()

    def upsert(self, docs: Iterable[DocRecord]) -> int:
        self._invalidate()
        return self.lexical.upsert(docs)

    def delete(self, doc_ids: Iterable[DocId]) -> int:
        self._invalidate()
        return self.lexical.delete(doc_ids)

    def commit(self) -> None:
        self.lexical.commit()
        self._invalidate()

    def close(self) -> None:
        self.lexical.close()


def cosine_top(query: np.ndarray, matrix: np.ndarray, k: int) -> list[int]:
    """Indices of the ``k`` rows most similar to ``query`` (rows and query L2-normalized)."""
    sims = matrix @ query
    k = min(k, len(sims))
    if k <= 0:
        return []
    top = np.argpartition(-sims, k - 1)[:k]
    return [int(i) for i in top[np.argsort(-sims[top], kind="stable")]]
