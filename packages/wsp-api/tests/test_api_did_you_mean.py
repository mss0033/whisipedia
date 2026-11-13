"""Did-you-mean: one every-word count that asks for five, a correction that makes a title, and the two
stage timings (2026-11-10 trawl, search-index-11, with the changes its review required)."""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, cast

import httpx

from wsp_api import create_app
from wsp_api.schemas import SearchResults
from wsp_api.testing import local_client
from wsp_api.testing.browse import browse_services
from wsp_core.model import SearchHit


@dataclass(frozen=True)
class _Result:
    hits: tuple[SearchHit, ...]
    low_confidence: bool = False
    scope: object = None
    exhaustive: bool | None = True


@dataclass
class _Index:
    """An Oracle with ``found`` hits for any query, ``holding`` documents with every word of a
    correction, and ``titles`` in its suggest trie."""

    found: int = 8
    holding: int = 100
    titles: frozenset[str] = frozenset()
    counts: list[tuple[str, int, tuple[str, ...] | None]] = field(default_factory=list[tuple[str, int, Any]])
    hit_searches: list[str] = field(default_factory=list[str])
    named_asked: list[str] = field(default_factory=list[str])

    def _hits(self) -> tuple[SearchHit, ...]:
        return tuple(browse_services().index.search("the", limit=self.found))

    def result(self, query: str, *, sources: Sequence[str] | None = None, depth: int = 0) -> _Result:
        return _Result(self._hits())

    def search(
        self, query: str, *, limit: int = 10, offset: int = 0, sources: Sequence[str] | None = None
    ) -> Sequence[SearchHit]:
        self.hit_searches.append(query)
        return self._hits()[offset : offset + limit]

    def search_hits(
        self, query: str, *, limit: int = 10, offset: int = 0, sources: Sequence[str] | None = None
    ) -> Sequence[SearchHit]:
        self.hit_searches.append(query)
        return self._hits()[offset : offset + limit]

    def count_every_word(self, query: str, *, at_least: int, sources: Sequence[str] | None = None) -> int:
        self.counts.append((query, at_least, None if sources is None else tuple(sources)))
        return min(self.holding, at_least)

    def named(self, text: str, *, limit: int = 16) -> Sequence[tuple[str, str]]:
        self.named_asked.append(text)
        return [("wsp:w/x", text)] if text.casefold() in self.titles else []

    def suggest(self, prefix: str, *, limit: int = 10) -> Sequence[str]:
        return []


class _Speller:
    """Corrects "turring" to the commoner "turning"; knows "turing" and "tuning" as near."""

    def __init__(self, *, rivals: bool = True) -> None:
        if not rivals:
            self.rivals = None  # type: ignore[assignment]  # a corrector without the method

    def correct(self, query: str, *, only: Iterable[str] | None = None) -> str | None:
        fixed = query.replace("turring", "turning").replace("Turring", "Turning")
        return None if fixed == query else fixed

    def unknown(self, query: str) -> list[str]:
        return [w for w in query.lower().split() if w in ("turring",)]

    def rivals(self, query: str, fixed: str, limit: int = 8) -> list[str]:
        if len(query.split()) < 2:
            return []  # as the real one: a word alone has no rivals
        return [query.replace("turring", w) for w in ("tuning", "turing")]


def _ask(index: _Index, q: str, speller: object | None = None, **params: object) -> SearchResults:
    services = replace(browse_services(), index=cast(Any, index), speller=cast(Any, speller or _Speller()))
    with local_client(create_app(services)) as c:
        r: httpx.Response = c.get("/v1/search", params=cast(Any, {"q": q, **params}))
    assert r.status_code == 200, r.text
    return SearchResults.model_validate(r.json())


def test_did_you_mean_counts_every_word_once_and_asks_for_five() -> None:
    # The count was a whole lexical search of the correction, as deep as the list shown (200).
    index = _Index(found=8)
    res = _ask(index, "turring")
    assert res.did_you_mean == "turning"
    assert index.counts == [("turning", 5, None)]  # every word, five documents, one query
    assert index.hit_searches == []  # no search of the correction
    # fewer found than five: as many as were found
    few = _Index(found=2)
    assert _ask(few, "turring").did_you_mean == "turning"
    assert few.counts == [("turning", 2, None)]
    none = _Index(found=0)
    _ask(none, "turring")
    assert none.counts == [("turning", 1, None)]


def test_a_correction_too_few_documents_hold_is_not_offered() -> None:
    index = _Index(found=8, holding=4)
    assert _ask(index, "turring").did_you_mean is None
    assert index.counts == [("turning", 5, None)]


def test_a_correction_that_is_a_title_beats_a_commoner_word() -> None:
    index = _Index(titles=frozenset({"alan turing"}))
    res = _ask(index, "alan turring")
    assert res.did_you_mean == "alan turing"
    # the chosen correction first, then the rivals in the speller's order, stopping at the title
    assert index.named_asked[:3] == ["alan turning", "alan tuning", "alan turing"]
    assert index.counts == []  # a page is titled so: nothing to count
    # without the title the speller's choice stands, and is counted
    plain = _Index()
    assert _ask(plain, "alan turring").did_you_mean == "alan turning"
    assert plain.counts == [("alan turning", 5, None)]
    # a corrector that offers no rivals: its choice stands
    old = _Index(titles=frozenset({"alan turing"}))
    assert _ask(old, "alan turring", _Speller(rivals=False)).did_you_mean == "alan turning"


def test_a_correction_that_is_already_a_title_is_kept() -> None:
    index = _Index(titles=frozenset({"alan turning", "alan turing"}))
    assert _ask(index, "alan turring").did_you_mean == "alan turning"
    assert index.named_asked.count("alan turing") == 0  # the rivals are not tried
    assert index.counts == []


@dataclass
class _Answered(_Index):
    """As :class:`_Index`, and the first result is titled ``first``."""

    first: str = ""
    at: int = 0

    def _hits(self) -> tuple[SearchHit, ...]:
        hits = list(super()._hits())
        hits = (hits * (self.at + 1))[: max(len(hits), self.at + 1)]  # long enough to hold ``at``
        hits[self.at] = replace(hits[self.at], title=self.first)
        return tuple(hits)


def test_a_query_that_is_a_results_title_is_offered_nothing() -> None:
    # "Alan Turring" is a page and it is among the results: no title one letter away in its place,
    # however few results there are; the title list and the index are not asked.
    for found in (8, 2):
        index = _Answered(found=found, titles=frozenset({"alan turing"}), first="Alan Turring")
        assert _ask(index, "alan  turring").did_you_mean is None
        assert index.named_asked == []
        assert index.counts == []
    # far down the list it is no answer (a forum question titled with the slip): corrected as ever
    deep = _Answered(titles=frozenset({"alan turing"}), first="Alan Turring", at=10)
    assert _ask(deep, "alan turring").did_you_mean == "alan turing"
    # one word that is a title may still be a slip: the speller's own choice, counted or titled
    one = _Answered(titles=frozenset({"turing"}), first="Turring")
    assert _ask(one, "turring").did_you_mean == "turning"
    # and without such a result the title is offered as before
    other = _Answered(titles=frozenset({"alan turing"}), first="Alan Turing (film)")
    assert _ask(other, "alan turring").did_you_mean == "alan turing"


def test_one_word_is_never_swapped_for_a_title() -> None:
    # Every word is some page's title: only a phrase of two words or more says which word was meant.
    index = _Index(titles=frozenset({"turing"}))
    assert _ask(index, "turring").did_you_mean == "turning"


def test_under_a_kind_filter_the_correction_is_counted_in_the_kind() -> None:
    # The title list knows no source filter: a title elsewhere must not stand in for the count.
    index = _Index(titles=frozenset({"turning"}))
    kind = _ask(_Index(), "the").hits[0].kind
    res = _ask(index, "turring", kind=kind)
    assert res.did_you_mean == "turning"
    assert len(index.counts) == 1
    assert index.counts[0][:2] == ("turning", min(res.total, 5))  # as many as the kind's list found
    assert index.counts[0][2]  # the kind's sources


def test_the_two_halves_of_did_you_mean_are_timed() -> None:
    t = _ask(_Index(), "turring").timings_ms
    assert {"did_you_mean", "did_you_mean.spell", "did_you_mean.count"} <= set(t)
    assert t["did_you_mean.spell"] + t["did_you_mean.count"] <= t["did_you_mean"] + 0.2
    # nothing to correct: no count, and no count stage
    t = _ask(_Index(), "the").timings_ms
    assert "did_you_mean.spell" in t
    assert "did_you_mean.count" not in t
    # later pages never ask
    assert "did_you_mean.spell" not in _ask(_Index(found=30), "turring", offset=10).timings_ms


def test_the_vocabulary_is_opened_when_the_server_starts() -> None:
    from wsp_api.app import _warm_search  # pyright: ignore[reportPrivateUsage]

    asked: list[str] = []

    class Opening(_Speller):
        def unknown(self, query: str) -> list[str]:
            asked.append(query)
            return []

    _warm_search(replace(browse_services(), speller=cast(Any, Opening())))
    assert asked == ["the library"]
    _warm_search(replace(browse_services(), speller=None))  # no speller: nothing to open


def test_the_real_speller_and_a_real_title_list_agree_on_alan_turing(tmp_path: Path) -> None:
    # No fake between them: the vocabulary prefers "turning", the trie holds "Alan Turing".
    from wsp_index.spelling import Speller
    from wsp_index.suggest import SuggestEntry, Suggester, TitleTrie

    speller = Speller.from_counts({"alan": 5000, "turning": 50, "turing": 10, "tuning": 30})
    assert speller.correct("alan turring") == "alan turning"
    trie = Suggester(TitleTrie.build([SuggestEntry("wsp:w/en/Alan_Turing", "Alan Turing")], tmp_path))

    @dataclass
    class Titled(_Index):
        def named(self, text: str, *, limit: int = 16) -> Sequence[tuple[str, str]]:
            return trie.named(text, limit=limit)

    index = Titled()
    assert _ask(index, "alan turring", speller).did_you_mean == "alan turing"
    assert index.counts == []
    assert (
        _ask(Titled(), "Alan Turring machines", speller).did_you_mean == "Alan Turning machines"
    )  # no title
    assert _ask(Titled(), "turring", speller).did_you_mean == "turning"  # one word: the speller's choice


def test_a_title_list_that_cannot_be_read_does_not_fail_the_search() -> None:
    @dataclass
    class Broken(_Index):
        def named(self, text: str, *, limit: int = 16) -> Sequence[tuple[str, str]]:
            raise OSError("the trie is being replaced")

    index = Broken()
    assert _ask(index, "alan turring").did_you_mean == "alan turning"
    assert index.counts == [("alan turning", 5, None)]
