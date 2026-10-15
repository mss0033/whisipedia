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
