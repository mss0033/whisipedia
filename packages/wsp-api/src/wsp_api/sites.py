"""Per-source "site" facts derived from the registry and the vault: kind, health, related sites."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from wsp_api.schemas import DocKind, SourceHealth
from wsp_core.model import Generation, GenerationStatus, SourceSpec, SourceStatus

# Registry domains (registry/sources.yaml) to result kinds. Unknown domains are "other".
_DOMAIN_KIND: dict[str, DocKind] = {
    "encyclopedia": DocKind.ENCYCLOPEDIA,
    "philosophy": DocKind.ENCYCLOPEDIA,
    "books": DocKind.BOOKS,
    "textbooks": DocKind.BOOKS,
    "primary-sources": DocKind.BOOKS,
    "science": DocKind.PAPERS,
    "standards": DocKind.PAPERS,
    "law": DocKind.LAW,
    "geography": DocKind.MAPS,
    "maps": DocKind.MAPS,
    "qa": DocKind.QA,
    "dictionary": DocKind.DICTIONARY,
    "knowledge-graph": DocKind.DATA,
    "catalog": DocKind.DATA,
    "medicine": DocKind.MEDICINE,
    "news": DocKind.NEWS,
    # The Screening Room (ADR pending): film, lectures and recordings.
    "video": DocKind.VIDEO,
    "film": DocKind.VIDEO,
    "lectures": DocKind.VIDEO,
    "audio": DocKind.AUDIO,
    "audiobooks": DocKind.AUDIO,
    "radio": DocKind.AUDIO,
    "music": DocKind.AUDIO,
}

_DOMAIN_NAMES = {"qa": "Q&A", "knowledge-graph": "Knowledge Graph", "howto": "How-To"}


def kind_of(spec: SourceSpec) -> DocKind:
    for domain in spec.domain:
        if domain in _DOMAIN_KIND:
            return _DOMAIN_KIND[domain]
    return DocKind.OTHER


def domain_name(domain: str) -> str:
    return _DOMAIN_NAMES.get(domain) or domain.replace("-", " ").title()


def raw_str(spec: SourceSpec, key: str) -> str | None:
    value = spec.raw.get(key)
    return value.strip() or None if isinstance(value, str) else None


def related(spec: SourceSpec, specs: Sequence[SourceSpec], *, limit: int = 5) -> list[str]:
    """Other sources sharing a domain, most shared domains first (deterministic)."""
    mine = set(spec.domain)
    scored = [(len(mine & set(o.domain)), o.id) for o in specs if o.id != spec.id]
    return [sid for n, sid in sorted(scored, key=lambda x: (-x[0], x[1])) if n > 0][:limit]


def cadence(iso: str | None) -> timedelta | None:
    """Coarse ISO 8601 duration (PnY, PnM, PnW, PnD) for staleness checks."""
    if not iso or not iso.startswith("P") or len(iso) < 3 or not iso[1:-1].isdigit():
        return None
    days = {"Y": 365, "M": 31, "W": 7, "D": 1}.get(iso[-1])
    return None if days is None else timedelta(days=int(iso[1:-1]) * days)


@dataclass(frozen=True, slots=True)
class Freshness:
    age_seconds: float | None
    stale: bool
    health: SourceHealth


def freshness(spec: SourceSpec, current: Generation | None, now: datetime) -> Freshness:
    sealed = None if current is None else current.sealed_at
    age = None if sealed is None else (now - sealed).total_seconds()
    limit = cadence(spec.cadence)
    stale = (current is None and spec.status is SourceStatus.ACTIVE) or (
        age is not None and limit is not None and age > limit.total_seconds()
    )
    if current is None:
        health = SourceHealth.OFFLINE
    elif current.status is GenerationStatus.DEGRADED:
        health = SourceHealth.DEGRADED
    elif stale:
        health = SourceHealth.STALE
    else:
        health = SourceHealth.OK
    return Freshness(age, stale, health)


_ALIASES = {"wiki": "wikipedia", "pg": "gutenberg", "maps": "osm", "map": "osm", "stack": "stackexchange"}
_WORD = re.compile(r"^[a-z0-9][a-z0-9.-]*$")


def sources_named(word: str, specs: Sequence[SourceSpec]) -> list[str]:
    """Source ids a word in a query refers to: ``rfc``, ``wikipedia`` (→ ``wikipedia-en-zim``, …).

    Exact ids first, then ids that start with the word as a dash-separated prefix, then registry
    ``aliases``. Deterministic; no fuzzy matching.
    """
    w = word.casefold().replace(".", "-")
    w = _ALIASES.get(w, w)
    if not _WORD.match(w):
        return []
    exact = [s.id for s in specs if s.id == w]
    prefix = sorted(s.id for s in specs if s.id.startswith(w + "-"))
    alias = sorted(s.id for s in specs if w in _aliases(s))
    return list(dict.fromkeys([*exact, *prefix, *alias]))


def _aliases(spec: SourceSpec) -> set[str]:
    value = spec.raw.get("aliases")
    if not isinstance(value, list | tuple):
        return set()
    return {str(a).casefold() for a in value}  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
