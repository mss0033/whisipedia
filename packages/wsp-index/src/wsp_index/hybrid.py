"""Hybrid retrieval: reciprocal-rank fusion of lexical and dense results with source priors (PLAN §9.3).

``score(d) = prior(source(d)) * (1 + w_s * static(d)) * sum_r  w_r / (k + rank_r(d))`` with 1-based
ranks, where ``static`` is the link-analysis feature (``wsp_index.linkrank``) when one is
configured. RRF needs no score calibration between BM25 and cosine similarity and is robust to one
retriever being absent, which keeps the dense path optional until Phase 3.

The dense rank of a lexical hit is the better of its dense and its lexical rank: dense retrieval can
lift a lexical hit (or add one BM25 missed), never sink it. Only some sources are embedded, and a
document the dense side does not rank may simply have no vectors; counting that as a miss sank the
embedded source's own best lexical match below 120 others (audit 2026-10-12 #5), and crediting only
the sources without vectors (the previous rule) needed a list of embedded sources read from the
vector store. Now a document without vectors and an embedded one the dense side missed are treated
alike, as if the dense side agreed with BM25; demoting is left to the reranker, which sees the head.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, cast

from wsp_core.ids import DocId, InvalidDocIdError, parse_doc_id
from wsp_core.model import DocRecord, SearchHit
from wsp_core.ports import Embedder, LexicalIndex, VectorIndex
from wsp_index.lexical import LexicalWindow

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


@dataclass(frozen=True, slots=True)
class Fused:
    """One page of fused hits (:meth:`HybridSearcher.fuse`)."""

    hits: Sequence[SearchHit]
    exhaustive: bool
    """``hits`` are every candidate of both retrievers: the lexical window held every match, dense
    retrieval returned fewer than it was asked for, and nothing was cut at ``offset + limit``."""
    lexical: Sequence[SearchHit] = ()
    """The lexical window fused (before the cut at ``offset + limit``)."""


class HybridSearcher:
    """Fuses a ``LexicalIndex`` with an optional ``VectorIndex`` + ``Embedder``.

    Dense-only hits carry no title; ``resolve`` (e.g. ``TantivyIndex.lookup``) fills it in. A hit
    the resolver does not know is a document removed since it was embedded and is dropped (it used
    to be shown with its doc id as title); without a resolver hits keep the doc id as title.

    The static-rank boost ``1 + w_s * static`` is capped: the feature is a percentile in ``[0, 1]``
    (:mod:`wsp_index.linkrank`), so ``w_s = 0.1`` moves a document by at most 10%.
    """

    def __init__(
        self,
        lexical: LexicalIndex,
        vector: VectorIndex | None = None,
        embedder: Embedder | None = None,
        *,
        config: FusionConfig | None = None,
        resolve: Resolver | None = None,
        snippet_free: bool = False,
        partial: bool = True,
        per_source: int | None = None,
    ) -> None:
        if (vector is None) != (embedder is None):
            raise ValueError("vector index and embedder must be given together")
        self.lexical = lexical
        self.vector = vector
        self.embedder = embedder
        self.config = config or FusionConfig()
        self.resolve = resolve
        self.snippet_free = snippet_free  # rank lexical candidates without snippets (see search)
        # False: a lexical window may end with the documents matching every word, when there are enough
        # of them (``TantivyIndex.search_window``); the costly any-word query is then never run.
        self.partial = partial
        self.per_source = per_source  # places one source may take in a shared lexical window

    def prior(self, source_id: str) -> float:
        return self.config.source_priors.get(source_id, self.config.default_prior)

    def search(
        self, query: str, *, limit: int = 10, offset: int = 0, sources: Sequence[str] | None = None
    ) -> Sequence[SearchHit]:
        return self.fuse(query, limit=limit, offset=offset, sources=sources).hits

    def _lexical(self, query: str, depth: int, sources: Sequence[str] | None) -> LexicalWindow:
        """The lexical candidates: without snippets when the index can (a window of ``depth`` is ranked,
        a page shown; the caller cuts snippets for what it shows, ``snippets``)."""
        if self.snippet_free:
            window = getattr(self.lexical, "search_window", None)
            if callable(window):
                found = window(
                    query, limit=depth, sources=sources, partial=self.partial, per_source=self.per_source
                )
                return cast(LexicalWindow, found)
        cheap = getattr(self.lexical, "search_hits", None)
        run = cast(Any, cheap) if callable(cheap) and self.snippet_free else self.lexical.search
        hits = list(cast(Sequence[SearchHit], run(query, limit=depth, sources=sources)))
        return LexicalWindow(hits, len(hits) < depth)

    def fuse(
        self, query: str, *, limit: int = 10, offset: int = 0, sources: Sequence[str] | None = None
    ) -> Fused:
        """A page of fused hits, and whether they are every candidate (a total can be exact)."""
        if limit <= 0 or offset < 0 or not query.strip():
            return Fused([], True)
        cfg = self.config
        depth = max(cfg.depth, offset + limit)
        window = self._lexical(query, depth, sources)
        lexical = list(window.hits)
        dense: list[tuple[str, float]] = []
        dense_all = True  # the dense side returned everything it had
        if self.vector is not None and self.embedder is not None:
            qvec = self.embedder.embed([query])[0]
            allowed = None if sources is None else frozenset(sources)
            # Over-fetch when filtering by source, since the ANN cannot filter.
            fetch = depth if allowed is None else depth * 4
            found = self.vector.search(qvec, limit=fetch)
            kept = [(d, s) for d, s in found if allowed is None or _source_of(d) in allowed]
            dense = kept[:depth]
            dense_all = len(found) < fetch and len(kept) <= depth

        fused: dict[str, float] = {}
        info: dict[str, SearchHit] = {}
        dense_rank = {doc_id: rank for rank, (doc_id, _sim) in enumerate(dense, start=1)}
        for rank, hit in enumerate(lexical, start=1):
            fused[hit.doc_id] = fused.get(hit.doc_id, 0.0) + cfg.lexical_weight / (cfg.k + rank)
            info[hit.doc_id] = hit
            if dense:  # the better of its dense and its lexical rank (module docstring)
                best = min(dense_rank.get(hit.doc_id, rank), rank)
                fused[hit.doc_id] += cfg.dense_weight / (cfg.k + best)
        for doc_id, rank in dense_rank.items():
            if doc_id not in info:
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
        cut = False
        for score, doc_id in scored:
            if len(out) >= offset + limit:
                cut = True
                break
            hit = info.get(doc_id)
            if hit is None:
                if self.resolve is not None:
                    hit = self.resolve(doc_id)
                    if hit is None:
                        continue  # removed from the library since it was embedded: never shown
                else:
                    hit = SearchHit(DocId(doc_id), 0.0, doc_id, "", _source_of(doc_id))
            out.append(replace(hit, score=score))
        return Fused(out[offset:], window.exhaustive and dense_all and not cut, lexical)


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
