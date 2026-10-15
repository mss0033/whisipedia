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
