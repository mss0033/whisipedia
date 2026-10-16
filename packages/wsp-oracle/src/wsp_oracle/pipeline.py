"""The query pipeline (PLAN §9.3): router → lexical + dense → RRF with priors → rerank top 50.

:class:`OracleSearcher` runs one query end to end and returns an :class:`OracleResult` with timings
and a confidence signal. :class:`OracleIndex` wraps it as a ``LexicalIndex`` so the API's search
route uses it unchanged; it caches recent result lists so paging and facet queries reuse one run.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterable, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from typing import cast

import numpy as np

from wsp_core.ids import DocId, InvalidDocIdError, content_hash, parse_doc_id
from wsp_core.model import DocRecord, SearchHit
from wsp_core.ports import Embedder, LexicalIndex, Reranker
from wsp_index import FusionConfig, HybridSearcher
from wsp_index.text import primary_lang, query_language
from wsp_oracle.embedder import encode_queries
from wsp_oracle.reranker import plain
from wsp_oracle.router import Intent, Route, Router
from wsp_oracle.vectors import DocVectorIndex

log = logging.getLogger("wsp_oracle")

Resolver = Callable[[str], SearchHit | None]
ContentHashes = Callable[[Sequence[str]], Mapping[str, str]]
PREFER_BOOST = 1.5
# Soft preference for documents in the query's language (English, the interface language, when the
# query does not say): the cross-encoder's scores saturate near 1.0 for a page of good matches, so
# their order there is noise ("python list comprehension" led with a Spanish Stack Overflow question).
LANG_BONUS = 0.01  # added to reranker scores (0-1): reorders near-ties only
LANG_BOOST = 1.15  # multiplies fused scores below the reranked head
SOURCE_SHARE = 8
"""One source takes at most 1/8 of the lexical window while other sources have matches (``per_source``)."""
TITLE_PINS = 8
"""Near-exact titles pinned after the exact one for a page-name query, one per source, at most."""


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
    exhaustive: bool = False
    """``hits`` are every match for the query: the retrievers ran out before the depth asked for, so a
    deeper run finds nothing more and a count of ``hits`` is exact. Not the same as ``len(hits)`` being
    under that depth: collapsed duplicates and filtered hits make a list shorter, not complete."""


class _StaticDense:
    """A ``VectorIndex`` answering with one query's dense retrieval, computed in parallel with the
    lexical search (``results`` is a future, resolved when fusion asks for it)."""

    def __init__(self, results: Future[tuple[list[tuple[str, float]], dict[str, str], float]]) -> None:
        self._future = results

    @property
    def _results(self) -> list[tuple[str, float]]:
        return self._future.result()[0]

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


_BOOK_INTENT = re.compile(
    r"\b(books?|novels?|editions?|isbn|authors?|publishers?|catalog(ue)?|bibliography|written by)\b", re.I
)
_RESEARCH_INTENT = re.compile(
    r"\b(study|studies|trials?|papers?|journals?|evidence|abstracts?|meta-analysis|research|pubmed|cohort)\b",
    re.I,
)
_LEGAL_INTENT = re.compile(
    r"\b(v\.?|vs\.?|court|courts|case law|cases?|rulings?|ruled|opinions?|statutes?|laws?|legal|lawsuits?|"
    r"sued|precedent|plaintiffs?|defendants?|appeals?|judges?|liability|negligence|constitution(al)?)\b",
    re.I,
)


def weight_exempt(query: str) -> frozenset[str]:
    """Sources whose weight a query lifts: the catalog when it looks for a book, abstracts when it
    looks for research, court opinions when it asks about the law."""
    out: set[str] = set()
    if _BOOK_INTENT.search(query):
        out.add("open-library")
    if _RESEARCH_INTENT.search(query):
        out.add("pubmed")
    if _LEGAL_INTENT.search(query):
        out.add("courtlistener")
    return frozenset(out)


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
        content_hashes: ContentHashes | None = None,
        source_weights: Mapping[str, float] | None = None,
    ) -> None:
        """``content_hashes`` (``ParquetDocumentStore.content_hashes``) tells duplicate records from
        distinct documents that share a title; without it no hit is collapsed."""
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
        self.content_hashes = content_hashes
        self.source_weights = dict(source_weights or {})
        self._pool: ThreadPoolExecutor | None = None
        self._pool_lock = threading.Lock()

    # --- stages --------------------------------------------------------------------------------

    def _dense(
        self, route: Route, depth: int, sources: Sequence[str] | None
    ) -> tuple[list[tuple[str, float]], dict[str, str]]:
        """Dense retrieval; a source filter is applied *in* the candidate search (which widens until
        enough allowed passages are found), not after a fixed over-fetch."""
        if self.embedder is None or self.vectors is None or not route.dense:
            return [], {}
        q = encode_queries(self.embedder, [route.dense], self.vectors.dimensions)[0]
        allowed = None if sources is None else frozenset(sources)

        def allow(chunk_id: str) -> bool:
            return allowed is not None and source_of(chunk_id) in allowed

        docs = self.vectors.search_docs(q, limit=depth, allow=None if allowed is None else allow)
        return [(d, s) for d, s, _ in docs], {d: c for d, _, c in docs}

    def _dense_pool(self) -> ThreadPoolExecutor:
        with self._pool_lock:
            if self._pool is None:
                self._pool = ThreadPoolExecutor(4, thread_name_prefix="oracle-dense")
            return self._pool

    def close(self) -> None:
        """Stop the dense-retrieval threads (they also stop when the searcher is collected)."""
        with self._pool_lock:
            if self._pool is not None:
                self._pool.shutdown(wait=False)
                self._pool = None

    def _timed_dense(
        self, route: Route, depth: int, sources: Sequence[str] | None
    ) -> tuple[list[tuple[str, float]], dict[str, str], float]:
        t = time.perf_counter()
        dense, best = self._dense(route, depth, sources)
        return dense, best, round((time.perf_counter() - t) * 1000, 3)

    def _exact_title(self, title: str, sources: Sequence[str] | None) -> SearchHit | None:
        key = _title_key(title)
        if not key:
            return None
        # Snippet-free: a pin's snippet is cut with the page's (``with_snippets``); ``search`` read ten
        # documents' texts over the NAS for one title (1-2 s of a query, 2026-10-13).
        fast = getattr(self.lexical, "search_hits", None)
        found = (
            fast(title, limit=10, sources=sources)
            if callable(fast)
            else self.lexical.search(title, limit=10, sources=sources)
        )
        for h in cast(Sequence[SearchHit], found):
            if _title_key(h.title) == key:
                return h
        return None

    def _pinnable(self, query: str, hit: SearchHit) -> bool:
        """A down-weighted source (catalog cards, abstracts) never leads by its title alone, unless the
        query asks for it: "capital of australia" pinned an Open Library record of that title above
        the article on Canberra (2026-10-14)."""
        src = hit.source_id or source_of(hit.doc_id)
        return self.source_weights.get(src, 1.0) >= 1.0 or src in weight_exempt(query)

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
            hit = next(
                (h for h in fused if _title_key(h.title) == key and self._pinnable(route.query, h)), None
            ) or self._exact_title(title, sources)
            if hit is not None and not self._pinnable(route.query, hit):
                hit = None
            if hit is not None and all(p.doc_id != hit.doc_id for p in pins):
                pins.append(hit)
        return pins

    def _title_pins(self, route: Route, candidates: Iterable[SearchHit], pinned: set[str]) -> list[SearchHit]:
        """For a page-name query, each source's best near-exact title: one holding every word of the
        query and at most as many others, in the query's language first (English when the query does
        not say), then the shortest, then the best ranked.

        A title query lost the book it names: "huckleberry finn" across all sites showed no Gutenberg
        book (Open Library's editions and Stack Exchange users named so outscored its long text in
        BM25), and within Gutenberg a Hungarian translation led the English edition (the reranker's
        scores saturate near 1.0 for every title match; 2026-10-12). Translations and other sites'
        titles are still found, one per source, after the query language's. A title with a qualifier
        in parentheses comes after one without: "new york times" led with "New York Times
        (disambiguation)" over "The New York Times", both four words (2026-10-16).
        """
        if route.intent not in (Intent.NAVIGATE, Intent.LOOKUP) or not route.title:
            return []
        words = set(_words_of(route.title))
        if not words:
            return []
        lang = query_language(route.title.split()) or "en"
        best: dict[str, tuple[tuple[bool, bool, int, int], SearchHit]] = {}
        for i, h in enumerate(candidates):
            title = _words_of(h.title)
            if h.doc_id in pinned or not words <= set(title) or len(title) > 2 * len(words):
                continue
            if not self._pinnable(route.query, h):
                continue
            key = (not h.lang or primary_lang(h.lang) != lang, "(" in h.title, len(title), i)
            src = h.source_id or source_of(h.doc_id)
            if src not in best or key < best[src][0]:
                best[src] = (key, h)
        return [h for _, h in sorted(best.values(), key=lambda t: t[0])]

    def _embedded_title(
        self, route: Route, candidates: Iterable[SearchHit], pinned: set[str]
    ) -> list[SearchHit]:
        """For a question or a how-to, the candidate whose title (two words or more) the query holds
        word for word, the longest such title first, then the best ranked; at most one. The query
        "Incident Command System span of control" listed CIMS, NIMS and AIIMS above the article
        Incident Command System, 17th (2026-10-16 field exam). One-word titles never qualify: every
        question about water holds the title "Water".

        A page-name query whose own title pins nothing qualifies too: "Kant categorical imperative"
        put "Categorical" and the Simple English article first and the English Wikipedia article
        20th (2026-10-16 field exam). Between equal titles the heavier source leads.

        First, though, a page whose title begins with the whole query (four words or more; the
        shortest such title): a title too long for a page-name query is asked as a question, and
        "Pilot's Handbook of Aeronautical Knowledge" led with the handbook's Addendum, not the
        handbook "Pilot's Handbook of Aeronautical Knowledge, FAA-H-8083-25C (Full Version)"
        (2026-10-16 field exam)."""
        if route.intent not in (Intent.ASK, Intent.HOW_TO) and not (
            route.intent is Intent.LOOKUP and not pinned
        ):
            return []
        query = _words_of(route.query)
        best: tuple[tuple[int, int, float, int], SearchHit] | None = None
        for i, h in enumerate(candidates):
            title = _words_of(h.title)
            if h.doc_id in pinned:
                continue
            src = h.source_id or source_of(h.doc_id)
            weight = -self.source_weights.get(src, 1.0)  # a tie goes to the reference copy
            if len(query) >= 4 and title[: len(query)] == query:
                key = (0, len(title), weight, i)
            elif 2 <= len(title) < len(query) and _holds(query, title):
                key = (1, -len(title), weight, i)
            else:
                continue
            if not self._pinnable(route.query, h):
                continue
            if best is None or key < best[0]:
                best = (key, h)
        return [] if best is None else [best[1]]

    def _weigh(self, query: str, hits: list[SearchHit]) -> list[SearchHit]:
        """Hits scaled by their source's weight (``source_weights``), best first; a source the query
        asks for by its nature (a book for the catalog, research for abstracts) keeps its score."""
        if not self.source_weights or not hits:
            return hits
        exempt = weight_exempt(query)
        out: list[SearchHit] = []
        for h in hits:
            src = h.source_id or source_of(h.doc_id)
            w = 1.0 if src in exempt else self.source_weights.get(src, 1.0)
            out.append(replace(h, score=h.score * w) if w != 1.0 else h)
        return sorted(out, key=lambda h: -h.score)

    def _boost(self, route: Route, fused: list[SearchHit]) -> list[SearchHit]:
        """Soft priors: hits from preferred sources (a site named without an identifier, dictionaries
        for "define", maps for "where is") score ``PREFER_BOOST`` times higher."""
        if not route.prefer and not route.prefer_sources:
            return fused
        exact = frozenset(route.prefer_sources)
        projects = frozenset(route.prefer)

        def preferred(h: SearchHit) -> bool:
            src = h.source_id or source_of(h.doc_id)
            if src in exact or _project(src) in projects:
                return True
            # A bundle's book is the first part of the native id ("kiwix-curated-t2/wikibooks_en_all/...").
            book, sep, _ = h.doc_id.removeprefix(f"wsp:{src}/").partition("/")
            return bool(sep) and _project(book) in projects

        boosted = [replace(h, score=h.score * PREFER_BOOST) if preferred(h) else h for h in fused]
        return sorted(boosted, key=lambda h: -h.score)

    def _passages(self, hits: Sequence[SearchHit], best_chunk: Mapping[str, str]) -> dict[str, str]:
        """What the reranker scores per hit: the passage that matched. For a dense hit its best
        chunk; for a lexical-only hit BM25's snippet (the matching region), and the document's lead
        passage only when there is no snippet."""
        out: dict[str, str] = {}
        for h in hits:
            if h.doc_id not in best_chunk and h.snippet.strip():
                out[h.doc_id] = plain(h.snippet)
        if self.passage_text is not None:
            wanted = {
                h.doc_id: best_chunk.get(h.doc_id, f"{h.doc_id}#0") for h in hits if h.doc_id not in out
            }
            texts = self.passage_text(list(wanted.values()))
            for doc_id, chunk in wanted.items():
                if chunk in texts:
                    out[doc_id] = texts[chunk]
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
        """The route and, for a query naming a site and an identifier, where to search.

        Only ``<site> <identifier>`` (``rfc 9110``) scopes; a site named with other words is a soft
        prior (``Route.prefer_sources``). An explicit ``sources`` filter always wins. A named site
        with nothing for the query (not indexed, or no match there) falls back to the whole library
        with the full query text, so ``rfc 9110`` still finds pages about RFC 9110 when the RFC
        site is missing.
        """
        route = self.router.route(query)
        if not route.site or not route.scoped or sources is not None:
            return route, None
        named = self.router.sources_for(route.site)
        if not named:
            return route, None
        if (route.lexical or route.dense) and self._any_hit(route.lexical or route.dense, named):
            return route, Scope(route.site, tuple(named))
        return replace(route, lexical=route.query, dense=route.query), Scope(route.site, tuple(named), True)

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
            return OracleResult(
                query, route, (), low_confidence=True, timings_ms=timings, scope=scope, exhaustive=True
            )
        # Never fewer candidates than the configured depth: the ranking of the first results must not
        # depend on the page size (I'm Feeling Lucky asks for one hit, the results page for ten; E2E-6).
        depth = self.depth_for(depth)
        if self.vectors is None:
            dense_future: Future[tuple[list[tuple[str, float]], dict[str, str], float]] = Future()
            dense_future.set_result(([], {}, 0.0))
        else:  # dense retrieval runs beside BM25
            dense_future = self._dense_pool().submit(self._timed_dense, route, depth, sources)
        hybrid = HybridSearcher(
            self.lexical,
            _StaticDense(dense_future) if self.vectors is not None else None,
            _NoEmbed() if self.vectors is not None else None,
            config=replace(self.fusion, depth=depth),
            resolve=self.resolve,
            snippet_free=self._cut is not None,  # snippets are cut below, for the head only
            # Enough every-word matches are the lexical candidates on their own: the any-word query costs
            # seconds on the full library (audit 2026-10-12 #7). The result then is not exhaustive.
            partial=False,
            per_source=max(depth // SOURCE_SHARE, 1),
        )
        found = hybrid.fuse(route.lexical or route.dense, limit=depth, sources=sources)
        _dense, best_chunk, timings["dense"] = dense_future.result()
        fused = _collapse_duplicates(
            self._boost(route, self._weigh(route.query, list(found.hits))), self.content_hashes
        )
        t = lap("lexical+fusion", t)
        pins = self._pins(route, fused, sources)
        # Candidates from the lexical window too: a book's long text ranks it below the fusion's cut.
        named = self._title_pins(route, [*fused, *found.lexical], {p.doc_id for p in pins})
        pins += named
        pins += self._embedded_title(route, [*fused, *found.lexical], {p.doc_id for p in pins})
        pinned = {p.doc_id for p in pins}
        rest = [h for h in fused if h.doc_id not in pinned]
        t = lap("titles", t)

        head, tail = rest[: self.rerank_top], rest[self.rerank_top :]
        pins, head = (
            self.with_snippets(route.lexical or query, pins),
            self.with_snippets(route.lexical or query, head),
        )
        t = lap("snippets", t)
        passages = self._passages([*pins, *head], best_chunk)
        confidence: float | None = None
        if self.reranker is not None and head:
            display = {h.doc_id: h.snippet for h in head}
            probe = [replace(h, snippet=passages[h.doc_id]) for h in [*pins, *head]]
            ranked = self._weigh(query, list(self.reranker.rerank(route.dense or query, probe)))
            confidence = max((h.score for h in ranked), default=None)
            scores = {h.doc_id: h.score for h in ranked}
            pins = [replace(p, score=scores.get(p.doc_id, p.score)) for p in pins]
            head = [replace(h, snippet=display[h.doc_id]) for h in ranked if h.doc_id in display]
            t = lap("rerank", t)
        if named:  # near-exact titles: the query's language first, then by the reranker's score
            lang = query_language(route.title.split()) or "en"
            exact, near = pins[: len(pins) - len(named)], pins[len(pins) - len(named) :]
            if confidence is not None:
                near.sort(key=lambda h: (not h.lang or primary_lang(h.lang) != lang, -h.score))
            pins, head = [*exact, *near[:TITLE_PINS]], [*near[TITLE_PINS:], *head]
        head, tail = _prefer_language(route.query or query, head, tail, reranked=confidence is not None)
        hits = tuple([*pins, *head, *tail])
        total = (time.perf_counter() - t0) * 1000
        timings["total"] = round(total, 3)
        if total > self.budget_ms:
            log.info("oracle query over budget: %.1f ms (%s)", total, timings)
        low = not hits or (confidence is not None and confidence < self.low_confidence)
        return OracleResult(query, route, hits, passages, confidence, low, timings, scope, found.exhaustive)

    @property
    def _cut(self) -> Callable[[str, Sequence[str]], Mapping[str, str]] | None:
        cut = getattr(self.lexical, "snippets", None)
        return cast(Callable[[str, Sequence[str]], Mapping[str, str]], cut) if callable(cut) else None

    def with_snippets(self, query: str, hits: Sequence[SearchHit]) -> list[SearchHit]:
        """``hits`` with a snippet cut for each one that has none (when the lexical index can cut them)."""
        cut = self._cut
        missing = [h.doc_id for h in hits if not h.snippet]
        if cut is None or not missing or not query.strip():
            return list(hits)
        found = cut(query, missing)
        return [
            replace(h, snippet=found[h.doc_id]) if not h.snippet and h.doc_id in found else h for h in hits
        ]

    def depth_for(self, depth: int | None) -> int:
        """How many candidates :meth:`run` fuses and ranks for a requested depth."""
        return max(depth or 0, self.fusion.depth, self.rerank_top)

    def search(self, query: str, *, limit: int = 10, sources: Sequence[str] | None = None) -> list[SearchHit]:
        return list(self.run(query, sources=sources).hits[:limit])


class OracleIndex:
    """``LexicalIndex`` port over :class:`OracleSearcher`; writes go to the wrapped lexical index.

    Vectors are maintained by ``embed_generation`` (a batch job), never by the API process.
    """

    def __init__(
        self,
        searcher: OracleSearcher,
        *,
        cache_size: int = 256,
        refresh: Callable[[], bool] | None = None,
        refresh_seconds: float = 5.0,
        cache_seconds: float = 600.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """``refresh`` (``ChunkVectorStore.refresh``) is polled every ``refresh_seconds``: a rebuilt
        vector store is reopened and the cache cleared. So is the lexical index's commit stamp
        (``TantivyIndex.commit_stamp``): a commit by another process (``wsp index build``, the
        orchestrator) clears the cache, so removed documents are not listed (and 404) for minutes after
        and new ones are found (audit 2026-10-12 #10). Cached results also expire after ``cache_seconds``."""
        self.searcher = searcher
        self.lexical = searcher.lexical
        # (result, the depth it was computed for, when)
        self._cache: OrderedDict[tuple[str, tuple[str, ...] | None], tuple[OracleResult, int, float]] = (
            OrderedDict()
        )
        self._cache_size = cache_size
        self._lock = threading.Lock()
        self._refresh = refresh
        self._refresh_seconds = refresh_seconds
        self._cache_seconds = cache_seconds
        self._clock = clock
        self._checked = clock()
        self._stamp = self._lexical_stamp()

    def _lexical_stamp(self) -> object:
        stamp = getattr(self.lexical, "commit_stamp", None)
        return stamp() if callable(stamp) else None

    def _maybe_refresh(self) -> None:
        now = self._clock()
        if now - self._checked < self._refresh_seconds:
            return
        self._checked = now
        stamp = self._lexical_stamp()
        if stamp != self._stamp:
            self._stamp = stamp
            log.info("oracle: lexical index committed; result cache cleared")
            self._invalidate()
        if self._refresh is None:
            return
        try:
            changed = self._refresh()
        except Exception:  # a store being rewritten: keep serving the old one, retry later
            log.warning("oracle: vector store refresh failed", exc_info=True)
            return
        if changed:
            log.info("oracle: vector store changed on disk; reopened, result cache cleared")
            self._invalidate()

    def result(self, query: str, *, sources: Sequence[str] | None = None, depth: int = 0) -> OracleResult:
        """The pipeline's result for a query, cached. A deeper request than the cached one re-runs
        the pipeline but keeps the hits already shown in their order (pages never disagree)."""
        self._maybe_refresh()
        key = (" ".join(query.split()), None if sources is None else tuple(sorted(sources)))
        searched = self.searcher.depth_for(depth)
        now = self._clock()
        shown: OracleResult | None = None
        with self._lock:
            cached = self._cache.get(key)
            if cached is not None:
                hit, deep, at = cached
                fresh = now - at < self._cache_seconds
                if fresh and (depth <= deep or hit.exhaustive):  # deep enough, or nothing more to find
                    self._cache.move_to_end(key)
                    return hit
                shown = hit if fresh else None
        res = self.searcher.run(query, sources=sources, depth=depth or None)
        if shown is not None:
            kept = {h.doc_id for h in shown.hits}
            res = replace(res, hits=(*shown.hits, *(h for h in res.hits if h.doc_id not in kept)))
        with self._lock:
            self._cache[key] = (res, searched, now)
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
        return self.searcher.with_snippets(res.route.lexical or query, res.hits[offset : offset + limit])

    def with_snippets(self, query: str, hits: Sequence[SearchHit]) -> list[SearchHit]:
        """Snippets for a page of hits taken from :meth:`result` (only the head of a run has them)."""
        route, _ = self.searcher.scoped(query, None)
        return self.searcher.with_snippets(route.lexical or query, hits)

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
        self.searcher.close()
        self.lexical.close()


def cosine_top(query: np.ndarray, matrix: np.ndarray, k: int) -> list[int]:
    """Indices of the ``k`` rows most similar to ``query`` (rows and query L2-normalized)."""
    sims = matrix @ query
    k = min(k, len(sims))
    if k <= 0:
        return []
    top = np.argpartition(-sims, k - 1)[:k]
    return [int(i) for i in top[np.argsort(-sims[top], kind="stable")]]


def _prefer_language(
    query: str, head: Sequence[SearchHit], tail: Sequence[SearchHit], *, reranked: bool
) -> tuple[list[SearchHit], list[SearchHit]]:
    """Favour hits in the query's language (:data:`LANG_BONUS`, :data:`LANG_BOOST`), each list kept sorted."""
    lang = query_language(query.split()) or "en"

    def bonus(h: SearchHit, *, head_part: bool) -> SearchHit:
        if not h.lang or primary_lang(h.lang) != lang:  # stored tags are full BCP-47 ("en-GB")
            return h
        additive = head_part and reranked  # reranker scores are 0-1; fused (RRF) scores are ~0.03
        return replace(h, score=h.score + LANG_BONUS if additive else h.score * LANG_BOOST)

    new_head = sorted((bonus(h, head_part=True) for h in head), key=lambda h: -h.score)
    new_tail = sorted((bonus(h, head_part=False) for h in tail), key=lambda h: -h.score)
    return new_head, new_tail


_TITLE_WORDS = re.compile(r"[^\w]+", re.UNICODE)
_LEADING_ARTICLE = re.compile(r"^(?:a|an|the)\s+")
_PROJECT = re.compile(r"[-_.]")
_EMPTY_TEXT = content_hash("")


def _holds(words: Sequence[str], part: Sequence[str]) -> bool:
    """``part`` occurs in ``words`` as consecutive words."""
    n = len(part)
    return any(list(words[i : i + n]) == list(part) for i in range(len(words) - n + 1))


def _words_of(title: str) -> list[str]:
    """A title's words, case-folded, without punctuation."""
    return [w for w in _TITLE_WORDS.split(title.replace("_", " ").casefold()) if w]


def _project(name: str) -> str:
    """The project a source id or bundle book is named after: ``osm-planet`` -> ``osm``,
    ``wikibooks_en_all`` -> ``wikibooks`` (preferences name projects, not substrings)."""
    return _PROJECT.split(name, maxsplit=1)[0]


def _collapse_duplicates(hits: Sequence[SearchHit], content_hashes: ContentHashes | None) -> list[SearchHit]:
    """One hit per duplicate record: PubMed holds the same 1890s "A Case of Snake-Bite Treated ..." note
    four times, and a page of them hides everything else. Hits come best first, so the best is kept.

    Duplicates share the source, the title up to case, punctuation and a leading article, *and* the
    text (content hash). A title alone proves nothing: Stack Overflow has many separate questions
    titled "Python list comprehension", Open Library many works called "Berlin" (audit 2026-10-12 #8).
    Hashes are read only for hits whose title repeats; an empty text proves nothing either.
    """
    keys: list[tuple[str, str]] = []
    count: dict[tuple[str, str], int] = {}
    for h in hits:
        title = _LEADING_ARTICLE.sub("", _TITLE_WORDS.sub(" ", h.title.casefold()).strip())
        key = (h.source_id or source_of(h.doc_id), title)
        keys.append(key)
        if title:
            count[key] = count.get(key, 0) + 1
    shared = [h.doc_id for h, key in zip(hits, keys, strict=True) if count.get(key, 0) > 1]
    if not shared or content_hashes is None:
        return list(hits)
    hashes = content_hashes(shared)
    seen: set[tuple[str, str, str]] = set()
    out: list[SearchHit] = []
    for h, key in zip(hits, keys, strict=True):
        digest = hashes.get(h.doc_id)
        if digest is not None and digest != _EMPTY_TEXT and count.get(key, 0) > 1:
            if (*key, digest) in seen:
                continue
            seen.add((*key, digest))
        out.append(h)
    return out
