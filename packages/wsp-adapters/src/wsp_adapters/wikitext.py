"""Wikitext to plain text, tuned for throughput over fidelity.

Why not ``mwparserfromhell``: it builds a full node tree in Python and needs roughly 5-20 ms per
average English Wikipedia article (``parse`` + ``strip_code``), i.e. 10-40 CPU hours for the 7M
articles of one monthly dump. This module is a handful of linear scans and pre-compiled regexes,
around 10x faster, with no dependency. The loss is deliberate and small for search and diffing:
templates (infoboxes, citations, navboxes) are dropped instead of expanded, exactly as
``strip_code`` does, so the text is the article prose plus headings, lists and table cells.

Outbound wiki links are collected on the same pass so the Refinery gets ``links`` for free.
"""

from __future__ import annotations

import html
import re

# Tags whose content is never prose.
_DROP_ELEMENTS_RE = re.compile(
    r"<(ref|references|gallery|timeline|graph|mapframe|maplink|templatedata|templatestyles|score|imagemap|"
    r"syntaxhighlight|source|math|chem|ce)\b[^>]*?(?:/>|>.*?</\1\s*>)",
    re.IGNORECASE | re.DOTALL,
)
_COMMENT_RE = re.compile(r"<!--.*?(?:-->|\Z)", re.DOTALL)
_BR_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)
_TAG_RE = re.compile(r"</?[a-zA-Z][a-zA-Z0-9]*\b[^>]*?/?>")
_BRACES_RE = re.compile(r"\{\{|\}\}")
_LINK_RE = re.compile(r"\[\[([^\[\]]*)\]\]([a-z]*)")
_EXT_LINK_RE = re.compile(r"\[(?:https?:|ftp:)?//[^\s\]]*(?:\s+([^\]]*))?\]")
_BOLD_ITALIC_RE = re.compile(r"'{2,5}")
_HEADING_RE = re.compile(r"^(={1,6})\s*(.*?)\s*\1\s*$", re.MULTILINE)
_LIST_PREFIX_RE = re.compile(r"^[*#:;]+\s*", re.MULTILINE)
_MAGIC_RE = re.compile(r"__[A-Z]+__")
_TABLE_LINE_RE = re.compile(r"^\s*(?:\|[-+}]|\{\|).*$", re.MULTILINE)
_TABLE_CELL_RE = re.compile(r"^\s*[|!]\s?", re.MULTILINE)
_CELL_SEP_RE = re.compile(r"\s*(?:\|\||!!)\s*")
# Atomic group + possessive repeat: the plain ``(...)+`` backtracks exponentially on a long attribute-like
# line with no closing ``|`` (one English Wikipedia page stalled a refine for over 10 minutes).
_CELL_ATTR_RE = re.compile(r"^(?>[a-z-]+\s*=\s*(?:\"[^\"]*\"|'[^']*'|[^\s|]+)\s*)++\|(?!\|)", re.MULTILINE)
_SPACES_RE = re.compile("[ \t\u00a0]+")
_BLANKS_RE = re.compile(r"\n{2,}")

# Namespaces whose links are not prose (files, categories) in English and as canonical names.
_DROP_LINK_PREFIXES = frozenset(
    {"file", "image", "media", "category", "datei", "fichier", "bild", "kategorie", "catégorie"}
)
_INTERWIKI_RE = re.compile(r"^:?[a-z]{2,3}(?:-[a-z]+)?:")


def _strip_templates(text: str) -> str:
    """Remove nested ``{{...}}`` templates (and parser functions) in one linear scan."""
    out: list[str] = []
    depth = 0
    pos = 0
    for match in _BRACES_RE.finditer(text):
        if match.group() == "{{":
            if depth == 0:
                out.append(text[pos : match.start()])
            depth += 1
        elif depth > 0:
            depth -= 1
            if depth == 0:
                pos = match.end()
    if depth == 0:
        out.append(text[pos:])
    return "".join(out)


def _render_links(text: str, links: list[str], capitalize: bool) -> str:
    def repl(match: re.Match[str]) -> str:
        inner, trail = match.group(1), match.group(2)
        target, _, label = inner.partition("|")
        target = target.strip()
        prefix = target.lstrip(":").partition(":")[0].strip().lower()
        if ":" in target.lstrip(":") and prefix in _DROP_LINK_PREFIXES:
            return ""
        if _INTERWIKI_RE.match(target) and not target.startswith(":"):
            return ""  # interlanguage link
        page = target.partition("#")[0].strip()
        if page:
            page = page.replace(" ", "_")
            if capitalize:
                page = page[:1].upper() + page[1:]
            links.append(page)
        shown = label.rpartition("|")[2] if label else target.lstrip(":")
        return shown + trail

    # Innermost links first: captions of [[File:...]] contain links. Three passes cover real nesting.
    for _ in range(3):
        text, count = _LINK_RE.subn(repl, text)
        if not count:
            break
    return text


def _render_tables(text: str) -> str:
    text = _TABLE_LINE_RE.sub("", text)
    text = _CELL_ATTR_RE.sub("", _TABLE_CELL_RE.sub("", text))
    return _CELL_SEP_RE.sub(" • ", text) if "||" in text or "!!" in text else text


def wikitext_to_text(wikitext: str, *, capitalize_links: bool = True) -> tuple[str, tuple[str, ...]]:
    """Return ``(plain_text, outbound_link_targets)``.

    Link targets use MediaWiki URL form (spaces as underscores, fragment removed). With
    ``capitalize_links`` (all projects except Wiktionary) the first letter is upper-cased.
    """
    text = _COMMENT_RE.sub("", wikitext)
    text = _DROP_ELEMENTS_RE.sub("", text)
    text = _strip_templates(text)
    links: list[str] = []
    text = _render_links(text, links, capitalize_links)
    text = _EXT_LINK_RE.sub(lambda m: m.group(1) or "", text)
    text = _BR_RE.sub("\n", text)
    text = _TAG_RE.sub("", text)
    text = _render_tables(text)
    text = _HEADING_RE.sub(r"\2", text)
    text = _LIST_PREFIX_RE.sub("", text)
    text = _BOLD_ITALIC_RE.sub("", text)
    text = _MAGIC_RE.sub("", text)
    text = html.unescape(text)
    text = "\n".join(_SPACES_RE.sub(" ", line).strip() for line in text.split("\n"))
    text = _BLANKS_RE.sub("\n", text).strip()
    return text, tuple(dict.fromkeys(links))
