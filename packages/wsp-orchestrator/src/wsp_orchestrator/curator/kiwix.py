"""Kiwix OPDS watcher (PLAN §5.3): new titles in watched catalog categories become candidates.

Updated releases of titles the registry already selects are picked up by the regular ``discover``
job; this bot looks for *titles* nobody selects yet. Topic subsets of a project the registry already
holds (``wikipedia_en_medicine`` next to ``wikipedia_en_all``) are skipped as redundant.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode

from wsp_core.model import SourceSpec, SourceStatus
from wsp_orchestrator.bots import BotContext, BotResult
from wsp_orchestrator.policy import Candidate, CandidateKind

CATALOG_URL = "https://library.kiwix.org/catalog/v2/entries"
_ATOM = "{http://www.w3.org/2005/Atom}"
_DC = "{http://purl.org/dc/terms/}"
_ACQUISITION = "http://opds-spec.org/acquisition/open-access"
_FLAVOURS = ("maxi", "", "nopic", "mini", "nodet")
_SLUG_BAD = re.compile(r"[^a-z0-9]+")

# Wikimedia projects and Stack Exchange publish under CC BY-SA; everything else is checked by a human.
_CC_BY_SA_PREFIXES = (
    "wikipedia_",
    "wiktionary_",
    "wikibooks_",
    "wikivoyage_",
    "wikiquote_",
    "wikiversity_",
    "wikisource_",
    "wikinews_",
)
CATEGORY_DOMAINS: Mapping[str, tuple[str, ...]] = {
    "wikipedia": ("encyclopedia",),
    "wiktionary": ("dictionary",),
    "wikibooks": ("textbooks",),
    "wikiversity": ("education",),
    "wikivoyage": ("geography",),
    "wikiquote": ("primary-sources",),
    "wikisource": ("primary-sources",),
    "wikinews": ("news",),
    "stack_exchange": ("qa",),
    "ted": ("education",),
    "gutenberg": ("books",),
}
DEFAULT_SCORES: Mapping[str, int] = {
    "authority": 3,
    "density": 3,
    "uniqueness": 3,
    "license": 3,
    "bulk": 5,
    "health": 4,
    "longevity": 4,
    "cost": 3,
}


@dataclass(frozen=True, slots=True)
class KiwixTitle:
    name: str
    title: str
    category: str
    languages: tuple[str, ...]
    flavour: str
    issued: datetime | None
    length: int | None
    url: str


def parse_catalog(data: bytes) -> tuple[list[KiwixTitle], int]:
    """One OPDS v2 acquisition page: ``(entries, totalResults)``."""
    root = ET.fromstring(data)  # noqa: S314 - HTTPS catalog; expat does not resolve external entities

    def text(elem: ET.Element, tag: str) -> str:
        found = elem.find(tag)
        return (found.text or "").strip() if found is not None else ""

    out: list[KiwixTitle] = []
    for e in root.iter(f"{_ATOM}entry"):
        link = next((ln for ln in e.iter(f"{_ATOM}link") if ln.get("rel") == _ACQUISITION), None)
        if link is None or not link.get("href"):
            continue
        issued_text = text(e, f"{_DC}issued") or text(e, f"{_ATOM}updated")
        try:
            issued = datetime.fromisoformat(issued_text.replace("Z", "+00:00")) if issued_text else None
        except ValueError:
            issued = None
        length = link.get("length") or ""
        out.append(
            KiwixTitle(
                name=text(e, f"{_ATOM}name"),
                title=text(e, f"{_ATOM}title"),
                category=text(e, f"{_ATOM}category"),
                languages=tuple(x for x in text(e, f"{_ATOM}language").split(",") if x),
                flavour=text(e, f"{_ATOM}flavour"),
                issued=issued,
                length=int(length) if length.isdigit() else None,
                url=link.get("href", "").removesuffix(".meta4"),
            )
        )
    total = text(root, f"{_ATOM}totalResults")
    return out, int(total) if total.isdigit() else len(out)


def _selector(spec: SourceSpec) -> Mapping[str, Any]:
    access = spec.raw.get("access")
    if not isinstance(access, Mapping):
        return {}
    sel = access.get("selector")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    return sel if isinstance(sel, Mapping) else {}  # pyright: ignore[reportUnknownVariableType]


def covered_by_registry(sources: Sequence[SourceSpec]) -> tuple[set[str], set[str], set[str]]:
    """``(book names, categories, project families)`` already selected by ``kiwix-opds`` sources."""
    names: set[str] = set()
    categories: set[str] = set()
    for spec in sources:
        if spec.adapter != "kiwix-opds" or spec.status is SourceStatus.EXCLUDED:
            continue
        sel = _selector(spec)
        for key in ("name", "names"):
            value = sel.get(key)
            if isinstance(value, str):
                names.add(value)
            elif isinstance(value, list):
                names.update(str(v) for v in value)  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
        if isinstance(sel.get("category"), str):
            categories.add(str(sel["category"]))
    return names, categories, {_family(n) for n in names}


def _family(name: str) -> str:
    """``wikipedia_en_all`` -> ``wikipedia_en``: the project and language."""
    return "_".join(name.split("_")[:2])


def license_for(name: str, category: str) -> str:
    if name.startswith(_CC_BY_SA_PREFIXES) or category == "stack_exchange":
        return "CC-BY-SA-4.0"
    return "LicenseRef-per-item"


def source_id_for(name: str) -> str:
    return "kiwix-" + (_SLUG_BAD.sub("-", name.casefold()).strip("-") or "title")


def _rank(t: KiwixTitle) -> tuple[int, float]:
    flavour = _FLAVOURS.index(t.flavour) if t.flavour in _FLAVOURS else len(_FLAVOURS)
    return flavour, -(t.issued.timestamp() if t.issued else 0.0)


class KiwixWatcher:
    """Watches ``categories`` (OPDS ``category`` values) in ``languages`` (ISO 639-3)."""

    name = "kiwix-opds"
    cadence: str | None = None
    network = True

    def __init__(
        self,
        *,
        categories: Sequence[str],
        languages: Sequence[str] = ("eng",),
        catalog_url: str = CATALOG_URL,
        page_size: int = 500,
        scores: Mapping[str, Mapping[str, int]] | None = None,
    ) -> None:
        self.categories = tuple(categories)
        self.languages = tuple(languages)
        self.catalog_url = catalog_url
        self.page_size = page_size
        self.scores = dict(scores or {})

    def _fetch(self, ctx: BotContext, category: str, lang: str) -> list[KiwixTitle]:
        found: list[KiwixTitle] = []
        start = 0
        while True:
            query = {"category": category, "lang": lang, "count": self.page_size, "start": start}
            page, total = parse_catalog(ctx.fetcher.get_bytes(f"{self.catalog_url}?{urlencode(query)}"))
            found.extend(page)
            start += len(page)
            if not page or start >= total:
                return found

    def run(self, ctx: BotContext) -> BotResult:
        names, categories, families = covered_by_registry(ctx.sources)
        best: dict[str, KiwixTitle] = {}
        seen = 0
        for category in self.categories:
            if category in categories:
                continue  # a whole category is already selected
            for lang in self.languages:
                for t in self._fetch(ctx, category, lang):
                    seen += 1
                    if not t.name or (t.name in best and _rank(best[t.name]) <= _rank(t)):
                        continue
                    best[t.name] = t
        new = [t for n, t in sorted(best.items()) if n not in names and _family(n) not in families]
        today = ctx.now.astimezone(UTC).date().isoformat()
        candidates = [self._candidate(t, today) for t in new]
        notes: dict[str, object] = {
            "titles_seen": seen,
            "books": len(best),
            "new": [t.name for t in new][:50],
            "redundant_subsets": sum(1 for n in best if n not in names and _family(n) in families),
        }
        return BotResult(candidates=candidates, notes=notes)

    def _candidate(self, t: KiwixTitle, today: str) -> Candidate:
        license_ = license_for(t.name, t.category)
        domain = CATEGORY_DOMAINS.get(t.category, ("education",))
        scores = dict(self.scores.get(t.category, DEFAULT_SCORES))
        if license_ == "CC-BY-SA-4.0":
            scores["license"] = max(scores.get("license", 0), 5)
        selector: dict[str, object] = {"name": t.name}
        if t.flavour:
            selector["flavour"] = t.flavour
        entry: dict[str, object] = {
            "id": source_id_for(t.name),
            "name": f"{t.title or t.name} (Kiwix ZIM)",
            "publisher": "Kiwix",
            "domain": list(domain),
            "tier": "T2",
            "status": "active",
            "license": license_,
            "legal_basis": "open-license",
            "access": {"adapter": "kiwix-opds", "selector": selector},
            "update": {"strategy": "snapshot-retention"},
            "size": {"bytes": t.length or 0, "measured": t.length is not None, "as_of": today},
            "retention": {"gfs": {"last": 2, "monthly": 6, "yearly": "forever"}},
            "scores": scores,
            "notes": f"Found by the kiwix-opds bot on {today} in category {t.category!r}.",
        }
        return Candidate(
            key=f"kiwix:{t.name}",
            name=t.title or t.name,
            kind=CandidateKind.KIWIX_TITLE,
            license=license_,
            domain=domain,
            found_by=self.name,
            scores=scores,
            size_bytes=t.length,
            category=t.category,
            url=t.url,
            registry_entry=entry,
        )
