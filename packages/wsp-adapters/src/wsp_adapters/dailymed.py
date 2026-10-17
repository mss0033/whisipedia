"""``dailymed`` adapter: the FDA drug labels DailyMed publishes, one document per label.

DailyMed (National Library of Medicine) publishes every Structured Product Label (SPL) the FDA
holds: prescription prescribing information, OTC Drug Facts, vaccines and animal drugs. Federal
works in the public domain.

**Discover.** The "all drug labels" page lists the full release: each set split into parts
(``dm_spl_release_human_rx_part1.zip`` ...), each with its size and MD5 and a "Last Modified" date.
``access.sets`` picks the sets (default :data:`DEFAULT_SETS`); the version is the newest date.

**Documents.** A part is a zip of zips, one per label (the SPL XML and its images). Each label is a
document: ``native_id`` its set id (stable across the label's versions), the title its product
(``Duloxetine delayed-release capsules (duloxetine) — Remedyrepack Inc.``), the text its sections in
order with their headings. The product-data and package-label sections (codes and carton images) and
the "highlights" excerpts (a summary of sections that follow in full) are left out. A label whose
text is identical to one already read (the same label under another packager) is not repeated.
"""

from __future__ import annotations

import hashlib
import html
import io
import logging
import re
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Iterable, Iterator
from datetime import datetime
from pathlib import Path

from wsp_adapters._common import access, fetch_all, plan_missing, str_list, upstream_dir, verify_file
from wsp_core.ids import content_hash, make_doc_id
from wsp_core.model import (
    Checksum,
    DocRecord,
    FetchPlan,
    FetchResult,
    Generation,
    ReleaseRef,
    RemoteFile,
    Verdict,
)
from wsp_core.ports import Ctx

log = logging.getLogger(__name__)

PAGE_URL = "https://dailymed.nlm.nih.gov/dailymed/spl-resources-all-drug-labels.cfm"
FILES_URL = "https://dailymed-data.nlm.nih.gov/public-release-files/"
DEFAULT_SETS = ("human_rx", "human_otc", "remainder", "animal")
V3 = "{urn:hl7-org:v3}"
# Sections that are not reading matter: product data elements (codes), the carton image panel, and
# the highlights' recent-changes table.
SKIP_SECTIONS = frozenset({"48780-1", "51945-4", "43683-2"})
_BLOCKS = frozenset({"paragraph", "item", "title", "caption", "tr", "list", "table", "br"})
_SKIP = frozenset({"excerpt"})
_PART = re.compile(
    r"(dm_spl_release_(\w+?)(?:_part\d+)?\.zip)\s*\[\s*HTTPS\s*/\s*FTP\s*\]\s*Number of files:\s*[\d,]+\s*"
    r"File size:\s*[\d.]+\s*[KMGT]?B\s*MD5 checksum:\s*([0-9a-f]{32})\s*"
    r"Last Modified:\s*([A-Z][a-z]{2} \d{1,2}, \d{4})"
)
_WS = re.compile(r"\s+")


def release_files(page: str, sets: Iterable[str]) -> tuple[list[RemoteFile], str | None]:
    """The parts of the chosen sets on the "all drug labels" page, and the newest Last-Modified date."""
    text = _WS.sub(" ", html.unescape(re.sub(r"<[^>]+>", " ", page)))
    wanted = set(sets)
    files: dict[str, RemoteFile] = {}
    newest: str | None = None
    for m in _PART.finditer(text):
        name, set_name, md5, modified = m.groups()
        if set_name not in wanted or name in files:
            continue
        files[name] = RemoteFile(url=FILES_URL + name, name=name, checksums=(Checksum("md5", md5),))
        day = datetime.strptime(modified, "%b %d, %Y").strftime("%Y-%m-%d")
        newest = max(newest or day, day)
    return [files[n] for n in sorted(files, key=_part_order)], newest


def _part_order(name: str) -> tuple[str, int]:
    m = re.match(r"dm_spl_release_(\w+?)(?:_part(\d+))?\.zip$", name)
    return (m.group(1), int(m.group(2) or 0)) if m else (name, 0)


def _text(el: ET.Element) -> str:
    """Readable text of an SPL element: whitespace collapsed, a line break per block element."""
    out: list[str] = []

    def walk(node: ET.Element) -> None:
        tag = node.tag.rsplit("}", 1)[-1]
        if tag in _SKIP:
            return
        block = tag in _BLOCKS
        if block:
            out.append("\x00")
        if node.text:
            out.append(node.text)
        for child in node:
            walk(child)
            if child.tail:
                out.append(child.tail)
        if block:
            out.append("\x00")

    walk(el)
    lines = (_WS.sub(" ", part).strip() for part in "".join(out).split("\x00"))
    return "\n".join(line for line in lines if line and line != "Image")  # "Image": a figure's stand-in


def _attr(root: ET.Element, child: str, name: str) -> str:
    found = root.find(f"{V3}{child}")
    return "" if found is None else found.get(name, "")


def _name(text: str | None) -> str:
    return _WS.sub(" ", text or "").strip()


def label_record(xml: bytes, source_id: str, license_: str) -> DocRecord | None:
    """One SPL label as a document, or None when it has no set id or no text."""
    root = ET.fromstring(xml)  # noqa: S314 - NLM's published SPL files, checksum-verified
    setid_el = root.find(f"{V3}setId")
    setid = setid_el.get("root") if setid_el is not None else None
    if not setid:
        return None
    code = root.find(f"{V3}code")
    kind = (code.get("displayName") or "Drug label").capitalize() if code is not None else "Drug label"
    effective = _attr(root, "effectiveTime", "value")
    version = _attr(root, "versionNumber", "value")
    labeler = _name(root.findtext(f"{V3}author/{V3}assignedEntity/{V3}representedOrganization/{V3}name"))
    names: list[str] = []
    generics: list[str] = []
    form = ""
    for product in root.iter(f"{V3}manufacturedProduct"):
        if (n := _name(product.findtext(f"{V3}name"))) and n not in names:
            names.append(n)
        if (
            g := _name(product.findtext(f"{V3}asEntityWithGeneric/{V3}genericMedicine/{V3}name"))
        ) and g not in generics:
            generics.append(g)
        f = product.find(f"{V3}formCode")
        if not form and f is not None and f.get("displayName"):
            form = str(f.get("displayName")).lower()
    sections: list[str] = []
    body = root.find(f"{V3}component/{V3}structuredBody")
    for comp in body.findall(f"{V3}component") if body is not None else []:
        section = comp.find(f"{V3}section")
        if section is None:
            continue
        sec_code = section.find(f"{V3}code")
        if sec_code is not None and sec_code.get("code") in SKIP_SECTIONS:
            continue
        if text := _text(section):
            sections.append(text)
    if not sections:
        return None
    product = names[0] if names else (generics[0] if generics else "Drug")
    product = product.title() if product.isupper() else product
    title = product
    if generics and generics[0].casefold() != product.casefold():
        title += f" ({generics[0].lower()})"
    if form:
        title += f", {form}"
    company = labeler.title() if labeler.isupper() else labeler
    if company:
        title += f" — {company}"
    dated = f"{effective[:4]}-{effective[4:6]}-{effective[6:8]}" if len(effective) == 8 else ""
    head = f"{kind}. {product}" + (f" ({', '.join(g.lower() for g in generics)})" if generics else "")
    facts = "; ".join(
        x
        for x in (
            f"Labeler: {company}" if company else "",
            f"Effective {dated}" if dated else "",
            f"version {version}" if version else "",
        )
        if x
    )
    text = "\n\n".join([head + ("\n" + facts if facts else ""), *sections])
    aliases = tuple(dict.fromkeys(a for a in (*names, *generics) if a.casefold() != product.casefold()))
    return DocRecord(
        doc_id=make_doc_id(source_id, setid),
        source_id=source_id,
        native_id=setid,
        title=title,
        lang="en",
        license=license_,
        text=text,
        revision=version or None,
        url_native=f"https://dailymed.nlm.nih.gov/dailymed/drugInfo.cfm?setid={setid}",
        content_hash=content_hash(text),
        aliases=aliases,
        meta={k: v for k, v in (("labeler", company), ("label_type", kind), ("effective", dated)) if v},
    )


def labels(part: Path) -> Iterator[bytes]:
    """The SPL XML of every label in a release part (a zip of per-label zips), in archive order."""
    with zipfile.ZipFile(part) as outer:
        for name in outer.namelist():
            if not name.endswith(".zip"):
                continue
            try:
                with zipfile.ZipFile(io.BytesIO(outer.read(name))) as inner:
                    for member in inner.namelist():
                        if member.endswith(".xml"):
                            yield inner.read(member)
            except zipfile.BadZipFile:
                log.warning("%s: %s is not a zip", part.name, name)


class DailyMedAdapter:
    """``SourceAdapter`` for DailyMed's full SPL release."""

    id = "dailymed"

    @staticmethod
    def sets(ctx: Ctx) -> tuple[str, ...]:
        return str_list(access(ctx.source).get("sets")) or DEFAULT_SETS

    def discover(self, ctx: Ctx) -> Iterable[ReleaseRef]:
        page = str(access(ctx.source).get("page") or PAGE_URL)
        text = ctx.fetcher.get_bytes(page, max_bytes=8 * 1024 * 1024).decode("utf-8", "replace")
        files, newest = release_files(text, self.sets(ctx))
        if not files or newest is None:
            log.warning("%s: no release files found on %s", ctx.source.id, page)
            return []
        return [
            ReleaseRef(
                ctx.source.id, newest, None, tuple(files), format="spl-zip", license=ctx.source.license
            )
        ]

    def plan(self, ctx: Ctx, have: Generation | None, want: ReleaseRef) -> FetchPlan:
        return plan_missing(have, want)

    def fetch(self, ctx: Ctx, plan: FetchPlan) -> Iterable[FetchResult]:
        return fetch_all(ctx, plan)

    def verify(self, ctx: Ctx, remote: RemoteFile, local: Path) -> Verdict:
        return verify_file(remote, local)

    def documents(self, ctx: Ctx, generation_dir: Path) -> Iterator[DocRecord]:
        spec = ctx.source
        seen_ids: set[str] = set()
        seen_text: set[bytes] = set()
        parts = sorted(
            upstream_dir(generation_dir).glob("dm_spl_release_*.zip"), key=lambda p: _part_order(p.name)
        )
        for part in parts:
            for xml in labels(part):
                try:
                    doc = label_record(xml, spec.id, spec.license)
                except ET.ParseError as exc:
                    log.warning("%s: a label in %s does not parse: %s", spec.id, part.name, exc)
                    continue
                if doc is None or doc.native_id in seen_ids:
                    continue
                body = hashlib.blake2b(doc.text.split("\n\n", 1)[-1].encode(), digest_size=16).digest()
                if body in seen_text:
                    continue  # the same label under another packager
                seen_ids.add(doc.native_id)
                seen_text.add(body)
                yield doc
