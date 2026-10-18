"""``GET /v1/search``: one retrieval per query, and every number on the page counted in it (audit #7).

The page, ``total`` and the facets all come from one ranked list of at most :data:`WINDOW` hits for
the query (the Oracle's result when the index is the Oracle). The facets count the kinds of that list
(kinds with nothing are not offered); a ``kind`` filter searches that kind's sources, so its list
reaches as deep as an unfiltered one.
Offsets beyond the list are refused (422) rather than paged deeper and inconsistently.

Identical searches in flight share one retrieval; the Oracle also caches recent results, so the next
page and a facet click reuse it. "Did you mean" asks the vocabulary which words are unknown (no
search per word) and runs one cheap lexical count only when it has a correction to offer.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections import Counter
from collections.abc import Callable, Hashable, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from typing import Any, cast

from wsp_api import convert, instant, search, sites
from wsp_api.schemas import DocKind, Hit, KindFacet, SearchResults, SearchScope
from wsp_api.snapshot import ZIM_ADAPTER
from wsp_api.views import Library
from wsp_core.listing import skip_paths, skip_regex
from wsp_core.model import SearchHit, SourceSpec, searchable

log = logging.getLogger("wsp.api.search")
SLOW_SEARCH_MS = 3000.0  # a search slower than this is logged with its stages
WINDOW = 200  # hits ranked per query: the page, total and facets are all counted in them
HIT_FIELD_WORKERS = 6  # header-field reads of one search page run side by side
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
    timings_ms: Mapping[str, float] = field(default_factory=dict[str, float])  # the Oracle's stages


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
        stages = cast(Mapping[str, float], getattr(res, "timings_ms", None) or {})
        return Retrieval(
            hits[:WINDOW], complete, bool(res.low_confidence) or not hits, res.scope, dict(stages)
        )
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


def _access(spec: SourceSpec) -> Mapping[str, object]:
    raw = spec.raw.get("access")
    return cast(Mapping[str, object], raw) if isinstance(raw, Mapping) else {}


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

    def searched_instead(
        self, wanted: Sequence[str] | None
    ) -> tuple[list[str] | None, list[tuple[str, list[str]]]]:
        """``wanted`` with each source that is not indexed (registry ``index: false``) replaced by the
        indexed copy it is searched through, and the replacements made.

        A ZIM edition that renders an indexed dump (wikipedia-en-zim for wikipedia-top10's English
        pages) or a reader edition of indexed texts (gutenberg-zim) holds nothing in the index, so a
        search limited to it found nothing, silently (2026-10-14 field exam, three examinees). The copy
        is named by ``access.searched_as`` or found as a source that borrows its renditions.
        """
        if wanted is None:
            return None, []
        out: list[str] = []
        replaced: list[tuple[str, list[str]]] = []
        for sid in wanted:
            spec = self.lib.s.source(sid)
            twins = [] if spec is None or searchable(spec) else self._twins(spec)
            if twins:
                out.extend(twins)
                replaced.append((sid, twins))
            else:
                out.append(sid)
        return list(dict.fromkeys(out)), replaced

    def _twins(self, spec: SourceSpec) -> list[str]:
        named = _access(spec).get("searched_as")
        if isinstance(named, list):
            ids = [str(x) for x in cast(list[object], named)]
            return [x for x in ids if self.lib.s.source(x) is not None]
        twins: list[str] = []
        for other in self.lib.s.sources:
            rends = _access(other).get("rendition")
            if isinstance(rends, list) and any(
                isinstance(r, Mapping) and cast(Mapping[str, object], r).get("source") == spec.id
                for r in cast(list[object], rends)
            ):
                twins.append(other.id)
        return twins

    def hit_fields(self, page: Sequence[SearchHit]) -> list[Mapping[str, object] | None]:
        """The header fields of each hit on the page, read side by side: each is a row-group read on the
        NAS (30-80 ms idle, seconds under load), and one after another they were up to 6 s of a search
        (2026-10-14). The pool is small so one page cannot flood the link."""
        if len(page) <= 1:
            return [self.lib.hit_fields(h.doc_id) for h in page]
        with ThreadPoolExecutor(min(HIT_FIELD_WORKERS, len(page)), thread_name_prefix="wsp-hit") as pool:
            return list(pool.map(self.lib.hit_fields, [h.doc_id for h in page]))

    def run(
        self, q: str, wanted: Sequence[str] | None, kind: DocKind | None, limit: int, offset: int
    ) -> SearchResults:
        t0 = time.perf_counter()
        lib, s = self.lib, self.lib.s
        wanted, replaced = self.searched_instead(wanted)
        note = (
            "; ".join(
                f"{a} is searched through {', '.join(b)}, which holds the same pages" for a, b in replaced
            )
            or None
        )
        stages: dict[str, float] = {}

        def lap(name: str, since: float) -> float:
            now = time.perf_counter()
            stages[name] = round((now - since) * 1000, 1)
            return now

        found = self.retrieval(q, wanted)
        t = lap("retrieval", t0)
        content = [h for h in found.hits if not self.is_listing(h)]
        kinds = [self.kind_of(h) for h in content]
        shown = [h for h, k in zip(content, kinds, strict=True) if kind is None or k is kind]
        if kind is not None:
            # A kind is searched in its own sources, not picked out of the mixed list: "Books" for
            # "bread" offered 2 Wikisource pages of the top 200 while Gutenberg holds hundreds
            # (2026-10-17 field exam). The facets still count the mixed list.
            scope = self.scope_of(kind, wanted)
            if scope:
                found = self.retrieval(q, scope)
                t = lap("kind_retrieval", t)
                shown = [h for h in found.hits if not self.is_listing(h) and self.kind_of(h) is kind]
            else:
                shown = []
        nav = search.navigate_to(s, q, exists=lib.exists) if offset == 0 else None
        pin = nav or search.cited(s, q, exists=lib.exists)
        if pin is not None and (kind is None or self.kind_of(SearchHit(pin, 0.0, "")) is kind):
            held = next((h for h in shown if h.doc_id == pin), None)
            first = held or SearchHit(pin, 0.0, lib.title(pin) or pin, source_id=pin[4:].partition("/")[0])
            shown = [first, *(h for h in shown if h.doc_id != pin)]
        t = lap("navigate", t)
        page = shown[offset : offset + limit]
        fill = getattr(s.index, "with_snippets", None)
        if callable(fill) and any(not h.snippet for h in page):  # ranked without them: cut for this page
            page = list(cast(Sequence[SearchHit], fill(q, page)))
        t = lap("page_snippets", t)
        more = offset + limit < len(shown)
        terms = search.terms(q)
        hits: list[Hit] = []
        for h, doc in zip(page, self.hit_fields(page), strict=True):
            plain = search.plain_snippet(h.snippet)  # index snippets are HTML (<b>…</b>); the API is plain
            hits.append(
                convert.hit(
                    replace(h, snippet=plain),
                    spec=s.source(h.source_id or h.doc_id[4:].partition("/")[0]),
                    doc=doc,
                    highlights=search.highlights(plain, terms),
                )
            )
        t = lap("hit_fields", t)
        counts = Counter(kinds)
        did_you_mean = self.did_you_mean(q, self.scope_of(kind, wanted), len(shown)) if offset == 0 else None
        t = lap("did_you_mean", t)
        low = found.low_confidence or not shown
        if s.queries is not None and offset == 0:
            s.queries.record(s.clock(), q, len(shown), low)
        routed = found.scope
        took = round((time.perf_counter() - t0) * 1000, 2)
        # The Oracle's own stages (route, lexical+fusion, dense, titles, snippets, rerank) beside the
        # API's: a slow search says where its time went (it said only its total before 2026-10-14).
        timings = {**{f"oracle.{k}": float(v) for k, v in found.timings_ms.items()}, **stages}
        if took >= SLOW_SEARCH_MS:
            log.warning("slow search %.0f ms %r: %s", took, q[:80], timings)
        return SearchResults(
            query=q,
            offset=offset,
            limit=limit,
            hits=hits,
            next_offset=offset + limit if more else None,
            kind=kind,
            total=len(shown),
            total_is_estimate=not found.complete,
            took_ms=took,
            timings_ms=timings,
            intent=search.intent(q, navigational=nav is not None),
            facets=[
                KindFacet(kind=k, count=n) for k, n in sorted(counts.items(), key=lambda x: (-x[1], x[0]))
            ],
            navigate_to=nav,
            instant=instant.definition(q, lib.s.documents.get) if offset == 0 else None,
            did_you_mean=did_you_mean,
            low_confidence=low,
            scope_note=note,
            scope=None
            if routed is None
            else SearchScope(site=routed.site, sources=list(routed.sources), fallback=routed.fallback),
        )
