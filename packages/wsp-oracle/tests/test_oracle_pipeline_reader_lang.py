"""Which language a query is in when it does not announce one, and what that decides: the exact
title, the near titles, the language bonus and the dictionary editions (2026-11-10 trawl,
search-index-09 and -16, with the changes its review required)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

import pytest
from oracle_helpers import OverlapReranker

from wsp_core.ids import DocId, make_doc_id
from wsp_core.model import DocRecord, SearchHit
from wsp_index import FusionConfig, TantivyConfig, TantivyIndex
from wsp_oracle import pipeline
from wsp_oracle.pipeline import (
    OracleIndex,
    OracleSearcher,
    _language,  # pyright: ignore[reportPrivateUsage]
    _near_pins,  # pyright: ignore[reportPrivateUsage]
    _own_language_first,  # pyright: ignore[reportPrivateUsage]
    _reader,  # pyright: ignore[reportPrivateUsage]
    _script_language,  # pyright: ignore[reportPrivateUsage]
    _stated_language,  # pyright: ignore[reportPrivateUsage]
    reader_key,
)
from wsp_oracle.router import Router

STORED = TantivyConfig(languages=("en",), writer_heap_bytes=30_000_000, writer_threads=1, store_text=True)


def _doc(source: str, native: str, title: str, text: str, lang: str = "en", *aliases: str) -> DocRecord:
    return DocRecord(
        doc_id=make_doc_id(source, native),
        source_id=source,
        native_id=native,
        title=title,
        lang=lang,
        license="CC0-1.0",
        text=text,
        aliases=aliases,
    )


def _index(tmp_path: Path, docs: Sequence[DocRecord]) -> TantivyIndex:
    idx = TantivyIndex(tmp_path / "lexical", STORED)
    idx.upsert(docs)
    idx.commit()
    return idx


def _searcher(idx: TantivyIndex, *sources: str, reranker: object | None = None) -> OracleSearcher:
    searcher = OracleSearcher(
        idx,
        router=Router.from_sources(list(sources)),
        reranker=OverlapReranker() if reranker is None else reranker,  # type: ignore[arg-type]
        resolve=idx.lookup,
    )
    searcher.source_weights = {"wiki": 1.15}
    return searcher


class _Flat:
    """The cross-encoder on a page of title matches: the same score for all, so only the rules order."""

    def rerank(self, query: str, hits: Sequence[SearchHit]) -> Sequence[SearchHit]:
        return [replace(h, score=0.9) for h in hits]


# --- which language ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "lang"),
    [
        ("القاهرة", "ar"),
        ("תל אביב", "he"),
        ("Αθήνα", "el"),
        ("서울", "ko"),
        ("とうきょう", "ja"),
        ("東京タワー", "ja"),  # kana with Han (and the long-vowel mark, a letter of both kana)
        ("ラーメン", "ja"),
        ("กรุงเทพ", "th"),
        ("दिल्ली", "hi"),
        ("القاهرة 1952", "ar"),  # digits and punctuation are no letters
        ("東京", None),  # Han alone: Chinese or Japanese
        ("Berliner Mauer", None),  # Latin
        ("Москва", None),  # Cyrillic is query_language's
        ("", None),
        ("1984", None),
        # a script names the language only when the whole query is in it (review change 3)
        ("β-carotene", None),
        ("μ decay", None),
        ("Δ9-THC", None),
        ("λ calculus", None),
        ("π", None),  # a Greek letter alone is a symbol
        ("ΔΣ", None),
        ("人々", None),  # the iteration mark is Han: still Chinese or Japanese
        ("日々の生活", "ja"),
        ("β δ", None),
        ("Tokyo 東京タワー", None),
        ("القاهرة Cairo", None),
        ("서울 東京", None),
    ],
)
def test_script_language(text: str, lang: str | None) -> None:
    assert _script_language(text) == lang


def test_the_language_is_announced_then_the_scripts_then_the_readers_then_the_default() -> None:
    assert _language("la tour de la ville") == ("fr", True)
    assert _language("la tour de la ville", "de") == ("fr", True)  # the query's own words win
    assert _language("القاهرة", "de") == ("ar", False)  # a script is a preference, never announced
    assert _language("Berliner Mauer", "de") == ("de", False)
    assert _language("Berliner Mauer") == ("en", False)
    assert _language("β-carotene") == ("en", False)
    assert _stated_language("Berliner Mauer") == (None, False)  # nothing says: not "en" as a guess
    assert _stated_language("東京") == (None, False)


def test_the_default_language_sent_by_the_reader_is_no_statement() -> None:
    # "en" from the Navigator and no lang at all are one reader: one cache entry, and neither says
    # which edition "東京" wants.
    assert [_reader(x) for x in (None, "", " ", "en", "EN", "en-GB", "en_US")] == [""] * 7
    assert [_reader(x) for x in ("de", "de-AT", "PT_br", "zh-Hant-TW")] == ["de", "de", "pt", "zh"]
    assert [_reader(x) for x in ("mul", "und", "zxx")] == ["", "", ""]  # no one language
    assert reader_key(None) == reader_key("en") == reader_key("en-GB") == "en"
    assert reader_key("de-AT") == reader_key("de") == "de"
    assert OracleIndex.reader_key("en-GB") == "en"


# --- the exact title -----------------------------------------------------------------------------


def _berlin(tmp_path: Path) -> tuple[TantivyIndex, OracleSearcher]:
    docs = [
        _doc("wiki", "de/Berliner_Mauer", "Berliner Mauer", "Die Berliner Mauer war ein Grenzwall.", "de"),
        _doc("quote", "Berliner_Mauer", "Berliner Mauer", "Zitate zur Berliner Mauer.", "de"),
        _doc(
            "wiki",
            "en/Berlin_Wall",
            "Berlin Wall",
            "The Berlin Wall was a guarded concrete barrier.",
            "en",
            "Berliner Mauer",
        ),
    ]
    idx = _index(tmp_path, docs)
    return idx, _searcher(idx, "wiki", "quote")


def _hide(monkeypatch: pytest.MonkeyPatch, searcher: OracleSearcher, *hidden: str) -> None:
    """The title search misses ``hidden`` (buried past its depth on the full library)."""
    titled = searcher._titled  # pyright: ignore[reportPrivateUsage]

    def without(title: str, sources: Sequence[str] | None) -> list[SearchHit]:
        return [h for h in titled(title, sources) if h.doc_id not in hidden]

    monkeypatch.setattr(searcher, "_titled", without)


def test_an_exact_title_in_an_unannounced_language_is_found_in_the_trie(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # "Berliner Mauer" has no function word and was taken for English: the trie's German article was
    # dropped for its language and Wikiquote's page led.
    idx, searcher = _berlin(tmp_path)
    german = make_doc_id("wiki", "de/Berliner_Mauer")
    _hide(monkeypatch, searcher, german)
    route = searcher.router.route("Berliner Mauer")
    hit = searcher._best_titled(route, "Berliner Mauer", [], None)  # pyright: ignore[reportPrivateUsage]
    assert hit is not None
    assert hit.doc_id == german
    # a guard, not the proof (in three documents the article is among the candidates anyway): the
    # German article leads and the English page of that name follows
    ids = [h.doc_id for h in searcher.run("Berliner Mauer").hits]
    assert ids[:2] == [german, make_doc_id("wiki", "en/Berlin_Wall")]
    idx.close()


def test_a_query_that_names_its_language_still_drops_other_editions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    idx, searcher = _berlin(tmp_path)
    german = make_doc_id("wiki", "de/Berliner_Mauer")
    named = searcher._named_titled  # pyright: ignore[reportPrivateUsage]
    assert [h.doc_id for h in named("Berliner Mauer", "en", ["wiki"], strict=False)] == [german]
    assert named("Berliner Mauer", "en", ["wiki"], strict=True) == []
    assert [h.doc_id for h in named("Berliner Mauer", "de", ["wiki"], strict=True)] == [german]
    # through _best_titled: a query in French (two function words) is not answered with the trie's
    # German article
    _hide(monkeypatch, searcher, german)
    french = replace(searcher.router.route("Berliner Mauer"), query="la chute de la Berliner Mauer", title="")
    hit = searcher._best_titled(french, "Berliner Mauer", [], None)  # pyright: ignore[reportPrivateUsage]
    assert hit is not None
    assert hit.doc_id == make_doc_id("quote", "Berliner_Mauer")
    idx.close()


def test_a_script_names_the_language_of_an_exact_title(tmp_path: Path) -> None:
    # The Cebuano page comes first among equals (same source, same weight): the script decides.
    docs = [
        _doc("wiki", "ceb/القاهرة", "القاهرة", "Ang القاهرة usa ka lungsod.", "ceb"),
        _doc("wiki", "ar/القاهرة", "القاهرة", "القاهرة هي عاصمة مصر.", "ar"),
    ]
    idx = _index(tmp_path, docs)
    searcher = _searcher(idx, "wiki")
    held = [
        SearchHit(make_doc_id("wiki", "ceb/القاهرة"), 1.0, "القاهرة", "", "wiki", "ceb"),
        SearchHit(make_doc_id("wiki", "ar/القاهرة"), 0.9, "القاهرة", "", "wiki", "ar"),
    ]
    route = searcher.router.route("القاهرة")
    hit = searcher._best_titled(route, "القاهرة", held, None)  # pyright: ignore[reportPrivateUsage]
    assert hit is not None
    assert hit.doc_id == make_doc_id("wiki", "ar/القاهرة")
    idx.close()


def test_the_readers_language_chooses_among_editions_of_a_title(tmp_path: Path) -> None:
    docs = [
        _doc("wiki", "en/Golem", "Golem", "A golem is an animated being of clay."),
        _doc("wiki", "de/Golem", "Golem", "Der Golem ist ein Wesen aus Lehm.", "de"),
    ]
    idx = _index(tmp_path, docs)
    searcher = _searcher(idx, "wiki")
    assert searcher.run("Golem").hits[0].doc_id == make_doc_id("wiki", "en/Golem")
    assert searcher.run("Golem", lang="en-GB").hits[0].doc_id == make_doc_id("wiki", "en/Golem")
    assert searcher.run("Golem", lang="de-AT").hits[0].doc_id == make_doc_id("wiki", "de/Golem")
    idx.close()


def test_a_foreign_stub_known_only_to_the_trie_leads_and_the_alias_target_follows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Review change 4, an intended difference: "Einstein" is only a redirect in English. When the
    # title search misses the Cebuano stub and the trie holds it, the stub is now the exact pin (as it
    # already was whenever the title search found it) and Albert Einstein is the second hit.
    docs = [
        _doc("wiki", "ceb/Einstein", "Einstein", "Ang Einstein usa ka kráter sa bulan.", "ceb"),
        _doc("wiki", "en/Albert_Einstein", "Albert Einstein", "A theoretical physicist.", "en", "Einstein"),
        _doc("music", "Einstein_on_the_Beach", "Einstein on the Beach", "An opera: Einstein, Einstein."),
    ]
    idx = _index(tmp_path, docs)
    searcher = _searcher(idx, "wiki", "music")
    stub, albert = make_doc_id("wiki", "ceb/Einstein"), make_doc_id("wiki", "en/Albert_Einstein")
    _hide(monkeypatch, searcher, stub)
    route = searcher.router.route("Einstein")
    pins = searcher._pins(route, [], None)  # pyright: ignore[reportPrivateUsage]
    assert [p.doc_id for p in pins] == [stub]
    pins += searcher._aliased(route, pins, None)  # pyright: ignore[reportPrivateUsage]
    assert [p.doc_id for p in pins] == [stub, albert]
    assert albert in [h.doc_id for h in searcher.run("Einstein").hits[:2]]
    idx.close()


# --- the near titles -----------------------------------------------------------------------------


def _hit(source: str, native: str, lang: str, score: float = 0.9) -> SearchHit:
    return SearchHit(make_doc_id(source, native), score, native, "", source, lang)


def test_an_exact_title_widens_the_languages_of_the_near_titles_and_never_replaces_the_readers() -> None:
    en, de, ja = _hit("a", "en", "en", 0.7), _hit("b", "de", "de", 0.8), _hit("c", "ja", "ja", 0.95)
    untagged = _hit("d", "none", "", 0.99)
    german = _hit("wiki", "de/Berliner_Mauer", "de")
    # "Berliner Mauer": the German near titles stay pinned beside the English ones; Japanese leaves
    assert _near_pins([ja, en, de], [german], "en", False, reranked=True) == ([en, de], [ja])
    # a Cebuano stub as the only exact title: English near titles stay pinned
    stub = _hit("wiki", "ceb/Tambora", "ceb")
    ceb = _hit("e", "ceb", "ceb", 0.6)
    assert _near_pins([ja, ceb, en], [stub], "en", False, reranked=True) == ([en, ceb], [ja])
    # ... and first, however the reranker scores them: eight Cebuano near titles a hair ahead would
    # otherwise push the English one past the pins kept (TITLE_PINS)
    stubs = [_hit(f"s{i}", "ceb", "ceb", 0.95) for i in range(8)]
    assert _near_pins([*stubs, en], [stub], "en", False, reranked=True)[0][0] == en
    # an English exact title: every other language leaves; a page that states none stays, last
    article = _hit("wiki", "en/United_States", "en")
    assert _near_pins([ja, untagged, de, en], [article], "en", False, reranked=True) == (
        [en, untagged],
        [ja, de],
    )
    # the query's words announce the language: no exact pin is needed
    assert _near_pins([ja, en], [], "en", True, reranked=True) == ([en], [ja])


def test_without_any_exact_title_the_near_titles_keep_their_order() -> None:
    en, de, ja = _hit("a", "en", "en", 0.7), _hit("b", "de", "de", 0.8), _hit("c", "ja", "ja", 0.95)
    # nothing says what the reader's pages are: the query's language first, then the score, all pinned
    assert _near_pins([ja, de, en], [], "en", False, reranked=True) == ([en, ja, de], [])
    # an exact pin that states no language says nothing either
    assert _near_pins([ja, de, en], [_hit("x", "x", "")], "en", False, reranked=True) == ([en, ja, de], [])
    # without reranker scores there is nothing to place a demoted title by: the order given stands
    article = _hit("wiki", "en/United_States", "en")
    assert _near_pins([ja, de, en], [article], "en", False, reranked=False) == ([ja, de, en], [])


def _states(tmp_path: Path) -> tuple[TantivyIndex, OracleSearcher]:
    docs = [
        _doc("wiki", "en/United_States", "United States", "The United States is a country."),
        _doc("jawikt", "United_States", "United States", "United States: アメリカ合衆国。", "ja"),
        _doc("pt", "Classe_United_States", "Classe United States", "A classe United States.", "pt"),
        _doc("gov", "constitution", "Constitution", "The constitution of the United States."),
        _doc("gov", "congress", "Congress", "The Congress of the United States makes its laws."),
        _doc("atlas", "United_States_map", "United States map", "A map of the United States."),
    ]
    idx = _index(tmp_path, docs)
    return idx, _searcher(idx, "wiki", "jawikt", "pt", "gov", "atlas", reranker=_Flat())


def test_foreign_near_titles_follow_the_reader_language_results(tmp_path: Path) -> None:
    # "united states": every source gave its best near title as a pin, so a Japanese dictionary entry
    # and a Portuguese page stood at ranks 2-5 before every reranked English result.
    idx, searcher = _states(tmp_path)
    ids = [h.doc_id for h in searcher.run("united states").hits]
    article, near = make_doc_id("wiki", "en/United_States"), make_doc_id("atlas", "United_States_map")
    foreign = [make_doc_id("jawikt", "United_States"), make_doc_id("pt", "Classe_United_States")]
    english = [make_doc_id("gov", "constitution"), make_doc_id("gov", "congress")]
    assert ids[:2] == [article, near]  # the English near title is still pinned
    assert set(foreign) <= set(ids)  # nothing is dropped
    assert max(ids.index(x) for x in english) < min(ids.index(x) for x in foreign)
    idx.close()


class _Scored:
    """The cross-encoder as it scored the live library: by title, ``default`` for any other page."""

    def __init__(self, scores: dict[tuple[str, str], float], default: float = 0.02) -> None:
        self.scores = scores
        self.default = default

    def rerank(self, query: str, hits: Sequence[SearchHit]) -> Sequence[SearchHit]:
        return [replace(h, score=self.scores.get((h.title, h.lang), self.default)) for h in hits]


# What GET /v1/search?q=united+states gave on the live library at 6b5d7cc (2026-11-13), top 20: the
# doc ids, languages and reranker scores (the response's score divided by the source weight, 1.15 for
# wikipedia-top10). A page titled as the query scores near 1.0 in any language; the best English
# result after the article 0.72.
_LIVE_STATES = {
    ("United States", "en"): 0.991,
    ("United States", "de"): 0.981,
    ("United States", "ja"): 0.995,
    ("Classe United States", "pt"): 0.962,
    ("COVID-19 pandemic in the United States", "en"): 0.721,
    ("United States Range", "ceb"): 0.533,
    ("United States of Mind", "pt"): 0.581,
    ("U.S. federal government response to the COVID-19 pandemic", "en"): 0.347,
    ("COVID-19 pandemic in North America", "en"): 0.194,  # 0.204 with the language bonus
    ("Healthcare in the United States", "en"): 0.099,
    ("United States Mine", "ceb"): 0.111,
    ("Sanctions of the United States", "en"): 0.03,  # below the low-confidence bar (0.05)
}


def _live_states(tmp_path: Path) -> tuple[TantivyIndex, OracleSearcher]:
    top10, md = "wikipedia-top10", "mdwiki"
    ja, pt, extra = "kiwix-multilingual-ja", "kiwix-multilingual-pt", "wikipedia-text-extra"
    docs = [
        _doc(top10, "en/United_States", "United States", "The United States is a country."),
        # the same source's German page of that title: a disambiguation of ships and films
        _doc(top10, "de/United_States", "United States", "United States steht für: ein Schiff.", "de"),
        _doc(
            top10, "ceb/United_States_Range", "United States Range", "Bukid ang United States Range.", "ceb"
        ),
        _doc(top10, "ceb/United_States_Mine", "United States Mine", "Minahan ang United States Mine.", "ceb"),
        _doc(ja, "wiktionary_ja_all/United_States", "United States", "United States: アメリカ合衆国。", "ja"),
        _doc(
            pt,
            "wikipedia_pt_all/Classe_United_States",
            "Classe United States",
            "A classe United States.",
            "pt",
        ),
        _doc(
            pt,
            "wikipedia_pt_all/United_States_of_Mind",
            "United States of Mind",
            "United States of Mind.",
            "pt",
        ),
        _doc(
            extra,
            "pt/Classe_United_States",
            "Classe United States",
            "A classe United States de navios.",
            "pt",
        ),
        _doc(
            md,
            "COVID-19_pandemic_in_the_United_States",
            "COVID-19 pandemic in the United States",
            "The COVID-19 pandemic in the United States.",
        ),
        _doc(
            md,
            "U.S._federal_government_response_to_the_COVID-19_pandemic",
            "U.S. federal government response to the COVID-19 pandemic",
            "The federal government of the United States responded.",
        ),
        _doc(
            md,
            "COVID-19_pandemic_in_North_America",
            "COVID-19 pandemic in North America",
            "The pandemic in North America: the United States, Canada and Mexico.",
        ),
        _doc(
            md,
            "Healthcare_in_the_United_States",
            "Healthcare in the United States",
            "Healthcare in the United States is provided by many organizations.",
        ),
        _doc(
            md,
            "Sanctions_of_the_United_States",
            "Sanctions of the United States",
            "Sanctions of the United States, a list.",
        ),
    ]
    idx = _index(tmp_path, docs)
    searcher = _searcher(idx, top10, md, ja, pt, extra, reranker=_Scored(_LIVE_STATES))
    searcher.source_weights = {top10: 1.15}
    return idx, searcher


def test_foreign_pages_titled_as_the_query_follow_the_readers_pages_whatever_the_reranker_scores(
    tmp_path: Path,
) -> None:
    # Live after 4a-1: "united states" still listed de, ja, pt, pt at ranks 2-5. The near titles were
    # no longer pinned, but the reranker scores a page titled as the query 0.96-1.0 in any language
    # (the English results 0.72 and less), so the head they joined opened with them; the language
    # bonus is 0.01. The test above gives every page one score and so never met this.
    idx, searcher = _live_states(tmp_path)
    hits = searcher.run("united states").hits
    ids = [h.doc_id for h in hits]
    langs = [h.lang for h in hits]
    assert ids[0] == make_doc_id("wikipedia-top10", "en/United_States")
    confident = [
        make_doc_id("mdwiki", native)
        for native in (
            "COVID-19_pandemic_in_the_United_States",
            "U.S._federal_government_response_to_the_COVID-19_pandemic",
            "COVID-19_pandemic_in_North_America",
        )
    ]
    assert ids[1:4] == confident  # the reader's pages (0.72, 0.35, 0.20), by the reranker's score
    # then the other languages' pages, by their score: nothing is dropped
    assert ids[4] == make_doc_id("wikipedia-top10", "de/United_States")
    assert ids[5] == make_doc_id("kiwix-multilingual-ja", "wiktionary_ja_all/United_States")
    assert len(ids) == 13
    assert sorted(langs[4:11]) == ["ceb", "ceb", "de", "ja", "pt", "pt", "pt"]
    # English pages the reranker gives less than OWN_LEAD (0.10, 0.03) do not come before them
    assert ids[-2:] == [
        make_doc_id("mdwiki", "Healthcare_in_the_United_States"),
        make_doc_id("mdwiki", "Sanctions_of_the_United_States"),
    ]
    idx.close()


def test_a_foreign_title_the_query_names_is_the_exact_pin_and_nothing_moves(tmp_path: Path) -> None:
    # "Classe United States" has no English page of its name: the Portuguese page is the exact title,
    # the other Portuguese one follows by its score, before the English results.
    idx, searcher = _live_states(tmp_path)
    ids = [h.doc_id for h in searcher.run("Classe United States").hits]
    assert set(ids[:2]) == {
        make_doc_id("kiwix-multilingual-pt", "wikipedia_pt_all/Classe_United_States"),
        make_doc_id("wikipedia-text-extra", "pt/Classe_United_States"),
    }
    idx.close()


def test_the_readers_pages_lead_the_head_only_when_the_query_names_a_page_of_theirs() -> None:
    en, low = _hit("a", "en", "en", 0.72), _hit("a", "en2", "en", 0.1)
    de, ja, pt = _hit("b", "de", "de", 0.98), _hit("c", "ja", "ja", 0.99), _hit("d", "pt", "pt", 0.5)
    untagged = _hit("e", "none", "", 0.4)
    head = [ja, de, en, pt, untagged, low]
    first = [en, untagged, ja, de, pt, low]
    article = _hit("wiki", "en/United_States", "en")
    # an English exact title: English (and pages that state no language) at OWN_LEAD or more first,
    # then the rest by score; the English page below it (0.1) stays where its score put it
    assert _own_language_first(head, [article], [article], "en", False) == first
    assert _own_language_first(head, [article], [article], "en", True) == first  # "the united states"
    # no exact title, or one in another language or in none: the scores' order stands
    assert _own_language_first(head, [], [], "en", False) == head
    classe = _hit("wiki", "pt/Classe", "pt")
    assert _own_language_first(head, [classe], [classe], "en", False) == head
    assert _own_language_first(head, [_hit("x", "x", "")], [_hit("x", "x", "")], "en", False) == head
    # "Berliner Mauer", "Estados Unidos": the foreign exact title and its English alias. The alias is
    # no page titled as the query: nothing moves (the Portuguese "Estados Unidos" keeps its place)
    mauer, wall = _hit("wiki", "de/Berliner_Mauer", "de"), _hit("wiki", "en/Berlin_Wall", "en")
    assert _own_language_first(head, [mauer], [mauer, wall], "en", False) == head
    # a reader who set German (a preference): German and the default language are own
    assert _own_language_first(head, [mauer], [mauer], "de", False) == [de, en, untagged, ja, pt, low]
    # ... and the default language's exact title is a page of theirs too (no Finnish one held)
    assert _own_language_first(head, [article], [article], "fi", False) == first
    # an exact title beside the reader's widens "own": Japanese with English
    both = [article, _hit("wiki", "ja/United_States", "ja")]
    assert _own_language_first(head, [article], both, "en", False) == [ja, en, untagged, de, pt, low]
    # a query whose words announce another language (every Cyrillic query is "Russian"): untouched
    assert _own_language_first(head, [mauer], [mauer], "de", True) == head
    assert _own_language_first(head, [article], [article], "ru", True) == head
    # no page of the reader's at OWN_LEAD: nothing to put first
    assert _own_language_first([ja, de, low], [article], [article], "en", False) == [ja, de, low]


def test_a_foreign_title_with_an_english_alias_keeps_the_other_editions_of_that_title(tmp_path: Path) -> None:
    # Review of this change: "Estados Unidos" pins the Spanish article and its English alias "United
    # States". The alias alone made English the reader's pages, and the Portuguese article of the
    # very title fell behind every English result.
    docs = [
        _doc("wiki", "es/Estados_Unidos", "Estados Unidos", "Estados Unidos es un país.", "es"),
        _doc("wiki", "en/United_States", "United States", "A country.", "en", "Estados Unidos"),
        _doc("ptwiki", "Estados_Unidos", "Estados Unidos", "Estados Unidos é um país.", "pt"),
        _doc("guide", "Mexico", "Mexico", "Mexico borders the Estados Unidos to the north."),
    ]
    idx = _index(tmp_path, docs)
    scores = {("Estados Unidos", "es"): 0.99, ("Estados Unidos", "pt"): 0.99, ("Mexico", "en"): 0.4}
    searcher = _searcher(idx, "wiki", "ptwiki", "guide", reranker=_Scored(scores, default=0.9))
    ids = [h.doc_id for h in searcher.run("Estados Unidos").hits]
    assert ids[:2] == [make_doc_id("wiki", "es/Estados_Unidos"), make_doc_id("wiki", "en/United_States")]
    assert ids.index(make_doc_id("ptwiki", "Estados_Unidos")) < ids.index(make_doc_id("guide", "Mexico"))
    idx.close()


def test_a_query_in_han_characters_does_not_send_the_chinese_pages_below_the_english(tmp_path: Path) -> None:
    # "東京" alone is Chinese or Japanese: with the Japanese exact title and the English alias pinned,
    # the default language is still a guess, and the Chinese page keeps the place its score gives it.
    docs = [
        _doc("wiki", "ja/東京", "東京", "東京は日本の首都。", "ja"),
        _doc("wiki", "en/Tokyo", "Tokyo", "Tokyo (東京) is the capital of Japan.", "en", "東京"),
        _doc("zhwiki", "東京都", "東京都", "東京 都是日本首都。", "zh"),
        _doc("guide", "Kanto", "Kanto", "Kanto holds 東京 and its neighbours."),
    ]
    idx = _index(tmp_path, docs)
    scores = {("東京", "ja"): 0.99, ("Tokyo", "en"): 0.9, ("東京都", "zh"): 0.8, ("Kanto", "en"): 0.3}
    searcher = _searcher(idx, "wiki", "zhwiki", "guide", reranker=_Scored(scores))
    for lang in (None, "de"):  # nor does a German reader's setting say which of the two it is
        ids = [h.doc_id for h in searcher.run("東京", lang=lang).hits]
        assert ids[0] == make_doc_id("wiki", "ja/東京")
        assert ids.index(make_doc_id("zhwiki", "東京都")) < ids.index(make_doc_id("guide", "Kanto"))
    idx.close()


def test_a_foreign_stub_as_the_only_exact_title_does_not_demote_the_reader_language(tmp_path: Path) -> None:
    # Review change 1: the Cebuano edition holds stubs titled with names the English one lacks. Such
    # a stub as the only exact title must not make Cebuano the language of the run: English near
    # titles stay pinned, the language bonus stays English, and the English dictionary is not the
    # foreign one.
    docs = [
        _doc("wiki", "ceb/Tambora", "Tambora", "Ang Tambora usa ka bukid.", "ceb"),
        _doc("volcano", "Mount_Tambora", "Mount Tambora", "Mount Tambora is a volcano; Tambora erupted."),
        _doc("svwiki", "Tambora_(djur)", "Tambora (djur)", "Tambora är ett släkte.", "sv"),
        _doc("history", "1815", "Year Without a Summer", "The eruption of Tambora in 1815."),
        _doc("cebu", "Bukid", "Bukid", "Ang bukid nga Tambora.", "ceb"),
        _doc("wiktionary-en", "Tambora", "Tambora", "Tambora (proper noun): a volcano."),
        _doc("kiwix-multilingual-pl", "wiktionary_pl_all/Tambora", "Tambora", "Tambora: wulkan.", "pl"),
        _doc("kiwix-multilingual-ar", "wiktionary_ar_all/Tambora", "Tambora", "Tambora: بركان.", "ar"),
    ]
    idx = _index(tmp_path, docs)
    searcher = _searcher(
        idx,
        "wiki",
        "volcano",
        "svwiki",
        "history",
        "cebu",
        "wiktionary-en",
        "kiwix-multilingual-pl",
        "kiwix-multilingual-ar",
        reranker=_Flat(),
    )
    ids = [h.doc_id for h in searcher.run("Tambora").hits]
    at = ids.index
    stub, mount = make_doc_id("wiki", "ceb/Tambora"), make_doc_id("volcano", "Mount_Tambora")
    assert ids[0] == stub  # the exact title (accepted since 2026-11-05)
    # then the English near titles, still pinned
    assert set(ids[1:3]) == {mount, make_doc_id("wiktionary-en", "Tambora")}
    # the Swedish near title is no longer pinned before the English result
    assert at(make_doc_id("history", "1815")) < at(make_doc_id("svwiki", "Tambora_(djur)"))
    # the bonus is English: of two results the reranker scores alike, the English one leads
    assert at(make_doc_id("history", "1815")) < at(make_doc_id("cebu", "Bukid"))
    # the English dictionary entry is not one of the foreign editions sent to the end
    foreign = [
        make_doc_id("kiwix-multilingual-pl", "wiktionary_pl_all/Tambora"),
        make_doc_id("kiwix-multilingual-ar", "wiktionary_ar_all/Tambora"),
    ]
    assert at(make_doc_id("wiktionary-en", "Tambora")) < min(at(x) for x in foreign)
    assert max(at(x) for x in foreign) == len(ids) - 1  # the second foreign edition is the last hit
    idx.close()


# --- the dictionary editions ---------------------------------------------------------------------


def _spy(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str | None, bool]]:
    """Each call of ``_spread_foreign_dictionaries`` by ``run``: the language given, and whether the
    order came back as it went in."""
    calls: list[tuple[str | None, bool]] = []
    spread = pipeline._spread_foreign_dictionaries  # pyright: ignore[reportPrivateUsage]

    def spy(
        query: str, pins: Sequence[SearchHit], head: Sequence[SearchHit], *, fixed: int, lang: str | None
    ) -> tuple[list[SearchHit], list[SearchHit]]:
        out = spread(query, pins, head, fixed=fixed, lang=lang)
        calls.append((lang, out == (list(pins), list(head))))
        return out

    monkeypatch.setattr(pipeline, "_spread_foreign_dictionaries", spy)
    return calls


def test_a_query_in_han_characters_keeps_its_dictionary_editions_in_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Review change 2: "東京" says nothing about which edition it wants. The default language is a
    # guess, and passing it on would send the Japanese, Chinese and Polish entries to the end.
    docs = [
        _doc("kiwix-multilingual-ja", "wiktionary_ja_all/東京", "東京", "東京: 日本の首都。", "ja"),
        _doc("kiwix-multilingual-zh", "wiktionary_zh_all/東京", "東京", "東京: 日本首都。", "zh"),
        _doc("kiwix-multilingual-pl", "wiktionary_pl_all/東京", "東京", "東京: stolica Japonii.", "pl"),
        _doc("wiki", "en/Tokyo", "Tokyo", "Tokyo (東京) is the capital of Japan.", "en", "東京"),
        _doc("wiktionary-en", "東京", "東京", "東京: Tokyo."),
    ]
    idx = _index(tmp_path, docs)
    names = ("wiki", "wiktionary-en", *(f"kiwix-multilingual-{x}" for x in ("ja", "zh", "pl")))
    searcher = _searcher(idx, *names, reranker=_Flat())
    calls = _spy(monkeypatch)
    hits = searcher.run("define 東京").hits
    assert sum(pipeline._is_dictionary(h) for h in hits) == 4  # pyright: ignore[reportPrivateUsage]
    assert calls == [(None, True)]
    calls.clear()
    searcher.run("define 東京", lang="en-GB")  # the default language sent is no statement either
    assert calls == [(None, True)]
    calls.clear()
    polish = searcher.run("define 東京", lang="pl")  # a reader who says Polish is heard
    assert [lang for lang, _ in calls] == ["pl"]
    assert polish.hits[0].doc_id == make_doc_id("kiwix-multilingual-pl", "wiktionary_pl_all/東京")
    calls.clear()
    searcher.run("Tambora")  # Latin letters and nothing said: the function's own rule, English
    assert [lang for lang, _ in calls] == [None]
    calls.clear()
    searcher.run("東京タワー")  # kana names Japanese
    assert [lang for lang, _ in calls] == ["ja"]
    idx.close()


# --- the cache -----------------------------------------------------------------------------------


class _Counting:
    def __init__(self) -> None:
        self.runs = 0

    def search(
        self, query: str, *, limit: int = 10, offset: int = 0, sources: Sequence[str] | None = None
    ) -> Sequence[SearchHit]:
        self.runs += 1
        return [SearchHit(DocId(f"wsp:a/{i}"), 1.0 / (i + 1), f"T{i}", "m", "a") for i in range(3)]


def test_the_reader_language_is_part_of_the_cache_key() -> None:
    lex = _Counting()
    index = OracleIndex(OracleSearcher(lex, fusion=FusionConfig(depth=10)))  # type: ignore[arg-type]
    index.result("q")
    once = lex.runs
    assert once
    index.result("q", lang="en")
    index.result("q", lang="en-GB")
    index.search("q", lang="EN")
    assert lex.runs == once  # no lang and the default language: one entry
    index.result("q", lang="de")
    assert lex.runs == 2 * once  # another reader's order is not served from it
    index.result("q", lang="de-AT")
    index.result("q")
    assert lex.runs == 2 * once


def test_a_guessed_language_with_no_page_falls_back_to_the_default(tmp_path: Path) -> None:
    # A script (or the reader) names a preference, not a filter: "서울" with no Korean page held must
    # still find the English article it redirects to, and English results keep their bonus.
    docs = [
        _doc("wiki", "en/Seoul", "Seoul", "Seoul is the capital of South Korea.", "en", "서울"),
        _doc("wiki", "de/Seoul", "Seoul", "Seoul ist die Hauptstadt Südkoreas.", "de", "서울"),
        _doc("other", "p", "Notes", "서울 서울 notes."),
    ]
    idx = _index(tmp_path, docs)
    searcher = _searcher(idx, "wiki", "other")
    seoul = make_doc_id("wiki", "en/Seoul")
    assert searcher.run("서울").hits[0].doc_id == seoul
    assert searcher.run("서울", lang="fr").hits[0].doc_id == seoul  # no French page either
    assert searcher.run("서울", lang="de").hits[0].doc_id == make_doc_id("wiki", "de/Seoul")
    idx.close()


def test_a_script_several_languages_write_does_not_name_the_foreign_dictionaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Persian "تهران" is in Arabic script: "ar" is a fair preference between editions, but not a
    # reason to send the Persian Wiktionary entry to the end as foreign.
    docs = [_doc("wiki", "fa/تهران", "تهران", "تهران پایتخت ایران است.", "fa")]
    idx = _index(tmp_path, docs)
    searcher = _searcher(idx, "wiki")
    calls = _spy(monkeypatch)
    searcher.run("تهران")
    searcher.run("تهران", lang="ar")  # a reader who says Arabic is heard
    searcher.run("서울")  # Hangul writes Korean alone
    assert [lang for lang, _ in calls] == [None, "ar", "ko"]
    idx.close()


def test_a_reader_language_with_no_edition_leaves_the_default_language_page_first(tmp_path: Path) -> None:
    # ``lang=fi`` and no Finnish page: every edition was "foreign" alike and the first of the pool led
    # (the Cebuano page here, same source and weight). The default language's page is the answer.
    docs = [
        _doc("wiki", "ceb/Paris", "Paris", "Ang Paris usa ka lungsod.", "ceb"),
        _doc("wiki", "en/Paris", "Paris", "Paris is the capital of France.", "en"),
        _doc("wiki", "de/Paris", "Paris", "Paris ist die Hauptstadt Frankreichs.", "de"),
    ]
    idx = _index(tmp_path, docs)
    searcher = _searcher(idx, "wiki")
    held = [
        SearchHit(make_doc_id("wiki", "ceb/Paris"), 1.0, "Paris", "", "wiki", "ceb"),
        SearchHit(make_doc_id("wiki", "de/Paris"), 0.95, "Paris", "", "wiki", "de"),
        SearchHit(make_doc_id("wiki", "en/Paris"), 0.9, "Paris", "", "wiki", "en"),
    ]

    def best(lang: str | None) -> DocId:
        route = replace(searcher.router.route("Paris"), reader_lang=_reader(lang))
        hit = searcher._best_titled(route, "Paris", held, None)  # pyright: ignore[reportPrivateUsage]
        assert hit is not None
        return hit.doc_id

    assert best("fi") == make_doc_id("wiki", "en/Paris")
    assert best("xx") == make_doc_id("wiki", "en/Paris")
    assert best("de") == make_doc_id("wiki", "de/Paris")  # a reader language that has the page
    assert best(None) == make_doc_id("wiki", "en/Paris")
    # a language the script names (not the reader) orders as before: no English page jumps the pool
    arabic = [
        SearchHit(make_doc_id("wiki", "ceb/Paris"), 1.0, "تهران", "", "wiki", "fa"),
        SearchHit(make_doc_id("wiki", "en/Paris"), 0.9, "تهران", "", "wiki", "en"),
    ]
    hit = searcher._best_titled(searcher.router.route("تهران"), "تهران", arabic, None)  # pyright: ignore[reportPrivateUsage]
    assert hit is not None
    assert hit.doc_id == arabic[0].doc_id
    idx.close()
