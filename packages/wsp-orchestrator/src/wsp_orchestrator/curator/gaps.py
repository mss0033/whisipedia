"""Gap miner (PLAN §5.3): local evidence of missing knowledge becomes "seek a source for X" proposals.

Two local logs feed it, both on local disk next to the index and never sent anywhere:

* ``gaps.jsonl`` - "not in library" links the Navigator reported (``POST /v1/gaps``);
* ``queries.jsonl`` - searches with their result counts (``wsp_api.gaps.JsonlQueryLog``).

Broken links are grouped by the site they point to (``wsp://<unknown site>`` or an Internet
domain); searches that found nothing are grouped by normalized query and by content word. A group
seen at least ``min_count`` times within ``window_days`` becomes a candidate the owner reviews; the
miner never activates anything itself.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Iterator, Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

from wsp_orchestrator.bots import BotContext, BotResult
from wsp_orchestrator.clock import ensure_utc
from wsp_orchestrator.policy import Candidate, CandidateKind

_WORD = re.compile(r"[\w'-]+", re.UNICODE)
_STOP = (
    "a an and are as at be by can define did do does for from how i in is it its me of on or "
    "rfc the this to was what when where which who why with wikipedia map maps book books about"
)
STOPWORDS = frozenset(_STOP.split())


def rotated(path: Path, keep: int = 5) -> list[Path]:
    """``path`` and its rotations (``path.1`` ... ``path.<keep>``), oldest first, existing only."""
    found = [path.with_name(f"{path.name}.{i}") for i in range(keep, 0, -1)] + [path]
    return [p for p in found if p.is_file()]


def read_jsonl(paths: Sequence[Path]) -> Iterator[dict[str, Any]]:
    for path in paths:
        with path.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if isinstance(obj, dict):
                    yield cast(dict[str, Any], obj)


def _when(obj: dict[str, Any]) -> datetime | None:
    try:
        return ensure_utc(datetime.fromisoformat(str(obj.get("at", ""))))
    except ValueError:
        return None


def gap_target(href: str, known_sources: frozenset[str]) -> tuple[str, str] | None:
    """``(group key, label)`` for a broken link, or ``None`` when a source we hold is merely incomplete."""
    href = href.strip()
    if href.startswith("wsp://"):
        host = href[6:].split("/", 1)[0].split("?", 1)[0].casefold()
        if not host or host in known_sources:
            return None
        return f"site:{host}", f"wsp://{host}"
    parts = urlsplit(href)
    if parts.scheme in ("http", "https") and parts.hostname:
        domain = parts.hostname.casefold().removeprefix("www.")
        return f"domain:{domain}", domain
    return None


def normalize_query(q: str) -> str:
    return " ".join(w.casefold() for w in _WORD.findall(q))


def content_words(q: str) -> list[str]:
    return [w for w in normalize_query(q).split() if len(w) >= 4 and w not in STOPWORDS and not w.isdigit()]


class GapMiner:
    name = "gap-miner"
    cadence: str | None = None
    network = False

    def __init__(
        self,
        *,
        gaps_path: Path,
        queries_path: Path,
        min_count: int = 5,
        window_days: int = 90,
        max_candidates: int = 10,
    ) -> None:
        self.gaps_path = gaps_path
        self.queries_path = queries_path
        self.min_count = min_count
        self.window = timedelta(days=window_days)
        self.max_candidates = max_candidates

    def run(self, ctx: BotContext) -> BotResult:
        since = ctx.now - self.window
        known = frozenset(s.id for s in ctx.sources)
        links: Counter[tuple[str, str]] = Counter()
        examples: dict[str, str] = {}
        n_gaps = 0
        for obj in read_jsonl(rotated(self.gaps_path)):
            at = _when(obj)
            if at is None or at < since:
                continue
            n_gaps += 1
            target = gap_target(str(obj.get("href", "")), known)
            if target is not None:
                links[target] += 1
                examples.setdefault(target[0], str(obj.get("href", "")))
        queries: Counter[str] = Counter()
        words: Counter[str] = Counter()
        n_low = 0
        for obj in read_jsonl(rotated(self.queries_path)):
            at = _when(obj)
            if at is None or at < since:
                continue
            total = obj.get("total")
            if not (obj.get("low") is True or total == 0):
                continue
            n_low += 1
            q = str(obj.get("q", ""))
            if normalize_query(q):
                queries[normalize_query(q)] += 1
            words.update(set(content_words(q)))

        groups: list[tuple[int, str, str, str | None]] = []  # (count, key, label, example)
        groups += [(n, key, label, examples.get(key)) for (key, label), n in links.items()]
        groups += [(n, f"query:{q}", f'searches for "{q}"', None) for q, n in queries.items()]
        groups += [(n, f"word:{w}", f'searches mentioning "{w}"', None) for w, n in words.items()]
        groups = sorted((g for g in groups if g[0] >= self.min_count), key=lambda g: (-g[0], g[1]))
        candidates = [
            Candidate(
                key=f"gap:{key}",
                name=f"Seek a source for {label} ({n} times in {self.window.days} days)",
                kind=CandidateKind.NEW_SOURCE,
                license="unknown",
                domain=("unknown",),
                found_by=self.name,
                url=example,
            )
            for n, key, label, example in groups[: self.max_candidates]
        ]
        notes: dict[str, object] = {
            "gap_reports": n_gaps,
            "low_result_queries": n_low,
            "top": [[label, n] for n, _, label, _ in groups[:10]],
        }
        return BotResult(candidates=candidates, notes=notes)
