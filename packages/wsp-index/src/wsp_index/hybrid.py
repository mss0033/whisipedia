"""Hybrid retrieval: reciprocal-rank fusion of lexical and dense results with source priors (PLAN §9.3).

``score(d) = prior(source(d)) * (1 + w_s * static(d)) * sum_r  w_r / (k + rank_r(d))`` with 1-based
ranks, where ``static`` is the link-analysis feature (``wsp_index.linkrank``) when one is
configured. RRF needs no score calibration between BM25 and cosine similarity and is robust to one
retriever being absent, which keeps the dense path optional until Phase 3.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace

from wsp_core.ids import DocId, InvalidDocIdError, parse_doc_id
from wsp_core.model import DocRecord, SearchHit
from wsp_core.ports import Embedder, LexicalIndex, VectorIndex

Resolver = Callable[[str], SearchHit | None]
StaticFeatures = Callable[[Sequence[str]], Sequence[float]]


def _source_of(doc_id: str) -> str:
    try:
        return parse_doc_id(doc_id)[0]
    except InvalidDocIdError:
        return ""


@dataclass(frozen=True, slots=True)
class FusionConfig:
    k: int = 60
    """RRF damping constant (60 is the value from Cormack et al. 2009)."""
    lexical_weight: float = 1.0
    dense_weight: float = 1.0
    depth: int = 50
    """Candidates taken from each retriever (at least ``offset + limit``)."""
    source_priors: Mapping[str, float] = field(default_factory=dict[str, float])
    """Multiplicative quality prior per source id; missing sources get ``default_prior``."""
    default_prior: float = 1.0
    static_rank: StaticFeatures | None = None
    """Link-analysis feature per doc id (``TantivyIndex.static_rank().features``); None: off."""
    static_weight: float = 0.0
    """``w_s`` above; 0 switches the static rank off in fusion."""


class HybridSearcher:
    """Fuses a ``LexicalIndex`` with an optional ``VectorIndex`` + ``Embedder``.

    Dense-only hits carry no title; ``resolve`` (e.g. ``TantivyIndex.lookup``) fills it in. Hits it
    cannot resolve keep the doc id as title, so nothing is silently dropped.
    """

    def __init__(
        self,
        lexical: LexicalIndex,
        vector: VectorIndex | None = None,
        embedder: Embedder | None = None,
        *,
        config: FusionConfig | None = None,
        resolve: Resolver | None = None,
    ) -> None:
        if (vector is None) != (embedder is None):
            raise ValueError("vector index and embedder must be given together")
        self.lexical = lexical
        self.vector = vector
        self.embedder = embedder
        self.config = config or FusionConfig()
        self.resolve = resolve

    def prior(self, source_id: str) -> float:
        return self.config.source_priors.get(source_id, self.config.default_prior)

    def search(
        self, query: str, *, limit: int = 10, offset: int = 0, sources: Sequence[str] | None = None
    ) -> Sequence[SearchHit]:
        if limit <= 0 or offset < 0 or not query.strip():
            return []
        cfg = self.config
        depth = max(cfg.depth, offset + limit)
        lexical = list(self.lexical.search(query, limit=depth, sources=sources))
        dense: list[tuple[str, float]] = []
        if self.vector is not None and self.embedder is not None:
            qvec = self.embedder.embed([query])[0]
            allowed = None if sources is None else frozenset(sources)
            # Over-fetch when filtering by source, since the ANN cannot filter.
            fetch = depth if allowed is None else depth * 4
            dense = [
                (d, s)
                for d, s in self.vector.search(qvec, limit=fetch)
                if allowed is None or _source_of(d) in allowed
            ][:depth]

        fused: dict[str, float] = {}
        info: dict[str, SearchHit] = {}
        for rank, hit in enumerate(lexical, start=1):
            fused[hit.doc_id] = fused.get(hit.doc_id, 0.0) + cfg.lexical_weight / (cfg.k + rank)
            info[hit.doc_id] = hit
        for rank, (doc_id, _sim) in enumerate(dense, start=1):
            fused[doc_id] = fused.get(doc_id, 0.0) + cfg.dense_weight / (cfg.k + rank)

        static: dict[str, float] = {}
        if cfg.static_rank is not None and cfg.static_weight > 0 and fused:
            ids = list(fused)
            static = dict(zip(ids, (float(x) for x in cfg.static_rank(ids)), strict=True))
        scored: list[tuple[float, str]] = []
        for doc_id, s in fused.items():
            src = info[doc_id].source_id if doc_id in info else _source_of(doc_id)
            boost = 1.0 + cfg.static_weight * static.get(doc_id, 0.0)
            scored.append((s * self.prior(src) * boost, doc_id))
        scored.sort(key=lambda t: (-t[0], t[1]))

        out: list[SearchHit] = []
        for score, doc_id in scored[offset : offset + limit]:
            hit = info.get(doc_id)
            if hit is None:
                resolved = self.resolve(doc_id) if self.resolve is not None else None
                hit = resolved or SearchHit(DocId(doc_id), 0.0, doc_id, "", _source_of(doc_id))
            out.append(replace(hit, score=score))
        return out


def embed_documents(
    docs: Iterable[DocRecord], embedder: Embedder, vector: VectorIndex, *, batch_size: int = 256
) -> int:
    """Embed ``title + text`` of each document into ``vector`` (whole-document granularity for now;
    Phase 3 chunks into ~200-word passages)."""
    n = 0
    ids: list[str] = []
    texts: list[str] = []

    def flush() -> None:
        if ids:
            vector.upsert(list(ids), embedder.embed(texts))
            ids.clear()
            texts.clear()

    for doc in docs:
        ids.append(doc.doc_id)
        texts.append(f"{doc.title}\n{doc.text}")
        n += 1
        if len(ids) >= batch_size:
            flush()
    flush()
    return n
