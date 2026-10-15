from wsp_adapters.wikitext import wikitext_to_text


def text(src: str) -> str:
    return wikitext_to_text(src)[0]


def test_measurements_keep_their_numbers() -> None:
    assert text(
        "At altitudes greater than {{convert|2000|m|ft}}, boiling should continue for 3 minutes."
    ) == ("At altitudes greater than 2000 m, boiling should continue for 3 minutes.")
    assert text("A gap of {{convert|0.025|-|0.030|in|mm}}.") == "A gap of 0.025 - 0.030 in."
    assert text("Mass {{val|1.989|e=30|u=kg}}") == "Mass 1.989 kg"
    assert text("{{cvt|5|x|10|m}} slab") == "5 x 10 m slab"


def test_wrappers_quotes_and_symbols_render_as_text() -> None:
    assert text("{{nowrap|[[Isaac Newton]] (1643{{ndash}}1727)}}") == "Isaac Newton (1643\u20131727)"
    assert text("{{quote|text=Congress shall make no law}}") == "Congress shall make no law"
    assert text("{{lang|fr|la belle époque}} and {{lang-de|Zeitgeist}}") == "la belle époque and Zeitgeist"
    assert text("{{frac|1|1|2}} cups, {{circa|1500}}") == "1 1/2 cups, c. 1500"


def test_infoboxes_citations_and_navboxes_still_drop() -> None:
    src = "{{Infobox element|number={{val|51}}|name=Antimony}}Antimony is a metal.{{cite web|url=x|title=y}}"
    assert text(src) == "Antimony is a metal."
