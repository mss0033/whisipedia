"""Hits in the query's language (English when the query does not say) win near-ties."""

from __future__ import annotations

from collections.abc import Sequence

from wsp_core.ids import make_doc_id
from wsp_core.model import SearchHit
from wsp_oracle.pipeline import _prefer_language  # pyright: ignore[reportPrivateUsage]
from wsp_oracle.router import Intent


def h(native: str, score: float, lang: str) -> SearchHit:
    return SearchHit(make_doc_id("s", native), score, native, "", "s", lang)


def test_near_ties_go_to_the_query_language_but_clear_winners_stay() -> None:
    head = [h("es-question", 0.998, "es"), h("en-article", 0.996, "en"), h("es-best", 0.99, "mul")]
    new_head, _ = _prefer_language("python list comprehension", head, [], reranked=True)
    assert [x.doc_id for x in new_head][:2] == [
        make_doc_id("s", "en-article"),
        make_doc_id("s", "es-question"),
    ]
    far = [h("es-great", 0.95, "es"), h("en-weak", 0.40, "en")]
    assert _prefer_language("python", far, [], reranked=True)[0][0].lang == "es"  # relevance still wins


def test_the_query_language_is_preferred_when_it_is_clear() -> None:
    head = [h("en", 0.99, "en"), h("fr", 0.985, "fr")]
    new_head, _ = _prefer_language("la tour de la ville", head, [], reranked=True)
    assert new_head[0].lang == "fr"


def test_without_a_reranker_scores_are_scaled_not_shifted() -> None:
    head = [h("es", 0.033, "es"), h("en", 0.030, "en")]
    new_head, tail = _prefer_language("water", head, [h("t", 0.01, "en")], reranked=False)
    assert new_head[0].lang == "en"  # 0.030 * 1.15 > 0.033
    assert tail[0].score == 0.01 * 1.15


def test_duplicate_records_collapse_to_the_best() -> None:
    from wsp_oracle.pipeline import _collapse_duplicates  # pyright: ignore[reportPrivateUsage]

    hits = [
        SearchHit(
            make_doc_id("pubmed", "1"), 0.9, "A Case of Snake Bite Treated with Strychnine.", "", "pubmed"
        ),
        SearchHit(
            make_doc_id("pubmed", "2"), 0.8, "Case of Snake-Bite Treated with Strychnine", "", "pubmed"
        ),
        SearchHit(make_doc_id("mdwiki", "3"), 0.7, "Snakebite", "", "mdwiki"),
        SearchHit(make_doc_id("wiki", "4"), 0.6, "Case of snake bite treated with strychnine", "", "wiki"),
    ]
    asked: list[list[str]] = []

    def hashes(ids: Sequence[str]) -> dict[str, str]:
        asked.append(list(ids))
        return dict.fromkeys(ids, "same note")

    out = _collapse_duplicates(hits, hashes)
    assert [h.doc_id for h in out] == [
        make_doc_id("pubmed", "1"),
        make_doc_id("mdwiki", "3"),
        make_doc_id("wiki", "4"),
    ]
    assert asked == [[make_doc_id("pubmed", "1"), make_doc_id("pubmed", "2")]]  # only titles that repeat


def test_distinct_documents_sharing_a_title_are_all_kept() -> None:
    # Audit 2026-10-12 #8: separate Stack Overflow questions (or Open Library works) with one title
    # disappeared from results, totals and facets.
    from wsp_core.ids import content_hash
    from wsp_oracle.pipeline import _collapse_duplicates  # pyright: ignore[reportPrivateUsage]

    hits = [
        SearchHit(make_doc_id("so", str(i)), 1.0 - i / 10, "Python list comprehension", "", "so")
        for i in range(3)
    ]
    texts: dict[str, str] = {
        hits[0].doc_id: "question one",
        hits[1].doc_id: "question two",
        hits[2].doc_id: "",
    }
    assert _collapse_duplicates(hits, lambda ids: {i: content_hash(texts[i]) for i in ids}) == hits
    assert _collapse_duplicates(hits, None) == hits  # without hashes nothing is proven a duplicate
    empty: dict[str, str] = {h.doc_id: content_hash("") for h in hits}
    assert _collapse_duplicates(hits, lambda ids: {i: empty[i] for i in ids}) == hits  # no text, no proof


def test_language_tags_are_compared_by_their_primary_subtag() -> None:
    # Audit 2026-10-12 #14: stored tags are full BCP-47 ("en-GB"); they never matched "en".
    head = [h("es", 0.033, "es"), h("gb", 0.030, "en-GB")]
    new_head, _ = _prefer_language("water", head, [], reranked=False)
    assert new_head[0].lang == "en-GB"


def test_preferences_name_projects_not_substrings() -> None:
    # Audit 2026-10-12 #17: "osm" matched any source id containing it, a native id's text when it had
    # no "/" (its "book"), and so on.
    from typing import Any, cast

    from wsp_oracle.pipeline import OracleSearcher
    from wsp_oracle.router import Route

    route = Route(Intent.LOCATE, "map of x", "x", "x", title="x", prefer=("osm", "maps", "wikibooks"))
    fused = [
        SearchHit(make_doc_id("cosmos", "1"), 1.0, "a", "", "cosmos"),
        SearchHit(make_doc_id("enc", "Old_maps_of_Berlin"), 0.99, "b", "", "enc"),
        SearchHit(make_doc_id("osm-planet", "node/1"), 0.9, "c", "", "osm-planet"),
        SearchHit(make_doc_id("kiwix", "wikibooks_en_all/Maps"), 0.8, "d", "", "kiwix"),
    ]
    out = OracleSearcher._boost(cast(Any, None), route, fused)  # pyright: ignore[reportPrivateUsage]
    boosted = {x.doc_id for x in out if x.score != next(f.score for f in fused if f.doc_id == x.doc_id)}
    assert boosted == {make_doc_id("osm-planet", "node/1"), make_doc_id("kiwix", "wikibooks_en_all/Maps")}


def test_how_to_prefers_books_inside_bundles() -> None:
    from typing import Any, cast

    from wsp_oracle.pipeline import OracleSearcher
    from wsp_oracle.router import Router

    route = Router.from_sources(["kiwix-curated-t2", "pubmed"], {}).route("how to treat a snake bite")
    fused = [
        SearchHit(make_doc_id("pubmed", "1"), 1.0, "A Case of Snake-Bite", "", "pubmed"),
        SearchHit(
            make_doc_id("kiwix-curated-t2", "wikibooks_en_all/First_Aid/Snakebite"),
            0.8,
            "First Aid",
            "",
            "kiwix-curated-t2",
        ),
    ]
    out = OracleSearcher._boost(cast(Any, None), route, fused)  # pyright: ignore[reportPrivateUsage]
    assert out[0].source_id == "kiwix-curated-t2"  # 0.8 x 1.5 > 1.0
