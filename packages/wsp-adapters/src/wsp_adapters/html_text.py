"""HTML to plain text for ZIM articles.

Uses ``selectolax`` (the Lexbor HTML5 parser, written in C). It parses a typical Wikipedia article
in well under a millisecond, is several times faster than ``lxml.html`` text extraction and an
order of magnitude faster than BeautifulSoup, and is a single wheel with no transitive
dependencies. Block structure is kept by injecting a newline before every block-level tag before
parsing, which is cheaper than walking the tree in Python.
"""

from __future__ import annotations

import re

from selectolax.lexbor import LexborHTMLParser

_BLOCK_TAGS = (
    "address|article|aside|blockquote|br|caption|dd|details|div|dl|dt|figcaption|figure|footer|"
    "h[1-6]|header|hr|li|main|nav|ol|p|pre|section|summary|table|tbody|td|th|thead|tr|ul"
)
_BLOCK_RE = re.compile(rf"<(/?)({_BLOCK_TAGS})\b", re.IGNORECASE)

# Elements that never carry article prose: code, chrome, citation markers and reference lists.
_DROP_SELECTORS = (
    "script, style, noscript, template, svg, math annotation, link, meta, "
    "sup.reference, .mw-editsection, .mw-references-wrap, ol.references, .reflist, "
    ".navbox, .noprint, .mw-jump-link, #mw-navigation"
)

_SPACES_RE = re.compile("[ \t\f\v\u00a0\u200b]+")


def html_to_text(html: str | bytes) -> str:
    """Return readable plain text with one paragraph per line."""
    if isinstance(html, bytes):
        html = html.decode("utf-8", errors="replace")
    tree = LexborHTMLParser(_BLOCK_RE.sub(r"\n<\1\2", html))
    for node in tree.css(_DROP_SELECTORS):
        node.decompose()
    root = tree.body or tree.root
    if root is None:
        return ""
    lines = (_SPACES_RE.sub(" ", line).strip() for line in root.text(separator="").split("\n"))
    return "\n".join(line for line in lines if line)
