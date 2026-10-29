def test_attributes_of_a_cell_after_a_separator_are_not_text() -> None:
    """2026-10-25: wikipedia-text-extra kept 'rowspan="3"|' and 'style="..." |' in its table rows."""
    from wsp_adapters.wikitext import wikitext_to_text

    w = (
        '{| class="wikitable"\n|-\n! Year !! Team\n|-\n'
        '| rowspan="3" | 1999 || style="text-align:center" | Bears || colspan=2 | x\n'
        "|-\n| 2000 || align=right|Lions || [[Link|Shown]] || a = b\n|}"
    )
    text, _ = wikitext_to_text(w)
    assert text == "Year • Team\n1999 • Bears • x\n2000 • Lions • Shown • a = b"
