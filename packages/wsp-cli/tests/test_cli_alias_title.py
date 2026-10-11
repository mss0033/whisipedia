"""Redirect titles keep their own "/" and lose only the source's language/book prefix (rank-oracle #3)."""

from __future__ import annotations

import pytest

from wsp_cli.indexer import _alias_title  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize(
    ("native", "target", "title"),
    [
        ("en/AC/DC_(album)", "en/AC/DC", "AC/DC (album)"),  # was "DC (album)"
        ("en/Turing", "en/Alan_Turing", "Turing"),
        ("zh-yue/香港", "zh-yue/香港特別行政區", "香港"),
        ("wikipedia_de_all/Berlin_(Stadt)", "wikipedia_de_all/Berlin", "Berlin (Stadt)"),
        ("AC/DC_(band)", "AC/DC", "AC/DC (band)"),  # a source without prefixes: "AC" is no prefix
        ("A/Turing", "A/Alan_Turing", "Turing"),  # an old ZIM namespace
        ("wikipedia_en_all/A/Turing", "wikipedia_en_all/A/Alan_Turing", "Turing"),  # book + namespace
        ("Turing", "Alan_Turing", "Turing"),
        ("en/Foo", "de/Foo", "en/Foo"),  # different prefixes: not a shared prefix
        ("en/Foo", "", "en/Foo"),
    ],
)
def test_alias_title(native: str, target: str, title: str) -> None:
    assert _alias_title(native, target) == title
