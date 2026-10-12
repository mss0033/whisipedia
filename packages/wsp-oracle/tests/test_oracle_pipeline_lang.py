"""Hits in the query's language (English when the query does not say) win near-ties."""

from __future__ import annotations

from wsp_core.ids import make_doc_id
from wsp_core.model import SearchHit
from wsp_oracle.pipeline import _prefer_language  # pyright: ignore[reportPrivateUsage]


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


def test_near_identical_titles_of_one_source_collapse_to_the_best() -> None:
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
    out = _collapse_duplicates(hits)
    assert [h.doc_id for h in out] == [
        make_doc_id("pubmed", "1"),
        make_doc_id("mdwiki", "3"),
        make_doc_id("wiki", "4"),
    ]
