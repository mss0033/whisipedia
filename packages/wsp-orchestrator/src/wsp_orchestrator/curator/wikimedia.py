"""Wikimedia top-N ranking check (PLAN §5.3): has the set of largest Wikipedias changed?

Article counts come from each wiki's ``siteinfo`` statistics (one small API request per candidate
language). A change to the top N is proposed as a new variant of the registry source; language sets
always go to the owner because the gate cannot size a language it does not hold yet.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any, cast

from wsp_core.errors import FetchError
from wsp_orchestrator.bots import BotContext, BotResult
from wsp_orchestrator.policy import Candidate, CandidateKind

SITEINFO = "https://{lang}.wikipedia.org/w/api.php?action=query&meta=siteinfo&siprop=statistics&format=json"
# Wikipedias that have been in, or close to, the top 10 by article count.
DEFAULT_CANDIDATES = (
    "en",
    "ceb",
    "de",
    "fr",
    "sv",
    "nl",
    "es",
    "ru",
    "it",
    "pl",
    "arz",
    "zh",
    "ja",
    "uk",
    "vi",
    "war",
    "ar",
    "pt",
    "fa",
    "ca",
)


def article_count(payload: bytes) -> int:
    doc = cast(dict[str, Any], json.loads(payload))
    return int(doc["query"]["statistics"]["articles"])


class WikimediaRankingBot:
    name = "wikimedia-ranking"
    cadence: str | None = None
    network = True

    def __init__(
        self,
        *,
        source_id: str = "wikipedia-top10",
        candidates: Sequence[str] = DEFAULT_CANDIDATES,
        top: int = 10,
    ) -> None:
        self.source_id = source_id
        self.candidates = tuple(candidates)
        self.top = top

    def run(self, ctx: BotContext) -> BotResult:
        spec = next((s for s in ctx.sources if s.id == self.source_id), None)
        if spec is None:
            return BotResult(notes={"skipped": f"{self.source_id} is not in the registry"})
        counts: dict[str, int] = {}
        errors: dict[str, str] = {}
        for lang in self.candidates:
            try:
                counts[lang] = article_count(
                    ctx.fetcher.get_bytes(SITEINFO.format(lang=lang), max_bytes=65536)
                )
            except (FetchError, ValueError, KeyError, TypeError) as exc:
                errors[lang] = f"{type(exc).__name__}: {exc}"
        ranking = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
        top = [lang for lang, _ in ranking[: self.top]]
        raw_langs = spec.raw.get("languages")
        current = [str(x) for x in cast(list[object], raw_langs)] if isinstance(raw_langs, list) else []
        notes: dict[str, object] = {"ranking": [[lang, n] for lang, n in ranking], "errors": errors}
        if len(ranking) < self.top or set(top) == set(current):
            notes["changed"] = False
            return BotResult(notes=notes)
        notes["changed"] = True
        entry = {**dict(spec.raw), "languages": top}
        scores = spec.raw.get("scores")
        candidate = Candidate(
            key=f"wikimedia-top{self.top}:{','.join(sorted(top))}",
            name=f"{spec.name}: {', '.join(top)}",
            kind=CandidateKind.NEW_VARIANT,
            license=spec.license,
            domain=spec.domain,
            found_by=self.name,
            scores={str(k): int(v) for k, v in cast(dict[str, int], scores).items()}
            if isinstance(scores, dict)
            else {},
            existing_source_id=spec.id,
            registry_entry=entry,
        )
        return BotResult(candidates=[candidate], notes=notes)
