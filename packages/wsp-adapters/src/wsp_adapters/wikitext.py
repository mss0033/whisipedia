"""Wikitext to plain text, tuned for throughput over fidelity.

Why not ``mwparserfromhell``: it builds a full node tree in Python and needs roughly 5-20 ms per
average English Wikipedia article (``parse`` + ``strip_code``), i.e. 10-40 CPU hours for the 7M
articles of one monthly dump. This module is a handful of linear scans and pre-compiled regexes,
around 10x faster, with no dependency. The loss is deliberate and small for search and diffing:
templates (infoboxes, citations, navboxes) are dropped instead of expanded, as ``strip_code`` does,
except the inline ones that carry prose (measurements, quotations, wrappers, symbols), which render as
their text; so the text is the article prose plus headings, lists and table cells.

Outbound wiki links are collected on the same pass so the Refinery gets ``links`` for free.
"""

from __future__ import annotations

import html
import re

# Tags whose content is never prose (``math``/``chem``/``ce`` keep their TeX source as text).
_DROP_ELEMENTS = frozenset(
    [
        "ref",
        "references",
        "gallery",
        "timeline",
        "graph",
        "mapframe",
        "maplink",
        "templatedata",
        "templatestyles",
        "score",
        "imagemap",
        "syntaxhighlight",
        "source",
    ]
)
_KEEP_SOURCE = frozenset({"math", "chem", "ce"})
_ELEMENT_OPEN_RE = re.compile(
    r"<(" + "|".join(sorted(_DROP_ELEMENTS | _KEEP_SOURCE)) + r")\b[^>]*?(/?)>", re.IGNORECASE
)
_NOWIKI_RE = re.compile(r"<nowiki\s*/>|<nowiki\b[^>]*>(.*?)(?:</nowiki\s*>|\Z)", re.IGNORECASE | re.DOTALL)
_PLACEHOLDER_RE = re.compile("\x00(\\d+)\x00")
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
_TABLE_LINE_RE = re.compile(r"^\s*(?:\|[-}]|\{\|).*$", re.MULTILINE)
_CAPTION_RE = re.compile(r"^\s*\|\+\s*(?:[^|\n]*\|(?!\|))?", re.MULTILINE)  # "|+ style=x | Caption"
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
# Sister projects and non-article namespaces: shown as text, never collected as links (they would be
# dangling ids in the refine: audit 2026-10-11 M2, LOW).
_OTHER_SITES = frozenset(
    [
        "w",
        "wikipedia",
        "wikt",
        "wiktionary",
        "c",
        "commons",
        "s",
        "wikisource",
        "q",
        "wikiquote",
        "b",
        "wikibooks",
        "n",
        "wikinews",
        "v",
        "wikiversity",
        "voy",
        "wikivoyage",
        "species",
        "wikispecies",
        "d",
        "wikidata",
        "m",
        "meta",
        "mw",
        "mediawiki",
        "phab",
        "foundation",
        "wmf",
        "metawikimedia",
    ]
)
_OTHER_NAMESPACES = frozenset(
    [
        "template",
        "help",
        "wikipedia",
        "wp",
        "project",
        "portal",
        "talk",
        "user",
        "user_talk",
        "special",
        "draft",
        "module",
        "mediawiki",
        "timedtext",
        "book",
        "education_program",
        "gadget",
        "topic",
    ]
)


_INNER_TEMPLATE_RE = re.compile(r"\{\{([^{}]*)\}\}")
_RANGE_WORDS = frozenset({"-", "\u2013", "to", "and", "or", "x", "\u00d7", "by", "+/-", "±"})
_KEEP_FIRST = frozenset(
    {
        "nowrap",
        "nobr",
        "small",
        "big",
        "smaller",
        "larger",
        "sic",
        "abbr",
        "abbrlink",
        "ill",
        "visible anchor",
        "anchor",
        "mvar",
        "math",
        "var",
        "em",
        "strong",
        "underline",
        "highlight",
        "nobold",
        "noitalic",
    }
)
_QUOTE_NAMES = frozenset(
    {"quote", "blockquote", "cquote", "quotation", "quote box", "rquote", "bq", "poem quote"}
)
_SYMBOLS = {
    "nbsp": " ",
    "ndash": "\u2013",
    "mdash": "—",
    "=": "=",
    "!": "|",
    "spaced ndash": " \u2013 ",
    "snd": " \u2013 ",
    "middot": "·",
    "bull": "•",
    "pi": "π",
    "times": "\u00d7",
    "-": "\u2212",
    "minus": "\u2212",
    "dot": "·",
}
_RENDER_PASSES = 8


def _params(body: str) -> tuple[str, list[str], dict[str, str]]:
    name, *rest = body.split("|")
    positional: list[str] = []
    named: dict[str, str] = {}
    for part in rest:
        key, eq, value = part.partition("=")
        if eq and re.fullmatch(r"\s*[A-Za-z_][\w ]*\s*", key):
            named[key.strip().lower()] = value.strip()
        else:
            positional.append(part.strip())
    return name.strip().lower().replace("_", " "), positional, named


def _render_template(body: str) -> str:
    """Text of the inline templates that carry prose: measurements ({{convert}}, {{cvt}}, {{val}}),
    wrappers ({{nowrap}}, {{lang}}, ...), quotations and symbols. Anything else renders as nothing,
    as before. The 2026-10-14 field exam found boiling times, altitudes and plug gaps missing from
    Wikipedia text ("boiling should continue for 3 minutes" read "for minutes")."""
    name, pos, named = _params(body)
    if name in _SYMBOLS:
        return _SYMBOLS[name]
    if name in ("convert", "cvt") and pos:
        # {{convert|3|km}}, {{convert|1|-|2|ft}}, {{convert|5|x|10|m}}: values and the first unit.
        out = [pos[0]]
        i = 1
        while i + 1 < len(pos) and pos[i].strip() in _RANGE_WORDS:
            out += [pos[i].strip(), pos[i + 1]]
            i += 2
        if i < len(pos):
            out.append(pos[i])
        return " ".join(x for x in out if x)
    if name == "val" and pos:
        unit = named.get("u") or named.get("ul") or ""
        value = pos[0] + (f"\u2013{pos[1]}" if len(pos) > 1 and pos[1] else "")
        return f"{value} {unit}".strip()
    if name in ("frac", "sfrac") and pos:
        return (
            f"{pos[0]} {pos[1]}/{pos[2]}"
            if len(pos) >= 3
            else "/".join(pos[:2])
            if len(pos) == 2
            else f"1/{pos[0]}"
        )
    if name in ("circa", "c.", "ca."):
        return f"c. {pos[0]}" if pos else "c."
    if name == "as of" and pos:
        return "As of " + " ".join(pos[:3])
    if name in _QUOTE_NAMES:
        return named.get("text") or named.get("quote") or (pos[0] if pos else "")
    if name == "lang" and len(pos) >= 2:
        return pos[1]
    if name.startswith("lang-") and pos:
        return pos[0]
    if name in ("transl", "transliteration") and pos:
        return pos[-1]
    if name in _KEEP_FIRST and pos:
        return pos[0]
    return ""


def _render_inline_templates(text: str) -> str:
    """Render innermost templates (see :func:`_render_template`) up to :data:`_RENDER_PASSES` levels
    deep; what remains (the outer templates of unknown kinds) :func:`_strip_templates` removes."""
    if "{{" not in text:
        return text
    for _ in range(_RENDER_PASSES):
        new = _INNER_TEMPLATE_RE.sub(lambda m: _render_template(m.group(1)), text)
        if new == text:
            break
        text = new
    return text


def _strip_templates(text: str) -> str:
    """Remove nested ``{{...}}`` templates (and parser functions) in linear time.

    Pass 1 pairs every ``{{`` with its ``}}`` on a stack; pass 2 removes the outermost pairs. An
    unmatched ``{{`` (a stray brace pair, or a template cut off by the end of the page) is dropped
    by itself: it used to swallow the rest of the article (audit 2026-10-11 M2).
    """
    stack: list[int] = []
    pairs: list[tuple[int, int]] = []
    for match in _BRACES_RE.finditer(text):
        if match.group() == "{{":
            stack.append(match.start())
        elif stack:
            pairs.append((stack.pop(), match.end()))
    cuts: list[tuple[int, int]] = []
    end = -1
    for start, stop in sorted(pairs):
        if start >= end:  # outermost: not inside an earlier kept pair
            cuts.append((start, stop))
            end = stop
    cuts.extend((u, u + 2) for u in stack)  # what is left on the stack never closed
    out: list[str] = []
    pos = 0
    for start, stop in sorted(cuts):
        if start < pos:
            continue  # an unmatched "{{" inside a removed template
        out.append(text[pos:start])
        pos = stop
    out.append(text[pos:])
    return "".join(out)


def _drop_elements(text: str) -> str:
    """Remove ``<ref>...</ref>`` and similar elements in one pass (keeping TeX of ``<math>``).

    A closing tag is searched for once per element name: when ``</ref`` does not occur after some
    position it occurs after no later one, so an unclosed ``<ref>`` costs one scan, not one per
    later tag (the old lazy regex rescanned to the end for every open tag: quadratic).
    """
    lower = text.lower()
    no_close_after: dict[str, int] = {}
    out: list[str] = []
    pos = 0
    for match in _ELEMENT_OPEN_RE.finditer(text):
        if match.start() < pos:
            continue  # inside an element already removed
        name = match.group(1).lower()
        out.append(text[pos : match.start()])
        pos = match.end()
        if match.group(2):  # <ref name="x" />
            continue
        close = -1
        if no_close_after.get(name, len(text) + 1) > match.end():
            close = lower.find(f"</{name}", match.end())
            if close < 0:
                no_close_after[name] = match.end()
        if close < 0:
            continue  # unclosed: drop the tag alone, keep what follows
        gt = text.find(">", close)
        body = text[match.end() : close]
        pos = len(text) if gt < 0 else gt + 1
        if name in _KEEP_SOURCE:
            out.append(f" {body.strip()} ")
    out.append(text[pos:])
    return "".join(out)


def _render_links(text: str, links: list[str], capitalize: bool) -> str:
    def repl(match: re.Match[str]) -> str:
        inner, trail = match.group(1), match.group(2)
        target, _, label = inner.partition("|")
        target = target.strip()
        prefix = target.lstrip(":").partition(":")[0].strip().lower()
        namespaced = ":" in target.lstrip(":")
        if namespaced and prefix in _DROP_LINK_PREFIXES:
            return ""
        if _INTERWIKI_RE.match(target) and not target.startswith(":"):
            return ""  # interlanguage link
        if namespaced and (
            prefix.replace(" ", "_") in _OTHER_NAMESPACES
            or prefix in _OTHER_SITES
            or _INTERWIKI_RE.match(target)
        ):
            # [[wikt:word]], [[:fr:Page]], [[Help:Editing]]: text, not an article link
            rest = target.lstrip(":").partition(":")[2]
            return (label.rpartition("|")[2] if label else rest) + trail
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
    text = _CAPTION_RE.sub("", text)  # a caption line keeps its text
    text = _TABLE_LINE_RE.sub("", text)
    text = _CELL_ATTR_RE.sub("", _TABLE_CELL_RE.sub("", text))
    return _CELL_SEP_RE.sub(" • ", text) if "||" in text or "!!" in text else text


def wikitext_to_text(wikitext: str, *, capitalize_links: bool = True) -> tuple[str, tuple[str, ...]]:
    """Return ``(plain_text, outbound_link_targets)``.

    Link targets use MediaWiki URL form (spaces as underscores, fragment removed). With
    ``capitalize_links`` (all projects except Wiktionary) the first letter is upper-cased.
    """
    kept: list[str] = []

    def stash(match: re.Match[str]) -> str:  # <nowiki> content is literal: no links, no templates
        kept.append(match.group(1) or "")
        return f"\x00{len(kept) - 1}\x00"

    text = _NOWIKI_RE.sub(stash, wikitext.replace("\x00", ""))
    text = _COMMENT_RE.sub("", text)
    text = _drop_elements(text)
    text = _strip_templates(_render_inline_templates(text))
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
    text = _PLACEHOLDER_RE.sub(lambda m: kept[int(m.group(1))], text)
    text = html.unescape(text)
    text = "\n".join(_SPACES_RE.sub(" ", line).strip() for line in text.split("\n"))
    text = _BLANKS_RE.sub("\n", text).strip()
    return text, tuple(dict.fromkeys(links))
