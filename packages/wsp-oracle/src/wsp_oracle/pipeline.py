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
import unicodedata
from collections import OrderedDict
from collections.abc import Callable, Iterable, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from typing import cast

import numpy as np

from wsp_core.ids import DocId, InvalidDocIdError, content_hash, parse_doc_id
from wsp_core.listing import is_default_listing
from wsp_core.model import DocRecord, SearchHit, Suggestion
from wsp_core.ports import DocSuggester, Embedder, LexicalIndex, Reranker
from wsp_index import FusionConfig, HybridSearcher
from wsp_index.spelling import script_of
from wsp_index.text import STOPWORDS_EN, normalize_key, primary_lang, query_language
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
NAMED_RUNS = 8  # runs of a question's words looked up as titles (each one title-list lookup)
DICTIONARY_PROJECTS = frozenset({"wiktionary"})
TITLED_DEPTH = 30  # title matches read to choose among same-titled pages (ten Wikipedias share titles)
NAMED_TARGETS = 16  # documents a name (a redirect) is looked up as, from the suggest trie
NAMED_LOOKUPS = 4  # of them resolved (a stored-document read each) to find one in the query's language
FOREIGN_WINDOW = 10  # first hits read to tell a query whose results hold nothing in its language
DEFAULT_LANG = "en"
"""The language preferred when neither the query nor the reader names one (the interface language)."""
# Scripts that all but name a language, by ``wsp_index.spelling.script_of``'s keys (the first eight
# letters of the script's Unicode name). Not Latin, not Cyrillic (``query_language`` answers "ru") and
# not Han alone (Chinese or Japanese).
_SCRIPT_LANG = {
    "ARABIC": "ar",
    "HEBREW": "he",
    "GREEK": "el",
    "HANGUL": "ko",
    "KANA": "ja",
    "THAI": "th",
    "DEVANAGA": "hi",
}


_SHARED_SCRIPT_LANGS = frozenset({"ar", "hi", "he"})
"""Languages of :data:`_SCRIPT_LANG` whose script other languages of the library are written in too."""


def _script_language(text: str) -> str | None:
    """The language the script of ``text`` all but names, when every letter of it is in that script
    (Japanese: kana, with or without Han); ``None`` otherwise.

    The whole query, not a letter of it: "β-carotene", "μ decay", "Δ9-THC" and "π calculus" are English
    queries, and a lone Greek letter beside Latin ones names nothing; nor do Greek letters on their
    own ("π", "ΔΣ"), which are symbols until three of them make a word. The answer is a preference
    between editions, never a filter: Arabic script also writes Persian and Urdu, Devanagari also
    Marathi and Nepali, so callers treat it as a language the query did not announce (see
    :func:`_language`, which returns it with ``known`` false)."""
    # "ー" is KATAKANA-HIRAGANA PROLONGED SOUND MARK and "々" IDEOGRAPHIC ITERATION MARK: kana and Han
    # under names ``script_of`` does not fold
    scripts = {
        "KANA" if s.startswith(("KATAKANA", "HIRAGANA")) else "CJK" if s == "IDEOGRAP" else s
        for s in map(script_of, filter(str.isalpha, text))
    }
    if not scripts:
        return None
    if "KANA" in scripts and scripts <= {"KANA", "CJK"}:
        return "ja"
    if len(scripts) != 1:
        return None
    script = next(iter(scripts))
    if script == "GREEK" and not any(sum(c.isalpha() for c in w) > 2 for w in text.split()):
        return None  # "π", "β δ", "ΔΣ": Greek letters as symbols, not Greek words
    return _SCRIPT_LANG.get(script)


def _reader(lang: str | None) -> str:
    """The reader's language as the pipeline keeps it: the primary subtag, and nothing for
    :data:`DEFAULT_LANG` (a reader who sends "en" and one who sends nothing are served alike, from one
    cache entry, and neither is taken to have said which edition "東京" wants)."""
    code = primary_lang(lang) if lang and lang.strip() else ""
    return "" if code in (DEFAULT_LANG, "mul", "und", "zxx") else code  # "mul": no one language


def _stated_language(text: str, reader: str = "") -> tuple[str | None, bool]:
    """``(language, known)``: the language the query's function words announce (``known``), else the
    one its script names, else the reader's (``reader`` as :func:`_reader` gives it); ``None`` when
    nothing says."""
    found = query_language(text.split())
    if found:
        return found, True
    return _script_language(text) or reader or None, False


def _language(text: str, reader: str = "") -> tuple[str, bool]:
    """``(the language to prefer, whether the query's own words announce it)``: the one place that
    decides it. Announced by two function words (:func:`wsp_index.text.query_language`), then named by
    the script, then the reader's, then :data:`DEFAULT_LANG`. Only an announced language may filter
    (drop other editions); the others order."""
    lang, known = _stated_language(text, reader)
    return lang or DEFAULT_LANG, known


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
    unmatched: bool = False
    """No document in the sources searched matches the query's words and no page is titled or named
    like it: ``hits`` are the dense stage's nearest neighbours alone (it always has some). Such a
    result is ``low_confidence``."""
    doubted: bool = False
    """``low_confidence`` by the older rules alone (no hits, or the reranker's best score under the
    bar): false when ``unmatched`` is the only reason. The search log tells the two apart."""


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


# A programming name in the query asks for its reference page: "functools.lru_cache",
# "Array.prototype.flat()", "rsync --delete", "highway=residential", "find(1)". Stack Overflow threads
# about the name buried the documentation for 7 of 11 such questions (2026-10-17 field exam).
_CODE_NAME = re.compile(
    r"(?<![\w.])[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*\.[a-z_]\w{2,}(?:\(\))?"  # dotted, a real last name
    r"|(?<![\w-])--?[a-z][\w-]{2,}"  # an option
    r"|\b[a-z_:]+=[\w*]+"  # a key=value tag
    r"|\b\w+\(\)"  # a call
    r"|\b[a-z][\w.-]*\([1-9]\)"  # a man page
)
_QA_INTENT = re.compile(
    r"\b(errors?|exceptions?|fails?|failing|failed|crash\w*|not working|doesn'?t work|why does|"
    r"stack ?overflow|stack ?exchange|how (?:do|can) i fix|warnings?|bug)\b",
    re.I,
)
QA_SOURCES = frozenset({"stackexchange"})
REFERENCE_QA_WEIGHT = 0.7  # times the Q&A source's own weight, for a query naming a programming name


def asks_for_reference(query: str) -> bool:
    """The query names a programming name and does not ask about a failure: the documentation first."""
    return bool(_CODE_NAME.search(query)) and not _QA_INTENT.search(query)


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

    def _titled(self, title: str, sources: Sequence[str] | None) -> list[SearchHit]:
        """Pages titled ``title`` (case and spacing aside), from a title search ``TITLED_DEPTH`` deep."""
        key = _title_key(title)
        if not key:
            return []
        # Snippet-free: a pin's snippet is cut with the page's (``with_snippets``); ``search`` read ten
        # documents' texts over the NAS for one title (1-2 s of a query, 2026-10-13).
        fast = getattr(self.lexical, "search_hits", None)
        found = (
            fast(title, limit=TITLED_DEPTH, sources=sources)
            if callable(fast)
            else self.lexical.search(title, limit=TITLED_DEPTH, sources=sources)
        )
        return [h for h in cast(Sequence[SearchHit], found) if _title_key(h.title) == key]

    def _best_titled(
        self, route: Route, title: str, held: Sequence[SearchHit], sources: Sequence[str] | None
    ) -> SearchHit | None:
        """Of the pages titled ``title`` (``held`` among the candidates, then a title search), the one
        to show first: in the query's language, then the heaviest source, then the best ranked. Not
        the source a route prefers: "define photosynthesis" and "map of Lisbon" want the encyclopedia's
        article first (the definition box and the map have their own places; quality run 2026-10-17).
        Ten Wikipedias, Citizendium, Wikiquote and Simple English all hold "Genghis Khan": the
        candidates had Citizendium's and the first title matches never the English Wikipedia's, so
        "who was genghis khan" led with Citizendium (2026-10-17); a heavy source the title search
        missed is searched on its own. "root cellar" opened Wiktionary's entry, not the article.

        Missed means no heavy page with the title in the query's language, and the suggest trie is
        asked last: "Albert Einstein" led with explainxkcd's page and the English article was in no
        result, as thirty Wikipedia pages with the words in their titles (schools, prizes, streets)
        came before it in the heavy title search (2026-11-05 review)."""
        key = _title_key(title)
        lang, known = _language(route.title or route.query, route.reader_lang)
        seen: set[str] = set()
        pool: list[SearchHit] = []
        heavy = [s for s, w in self.source_weights.items() if w > 1.0 and (sources is None or s in sources)]

        def missed(hits: Sequence[SearchHit]) -> bool:
            return bool(heavy) and not any(
                (h.source_id or source_of(h.doc_id)) in heavy
                and _title_key(h.title) == key
                and (not h.lang or primary_lang(h.lang) == lang)
                for h in hits
            )

        found = self._titled(title, sources)
        if missed([*held, *found]):
            # the reference copy may be past the first matches (every Wikipedia language has the title)
            found += self._titled(title, heavy)
        if missed([*held, *found]):
            # A language the query does not announce is a preference, not a filter: "Berliner Mauer"
            # has no function word, was taken for English, and the German article (buried in the
            # title search like "Albert Einstein" above) was dropped here; Wikiquote's page led
            # (2026-11-10 trawl, search-index-09). ``rank`` below still puts the preferred language first.
            found += self._named_titled(title, lang, heavy, strict=known)
        for h in [*held, *found]:
            if h.doc_id in seen or _title_key(h.title) != key or not self._pinnable(route.query, h):
                continue
            if route.intent is not Intent.DEFINE and _is_dictionary(h):
                continue  # "how does general anesthesia work" opened Wiktionary's entry (2026-10-17)
            seen.add(h.doc_id)
            pool.append(h)
        if not pool:
            return None

        def rank(item: tuple[int, SearchHit]) -> tuple[bool, float, int]:
            i, h = item
            src = h.source_id or source_of(h.doc_id)
            return (bool(h.lang) and primary_lang(h.lang) != lang, -self.source_weights.get(src, 1.0), i)

        return min(enumerate(pool), key=rank)[1]

    def _named_titled(self, title: str, lang: str, heavy: Sequence[str], *, strict: bool) -> list[SearchHit]:
        """The pages of the ``heavy`` sources the suggest trie holds as titled exactly ``title``, the
        edition in ``lang`` looked up first (``"en/"``), then the trie's order (its prior), at most
        :data:`NAMED_LOOKUPS` reads. ``strict`` (the query's words announce ``lang``): only those in
        ``lang``; otherwise every page that states a language, ``lang`` being a guess. The trie knows a
        title the word search buries."""
        named = getattr(self.lexical, "named", None)
        resolve = getattr(self.lexical, "lookup", None) or self.resolve
        if not callable(named) or not callable(resolve):
            return []
        key = _title_key(title)
        found = cast(Sequence[tuple[str, str]], named(title, limit=NAMED_TARGETS))
        wanted = [
            (i, doc_id)
            for i, (doc_id, name) in enumerate(found)
            if source_of(doc_id) in heavy and _title_key(name) == key
        ]
        wanted.sort(
            key=lambda t: (not t[1].removeprefix(f"wsp:{source_of(t[1])}/").startswith(f"{lang}/"), t[0])
        )
        out: list[SearchHit] = []
        for _, doc_id in wanted[:NAMED_LOOKUPS]:
            hit = cast(SearchHit | None, resolve(doc_id))
            if hit is not None and hit.lang and (not strict or primary_lang(hit.lang) == lang):
                out.append(hit)
        return out

    def _pinnable(self, query: str, hit: SearchHit) -> bool:
        """A down-weighted source (catalog cards, abstracts) never leads by its title alone, unless the
        query asks for it: "capital of australia" pinned an Open Library record of that title above
        the article on Canberra (2026-10-14). Nor does a listing page (:mod:`wsp_core.listing`), which
        the API drops anyway: an iFixit profile titled "einstein" took the exact-title pin for
        "Einstein" with a reranker score of 0.04, and the article went unpinned (2026-11-05 deep dive)."""
        src = hit.source_id or source_of(hit.doc_id)
        if is_default_listing(hit.doc_id.removeprefix(f"wsp:{src}/")):
            return False
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
            held = [h for h in fused if _title_key(h.title) == key]
            hit = self._best_titled(route, title, held, sources)
            if hit is not None and all(p.doc_id != hit.doc_id for p in pins):
                pins.append(hit)
        return pins

    def _aliased(
        self, route: Route, pins: Sequence[SearchHit], sources: Sequence[str] | None
    ) -> list[SearchHit]:
        """For a page-name query, the page it names by another name: the target of a redirect (or an
        alias) called exactly the query, in the query's language, from the heaviest source; at most one.

        Search matched the words, never the names: "heart attack" listed songs, a restaurant and films
        and no Myocardial infarction, "acetaminophen" no Paracetamol and "Einstein" no Albert Einstein,
        while suggest offered each from its redirects (2026-11-05 deep dive). The suggest trie holds the
        redirects of the titles it keeps (:mod:`wsp_index.titles`), so nothing is re-indexed: a redirect
        it does not hold is not found. A title that is the query itself is the exact pin's, not this,
        and an exact pin from a heavy source in the query's language (the reference article "Paris")
        needs no other name: a lesser wiki's redirect "Paris" to its "Paris (film)" would follow it.

        Every Wikipedia language may hold the redirect: a target whose native id starts with the
        query's language ("en/") is looked up first, before :data:`NAMED_LOOKUPS` reads run out."""
        if (
            route.intent not in (Intent.NAVIGATE, Intent.LOOKUP, Intent.DEFINE, Intent.LOCATE)
            or not route.title
        ):
            return []
        lang, known = _language(route.title, route.reader_lang)
        # A language the query's words do not announce (its script's, the reader's) is a preference:
        # the reader's, then the default language's page is the answer when it holds none. "서울" with
        # only the English article "Seoul" redirected from it must still find Seoul.
        langs = (
            (lang,)
            if known
            else tuple(dict.fromkeys(x for x in (lang, route.reader_lang, DEFAULT_LANG) if x))
        )
        # Only a heavy pin in the query's language: the Cebuano "Einstein" stub the title search falls
        # back to is no answer to "Einstein", and it kept Albert Einstein out (2026-11-05 review).
        if any(
            self.source_weights.get(p.source_id or source_of(p.doc_id), 1.0) > 1.0
            and p.lang
            and primary_lang(p.lang) in langs
            for p in pins
        ):
            return []
        named = getattr(self.lexical, "named", None)
        resolve = getattr(self.lexical, "lookup", None) or self.resolve
        if not callable(named) or not callable(resolve):
            return []
        key = normalize_key(route.title.replace("_", " "))
        allowed = None if sources is None else frozenset(sources)
        found = cast(Sequence[tuple[str, str]], named(route.title, limit=NAMED_TARGETS))
        pinned = {p.doc_id for p in pins}
        targets = [
            (i, doc_id)
            for i, (doc_id, title) in enumerate(found)
            if doc_id not in pinned
            and normalize_key(title.replace("_", " ")) != key
            and (allowed is None or source_of(doc_id) in allowed)
        ]

        def order(t: tuple[int, str]) -> tuple[float, int, int]:
            i, doc_id = t
            src = source_of(doc_id)
            native = doc_id.removeprefix(f"wsp:{src}/")
            # the heaviest source (the reference article), its edition in the query's language, the
            # trie's order (the prior)
            edition = next((n for n, x in enumerate(langs) if native.startswith(f"{x}/")), len(langs))
            return (-self.source_weights.get(src, 1.0), edition, i)

        targets.sort(key=order)
        found_in: dict[str, SearchHit] = {}
        for _, doc_id in targets[:NAMED_LOOKUPS]:
            hit = cast(SearchHit | None, resolve(doc_id))
            if hit is None or not hit.lang or primary_lang(hit.lang) not in langs:
                continue  # gone from the index since the trie was built, or another language's page
            if not self._pinnable(route.query, hit):
                continue
            if route.intent is not Intent.DEFINE and _is_dictionary(hit):
                continue
            if primary_lang(hit.lang) == lang:
                return [replace(hit, snippet="")]
            found_in.setdefault(primary_lang(hit.lang), hit)
        return [replace(found_in[x], snippet="") for x in langs if x in found_in][:1]

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
        (disambiguation)" over "The New York Times", both four words (2026-10-16). Words beyond the
        query count before length, function words not at all ("The New York Times" before "New York
        Times Index"), and between sources the heavier one leads before the better ranked: Wikiquote's
        and the Simple English "The New York Times" led the English article.
        """
        if route.intent not in (Intent.NAVIGATE, Intent.LOOKUP) or not route.title:
            return []
        words = set(_words_of(route.title))
        if not words:
            return []
        lang = _language(route.title, route.reader_lang)[0]
        best: dict[str, tuple[tuple[bool, bool, int, int, float, int], SearchHit]] = {}
        for i, h in enumerate(candidates):
            title = _words_of(h.title)
            if h.doc_id in pinned or not words <= set(title) or len(title) > 2 * len(words):
                continue
            if not self._pinnable(route.query, h):
                continue
            src = h.source_id or source_of(h.doc_id)
            extra = sum(1 for w in title if w not in words and w not in STOPWORDS_EN)
            key = (
                not h.lang or primary_lang(h.lang) != lang,
                "(" in h.title,
                extra,
                len(title),
                -self.source_weights.get(src, 1.0),
                i,
            )
            if src not in best or key < best[src][0]:
                best[src] = (key, h)
        return [h for _, h in sorted(best.values(), key=lambda t: t[0])]

    def _embedded_title(
        self,
        route: Route,
        candidates: Iterable[SearchHit],
        pinned: set[str],
        sources: Sequence[str] | None = None,
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
            elif 2 <= len(title) < len(query) and _named(title) and _holds(query, title):
                key = (1, -len(title), weight, i)
            else:
                continue
            if not self._pinnable(route.query, h) or _is_dictionary(h):
                continue
            if best is None or key < best[0]:
                best = (key, h)
        if best is None and route.intent in (Intent.ASK, Intent.HOW_TO):
            found = self._named_in_query(route, query, sources)
            return [] if found is None or found.doc_id in pinned else [found]
        if best is None:
            return []
        # the same title from the query's language and the heaviest source, if another holds it
        chosen = self._best_titled(route, best[1].title, [best[1]], sources) or best[1]
        return [] if chosen.doc_id in pinned else [chosen]

    def _named_in_query(
        self, route: Route, query: Sequence[str], sources: Sequence[str] | None
    ) -> SearchHit | None:
        """A page the question names that no candidate is: its words as a title the library holds.
        "root cellar relative humidity range recommended for storing root vegetables" never reached
        Wikipedia's Root cellar among the candidates (long texts about vegetables outscored it;
        2026-10-17 field exam). The title list answers which runs of the query are titles; the
        longest, then the first in the query, is looked up."""
        runs: list[list[str]] = []
        for n in (3, 2):
            for i in range(len(query) - n + 1):
                run = list(query[i : i + n])
                if run[0] in STOPWORDS_EN or run[-1] in STOPWORDS_EN or not _named(run):
                    continue
                runs.append(run)
        suggest = getattr(self.lexical, "suggest", None)
        if not callable(suggest):
            return None
        for run in runs[:NAMED_RUNS]:
            phrase = " ".join(run)
            titles = cast(Sequence[str], suggest(phrase, limit=5))
            if any(_title_key(t) == phrase for t in titles):
                hit = self._best_titled(route, phrase, [], sources)
                if hit is not None:
                    return hit
        return None

    def _weigh(self, query: str, hits: list[SearchHit]) -> list[SearchHit]:
        """Hits scaled by their source's weight (``source_weights``), best first; a source the query
        asks for by its nature (a book for the catalog, research for abstracts) keeps its score."""
        if not self.source_weights or not hits:
            return hits
        exempt = weight_exempt(query)
        reference = asks_for_reference(query)
        out: list[SearchHit] = []
        for h in hits:
            src = h.source_id or source_of(h.doc_id)
            w = 1.0 if src in exempt else self.source_weights.get(src, 1.0)
            if reference and src in QA_SOURCES:
                w *= REFERENCE_QA_WEIGHT
            out.append(replace(h, score=h.score * w) if w != 1.0 else h)
        return sorted(out, key=lambda h: -h.score)

    def _boost(self, route: Route, fused: list[SearchHit]) -> list[SearchHit]:
        """Soft priors: hits from preferred sources (a site named without an identifier, dictionaries
        for "define", maps for "where is") score ``PREFER_BOOST`` times higher."""
        if not route.prefer and not route.prefer_sources:
            return fused
        preferred = OracleSearcher._preferred  # a static rule: tests call this without a searcher
        boosted = [replace(h, score=h.score * PREFER_BOOST) if preferred(route, h) else h for h in fused]
        return sorted(boosted, key=lambda h: -h.score)

    @staticmethod
    def _preferred(route: Route, h: SearchHit) -> bool:
        """From a source the route prefers: a site named as a soft prior, a dictionary for "define"."""
        if not route.prefer and not route.prefer_sources:
            return False
        src = h.source_id or source_of(h.doc_id)
        if src in route.prefer_sources or _project(src) in route.prefer:
            return True
        # A bundle's book is the first part of the native id ("kiwix-curated-t2/wikibooks_en_all/...").
        book, sep, _ = h.doc_id.removeprefix(f"wsp:{src}/").partition("/")
        return bool(sep) and _project(book) in route.prefer

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
        self,
        query: str,
        *,
        sources: Sequence[str] | None = None,
        depth: int | None = None,
        lang: str | None = None,
    ) -> OracleResult:
        """One query end to end. ``lang`` is the reader's language (BCP-47): it is preferred when the
        query neither announces a language nor is written in a script that names one. Absent, or
        :data:`DEFAULT_LANG`, is the default (:func:`_reader`)."""
        t0 = time.perf_counter()
        timings: dict[str, float] = {}

        def lap(name: str, since: float) -> float:
            now = time.perf_counter()
            timings[name] = round((now - since) * 1000, 3)
            return now

        route, scope = self.scoped(query, sources)
        route = replace(route, reader_lang=_reader(lang))
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
        exact = self._pins(route, fused, sources)
        exact += self._aliased(route, exact, sources)
        # Candidates from the lexical window too: a book's long text ranks it below the fusion's cut.
        named = self._title_pins(route, [*fused, *found.lexical], {p.doc_id for p in exact})
        titled = {p.doc_id for p in [*exact, *named]}  # the pages the query names: their leads are shown
        embedded = self._embedded_title(route, [*fused, *found.lexical], set(titled), sources)
        # In this order to the end: the exact titles and the alias, the near titles, the embedded one.
        pins = [*exact, *named, *embedded]
        pinned = {p.doc_id for p in pins}
        rest = [h for h in fused if h.doc_id not in pinned]
        t = lap("titles", t)

        head, tail = rest[: self.rerank_top], rest[self.rerank_top :]
        pins, head = (
            self.with_snippets(route.lexical or query, self.with_leads(pins, titled)),
            self.with_snippets(route.lexical or query, head),
        )
        t = lap("snippets", t)
        passages = self._passages([*pins, *head], best_chunk)
        confidence: float | None = None
        reranked: list[SearchHit] | None = None
        display = {h.doc_id: h.snippet for h in head}
        if self.reranker is not None and head:
            probe = [replace(h, snippet=passages[h.doc_id]) for h in [*pins, *head]]
            try:
                reranked = list(self.reranker.rerank(route.dense or query, probe))
            except Exception:  # a reranker failure costs the order, never the answer
                log.exception("rerank failed; the fused order stands")
        if reranked is not None:
            ranked = self._weigh(query, reranked)
            confidence = max((h.score for h in ranked), default=None)
            scores = {h.doc_id: h.score for h in ranked}
            pins = [replace(p, score=scores.get(p.doc_id, p.score)) for p in pins]
            head = [replace(h, snippet=display[h.doc_id]) for h in ranked if h.doc_id in display]
            t = lap("rerank", t)
        fixed = len(exact)  # the pins no later rule moves: the exact titles and the alias
        if named:  # near-exact titles: the query's language first, then by the reranker's score
            first, near, last = pins[:fixed], pins[fixed : fixed + len(named)], pins[fixed + len(named) :]
            near, demoted = _near_pins(
                near,
                first,
                *_language(route.title, route.reader_lang),
                reranked=confidence is not None,
            )
            pins, head = [*first, *near[:TITLE_PINS], *last], [*near[TITLE_PINS:], *demoted, *head]
        # One preferred language for the rest: what the query announces, its script names or the
        # reader sent, else the default. An exact title in another language does not change it.
        stated, known = _stated_language(route.query or query, route.reader_lang)
        prefer = stated or DEFAULT_LANG
        if not known and not any(h.lang and primary_lang(h.lang) == prefer for h in (*head, *tail)):
            prefer = DEFAULT_LANG  # a guess no result is in: the default's pages keep their bonus
        head, tail = _prefer_language(
            route.query or query, head, tail, reranked=confidence is not None, lang=prefer
        )
        # For the dictionaries only a language something states outright: with nothing said the
        # function keeps its own rule (Latin letters only), and a script several languages write
        # (Arabic: Persian, Urdu; Devanagari: Marathi, Nepali; Hebrew: Yiddish) does not say which
        # edition is the foreign one.
        if not known and stated != route.reader_lang and stated in _SHARED_SCRIPT_LANGS:
            stated = None
        pins, head = _spread_foreign_dictionaries(route.query or query, pins, head, fixed=fixed, lang=stated)
        hits = tuple([*pins, *head, *tail])
        total = (time.perf_counter() - t0) * 1000
        timings["total"] = round(total, 3)
        if total > self.budget_ms:
            log.info("oracle query over budget: %.1f ms (%s)", total, timings)
        # Nonsense is low confidence: the dense stage returns its nearest neighbours for any text, so
        # "qwertyuiopasdf" had some 200 hits, the reranker's best score cleared the bar and the page
        # looked like an answer (2026-11-10 trawl, search-index-15). The lexical window is empty only
        # when no document searched matches a word of the query (or the words of a quoted phrase side
        # by side): with ``partial=False`` the any-word query still runs when fewer than
        # ``MIN_TIER_HITS`` documents hold every word. Not when the query names a page: a redirect's
        # name is in the suggest trie and in no text ("acetaminophen" -> Paracetamol), and the exact
        # title or the alias pinned first is the answer.
        unmatched = bool(route.lexical) and not found.lexical and not exact
        doubted = not hits or (confidence is not None and confidence < self.low_confidence)
        return OracleResult(
            query,
            route,
            hits,
            passages,
            confidence,
            doubted or unmatched,
            timings,
            scope,
            found.exhaustive,
            unmatched,
            doubted,
        )

    @property
    def _cut(self) -> Callable[[str, Sequence[str]], Mapping[str, str]] | None:
        cut = getattr(self.lexical, "snippets", None)
        return cast(Callable[[str, Sequence[str]], Mapping[str, str]], cut) if callable(cut) else None

    def with_leads(self, hits: Sequence[SearchHit], wanted: Iterable[str]) -> list[SearchHit]:
        """``hits`` with the lead of each page in ``wanted`` that has no snippet yet, as its snippet (when
        the lexical index can cut leads; a page without a prose lead keeps none and gets the usual cut).

        A title hit is the page the query names: its first paragraph says what it is, while the densest
        cut of the query's words is where the title repeats most, the external links and the lists of
        namesakes: "photosynthesis", "Brown v. Board of Education" and "Marie Curie" showed link lists
        under the English article while the Simple English copy showed its lead (2026-11-05 deep dive)."""
        lead = getattr(self.lexical, "leads", None)
        chosen = set(wanted)
        missing = [h.doc_id for h in hits if not h.snippet and h.doc_id in chosen]
        if not callable(lead) or not missing:
            return list(hits)
        found = cast(Mapping[str, str], lead(missing))
        return [
            replace(h, snippet=found[h.doc_id]) if not h.snippet and found.get(h.doc_id) else h for h in hits
        ]

    def correction_pin(
        self,
        query: str,
        hits: Sequence[SearchHit],
        corrected: Callable[[], str | None],
        sources: Sequence[str] | None = None,
        lang: str | None = None,
    ) -> SearchHit | None:
        """The page titled like the corrected query, in the query's language, when the first
        :data:`FOREIGN_WINDOW` of ``hits`` hold nothing in that language (``corrected`` is asked only
        then). "Isaac Newtonn" matched French pages alone (the French stemmer undoes the double n): all
        ten hits were French while "did you mean" offered "Isaac Newton" (2026-11-05 deep dive).
        ``lang`` is the reader's language, as in :meth:`run`."""
        reader = _reader(lang)
        lang = _language(query, reader)[0]
        first = hits[:FOREIGN_WINDOW]
        if not first or any(not h.lang or primary_lang(h.lang) == lang for h in first):
            return None
        fixed = corrected()
        if not fixed or fixed.casefold() == query.casefold():
            return None
        route = replace(self.router.route(fixed), reader_lang=reader)
        hit = self._best_titled(route, route.title or fixed, [], sources)
        if hit is None or not hit.lang or primary_lang(hit.lang) != lang:
            return None
        return hit

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
        self._cache: OrderedDict[tuple[str, tuple[str, ...] | None, str], tuple[OracleResult, int, float]] = (
            OrderedDict()
        )
        self._cache_size = cache_size
        self._generation = 0  # bumped by every invalidation: a result computed across one is not kept
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

    def result(
        self, query: str, *, sources: Sequence[str] | None = None, depth: int = 0, lang: str | None = None
    ) -> OracleResult:
        """The pipeline's result for a query, cached. A deeper request than the cached one re-runs
        the pipeline but keeps the hits already shown in their order (pages never disagree). A result
        from the cache carries the stage timings of the run that made it and ``cached: 1.0`` beside
        them, so a log line shows they are not this request's.

        Calls for the same query at the same time each run the pipeline: sharing a run in flight is the
        caller's (``wsp_api.searching.Searcher.retrieval``, the only concurrent caller), and the run a
        timed-out request left behind fills this cache for the retry.
        ``lang`` is the reader's language (:meth:`OracleSearcher.run`); it is part of the cache key as
        :func:`reader_key` gives it, so no ``lang`` and the default language share an entry."""
        self._maybe_refresh()
        key = (" ".join(query.split()), None if sources is None else tuple(sorted(sources)), reader_key(lang))
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
                    return replace(hit, timings_ms={**hit.timings_ms, "cached": 1.0})
                shown = hit if fresh else None
            began = self._generation
        res = self.searcher.run(query, sources=sources, depth=depth or None, lang=lang)
        if shown is not None:
            kept = {h.doc_id for h in shown.hits}
            res = replace(res, hits=(*shown.hits, *(h for h in res.hits if h.doc_id not in kept)))
        with self._lock:
            # The index changed while this ran (a release removed documents): served once, not cached,
            # or readers paging it would see deleted hits for ten minutes (2026-10-26 review #106).
            if self._generation == began:
                self._cache[key] = (res, searched, now)
                while len(self._cache) > self._cache_size:
                    self._cache.popitem(last=False)
        return res

    def search(
        self,
        query: str,
        *,
        limit: int = 10,
        offset: int = 0,
        sources: Sequence[str] | None = None,
        lang: str | None = None,
    ) -> Sequence[SearchHit]:
        if limit <= 0 or offset < 0 or not query.strip():
            return []
        if sources is not None and not sources:
            return []
        res = self.result(query, sources=sources, depth=offset + limit, lang=lang)
        return self.searcher.with_snippets(res.route.lexical or query, res.hits[offset : offset + limit])

    def with_snippets(self, query: str, hits: Sequence[SearchHit]) -> list[SearchHit]:
        """Snippets for a page of hits taken from :meth:`result` (only the head of a run has them)."""
        route, _ = self.searcher.scoped(query, None)
        return self.searcher.with_snippets(route.lexical or query, hits)

    def correction_pin(
        self,
        query: str,
        hits: Sequence[SearchHit],
        corrected: Callable[[], str | None],
        sources: Sequence[str] | None = None,
        lang: str | None = None,
    ) -> SearchHit | None:
        """:meth:`OracleSearcher.correction_pin` for ``hits`` of :meth:`result` (the API's list)."""
        return self.searcher.correction_pin(query, hits, corrected, sources, lang)

    @staticmethod
    def reader_key(lang: str | None) -> str:
        """:func:`reader_key`, for a caller that shares runs in flight (the API's ``Searcher.retrieval``):
        its key and this cache's must take the same tags for the same reader."""
        return reader_key(lang)

    def suggest(self, prefix: str, *, limit: int = 10) -> Sequence[str]:
        return self.lexical.suggest(prefix, limit=limit)

    def suggest_docs(self, prefix: str, *, limit: int = 10, lang: str | None = None) -> Sequence[Suggestion]:
        """:class:`DocSuggester` when the lexical index is one; ``NotImplementedError`` otherwise, so
        the API falls back to :meth:`suggest`'s plain titles. Not an empty answer: the API takes an
        empty one as "nothing matches" and does not search the trie twice (2026-11-06 review)."""
        if isinstance(self.lexical, DocSuggester):
            return self.lexical.suggest_docs(prefix, limit=limit, lang=lang)
        raise NotImplementedError("the lexical index suggests titles only")

    def scope(
        self, query: str, sources: Sequence[str] | None = None, lang: str | None = None
    ) -> Scope | None:
        """Where :meth:`search` looks for this query (a named site, or the whole library)."""
        return self.result(query, sources=sources, lang=lang).scope

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
            self._generation += 1

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


def reader_key(lang: str | None) -> str:
    """The reader's language as a cache or in-flight key: every tag the pipeline serves alike gives
    the same key (``None``, ``"en"`` and ``"en-GB"`` are all :data:`DEFAULT_LANG`)."""
    return _reader(lang) or DEFAULT_LANG


def _near_pins(
    near: Sequence[SearchHit], exact: Sequence[SearchHit], lang: str, known: bool, *, reranked: bool
) -> tuple[list[SearchHit], list[SearchHit]]:
    """``(the near-exact titles to pin, those to rank with the head)``, the first sorted: the
    preferred language, then the other "own" languages, each by the reranker's score.

    "Own" is the preferred language ``lang`` and the language of every exact pin: "Berliner Mauer"
    (exact pin: the German article) keeps its German near titles pinned beside the English ones. An
    exact pin only widens it; it never replaces ``lang``: the Cebuano and Swedish editions hold
    millions of bot-written stubs titled with place and species names the English one lacks, and such
    a stub as the only exact title must not turn the English near titles out.

    A near title in a language outside "own" is not pinned once something says what "own" is (the
    query's words announce the language, ``known``, or an exact pin states one): every source gives its
    best near title, so a source holding only Japanese or Portuguese pages gave one of those, and
    "united states" listed foreign pages at ranks 2-5 before every reranked hit (2026-11-10 trawl,
    search-index-16). They join the head, where the reranker's score and the language bonus place
    them; nothing is dropped. Without reranker scores there is nothing to place them by, and with
    neither an exact pin nor an announced language nothing to tell them from the reader's pages: the
    order stands."""
    own = {lang} | {primary_lang(p.lang) for p in exact if p.lang}

    def foreign(h: SearchHit) -> bool:
        return bool(h.lang) and primary_lang(h.lang) not in own

    kept = list(near)
    if not reranked:
        return kept, []
    # the preferred language still first, as before: eight Cebuano near titles the reranker scores
    # a hair higher must not push the English one past ``TITLE_PINS``
    kept.sort(
        key=lambda h: (
            not h.lang or primary_lang(h.lang) != lang,
            not h.lang or primary_lang(h.lang) not in own,
            -h.score,
        )
    )
    if not known and not any(p.lang for p in exact):
        return kept, []
    return [h for h in kept if not foreign(h)], [h for h in kept if foreign(h)]


def _prefer_language(
    query: str,
    head: Sequence[SearchHit],
    tail: Sequence[SearchHit],
    *,
    reranked: bool,
    lang: str | None = None,
) -> tuple[list[SearchHit], list[SearchHit]]:
    """Favour hits in the query's language (:data:`LANG_BONUS`, :data:`LANG_BOOST`), each list kept
    sorted. ``lang``, when given, is that language (the pipeline's choice, :func:`_language`)."""
    lang = lang or _language(query)[0]

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


def _named(title: Sequence[str]) -> bool:
    """A title worth taking from a question's words: not function words only, and not a number with
    one word ("10 days", "20 mg"): "... zinc 20 mg 10 days" led with the disambiguation page "10 Days"
    (2026-10-17 field exam), while "Carbon-14" and "Apollo 11" are the pages their questions name."""
    if not any(w not in STOPWORDS_EN for w in title):
        return False
    return not (len(title) == 2 and any(c.isdigit() for c in title[0]))


def _is_dictionary(h: SearchHit) -> bool:
    """A dictionary's entry (Wiktionary, either edition, or a bundle's: ``kiwix-multilingual-pl`` holds
    ``wiktionary_pl_all``): the page for "define", not for a question."""
    src = h.source_id or source_of(h.doc_id)
    if _project(src) in DICTIONARY_PROJECTS or _project(src.removeprefix("kiwix-")) in DICTIONARY_PROJECTS:
        return True
    book, sep, _ = h.doc_id.removeprefix(f"wsp:{src}/").partition("/")
    return bool(sep) and src.startswith("kiwix-") and _project(book) in DICTIONARY_PROJECTS


def _latin(text: str) -> bool:
    """Every letter of ``text`` is a Latin one (the query's language may default to English)."""
    return all(unicodedata.name(c, "").startswith("LATIN") for c in text if c.isalpha())


def _spread_foreign_dictionaries(
    query: str, pins: Sequence[SearchHit], head: Sequence[SearchHit], *, fixed: int, lang: str | None = None
) -> tuple[list[SearchHit], list[SearchHit]]:
    """Of the dictionary entries in another language than the query's, the first keeps its place and
    the others go to the end of the reranked head, below the hits in the query's language.

    Every Wiktionary has an entry for "acetaminophen": the Arabic, Polish and Chinese ones each took a
    slot of the first ten, five Wiktionary entries in all (2026-11-05 deep dive). The first ``fixed``
    pins (the exact title, the alias) stay. The query's language must be known, or the query written
    in Latin letters (English, the interface language, then): "東京" (seven Wiktionary entries) says
    nothing about which edition it wants, and keeps its order. Nothing is dropped.

    ``lang``, when given, is a language something stated (the query's words, its script, a reader's
    language other than the default: :func:`_stated_language`); never the default as a guess, which
    would undo the "東京" rule."""
    if lang is None:
        lang = query_language(query.split())
    if lang is None:
        if not _latin(query):
            return list(pins), list(head)
        lang = DEFAULT_LANG

    def foreign(h: SearchHit) -> bool:
        return bool(h.lang) and primary_lang(h.lang) != lang and _is_dictionary(h)

    seen = any(foreign(h) for h in pins[:fixed])
    later: list[SearchHit] = []
    near: list[SearchHit] = []
    rest: list[SearchHit] = []
    for out, hits in ((near, pins[fixed:]), (rest, head)):
        for h in hits:
            if foreign(h):
                if seen:
                    later.append(h)
                    continue
                seen = True
            out.append(h)
    return [*pins[:fixed], *near], [*rest, *later]


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
