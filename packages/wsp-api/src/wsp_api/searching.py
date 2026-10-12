"""``GET /v1/search``: one retrieval per query, and every number on the page counted in it (audit #7).

The page, ``total`` and the facets all come from one ranked list of at most :data:`WINDOW` hits for
the query (the Oracle's result when the index is the Oracle). A ``kind`` filter keeps that kind's hits
of the list, so "Medicine (4)" shows exactly 4 results and kinds with nothing are not offered.
Offsets beyond the list are refused (422) rather than paged deeper and inconsistently.

Identical searches in flight share one retrieval; the Oracle also caches recent results, so the next
page and a facet click reuse it. "Did you mean" asks the vocabulary which words are unknown (no
search per word) and runs one cheap lexical count only when it has a correction to offer.
"""

from __future__ import annotations

import re
import threading
import time
from collections import Counter
from collections.abc import Callable, Hashable, Mapping, Sequence
from concurrent.futures import Future
from dataclasses import dataclass, replace
from typing import Any, cast

from wsp_api import convert, instant, search, sites
from wsp_api.schemas import DocKind, Hit, KindFacet, SearchResults, SearchScope
from wsp_api.snapshot import ZIM_ADAPTER
from wsp_api.views import Library
from wsp_core.listing import skip_paths, skip_regex
from wsp_core.model import SearchHit

WINDOW = 200  # hits ranked per query: the page, total and facets are all counted in them
SPARSE = 5  # fewer results than this: "did you mean" may correct any unknown or rare word


class SingleFlight[K: Hashable, V]:
    """Concurrent calls with the same key run ``fn`` once and share its result (or exception)."""

    def __init__(self) -> None:
        self._calls: dict[K, Future[V]] = {}
        self._lock = threading.Lock()

    def do(self, key: K, fn: Callable[[], V]) -> V:
        with self._lock:
            pending = self._calls.get(key)
            mine = pending is None
            if pending is None:
                pending = self._calls[key] = Future()
        if not mine:
            return pending.result()
        try:
            value = fn()
        except BaseException as exc:
            with self._lock:
                del self._calls[key]
            pending.set_exception(exc)
            raise
        with self._lock:
            del self._calls[key]
        pending.set_result(value)
        return value


@dataclass(frozen=True, slots=True)
class Retrieval:
    hits: tuple[SearchHit, ...]  # ranked, at most WINDOW
    complete: bool  # every match is in ``hits``
    low_confidence: bool
    scope: Any = None  # the index's scope object (site, sources, fallback), if it routes queries


def retrieve(index: object, q: str, wanted: Sequence[str] | None) -> Retrieval:
    """One ranked retrieval of up to :data:`WINDOW` hits."""
    if wanted is not None and not wanted:  # a filter that matches no source: nothing, not everything
        return Retrieval((), True, True)
    oracle = getattr(index, "result", None)
    if callable(oracle):  # the Oracle: hits, confidence and scope from one (cached) run
        res = cast(Any, oracle(q, sources=wanted, depth=WINDOW))
        hits = tuple(cast(Sequence[SearchHit], res.hits))
        # The Oracle says whether its list holds every match: collapsed duplicates and filtered hits make
        # a list shorter than the window without making it complete (audit 2026-10-12 F10). An index that
        # does not say: a list shorter than the window is all there is.
        exhaustive = getattr(res, "exhaustive", None)
        complete = len(hits) < WINDOW if exhaustive is None else bool(exhaustive) and len(hits) <= WINDOW
        return Retrieval(hits[:WINDOW], complete, bool(res.low_confidence) or not hits, res.scope)
    lexical = cast(Any, index)
    hits = tuple(cast(Sequence[SearchHit], lexical.search(q, limit=WINDOW + 1, offset=0, sources=wanted)))
    scope_of = getattr(index, "scope", None)
    scope = scope_of(q, wanted) if callable(scope_of) else None
    return Retrieval(hits[:WINDOW], len(hits) <= WINDOW, not hits, scope)


def _count(index: object, q: str, wanted: Sequence[str] | None, at_least: int) -> int:
    """How many hits ``q`` has, up to ``at_least`` (snippet-free when the index can)."""
    if wanted is not None and not wanted:
        return 0
    cheap = getattr(index, "search_hits", None)
    run = cheap if callable(cheap) else cast(Any, index).search
    return len(cast(Sequence[SearchHit], run(q, limit=max(at_least, 1), offset=0, sources=wanted)))


class Searcher:
    def __init__(self, lib: Library) -> None:
        self.lib = lib
        self._flight: SingleFlight[tuple[str, tuple[str, ...] | None], Retrieval] = SingleFlight()
        self._listing: dict[str, re.Pattern[str] | None] = {}  # source id -> listing-path regex

    def is_listing(self, h: SearchHit) -> bool:
        """A ZIM listing page (a tag's question list, ``users_page=3``) an older refine indexed: an index of
        other pages, never a result (:mod:`wsp_core.listing`)."""
        source_id, _, native = h.doc_id.removeprefix("wsp:").partition("/")
        if source_id not in self._listing:
            self._listing[source_id] = self._listing_regex(source_id)
        rx = self._listing[source_id]
        if rx is None:
            return False
        inner = native.partition("/")[2]  # a bundle's native id is "<book>/<entry path>"
        return bool(rx.fullmatch(native) or (inner and rx.fullmatch(inner)))

    def _listing_regex(self, source_id: str) -> re.Pattern[str] | None:
        spec = self.lib.s.source(source_id)
        if spec is None or spec.adapter != ZIM_ADAPTER:
            return None
        access: object = spec.raw.get("access")
        raw: Mapping[str, object] = cast(Mapping[str, object], access) if isinstance(access, Mapping) else {}
        return re.compile(skip_regex(skip_paths(raw)))

    def kind_of(self, h: SearchHit) -> DocKind:
        spec = self.lib.s.source(h.source_id or h.doc_id[4:].partition("/")[0])
        return DocKind.OTHER if spec is None else sites.kind_of(spec)

    def scope_of(self, kind: DocKind | None, wanted: Sequence[str] | None) -> Sequence[str] | None:
        """The sources a ``kind`` filter (within ``wanted``) covers."""
        if kind is None:
            return wanted
        ids = [x.id for x in self.lib.s.sources if sites.kind_of(x) is kind]
        return [x for x in ids if wanted is None or x in wanted]

    def retrieval(self, q: str, wanted: Sequence[str] | None) -> Retrieval:
        key = (" ".join(q.split()), None if wanted is None else tuple(sorted(wanted)))
        return self._flight.do(key, lambda: retrieve(self.lib.s.index, q, wanted))

    def did_you_mean(self, q: str, wanted: Sequence[str] | None, found: int) -> str | None:
        """A correction when results are sparse, or when a word is unknown to the vocabulary (no hit
        can contain it; only those words may change). Offered only if it finds at least as much."""
        speller = self.lib.s.speller
        if speller is None:
            return None
        only: list[str] | None = None
        if found >= SPARSE:
            only = list(speller.unknown(q))
            if not only:
                return None
        fixed = speller.correct(q, only=only)
        if fixed is None or fixed.casefold() == q.casefold():
            return None
        return fixed if _count(self.lib.s.index, fixed, wanted, max(found, 1)) >= max(found, 1) else None

    def run(
        self, q: str, wanted: Sequence[str] | None, kind: DocKind | None, limit: int, offset: int
    ) -> SearchResults:
        t0 = time.perf_counter()
        lib, s = self.lib, self.lib.s
        found = self.retrieval(q, wanted)
        content = [h for h in found.hits if not self.is_listing(h)]
        kinds = [self.kind_of(h) for h in content]
        shown = [h for h, k in zip(content, kinds, strict=True) if kind is None or k is kind]
        page = shown[offset : offset + limit]
        fill = getattr(s.index, "with_snippets", None)
        if callable(fill) and any(not h.snippet for h in page):  # ranked without them: cut for this page
            page = list(cast(Sequence[SearchHit], fill(q, page)))
        more = offset + limit < len(shown)
        terms = search.terms(q)
        hits: list[Hit] = []
        for h in page:
            plain = search.plain_snippet(h.snippet)  # index snippets are HTML (<b>…</b>); the API is plain
            hits.append(
                convert.hit(
                    replace(h, snippet=plain),
                    spec=s.source(h.source_id or h.doc_id[4:].partition("/")[0]),
                    doc=lib.hit_fields(h.doc_id),
                    highlights=search.highlights(plain, terms),
                )
            )
        counts = Counter(kinds)
        nav = search.navigate_to(s, q, exists=lib.exists) if offset == 0 else None
        did_you_mean = self.did_you_mean(q, self.scope_of(kind, wanted), len(shown)) if offset == 0 else None
        low = found.low_confidence or not shown
        if s.queries is not None and offset == 0:
            s.queries.record(s.clock(), q, len(shown), low)
        routed = found.scope
        return SearchResults(
            query=q,
            offset=offset,
            limit=limit,
            hits=hits,
            next_offset=offset + limit if more else None,
            kind=kind,
            total=len(shown),
            total_is_estimate=not found.complete,
            took_ms=round((time.perf_counter() - t0) * 1000, 2),
            intent=search.intent(q, navigational=nav is not None),
            facets=[
                KindFacet(kind=k, count=n) for k, n in sorted(counts.items(), key=lambda x: (-x[1], x[0]))
            ],
            navigate_to=nav,
            instant=instant.definition(q, lib.s.documents.get) if offset == 0 else None,
            did_you_mean=did_you_mean,
            low_confidence=low,
            scope=None
            if routed is None
            else SearchScope(site=routed.site, sources=list(routed.sources), fallback=routed.fallback),
        )
