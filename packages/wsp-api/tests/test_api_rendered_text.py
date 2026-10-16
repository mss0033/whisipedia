from wsp_api.pages import rendered_text


def test_formulas_become_tex_and_paragraphs_stay() -> None:
    html = (
        "<html><head><title>T</title><style>p{}</style></head><body>"
        '<p>A space <math alttext="{\\displaystyle (X,d)}"><semantics><mi>X</mi>'
        "<annotation>{\\displaystyle (X,d)}</annotation></semantics></math> is complete.</p>"
        "<p>At altitudes greater than 2,000 m (6,562 ft), boiling should continue for 3 minutes.</p>"
        "<ul><li>one</li><li>two</li></ul></body></html>"
    )
    assert rendered_text(html).split("\n") == [
        "A space (X,d) is complete.",
        "At altitudes greater than 2,000 m (6,562 ft), boiling should continue for 3 minutes.",
        "one",
        "two",
    ]


def test_an_edition_year_or_date_dates_a_document() -> None:
    from datetime import UTC, datetime

    from wsp_api.convert import edition_time

    assert edition_time("1928") == datetime(1928, 1, 1, tzinfo=UTC)
    assert edition_time("1975-06") == datetime(1975, 6, 1, tzinfo=UTC)
    assert edition_time("1992-04-01") == datetime(1992, 4, 1, tzinfo=UTC)
    assert edition_time("2026-08") == datetime(2026, 8, 1, tzinfo=UTC)
    assert edition_time("xkcd.com_en_all_2026-08") is None
    assert edition_time(None) is None
    assert edition_time("1992-13") is None


def test_navigation_blocks_are_left_out_and_quoted_markup_stays_in_its_tag() -> None:
    html = (
        '<body><table class="sidebar nomobile"><tr><td><table class="sidebar-subgroup"><tr><td>Branches'
        "</td></tr></table></td></tr><tr><td>Laws</td></tr></table>"
        "<p>Gibbs free energy is a potential."
        '<sup id="cite_ref-X_<span_title=&quot;L&quot;><i>y</i></span>[[C]]"'
        ' rel="dc:references">[1]</sup></p>'
        '<div role="navigation" class="navbox"><div class="navbox">Inner</div>Outer</div><p>After.</p></body>'
    )
    assert rendered_text(html).split("\n") == ["Gibbs free energy is a potential. [1]", "After."]
