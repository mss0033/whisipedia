from __future__ import annotations

from wsp_adapters.html_text import html_to_text
from wsp_adapters.wikitext import wikitext_to_text

WIKITEXT = """{{Short description|English mathematician}}
{{Infobox person
| name = Alan Turing
| birth_date = {{birth date|1912|6|23}}
}}
'''Alan Mathison Turing''' was an English [[mathematician]], [[computer science|computer scientist]] \
and [[logic]]ian.<ref name="x">{{cite web|url=http://x}}</ref> He was <!-- hidden --> \
influential.<ref>Foo</ref>

[[File:Alan Turing Aged 16.jpg|thumb|Turing aged 16, with a [[bicycle]]]]
== Early life ==
Born in [[Maida Vale]], London.<br/>See [https://example.org the site] &amp; more.
* item [[two]]
{| class="wikitable"
|-
! Year !! Event
|-
| style="x" | 1936 || ''On Computable Numbers''
|}
[[Category:1912 births]]
[[de:Alan Turing]]
__NOTOC__
"""


def test_wikitext_to_text() -> None:
    text, links = wikitext_to_text(WIKITEXT)
    assert text.split("\n") == [
        "Alan Mathison Turing was an English mathematician, computer scientist and logician."
        " He was influential.",
        "Early life",
        "Born in Maida Vale, London.",
        "See the site & more.",
        "item two",
        "Year • Event",
        "1936 • On Computable Numbers",
    ]
    assert links == ("Mathematician", "Computer_science", "Logic", "Bicycle", "Maida_Vale", "Two")


def test_wikitext_unbalanced_template_keeps_the_rest_of_the_page() -> None:
    """Audit 2026-10-11 M2: a stray ``{{`` used to drop everything after it."""
    assert wikitext_to_text("Before {{broken|x")[0] == "Before broken|x"
    text, _ = wikitext_to_text("Intro {{ stray.\n\nA long article {{cite|x}} goes on.\n\nEnd.")
    assert text == "Intro stray.\nA long article goes on.\nEnd."
    assert wikitext_to_text("a {{x {{y}} }} b }} c")[0] == "a b }} c"
    assert wikitext_to_text("[[lower case]]", capitalize_links=False)[1] == ("lower_case",)


def test_wikitext_nowiki_interwiki_math_and_captions() -> None:
    text, links = wikitext_to_text(
        "Use <nowiki>[[not a link]] {{not a template}}</nowiki> here.<nowiki/>\n"
        "See [[wikt:serendipity|serendipity]], [[:fr:Paris]], [[Help:Editing|help]] and [[Real page]].\n"
        "Area <math>\\pi r^2</math> and <ce>H2O</ce>.\n"
        '{|\n|+ style="x" | Table caption\n|-\n| a || b\n|}'
    )
    assert text.split("\n") == [
        "Use [[not a link]] {{not a template}} here.",
        "See serendipity, Paris, help and Real page.",
        "Area \\pi r^2 and H2O .",
        "Table caption",
        "a • b",
    ]
    assert links == ("Real_page",)


def test_unclosed_refs_are_linear() -> None:
    import time

    page = "Text <ref>unclosed " * 20_000 + "end"
    start = time.perf_counter()
    text, _ = wikitext_to_text(page)
    assert time.perf_counter() - start < 1.0  # was quadratic: every <ref> rescanned to the end
    assert text.endswith("end")


def test_html_to_text() -> None:
    html = (
        "<html><head><title>T</title><style>x{}</style></head><body><h1>Title</h1>"
        '<p>Hello <b>world</b><sup class="reference">[1]</sup>.</p><ul><li>a</li><li>b &amp; c</li></ul>'
        '<script>var x</script><span class="mw-editsection">[edit]</span></body></html>'
    )
    assert html_to_text(html) == "Title\nHello world.\na\nb & c"
    assert html_to_text(b"") == ""


def test_html_to_text_drops_boilerplate_and_keeps_math_source() -> None:
    """Audit 2026-10-11 M1: navigation, category lists, hatnotes and the ZIM footer are not prose."""
    html = (
        '<body><h1>Water</h1><div class="hatnote">For other uses, see Water (disambiguation).</div>'
        '<table class="sidebar"><tr><td>Part of a series</td></tr></table>'
        '<p>Water is <math alttext="{\\displaystyle H_{2}O}"><mi>H</mi><mn>2</mn><mi>O</mi>'
        "<annotation>H_2O</annotation></math>.</p>"
        '<p>Bare <math><mi>x</mi><annotation encoding="application/x-tex">x</annotation></math>.</p>'
        '<div class="navbox">Chemistry topics</div>'
        '<div id="catlinks"><ul><li>Chemical compounds</li></ul>'
        '<div class="mw-hidden-catlinks">Articles with short description</div></div>'
        '<div class="zim-footer">This page is issued from Wikipedia.</div></body>'
    )
    assert html_to_text(html) == "Water\nWater is {\\displaystyle H_{2}O} .\nBare x."


def test_planetmath_link_targets_and_concept_clusters_are_dropped() -> None:
    from wsp_adapters.html_text import html_to_text

    page = (
        '<p>Every contraction has a unique <span class="ltx_text ltx_font_typewriter">'
        'http://planetmath.org/node/2777</span><a class="nnexus_concepts" href="javascript:void(0)">'
        'fixed point</a><sup><a class="nnexus_concept" href="http://planetmath.org/a"><img alt="P"></a>'
        '<a class="nnexus_concept" href="http://planetmath.org/b"><img alt="P"></a></sup>. '
        'Run <span class="ltx_text ltx_font_typewriter">ls -l</span>.</p>'
    )
    assert html_to_text(page) == "Every contraction has a unique fixed point. Run ls -l."
