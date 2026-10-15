"""Tables of numbers are not embedded: they sit near every query vector."""

from __future__ import annotations

from wsp_oracle.embedbuild import is_prose


def test_prose_and_tables() -> None:
    assert is_prose("Process snap beans in a pressure canner at 11 pounds for 20 minutes.")
    assert not is_prose("45 12.3 44 58.1 0.2 13 46 11.9 45 01.7 0.2 14 47 11.4 45 05.2 0.3")
    assert not is_prose("   ")
    assert is_prose("Table 1. Hot pack, pints 20 min, quarts 25 min at 0-2,000 ft.")
