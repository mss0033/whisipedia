"""Readers for collections published as structured data rather than as documents (a ``reader`` in the
registry ``access``, like :mod:`wsp_adapters.doccollection.tei`): each entry of a catalogue becomes a
document a reader can find and read.

* ``mitre`` (MITRE's defender knowledge bases, mitre-attack-cwe-capec):
  ATT&CK STIX bundles (``<domain>-attack/<domain>-attack.json`` of attack-stix-data): each technique,
  tactic, mitigation, group, software, campaign and data source a document, keyed by its ATT&CK id
  (``attack/T1055.011``), with its tactics, platforms, detection and the mitigations that answer it;
  CWE (``cwec_v<version>.xml``): each weakness (``cwe/CWE-79``) with its consequences, mitigations,
  detection methods and examples; CAPEC (``capec_latest.xml``): each attack pattern (``capec/CAPEC-66``)
  with its prerequisites, consequences, mitigations and the weaknesses it relates to. Deprecated and
  revoked entries are left out.
* ``oscal-catalog`` (NIST OSCAL content, ``NIST_SP-800-53_rev<4|5>_catalog.json``): each control and
  control enhancement (``800-53r5/ac-2.1``) with its statement (parameters shown as the
  "[Assignment: ...]" the publication prints), its discussion and its related controls.
* ``medlineplus-topics`` (MedlinePlus health topics XML, ``mplus_topics_<date>.xml``): each topic
  (``topic/6308``), English or Spanish, with its other names, summary, groups and related topics.
* ``standard-ebooks`` (a Standard Ebooks book repository, read whole): the book as one document
  (``book``), its chapters in the reading order of ``src/epub/content.opf``, each under its heading
  ("Chapter I"); the title page, imprint, colophon and licence page left out.

The 2026-10-18 owner request listed them as sources to bring in; each was a registered candidate whose
adapter (``git-mirror``, ``http-files``) was never written.
"""

from __future__ import annotations

import io
import json
import re
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Iterator, Mapping, Sequence
from typing import Any, cast

from wsp_adapters.doccollection.tei import TeiDoc
from wsp_adapters.html_text import html_to_text  # MedlinePlus summaries (HTML in the XML)

_WS = re.compile(r"[ \t]+")
_CITATION = re.compile(r"\(Citation: [^)]*\)")
_LINK = re.compile(r"\[([^\]]+)\]\(https?://[^)]+\)")
Json = Mapping[str, Any]


def _clean(text: str) -> str:
    """ATT&CK descriptions are Markdown with ``(Citation: ...)`` markers: plain prose for the reader."""
    text = _LINK.sub(r"\1", _CITATION.sub("", text or ""))
    text = text.replace("<code>", "").replace("</code>", "")
    lines = [_WS.sub(" ", line).strip() for line in text.splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _section(title: str, body: str) -> str:
    body = body.strip()
    return f"{title}\n{body}" if body else ""


def _join(*parts: str) -> str:
    return "\n\n".join(p for p in parts if p)


# --- ATT&CK (STIX 2.1) ---------------------------------------------------------------------------------

_ATTACK_KINDS = {
    "attack-pattern": "technique",
    "x-mitre-tactic": "tactic",
    "course-of-action": "mitigation",
    "intrusion-set": "group",
    "malware": "software",
    "tool": "software",
    "campaign": "campaign",
    "x-mitre-data-source": "data source",
}


def _attack_ref(obj: Json) -> tuple[str, str]:
    """The ATT&CK id (T1055.011, TA0001, M1040 ...) and its page."""
    for ref in cast(Sequence[Json], obj.get("external_references") or ()):
        if ref.get("source_name") in ("mitre-attack", "mitre-mobile-attack", "mitre-ics-attack"):
            return str(ref.get("external_id") or ""), str(ref.get("url") or "")
    return "", ""


def attack_bundle(data: bytes, stem: str) -> Iterator[TeiDoc]:
    domain = stem.rsplit("/", 1)[-1]  # enterprise, mobile, ics
    objects = cast(Sequence[Json], json.loads(data).get("objects") or ())
    live = {
        str(o["id"]): o
        for o in objects
        if "id" in o and not o.get("revoked") and not o.get("x_mitre_deprecated")
    }
    names = {i: str(o.get("name") or "") for i, o in live.items()}
    ids = {i: _attack_ref(o)[0] for i, o in live.items()}
    mitigated_by: dict[str, list[str]] = {}  # technique -> mitigations
    mitigates: dict[str, list[str]] = {}  # mitigation -> techniques
    used_by: dict[str, list[str]] = {}  # technique -> groups, software, campaigns
    uses: dict[str, list[str]] = {}  # group, software, campaign -> techniques and software
    for rel in objects:
        if rel.get("type") != "relationship" or rel.get("revoked") or rel.get("x_mitre_deprecated"):
            continue
        src, dst, kind = str(rel.get("source_ref")), str(rel.get("target_ref")), rel.get("relationship_type")
        if src not in live or dst not in live:
            continue
        src_label, dst_label = f"{ids[src]} {names[src]}".strip(), f"{ids[dst]} {names[dst]}".strip()
        if kind == "mitigates":
            mitigated_by.setdefault(dst, []).append(src_label)
            mitigates.setdefault(src, []).append(dst_label)
        elif kind == "uses":
            used_by.setdefault(dst, []).append(src_label)
            uses.setdefault(src, []).append(dst_label)
    for oid, obj in live.items():
        kind = _ATTACK_KINDS.get(str(obj.get("type")))
        if kind is None:
            continue
        aid, url = _attack_ref(obj)
        if not aid:
            continue
        name = names[oid]
        facts: list[str] = []
        if obj.get("kill_chain_phases"):
            tactics = ", ".join(
                str(p.get("phase_name", "")).replace("-", " ").title()
                for p in cast(Sequence[Json], obj["kill_chain_phases"])
            )
            facts.append(f"Tactics: {tactics}")
        for field, label in (("x_mitre_platforms", "Platforms"), ("aliases", "Also known as")):
            if obj.get(field):
                facts.append(f"{label}: {', '.join(map(str, obj[field]))}")
        text = _join(
            f"{name} ({aid}, ATT&CK {domain} {kind})",
            "\n".join(facts),
            _clean(str(obj.get("description") or "")),
            _section("Detection", _clean(str(obj.get("x_mitre_detection") or ""))),
            _section("Mitigations", "\n".join(sorted(set(mitigated_by.get(oid, []))))),
            _section("Techniques it addresses", "\n".join(sorted(set(mitigates.get(oid, []))))),
            _section("Used by", "\n".join(sorted(set(used_by.get(oid, []))))),
            _section("Uses", "\n".join(sorted(set(uses.get(oid, []))))),
        )
        yield TeiDoc(
            key=f"attack/{aid}",
            title=f"{aid}: {name} (ATT&CK {kind})",
            text=text,
            date=str(obj.get("modified") or "")[:10],
            url=url,
        )


# --- CWE and CAPEC (XML) -------------------------------------------------------------------------------


def _local(tag: object) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _words(el: ET.Element | None) -> str:
    """An element's text, paragraphs kept (its XHTML ``p``, ``li``, ``div`` children are blocks)."""
    if el is None:
        return ""
    out: list[str] = []

    def walk(e: ET.Element) -> None:
        if e.text and e.text.strip():
            out.append(e.text.strip())
        for child in e:
            block = _local(child.tag) in ("p", "li", "div", "br", "Description", "Text")
            if block:
                out.append("\n")
            walk(child)
            if block:
                out.append("\n")
            if child.tail and child.tail.strip():
                out.append(child.tail.strip())

    walk(el)
    text = " ".join(out)
    return re.sub(r" *\n[ \n]*", "\n", _WS.sub(" ", text)).strip()


def _child(el: ET.Element, name: str) -> ET.Element | None:
    return next((c for c in el if _local(c.tag) == name), None)


def _children(el: ET.Element | None, name: str) -> list[ET.Element]:
    return [] if el is None else [c for c in el if _local(c.tag) == name]


def _bulleted(items: Sequence[str]) -> str:
    return "\n".join(f"- {i}" for i in items if i)


def _consequences(el: ET.Element) -> str:
    rows: list[str] = []
    for c in _children(_child(el, "Common_Consequences"), "Consequence"):
        scope = ", ".join(_words(s) for s in _children(c, "Scope"))
        impact = ", ".join(_words(i) for i in _children(c, "Impact"))
        note = _words(_child(c, "Note"))
        rows.append(f"{scope}: {impact}" + (f". {note}" if note else ""))
    return _bulleted(rows)


def _mitigations(el: ET.Element) -> str:
    rows: list[str] = []
    holder = _child(el, "Potential_Mitigations")
    if holder is None:  # an element with no children is falsy: never `or` two of them
        holder = _child(el, "Mitigations")
    for m in _children(holder, "Mitigation"):
        phase = ", ".join(_words(p) for p in _children(m, "Phase"))
        body = _words(_child(m, "Description")) or _words(m)
        rows.append(f"{phase}: {body}" if phase else body)
    return _bulleted(rows)


def cwe_catalog(data: bytes, stem: str) -> Iterator[TeiDoc]:
    root = ET.fromstring(data)  # noqa: S314 - MITRE's own CWE catalogue
    for w in root.iter():
        if _local(w.tag) != "Weakness" or w.get("Status") == "Deprecated":
            continue
        wid, name = w.get("ID", ""), w.get("Name", "")
        examples = [
            _words(e) for e in _children(_child(w, "Demonstrative_Examples"), "Demonstrative_Example")
        ]
        observed = [
            f"{_words(_child(o, 'Reference'))}: {_words(_child(o, 'Description'))}"
            for o in _children(_child(w, "Observed_Examples"), "Observed_Example")
        ]
        detection = [
            f"{_words(_child(d, 'Method'))}: {_words(_child(d, 'Description'))}"
            for d in _children(_child(w, "Detection_Methods"), "Detection_Method")
        ]
        related = [
            f"{r.get('Nature')} CWE-{r.get('CWE_ID')}"
            for r in _children(_child(w, "Related_Weaknesses"), "Related_Weakness")
        ]
        text = _join(
            f"CWE-{wid}: {name} ({w.get('Abstraction', '')} weakness)",
            _words(_child(w, "Description")),
            _words(_child(w, "Extended_Description")),
            _section("Background", _words(_child(w, "Background_Details"))),
            _section("Consequences", _consequences(w)),
            _section("Detection", _bulleted(detection)),
            _section("Mitigations", _mitigations(w)),
            _section("Examples", "\n\n".join(e for e in examples if e)),
            _section("Observed examples", _bulleted(observed)),
            _section("Related weaknesses", ", ".join(dict.fromkeys(related))),
        )
        yield TeiDoc(
            key=f"cwe/CWE-{wid}",
            title=f"CWE-{wid}: {name}",
            text=text,
            url=f"https://cwe.mitre.org/data/definitions/{wid}.html",
        )


def capec_catalog(data: bytes, stem: str) -> Iterator[TeiDoc]:
    root = ET.fromstring(data)  # noqa: S314 - MITRE's own CAPEC catalogue
    for ap in root.iter():
        if _local(ap.tag) != "Attack_Pattern" or ap.get("Status") in ("Deprecated", "Obsolete"):
            continue
        cid, name = ap.get("ID", ""), ap.get("Name", "")
        weaknesses = [
            f"CWE-{r.get('CWE_ID')}" for r in _children(_child(ap, "Related_Weaknesses"), "Related_Weakness")
        ]
        text = _join(
            f"CAPEC-{cid}: {name} ({ap.get('Abstraction', '')} attack pattern)",
            _words(_child(ap, "Description")),
            "\n".join(
                f"{label}: {_words(_child(ap, tag))}"
                for tag, label in (("Likelihood_Of_Attack", "Likelihood"), ("Typical_Severity", "Severity"))
                if _child(ap, tag) is not None
            ),
            _section(
                "Prerequisites",
                _bulleted([_words(p) for p in _children(_child(ap, "Prerequisites"), "Prerequisite")]),
            ),
            _section("Consequences", _consequences(ap)),
            _section(
                "Mitigations",
                _bulleted([_words(m) for m in _children(_child(ap, "Mitigations"), "Mitigation")]),
            ),
            _section(
                "Examples",
                "\n\n".join(_words(e) for e in _children(_child(ap, "Example_Instances"), "Example")),
            ),
            _section("Related weaknesses", ", ".join(weaknesses)),
        )
        yield TeiDoc(
            key=f"capec/CAPEC-{cid}",
            title=f"CAPEC-{cid}: {name}",
            text=text,
            url=f"https://capec.mitre.org/data/definitions/{cid}.html",
        )


def mitre(data: bytes, stem: str) -> Iterator[TeiDoc]:
    if stem.startswith("attack/"):
        yield from attack_bundle(data, stem)
    elif stem == "cwe":
        yield from cwe_catalog(data, stem)
    elif stem == "capec":
        yield from capec_catalog(data, stem)


def _mitre_member(name: str) -> str | None:
    base = name.rsplit("/", 1)[-1]
    if m := re.fullmatch(r"(enterprise|mobile|ics)-attack\.json", base):
        return f"attack/{m.group(1)}"
    if re.fullmatch(r"cwec_v[\d.]+\.xml", base):
        return "cwe"
    if re.fullmatch(r"capec_(?:latest|v[\d.]+)\.xml", base):
        return "capec"
    return None


# --- OSCAL catalogs (NIST SP 800-53) -------------------------------------------------------------------


def _param_text(p: Json) -> str:
    if p.get("select"):
        sel = cast(Json, p["select"])
        how = "one or more" if sel.get("how-many") == "one-or-more" else "one"
        return f"[Selection ({how}): {'; '.join(map(str, sel.get('choice') or ()))}]"
    label = str(p.get("label") or "")
    return f"[Assignment: {label}]" if label else "[Assignment]"


def _parts(parts: Sequence[Json], params: Mapping[str, str], depth: int = 0) -> list[str]:
    out: list[str] = []
    for part in parts:
        label = next(
            (
                str(pr.get("value"))
                for pr in cast(Sequence[Json], part.get("props") or ())
                if pr.get("name") == "label"
            ),
            "",
        )
        prose = re.sub(
            r"\{\{\s*insert:\s*param,\s*([\w.-]+)\s*\}\}",
            lambda m: params.get(m.group(1), "[Assignment]"),
            str(part.get("prose") or ""),
        )
        line = f"{'  ' * depth}{label} {prose}".strip()
        if line:
            out.append(line)
        out += _parts(cast(Sequence[Json], part.get("parts") or ()), params, depth + 1)
    return out


def oscal_catalog(data: bytes, stem: str) -> Iterator[TeiDoc]:
    catalog = cast(Json, json.loads(data).get("catalog") or {})
    rev = stem  # 800-53r5
    pub = "NIST SP 800-53 Rev. " + rev.rsplit("r", 1)[-1]

    def controls(group: Json, family: str) -> Iterator[Json]:
        for c in cast(Sequence[Json], group.get("controls") or ()):
            yield {**c, "_family": family}
            yield from controls(c, family)

    for group in cast(Sequence[Json], catalog.get("groups") or ()):
        family = str(group.get("title") or "")
        for c in controls(group, family):
            props: dict[str, str] = {}
            for p in cast(Sequence[Json], c.get("props") or ()):
                # Two labels: "AC-2(1)" as the publication prints it, and SP 800-53A's "AC-02(01)" (class).
                if not (p.get("name") == "label" and p.get("class")):
                    props.setdefault(str(p.get("name")), str(p.get("value")))
            label = props.get("label") or str(c.get("id", "")).upper()
            params = {str(p.get("id")): _param_text(p) for p in cast(Sequence[Json], c.get("params") or ())}
            parts = cast(Sequence[Json], c.get("parts") or ())
            statement = [p for p in parts if p.get("name") == "statement"]
            guidance = [p for p in parts if p.get("name") == "guidance"]
            related = [
                str(link.get("href", "")).lstrip("#").upper()
                for link in cast(Sequence[Json], c.get("links") or ())
                if link.get("rel") == "related"
            ]
            withdrawn = props.get("status") == "withdrawn"
            moved = [
                str(link.get("href", "")).lstrip("#").upper()
                for link in cast(Sequence[Json], c.get("links") or ())
                if link.get("rel") in ("incorporated-into", "moved-to")
            ]
            text = _join(
                f"{label} {c.get('title', '')} ({family}, {pub})",
                f"Withdrawn{': incorporated into ' + ', '.join(moved) if moved else ''}."
                if withdrawn
                else "",
                _section("Control", "\n".join(_parts(statement, params))),
                _section("Discussion", "\n".join(_parts(guidance, params))),
                _section("Related controls", ", ".join(related)),
            )
            yield TeiDoc(
                key=f"{rev}/{c.get('id')}",
                title=f"{label} {c.get('title', '')} ({pub})",
                text=text,
                url=f"https://csrc.nist.gov/pubs/sp/800/53/r{rev.rsplit('r', 1)[-1]}/upd1/final",
            )


def _oscal_member(name: str) -> str | None:
    m = re.search(r"(?:^|/)NIST_SP-800-53_rev([45])_catalog\.json$", name)
    return f"800-53r{m.group(1)}" if m else None


# --- MedlinePlus health topics -------------------------------------------------------------------------


def medlineplus_topics(data: bytes, stem: str) -> Iterator[TeiDoc]:
    root = ET.fromstring(data)  # noqa: S314 - the National Library of Medicine's own topics file
    date = ""
    if m := re.match(r"(\d\d)/(\d\d)/(\d{4})", root.get("date-generated", "")):
        date = f"{m.group(3)}-{m.group(1)}-{m.group(2)}"
    for t in root.iter("health-topic"):
        tid, title = t.get("id", ""), t.get("title", "")
        lang = "es" if t.get("language") == "Spanish" else "en"
        also = [a.text or "" for a in t.findall("also-called")]
        groups = [g.text or "" for g in t.findall("group")]
        related = [r.text or "" for r in t.findall("related-topic")]
        sites = [s.get("title", "") for s in t.findall("site")][:40]
        summary = html_to_text(t.findtext("full-summary") or "")
        text = _join(
            title,
            ("También llamado: " if lang == "es" else "Also called: ") + ", ".join(also) if also else "",
            summary,
            _section("Temas" if lang == "es" else "Groups", ", ".join(groups)),
            _section("Temas relacionados" if lang == "es" else "Related topics", ", ".join(related)),
            _section("Recursos" if lang == "es" else "Resources", _bulleted(sites)),
        )
        yield TeiDoc(key=f"topic/{tid}", title=title, text=text, date=date, url=t.get("url", ""), lang=lang)


def _medlineplus_member(name: str) -> str | None:
    return "topics" if re.search(r"mplus_topics_\d{4}-\d{2}-\d{2}\.xml$", name) else None


# --- Standard Ebooks (a book repository, read whole) ---------------------------------------------------

_OPF = "{http://www.idpf.org/2007/opf}"
_DCNS = "{http://purl.org/dc/elements/1.1/}"
_XHTML = "{http://www.w3.org/1999/xhtml}"
_EPUB_TYPE = "{http://www.idpf.org/2007/ops}type"
SE_SKIP = frozenset({"titlepage", "imprint", "colophon", "uncopyright", "halftitlepage", "toc", "loi"})
_ROMAN_OR_NUMBER = re.compile(r"^(?:[IVXLCDM]+|\d+)\.?$")


_BLOCK_TAGS = frozenset(
    {"p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "dt", "dd", "td", "th", "figcaption", "pre"}
)


def _xhtml_blocks(body: ET.Element) -> list[str]:
    """The text of each block (paragraph, heading, list item) in order: a block within a block (a
    paragraph in a list item) is read with the outer one. Independent of the whitespace between tags."""
    out: list[str] = []

    def walk(el: ET.Element) -> None:
        for child in el:
            if _local(child.tag) in _BLOCK_TAGS:
                text = " ".join("".join(child.itertext()).split())
                if text:
                    out.append(text)
            else:
                walk(child)

    walk(body)
    return out


def se_book(archive: zipfile.ZipFile) -> Iterator[TeiDoc]:
    names = archive.namelist()
    opf_name = next((n for n in names if n.endswith("src/epub/content.opf")), None)
    if opf_name is None:
        return
    base = opf_name.rsplit("/", 1)[0]
    opf = ET.fromstring(archive.read(opf_name))  # noqa: S314 - Standard Ebooks' own EPUB source
    meta = opf.find(f"{_OPF}metadata")
    if meta is None:
        return

    def dc(tag: str) -> list[str]:
        return [
            " ".join((e.text or "").split()) for e in meta.iter(f"{_DCNS}{tag}") if (e.text or "").strip()
        ]

    title = (dc("title") or [""])[0]
    authors = dc("creator")
    url = next((u for i in dc("identifier") if (u := i.removeprefix("url:")).startswith("https://")), "")
    hrefs = {
        i.get("id", ""): i.get("href", "")
        for i in opf.iter(f"{_OPF}item")
        if i.get("media-type") == "application/xhtml+xml"
    }
    parts: list[str] = []
    for ref in opf.iter(f"{_OPF}itemref"):
        idref = ref.get("idref", "")
        href = hrefs.get(idref, "")
        if not href or idref.removesuffix(".xhtml") in SE_SKIP or f"{base}/{href}" not in names:
            continue
        page = ET.fromstring(archive.read(f"{base}/{href}"))  # noqa: S314 - Standard Ebooks' own XHTML
        head = " ".join((page.findtext(f"{_XHTML}head/{_XHTML}title") or "").split())
        body = page.find(f"{_XHTML}body")
        kinds = " ".join(e.get(_EPUB_TYPE, "") for e in (body.iter() if body is not None else ()))
        blocks = _xhtml_blocks(body) if body is not None else []
        if not blocks:
            continue
        if "chapter" in kinds and _ROMAN_OR_NUMBER.match(head):
            head = f"Chapter {head.rstrip('.')}"
        # The heading the page prints ("I") gives way to the one a reader can find ("Chapter I").
        if head and blocks[0].rstrip(".") in head:
            blocks = blocks[1:]
        body_text = "\n\n".join(blocks)  # a paragraph a block, a blank line between (not verse)
        parts.append(f"{head}\n\n{body_text}" if head else body_text)
    if not title or not parts:
        return
    by = f"by {', '.join(authors)}" if authors else ""
    text = "\n\n".join(x for x in (title, by, *parts) if x)
    lang = (dc("language") or ["en"])[0][:2].lower()
    yield TeiDoc(key="book", title=title, text=text, date=(dc("date") or [""])[0][:10], url=url, lang=lang)


# --- A repository's Markdown documentation (owner request 2026-10-18: mesh networks, weather models) ----

_MD_SKIP = re.compile(r"(?:^|/)(?:node_modules|i18n|\.github|vendor|third_party|blog|changelog)(?:/|$)", re.I)
_FRONT_MATTER = re.compile(r"\A---\n(.*?)\n---\n", re.S)
_JSX_LINE = re.compile(r"^\s*(?:import |export |<\/?[A-Z][\w.]*[^>]*>\s*$)")
_JSX_TAG = re.compile(r"<\/?[A-Z][\w.]*(?:\s[^<>]*)?\/?>")
_ADMONITION = re.compile(r"^\s*:::\s*(\w*)(.*)$")
_COMMENT = re.compile(r"<!--.*?-->|\{/\*.*?\*/\}", re.S)
MD_MAX_PAGES = 5000


def markdown_docs(archive: zipfile.ZipFile) -> Iterator[TeiDoc]:
    """Every Markdown page of a repository's documentation, one document each: Meshtastic's site
    (Docusaurus ``.mdx`` under ``docs/``), a weather model's ``README.md`` and ``docs/``. Pages under
    a ``docs`` folder when the repository has one, else every page; translations, blogs and vendored
    code are left out. The title is the front matter's ``title``, else the first heading; JSX
    components, imports and comments are dropped, admonitions (``:::note``) become plain lines."""
    names = [n for n in archive.namelist() if n.lower().endswith((".md", ".mdx")) and not n.endswith("/")]
    names = [n for n in names if not _MD_SKIP.search(n)]
    in_docs = [n for n in names if re.search(r"(?:^|/)docs?/", n, re.I)]
    readmes = [n for n in names if n.count("/") <= 1 and n.rsplit("/", 1)[-1].lower() == "readme.md"]
    chosen = list(dict.fromkeys([*readmes, *in_docs])) if in_docs else names
    for name in sorted(chosen)[:MD_MAX_PAGES]:
        raw = archive.read(name).decode("utf-8", errors="replace").replace("\r\n", "\n")
        title = ""
        if m := _FRONT_MATTER.match(raw):
            fm, raw = m.group(1), raw[m.end() :]
            if t := re.search(r"^title:\s*['\"]?(.+?)['\"]?\s*$", fm, re.M):
                title = t.group(1).strip()
        lines: list[str] = []
        fenced = False
        for line in _COMMENT.sub("", raw).split("\n"):
            if line.lstrip().startswith("```"):
                fenced = not fenced
                lines.append(line)
                continue
            if fenced:
                lines.append(line)
                continue
            if _JSX_LINE.match(line):
                continue
            if am := _ADMONITION.match(line):
                label = (am.group(1) or "").capitalize()
                lines.append(f"{label}:{am.group(2)}" if label else "")
                continue
            lines.append(_JSX_TAG.sub("", line))
        text = "\n".join(lines).strip()
        if not title:
            head = re.search(r"^#{1,2}\s+(.+)$", text, re.M)
            title = head.group(1).strip() if head else name.rsplit("/", 1)[-1].rsplit(".", 1)[0]
        if len(text) < 40:
            continue
        key = name.split("/", 1)[1] if "/" in name else name  # without the snapshot's top folder
        yield TeiDoc(key=key.rsplit(".", 1)[0], title=title, text=text)


OCW_PDF_PAGES = 400  # a long reader is still read whole; a scanned book of 1,000 pages is cut
OCW_TEXT_CHARS = 2_000_000


def _ocw_json(archive: zipfile.ZipFile, name: str) -> dict[str, Any]:
    try:
        data: object = json.loads(archive.read(name))
    except (KeyError, ValueError):
        return {}
    return cast(dict[str, Any], data) if isinstance(data, dict) else {}


def _pdf_bytes_text(data: bytes, max_pages: int) -> str:
    import pypdfium2  # imported on use: only courses with PDFs need it

    try:
        pdf = pypdfium2.PdfDocument(data)
    except pypdfium2.PdfiumError:
        return ""
    try:
        out: list[str] = []
        for i in range(min(len(pdf), max_pages)):
            try:
                page = pdf[i]
                out.append(page.get_textpage().get_text_range())
            except pypdfium2.PdfiumError:
                out.append(f"[Page {i + 1} of this PDF could not be read.]")
        return "\n\n".join(out)
    finally:
        pdf.close()


def ocw_course(archive: zipfile.ZipFile) -> Iterator[TeiDoc]:
    """An MIT OpenCourseWare course as it is offered for download (the course site, offline): the
    course's description, each page (syllabus, readings, lecture notes …) and each PDF it publishes
    (lecture notes, problem sets, transcripts), one document each, titled from the course's own
    ``data.json`` files. Videos and captions are left out (the videos are not in the zip)."""
    names = set(archive.namelist())
    course = _ocw_json(archive, "data.json")
    title = " ".join(str(course.get("course_title") or "").split())
    number = str(course.get("primary_course_number") or "")
    term = " ".join(p for p in (str(course.get("term") or ""), str(course.get("year") or "")) if p)
    label = " ".join(p for p in (number, title) if p) + (f" ({term})" if term else "")
    if not label:
        return
    url = f"https://ocw.mit.edu/{course.get('site_url_path', '')}".rstrip("/")
    about = html_to_text(str(course.get("course_description_html") or course.get("course_description") or ""))
    if about:
        yield TeiDoc(key="course", title=label, text=about, url=url)
    # static_resources/<32 hex>_<name> or static_resources/<name>
    by_base = {
        re.sub(r"^[0-9a-f]{32}_", "", n.rsplit("/", 1)[-1]).lower(): n
        for n in names
        if n.startswith("static_resources/")
    }
    for name in sorted(n for n in names if n.endswith("/data.json") and n.count("/") >= 2):
        folder = name.rsplit("/", 1)[0]
        data = _ocw_json(archive, name)
        page_title = " ".join(str(data.get("title") or "").split())
        if not page_title or str(data.get("draft")) == "True":
            continue
        text = html_to_text(str(data.get("content") or ""))
        file = str(data.get("file") or "")
        if file.lower().endswith(".pdf"):
            member = by_base.get(re.sub(r"^[0-9a-f]{32}_", "", file.rsplit("/", 1)[-1]).lower())
            if member is not None:
                body = _pdf_bytes_text(archive.read(member), OCW_PDF_PAGES)
                text = f"{text}\n\n{body}".strip() if text else body
        elif str(data.get("resourcetype")) == "Video" or "captions" in folder or file:
            continue  # a video's page, its caption file, an image: nothing to read here
        text = text[:OCW_TEXT_CHARS].strip()
        if len(text) < 40:
            continue
        yield TeiDoc(key=folder, title=f"{label}: {page_title}", text=text, url=f"{url}/{folder}/")


DOCX_MAX_BYTES = 128 * 1024 * 1024  # a Word file larger than this inside the zip is left out
_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def zipped_docx(archive: zipfile.ZipFile) -> Iterator[TeiDoc]:
    """Each Word (.docx) document in a zip, as its paragraphs: 3GPP publishes every specification as a
    zip holding its .docx. The title is the cover's line naming the specification ("3GPP TS 38.300
    V18.11.0 (2026-09)") and its title lines, up to the release."""
    for info in archive.infolist():
        if not info.filename.lower().endswith(".docx") or info.file_size > DOCX_MAX_BYTES:
            continue
        try:
            with zipfile.ZipFile(io.BytesIO(archive.read(info))) as docx:
                body = docx.getinfo("word/document.xml")
                if body.compress_size and body.file_size / body.compress_size > 200:
                    continue  # a zip bomb, not a document
                root = ET.fromstring(docx.read(body))  # noqa: S314 - expat rejects external entities
        except (zipfile.BadZipFile, KeyError, ET.ParseError):
            continue
        paragraphs = ["".join(t.text or "" for t in p.iter(f"{_W}t")).strip() for p in root.iter(f"{_W}p")]
        paragraphs = [p for p in paragraphs if p]
        text = "\n\n".join(paragraphs)
        if len(text) < 40:
            continue
        name = info.filename.rsplit("/", 1)[-1].removesuffix(".docx")
        spec = next((i for i, p in enumerate(paragraphs[:40]) if re.match(r"3GPP T[SR] \d", p)), None)
        title = name
        if spec is not None:
            # The cover: "3GPP TS 38.300 V18.11.0 (2026-09)", "Technical Specification", "3rd Generation
            # Partnership Project;", "Technical Specification Group …;", then the title lines up to
            # "(Release 18)".
            words: list[str] = []
            for p in paragraphs[spec + 1 : spec + 15]:
                if len(p) > 200:
                    break
                if not re.match(
                    r"(?:Technical (?:Specification|Report)$|3rd Generation|Technical Specification Group)", p
                ):
                    words.append(p)
                if p.startswith("(Release"):
                    break
            title = f"{paragraphs[spec]}: {' '.join(words)}" if words else paragraphs[spec]
        yield TeiDoc(key=name, title=title, text=text)


ARCHIVE_READERS = {
    "standard-ebooks": se_book,
    "markdown-docs": markdown_docs,
    "ocw-course": ocw_course,
    "zipped-docx": zipped_docx,
}

READERS = {
    "mitre": (_mitre_member, mitre),
    "oscal-catalog": (_oscal_member, oscal_catalog),
    "medlineplus-topics": (_medlineplus_member, medlineplus_topics),
}
