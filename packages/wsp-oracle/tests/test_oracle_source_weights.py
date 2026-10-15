from typing import Any, cast

from wsp_core.model import SearchHit
from wsp_oracle.pipeline import OracleSearcher, weight_exempt


def _hit(doc_id: str, score: float) -> SearchHit:
    return SearchHit(doc_id=cast(Any, doc_id), score=score, title=doc_id, snippet="")


def test_catalog_and_abstracts_are_weighed_down_unless_asked_for() -> None:
    s = cast(Any, OracleSearcher.__new__(OracleSearcher))
    s.source_weights = {"open-library": 0.3, "pubmed": 0.7}
    hits = [
        _hit("wsp:open-library/works/OL1W", 1.0),
        _hit("wsp:pubmed/1", 0.9),
        _hit("wsp:usda-home-canning/g4", 0.8),
    ]
    got = [h.doc_id for h in OracleSearcher._weigh(s, "pressure canning green beans", hits)]  # pyright: ignore[reportPrivateUsage]
    assert got == ["wsp:usda-home-canning/g4", "wsp:pubmed/1", "wsp:open-library/works/OL1W"]
    book = [h.doc_id for h in OracleSearcher._weigh(s, "a book on pressure canning", hits)]  # pyright: ignore[reportPrivateUsage]
    assert book[0] == "wsp:open-library/works/OL1W"


def test_exemptions_follow_the_question() -> None:
    assert weight_exempt("novels by Jane Austen") == {"open-library"}
    assert weight_exempt("clinical trials of metformin") == {"pubmed"}
    assert weight_exempt("how to purify water") == frozenset()


def test_a_catalog_card_is_not_pinned_by_its_title_alone() -> None:
    s = cast(Any, OracleSearcher.__new__(OracleSearcher))
    s.source_weights = {"open-library": 0.3}
    card = SearchHit(
        doc_id=cast(Any, "wsp:open-library/works/OL1W"),
        score=1.0,
        title="Capital of Australia",
        source_id="open-library",
    )
    article = SearchHit(
        doc_id=cast(Any, "wsp:wikipedia-top10/en/Canberra"),
        score=0.5,
        title="Canberra",
        source_id="wikipedia-top10",
    )
    assert not OracleSearcher._pinnable(s, "capital of australia", card)  # pyright: ignore[reportPrivateUsage]
    assert OracleSearcher._pinnable(s, "capital of australia", article)  # pyright: ignore[reportPrivateUsage]
    assert OracleSearcher._pinnable(s, "the book capital of australia", card)  # pyright: ignore[reportPrivateUsage]
