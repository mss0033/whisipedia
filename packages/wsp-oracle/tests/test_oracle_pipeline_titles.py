"""Totals after duplicates collapse, the result cache's freshness, and title queries across sites
(audit 2026-10-12 #2, #10, F10; the "huckleberry finn" report of the same day)."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import replace
from pathlib import Path

import pytest
from oracle_helpers import OverlapReranker

from wsp_core.ids import DocId, make_doc_id
from wsp_core.model import DocRecord, SearchHit
from wsp_index import TantivyConfig, TantivyIndex
from wsp_oracle.pipeline import OracleIndex, OracleSearcher
from wsp_oracle.router import Router


class _Editions:
    """A lexical index with 10,000 matches whose titles repeat in pairs (two records of each)."""

    def __init__(self) -> None:
        self.stamp = 1
        self.runs = 0

    def search(
        self, query: str, *, limit: int = 10, offset: int = 0, sources: object = None
    ) -> list[SearchHit]:
        self.runs += 1
        hits = [
            SearchHit(DocId(f"wsp:ol/{i:05}"), 10_000.0 - i, f"Title {i // 2}", "", "ol")
            for i in range(offset, min(offset + limit, 10_000))
        ]
        return hits

    def commit_stamp(self) -> int:
        return self.stamp


def _pairs(ids: Sequence[str]) -> dict[str, str]:
    """Content hashes: the two records of a title hold the same text."""
    return {i: f"text of {int(i.rsplit('/', 1)[1]) // 2}" for i in ids}


def test_a_list_shortened_by_collapsing_is_not_complete() -> None:
    # Audit 2026-10-12 #2: 200 candidates collapsed to 100 read as "every match": an exact total of 100
    # out of 10,000, and the cache never ran a deeper request.
    lex = _Editions()
    index = OracleIndex(OracleSearcher(lex, content_hashes=_pairs))  # type: ignore[arg-type]
    res = index.result("anything", depth=200)
    assert len(res.hits) == 100  # the duplicates collapsed
    assert not res.exhaustive
    runs = lex.runs
    deeper = index.result("anything", depth=400)
    assert lex.runs > runs  # not taken for exhaustive: the deeper request ran
    assert len(deeper.hits) == 200


def test_a_short_list_from_retrievers_that_ran_out_is_complete() -> None:
    class _Few(_Editions):
        def search(
            self, query: str, *, limit: int = 10, offset: int = 0, sources: object = None
        ) -> list[SearchHit]:
            return super().search(query, limit=limit, offset=offset)[: max(0, 5 - offset)]

    res = OracleIndex(OracleSearcher(_Few())).result("anything", depth=200)  # type: ignore[arg-type]
    assert len(res.hits) == 5
    assert res.exhaustive


def test_the_cache_is_cleared_when_the_lexical_index_commits() -> None:
    # Audit 2026-10-12 #10: a commit by another process (``wsp index build``) left cached result lists
    # in place for up to ten minutes; removed documents were listed and new ones missing.
    lex = _Editions()
    now = [0.0]
    index = OracleIndex(OracleSearcher(lex), refresh_seconds=5.0, clock=lambda: now[0])  # type: ignore[arg-type]
    index.result("anything")
    runs = lex.runs
    now[0] = 10.0
    index.result("anything")
    assert lex.runs == runs  # nothing committed: served from the cache
    lex.stamp += 1
    index.result("anything")
    assert lex.runs == runs  # not checked again yet
    now[0] = 20.0
    index.result("anything")
    assert lex.runs > runs


SMALL = TantivyConfig(languages=("en",), writer_heap_bytes=30_000_000, writer_threads=1)


def _doc(source: str, native: str, title: str, text: str, lang: str = "en") -> DocRecord:
    return DocRecord(
        doc_id=make_doc_id(source, native),
        source_id=source,
        native_id=native,
        title=title,
        lang=lang,
        license="CC0-1.0",
        text=text,
    )


BOOK = make_doc_id("gutenberg", "76")
TRANSLATION = make_doc_id("gutenberg", "66159")


@pytest.fixture
def library(tmp_path: Path) -> Iterator[TantivyIndex]:
    """Many short records titled like the query (BM25 favours them) beside the book's long text."""
    river = "the river raft and the widow and the island " * 400
    docs = [
        _doc("ol", f"OL{i}W", "Huckleberry Finn", f"Huckleberry Finn by Mark Twain, edition {i}")
        for i in range(150)
    ]
    docs += [_doc("se", f"users/{i}", "User Huckleberry Finn", "Huckleberry Finn, a user") for i in range(80)]
    docs += [
        _doc(
            "gutenberg",
            "76",
            "Adventures of Huckleberry Finn",
            f"Adventures of Huckleberry Finn. {river} Huck Finn",
        ),
        _doc(
            "gutenberg",
            "66159",
            "Huckleberry Finn kalandjai",
            "Huckleberry Finn kalandjai. a folyó",
            lang="hu",
        ),
        *(
            _doc(
                "gutenberg",
                f"71{i:02}",
                f"Adventures of Huckleberry Finn, Chapters {i} to {i + 4}",
                f"Huckleberry Finn {river}",
            )
            for i in range(1, 30, 5)
        ),
    ]
    idx = TantivyIndex(tmp_path / "lexical", SMALL)
    idx.upsert(docs)
    idx.commit()
    yield idx
    idx.close()


def _searcher(idx: TantivyIndex) -> OracleSearcher:
    return OracleSearcher(
        idx,
        router=Router.from_sources(["ol", "se", "gutenberg"]),
        reranker=OverlapReranker(),
        resolve=idx.lookup,
    )


def test_a_title_query_finds_the_book_across_all_sites(library: TantivyIndex) -> None:
    res = _searcher(library).run("huckleberry finn")
    page = [h.doc_id for h in res.hits[:10]]
    assert BOOK in page  # the book is on the first page, not crowded out by records named like it
    assert {h.source_id for h in res.hits[:10]} >= {"ol", "se", "gutenberg"}
    assert page.index(BOOK) < (page.index(TRANSLATION) if TRANSLATION in page else 10)


class _SaturatedReranker:
    """Like the cross-encoder on title matches: near 1.0 for all, the translation a hair ahead."""

    def rerank(self, query: str, hits: Sequence[SearchHit]) -> Sequence[SearchHit]:
        scores = {TRANSLATION: 0.9955, BOOK: 0.9633}
        return sorted((replace(h, score=scores.get(h.doc_id, 0.9)) for h in hits), key=lambda h: -h.score)


def test_within_a_site_the_query_language_edition_leads(library: TantivyIndex) -> None:
    searcher = _searcher(library)
    searcher.reranker = _SaturatedReranker()
    ids = [h.doc_id for h in searcher.run("huckleberry finn", sources=["gutenberg"]).hits]
    assert ids[0] == BOOK  # the English edition before the Hungarian translation
    assert TRANSLATION in ids[:10]  # which is still found


def test_a_question_holding_a_title_word_for_word_leads_with_that_page(tmp_path: Path) -> None:
    # 2026-10-16 field exam: "Incident Command System span of control" listed CIMS, NIMS and AIIMS
    # (short pages full of the words) above the article Incident Command System.
    span = "span of control incident command system " * 6
    docs = [
        _doc("wiki", "en/CIMS", "Coordinated Incident Management System", f"{span} New Zealand"),
        _doc("wiki", "en/NIMS", "National Incident Management System", f"{span} United States"),
        _doc(
            "wiki",
            "en/ICS",
            "Incident Command System",
            "A standard command structure. " * 80 + "Span of control",
        ),
        _doc("wiki", "en/Control", "Control", f"{span} control theory"),
        _doc("wiki", "en/Water", "Water", "water " * 30),
    ]
    idx = TantivyIndex(tmp_path / "lexical", SMALL)
    idx.upsert(docs)
    idx.commit()
    searcher = OracleSearcher(
        idx, router=Router.from_sources(["wiki"]), reranker=OverlapReranker(), resolve=idx.lookup
    )
    res = searcher.run("Incident Command System span of control")
    assert res.hits[0].doc_id == make_doc_id("wiki", "en/ICS")
    # The longest held title wins; one-word titles and the whole query never.
    route = searcher.router.route("history of the roman republic and its army")
    hits = [
        SearchHit(DocId(f"wsp:wiki/{t}"), 1.0, t, "", "wiki") for t in ("Rome", "Roman republic", "Republic")
    ]
    assert [h.title for h in searcher._embedded_title(route, hits, set())] == ["Roman republic"]  # pyright: ignore[reportPrivateUsage]
    water = [SearchHit(DocId("wsp:wiki/Water"), 1.0, "Water", "", "wiki")]
    assert searcher._embedded_title(searcher.router.route("how to purify water"), water, set()) == []  # pyright: ignore[reportPrivateUsage]
    lookup = searcher.router.route("roman republic")
    assert searcher._embedded_title(lookup, hits, set()) == []  # pyright: ignore[reportPrivateUsage]
    idx.close()


def test_a_question_that_begins_a_title_leads_with_that_page(library: TantivyIndex) -> None:
    # 2026-10-16 field exam: five words are asked as a question, and the handbook's Addendum led the
    # handbook the query names.
    searcher = _searcher(library)
    hits = [
        SearchHit(DocId(f"wsp:faa/{n}"), 1.0, t, "", "faa")
        for n, t in (
            ("add", "FAA-H-8083-25C, Pilot\u2019s Handbook of Aeronautical Knowledge Addendum"),
            ("phak", "Pilot\u2019s Handbook of Aeronautical Knowledge, FAA-H-8083-25C (Full Version)"),
            ("short", "Pilot\u2019s Handbook"),
        )
    ]
    route = searcher.router.route("Pilot's Handbook of Aeronautical Knowledge")
    pins = searcher._embedded_title(route, hits, set())  # pyright: ignore[reportPrivateUsage]
    assert [h.doc_id for h in pins] == ["wsp:faa/phak"]  # before the held, shorter "Pilot's Handbook"
    three = searcher.router.route("what is knowledge")  # too short to name a title by its start
    essay = [SearchHit(DocId("wsp:w/k"), 1.0, "What is knowledge? An essay", "", "w")]
    assert searcher._embedded_title(three, essay, set()) == []  # pyright: ignore[reportPrivateUsage]


def test_a_page_name_query_holding_a_title_leads_with_the_reference_copy(library: TantivyIndex) -> None:
    # 2026-10-16 field exam: "Kant categorical imperative" listed "Categorical" and the Simple English
    # article first and the English Wikipedia article 20th.
    searcher = _searcher(library)
    searcher.source_weights = {"wikipedia-top10": 1.15}
    route = searcher.router.route("Kant categorical imperative")
    hits = [
        SearchHit(DocId(f"wsp:{src}/{t}"), 1.0, t.replace("_", " "), "", src)
        for src, t in (
            ("wiki", "Categorical"),
            ("simple", "Categorical_imperative"),
            ("wikipedia-top10", "Categorical_imperative"),
        )
    ]
    pins = searcher._embedded_title(route, hits, set())  # pyright: ignore[reportPrivateUsage]
    assert [h.doc_id for h in pins] == ["wsp:wikipedia-top10/Categorical_imperative"]
    # A query whose own title already pinned a page keeps it alone.
    assert searcher._embedded_title(route, hits, {"wsp:x/y"}) == []  # pyright: ignore[reportPrivateUsage]


def test_a_title_without_a_qualifier_leads_one_with(library: TantivyIndex) -> None:
    searcher = _searcher(library)
    route = searcher.router.route("new york times")
    hits = [
        SearchHit(DocId(f"wsp:wiki/{n}"), 1.0, t, "", "wiki", lang="en")
        for n, t in (
            ("d", "New York Times (disambiguation)"),
            ("i", "New York Times Index"),
            ("t", "The New York Times"),
        )
    ]
    pins = searcher._title_pins(route, hits, set())  # pyright: ignore[reportPrivateUsage]
    assert [h.doc_id for h in pins] == ["wsp:wiki/t"]
    # Between sources, the heavier one leads the better ranked.
    searcher.source_weights = {"wikipedia-top10": 1.15}
    copies = [
        SearchHit(DocId(f"wsp:{src}/t"), 1.0, "The New York Times", "", src, lang="en")
        for src in ("wikiquote", "simple", "wikipedia-top10")
    ]
    pins = searcher._title_pins(route, copies, set())  # pyright: ignore[reportPrivateUsage]
    assert pins[0].source_id == "wikipedia-top10"


def test_a_question_names_a_page_no_candidate_is_and_not_a_number(tmp_path: Path) -> None:
    # 2026-10-17 field exam: "root cellar relative humidity ..." never reached the article Root cellar
    # (long texts about vegetables outscored it), and "... zinc 20 mg 10 days" led with "10 Days".
    veg = "store root vegetables humidity range recommended storing " * 20
    docs = [
        *(_doc("wiki", f"en/Veg{i}", f"Vegetable storage {i}", veg) for i in range(12)),
        _doc("dict", "root_cellar", "root cellar", "A cellar for storing root vegetables."),
        _doc("wiki", "en/Root_cellar", "Root cellar", "An underground room. " * 5),
        _doc("wiki", "en/10_Days", "10 Days", "may refer to films"),
    ]
    idx = TantivyIndex(tmp_path / "lexical", SMALL)
    idx.upsert(docs)
    idx.commit()
    searcher = OracleSearcher(
        idx, router=Router.from_sources(["wiki", "dict"]), reranker=OverlapReranker(), resolve=idx.lookup
    )
    searcher.source_weights = {"wiki": 1.15}  # the encyclopedia's article, not the dictionary's entry
    route = searcher.router.route(
        "root cellar relative humidity range recommended for storing root vegetables"
    )
    found = searcher._embedded_title(route, [], set())  # pyright: ignore[reportPrivateUsage]
    assert [h.doc_id for h in found] == [make_doc_id("wiki", "en/Root_cellar")]
    days = searcher.router.route("diarrhoea children oral rehydration zinc 20 mg 10 days")
    held = [SearchHit(make_doc_id("wiki", "en/10_Days"), 1.0, "10 Days", "", "wiki")]
    assert searcher._embedded_title(days, held, set()) == []  # pyright: ignore[reportPrivateUsage]
    idx.close()


def test_a_programming_name_asks_for_its_documentation(library: TantivyIndex) -> None:
    # 2026-10-17 field exam: Stack Overflow threads about functools.lru_cache, Array.prototype.flat(),
    # rsync --delete and highway=residential buried the reference pages for 7 of 11 questions.
    from wsp_oracle.pipeline import asks_for_reference

    searcher = _searcher(library)
    searcher.source_weights = {"stackexchange": 0.85}
    hits = [
        SearchHit(DocId("wsp:stackexchange/q1"), 1.0, "lru_cache typed", "", "stackexchange"),
        SearchHit(DocId("wsp:docs/functools"), 0.7, "functools", "", "docs"),
    ]
    named = searcher._weigh("functools.lru_cache typed=True", hits)  # pyright: ignore[reportPrivateUsage]
    assert [h.doc_id for h in named] == ["wsp:docs/functools", "wsp:stackexchange/q1"]
    plain = searcher._weigh("how to cache results in python", hits)  # pyright: ignore[reportPrivateUsage]
    assert plain[0].doc_id == "wsp:stackexchange/q1"
    assert asks_for_reference("rsync --delete")
    assert asks_for_reference("find(1)")
    assert not asks_for_reference("how do I fix this functools.lru_cache error")
    assert not asks_for_reference("26 U.S.C. 501")
    assert not asks_for_reference("Python 3.11")


def test_of_the_same_title_the_query_language_preferred_and_heaviest_source(tmp_path: Path) -> None:
    # 2026-10-17: "who was genghis khan" led with Citizendium's page (the only one among the
    # candidates); ten Wikipedias share the title and the English one was not in the first ten matches.
    docs = [
        _doc("citizendium", "Genghis_Khan", "Genghis Khan", "Mongol ruler."),
        *(_doc("wiki", f"{lang}/Genghis_Khan", "Genghis Khan", "Khan.", lang) for lang in ("de", "sv", "fr")),
        _doc("wiki", "en/Genghis_Khan", "Genghis Khan", "Founder of the Mongol Empire."),
        _doc("dict", "ephemeral", "ephemeral", "Lasting a short time."),
        _doc("wiki", "en/Ephemeral", "Ephemeral", "May refer to."),
    ]
    idx = TantivyIndex(tmp_path / "lexical", SMALL)
    idx.upsert(docs)
    idx.commit()
    searcher = OracleSearcher(
        idx, router=Router.from_sources(["wiki", "dict", "citizendium"]), resolve=idx.lookup
    )
    searcher.source_weights = {"wiki": 1.15}
    route = searcher.router.route("who was genghis khan")
    held = [
        SearchHit(make_doc_id("citizendium", "Genghis_Khan"), 1.0, "Genghis Khan", "", "citizendium", "en")
    ]
    best = searcher._best_titled(route, "Genghis Khan", held, None)  # pyright: ignore[reportPrivateUsage]
    assert best is not None
    assert best.doc_id == make_doc_id("wiki", "en/Genghis_Khan")
    define = replace(searcher.router.route("define ephemeral"), prefer=("dict",))
    word = searcher._best_titled(define, "ephemeral", [], None)  # pyright: ignore[reportPrivateUsage]
    assert word is not None
    assert word.doc_id == make_doc_id("wiki", "en/Ephemeral")  # the article; the definition box has the gloss
    idx.close()


# --- names, leads, other editions (2026-11-05 deep dive) -----------------------------------------

STORED = TantivyConfig(languages=("en",), writer_heap_bytes=30_000_000, writer_threads=1, store_text=True)


def _aliased(doc: DocRecord, *aliases: str) -> DocRecord:
    return replace(doc, aliases=aliases)


@pytest.fixture
def names(tmp_path: Path) -> Iterator[TantivyIndex]:
    """Pages a query names by another name (redirects), beside pages that merely hold its words."""
    docs = [
        _aliased(
            _doc(
                "wiki",
                "en/Myocardial_infarction",
                "Myocardial infarction",
                "Myocardial infarction\nSpecialty: Cardiology\n"
                "A myocardial infarction occurs when blood flow to a part of the myocardium stops.\n"
                "External links\nMyocardial infarction at a glance",
            ),
            "Heart attack",
        ),
        _aliased(
            _doc("wiki", "de/Herzinfarkt", "Herzinfarkt", "Ein Herzinfarkt ist ...", "de"), "Heart attack"
        ),
        _doc("medline", "heart-attack", "Heart Attack", "Heart attack: call for help at once."),
        _doc("music", "This_Heart_Attack", "This Heart Attack", "A song: heart attack, heart attack."),
        _doc(
            "wiktionary-en", "heart_attack", "heart attack", "heart attack (noun): a myocardial infarction."
        ),
        _aliased(
            _doc(
                "wiki",
                "en/Albert_Einstein",
                "Albert Einstein",
                "Albert Einstein was a theoretical physicist who developed the theory of relativity.",
            ),
            "Einstein",
        ),
        _doc(
            "ifixit",
            "ifixit_en_all/User/1554889/einstein",
            "einstein",
            "einstein\n@ein\neinstein\n1\nReputation",
        ),
        _doc(
            "wiki",
            "en/Photosynthesis",
            "Photosynthesis",
            "Photosynthesis\nFor other uses, see Photosynthesis (disambiguation) and the rest of them.\n"
            "Photosynthesis is the process by which plants turn light into chemical energy.\n"
            "External links\nPhotosynthesis at an intermediate level\nPhotosynthesis photosynthesis "
            "photosynthesis: an animation of photosynthesis",
        ),
    ]
    idx = TantivyIndex(tmp_path / "lexical", STORED)
    idx.upsert(docs)
    idx.commit()
    yield idx
    idx.close()


def _named(idx: TantivyIndex) -> OracleSearcher:
    searcher = OracleSearcher(
        idx,
        router=Router.from_sources(["wiki", "medline", "music", "wiktionary-en", "ifixit"]),
        reranker=OverlapReranker(),
        resolve=idx.lookup,
    )
    searcher.source_weights = {"wiki": 1.15}
    return searcher


def test_a_name_leads_to_the_page_it_redirects_to(names: TantivyIndex) -> None:
    # "heart attack" listed songs and films, never Myocardial infarction, while suggest offered it.
    res = _named(names).run("heart attack")
    ids = [h.doc_id for h in res.hits]
    assert ids[0] == make_doc_id("medline", "heart-attack")  # the exact title still leads
    assert ids[1] == make_doc_id("wiki", "en/Myocardial_infarction")  # then the page the name redirects to
    assert make_doc_id("wiki", "de/Herzinfarkt") not in ids[:2]  # another language's target is not pinned
    # its snippet is its lead, not the external links
    assert res.hits[1].snippet.startswith("A myocardial infarction occurs")


def test_a_user_profile_never_takes_the_exact_title_pin(names: TantivyIndex) -> None:
    # "Einstein" led with an iFixit profile titled "einstein" (reranker score 0.04).
    res = _named(names).run("Einstein")
    assert res.hits[0].doc_id == make_doc_id("wiki", "en/Albert_Einstein")


def test_a_title_hit_shows_its_lead_past_the_hatnote(names: TantivyIndex) -> None:
    res = _named(names).run("photosynthesis")
    assert res.hits[0].doc_id == make_doc_id("wiki", "en/Photosynthesis")
    assert res.hits[0].snippet.startswith("Photosynthesis is the process by which plants")


def test_a_name_that_is_a_title_or_leads_outside_the_sources_pins_nothing_more(names: TantivyIndex) -> None:
    searcher = _named(names)
    aliased = searcher._aliased  # pyright: ignore[reportPrivateUsage]
    heart = searcher.router.route("heart attack")
    assert aliased(searcher.router.route("This Heart Attack"), [], None) == []  # a title, not an alias
    assert aliased(heart, [], ["wiktionary-en", "medline"]) == []
    held = SearchHit(
        make_doc_id("wiki", "en/Myocardial_infarction"), 1.0, "Myocardial infarction", "", "wiki", "en"
    )
    assert aliased(heart, [replace(held, source_id="other")], None) == []  # held already; de is not English
    # an exact title from the reference source needs no other name
    medline = SearchHit(make_doc_id("medline", "heart-attack"), 1.0, "Heart Attack", "", "medline", "en")
    assert [h.doc_id for h in aliased(heart, [medline], None)] == [held.doc_id]
    searcher.source_weights["medline"] = 1.2
    assert aliased(heart, [medline], None) == []


def test_a_misspelling_matching_only_another_language_pins_the_corrected_title(tmp_path: Path) -> None:
    # "Isaac Newtonn": the French stemmer matched French pages only; the English article was absent.
    docs = [
        _doc("wiki", "fr/Isaac_Newton_(bryologiste)", "Isaac Newton (bryologiste)", "Bryologiste.", "fr"),
        _doc("wiki", "fr/Rue_Newton", "Rue Newton", "Une rue.", "fr"),
        _doc("wiki", "en/Isaac_Newton", "Isaac Newton", "Isaac Newton was an English mathematician."),
    ]
    idx = TantivyIndex(tmp_path / "lexical", SMALL)
    idx.upsert(docs)
    idx.commit()
    index = OracleIndex(_named(idx))
    french = [
        SearchHit(make_doc_id("wiki", "fr/Rue_Newton"), 1.0, "Rue Newton", "", "wiki", "fr"),
        SearchHit(make_doc_id("wiki", "fr/Isaac_Newton_(bryologiste)"), 0.9, "x", "", "wiki", "fr"),
    ]
    asked: list[str] = []

    def corrected() -> str:
        asked.append("yes")
        return "Isaac Newton"

    pin = index.correction_pin("Isaac Newtonn", french, corrected)
    assert pin is not None
    assert pin.doc_id == make_doc_id("wiki", "en/Isaac_Newton")
    # a list holding a page in the query's language is left alone, and the speller is not asked
    mixed = [*french, SearchHit(make_doc_id("wiki", "en/X"), 0.5, "X", "", "wiki", "en")]
    asked.clear()
    assert index.correction_pin("Isaac Newtonn", mixed, corrected) is None
    assert asked == []
    assert index.correction_pin("Isaac Newtonn", [], corrected) is None
    assert index.correction_pin("Isaac Newtonn", french, lambda: None) is None
    assert index.correction_pin("Isaac Newtonn", french, lambda: "Rue Newton") is None  # French: no pin
    idx.close()


def _hit(source: str, native: str, lang: str) -> SearchHit:
    return SearchHit(make_doc_id(source, native), 1.0, native, "", source, lang)


def test_other_language_dictionary_editions_make_room_for_the_query_language() -> None:
    from wsp_oracle.pipeline import (
        _is_dictionary,  # pyright: ignore[reportPrivateUsage]
        _spread_foreign_dictionaries,  # pyright: ignore[reportPrivateUsage]
    )

    ar = _hit("kiwix-multilingual-ar", "wiktionary_ar_all/acetaminophen", "ar")
    pl = _hit("kiwix-multilingual-pl", "wiktionary_pl_all/acetaminophen", "pl")
    zh = _hit("kiwix-multilingual-zh", "wiktionary_zh_all/acetaminophen", "zh")
    en = _hit("wiktionary-en", "acetaminophen", "en")
    ncbi = _hit("ncbi", "Acetaminophen", "en")
    article = _hit("wiki", "en/Paracetamol", "en")
    pt_wiki = _hit("kiwix-multilingual-pt", "wikipedia_pt_all/Paracetamol", "pt")
    assert _is_dictionary(ar)
    assert _is_dictionary(en)
    assert not _is_dictionary(pt_wiki)
    assert not _is_dictionary(_hit("wiki", "en/wiktionary_x/y", "en"))  # not a bundle's book
    pins, head = _spread_foreign_dictionaries(
        "acetaminophen", [article, ar, pl], [en, zh, pt_wiki, ncbi], fixed=1
    )
    assert pins == [article, ar]  # the first other-language entry keeps its place
    assert head == [en, pt_wiki, ncbi, pl, zh]  # the others follow the hits in English: moved, never dropped
    # an exact pin in another language counts as the first; a query in Chinese characters moves nothing
    assert _spread_foreign_dictionaries("acetaminophen", [ar], [pl, article], fixed=1) == (
        [ar],
        [article, pl],
    )
    assert _spread_foreign_dictionaries("東京", [], [en, zh, pl, article], fixed=0) == (
        [],
        [en, zh, pl, article],
    )
