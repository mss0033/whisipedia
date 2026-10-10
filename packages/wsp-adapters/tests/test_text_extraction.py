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


def test_wikitext_unbalanced_template_drops_tail() -> None:
    assert wikitext_to_text("Before {{broken|x")[0] == "Before"
    assert wikitext_to_text("[[lower case]]", capitalize_links=False)[1] == ("lower_case",)


def test_html_to_text() -> None:
    html = (
        "<html><head><title>T</title><style>x{}</style></head><body><h1>Title</h1>"
        '<p>Hello <b>world</b><sup class="reference">[1]</sup>.</p><ul><li>a</li><li>b &amp; c</li></ul>'
        '<script>var x</script><span class="mw-editsection">[edit]</span></body></html>'
    )
    assert html_to_text(html) == "Title\nHello world.\na\nb & c"
    assert html_to_text(b"") == ""
