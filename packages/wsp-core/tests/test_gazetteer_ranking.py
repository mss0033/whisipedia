"""Which place a name means (2026-10-18 live checks on the 6.7 million place gazetteer): the capital by
its English name, the town next door when searching from a map, and the town before the casino in it."""

from __future__ import annotations

from pathlib import Path

import pytest

from wsp_core.gazetteer import Gazetteer, Place, place_rank, write_gazetteer


def _place(osm: str, name: str, kind: str, lat: float, lon: float, pop: int | None, wiki: str | None,
           *aliases: str) -> Place:  # fmt: skip
    return Place(osm, name, kind, lat, lon, place_rank(kind, pop, wiki is not None), pop, None, wiki, aliases)


PLACES = [
    _place("n/1", "Lisboa", "place=city", 38.72, -9.14, 552_700, "en:Lisbon", "Lisbon", "Lissabon"),
    _place("n/2", "Lisbon", "place=town", 41.92, -91.39, 2_233, "en:Lisbon, Iowa"),
    _place("n/3", "Wien", "place=city", 48.21, 16.37, 1_900_000, "de:Wien", "Vienna", "Vienne"),
    _place("n/4", "Vienna", "place=town", 38.90, -77.27, 16_000, "en:Vienna, Virginia"),
    _place("n/5", "Roma", "place=city", 41.89, 12.48, 2_800_000, "it:Roma", "Rome"),
    _place("n/6", "România", "place=country", 45.9, 24.9, 19_000_000, "ro:România", "Romênia", "Romania"),
    _place("n/7", "Unterseen", "place=village", 46.685, 7.848, 5_646, "de:Unterseen"),
    _place("n/8", "Unterwalden", "place=region", 46.86, 8.33, None, "de:Unterwalden"),
    _place("n/9", "Unterschleißheim", "place=town", 48.27, 11.57, 26_363, "de:Unterschleißheim"),
    _place("n/10", "Interlaken", "place=town", 46.6863, 7.8632, 5_700, "de:Interlaken"),
    _place("n/11", "Casino Interlaken", "amenity=casino", 46.6883, 7.8580, None, None),
]


@pytest.fixture(scope="module")
def gz(tmp_path_factory: pytest.TempPathFactory) -> Gazetteer:
    db: Path = tmp_path_factory.mktemp("gz") / "places.sqlite"
    write_gazetteer(db, PLACES)
    return Gazetteer(db)


def first(gz: Gazetteer, q: str, near: tuple[float, float] | None = None) -> str:
    return gz.search(q, limit=3, near=near)[0].name


def test_an_important_place_by_another_of_its_names(gz: Gazetteer) -> None:
    assert first(gz, "lisbon") == "Lisboa"  # its article is en:Lisbon; Lisbon, Iowa is spelled so
    assert first(gz, "vienna") == "Wien"  # an alias of a city of millions, before Vienna, Virginia
    assert first(gz, "rome") == "Roma"  # "rome" as a word, before România where it starts a word


def test_searching_from_a_map_puts_the_matches_around_it_first(gz: Gazetteer) -> None:
    interlaken = (46.6875, 7.86)
    assert first(gz, "unter") == "Unterwalden"  # the most important "unter*" anywhere
    assert first(gz, "unter", near=interlaken) == "Unterseen"  # the village next door
    assert first(gz, "unter", near=(48.2, 11.6)) == "Unterschleißheim"  # seen from Munich


def test_nearby_names_the_town_before_the_casino_in_it(gz: Gazetteer) -> None:
    near = gz.nearby(46.6875, 7.86, radius_m=5000)
    assert [p.name for p in near][:2] == ["Interlaken", "Casino Interlaken"]
    assert near[1].distance_m is not None
    assert near[0].distance_m is not None
    assert near[1].distance_m < near[0].distance_m  # the casino is nearer: importance decides
