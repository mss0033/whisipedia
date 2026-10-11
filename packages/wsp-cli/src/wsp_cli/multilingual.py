"""``wsp plan multilingual``: the non-English budget planner (PLAN D15).

For each language in ``[curator.multilingual] languages``, choose the highest value-per-GB material
until ``budget_tb_per_language`` is spent. Candidates are the Kiwix catalog's Wikimedia projects in
that language (Wikipedia, Wiktionary, Wikisource, Wikibooks, Wikivoyage, Wikiquote, Wikiversity,
Wikinews; at most one flavour per project) and Wikimedia text dumps of Wikipedias the library
does not hold yet. What the registry already holds in that language counts against its budget.

``plan_language`` is a pure function (no network, no orchestrator): value is the project's rubric
score (PLAN §5.2) times a flavour factor (``maxi`` 1.0, unflavoured 0.95, ``nopic`` 0.8, ``mini``
0.35, text dump 0.6); items are taken greedily by value per GB, then a chosen item is upgraded to a
richer flavour of the same project when the remaining budget allows, best marginal value per
marginal GB first. Deterministic: ties break by name.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO, cast
from urllib.parse import urlencode

import yaml

from wsp_adapters.doccollection.discovery import Throttle
from wsp_adapters.kiwix import CATALOG_URL, parse_opds
from wsp_cli import bootstrap
from wsp_core.errors import FetchError
from wsp_core.model import SourceSpec
from wsp_core.ports import Fetcher

ISO3 = {
    "de": "deu", "fr": "fra", "es": "spa", "ru": "rus", "it": "ita", "pl": "pol", "ja": "jpn",
    "zh": "zho", "pt": "por", "ar": "ara", "nl": "nld", "sv": "swe", "en": "eng", "uk": "ukr",
    "ko": "kor", "fa": "fas", "tr": "tur", "vi": "vie", "id": "ind", "he": "heb", "hi": "hin",
}  # fmt: skip
# Rubric scores from research/SOURCES.md §2 (Wikimedia family); language editions share the axis view.
PROJECT_SCORES: Mapping[str, float] = {
    "wikipedia": 4.6,
    "wiktionary": 4.3,
    "wikisource": 4.1,
    "wikibooks": 3.6,
    "wikivoyage": 3.6,
    "wikiquote": 3.6,
    "wikiversity": 3.6,
    "wikinews": 3.0,
}
FLAVOUR_FACTOR: Mapping[str, float] = {"maxi": 1.0, "": 0.95, "nopic": 0.8, "mini": 0.35, "dump": 0.6}
DEFAULT_PLAN_PATH = Path("registry/plans/multilingual.yaml")


@dataclass(frozen=True, slots=True)
class LangCandidate:
    lang: str  # ISO 639-1
    project: str  # wikipedia, wiktionary, ...
    kind: str  # "kiwix" | "dump"
    name: str  # ZIM book name or dump wiki (dewiki)
    flavour: str  # maxi / nopic / mini / "" / dump
    size_bytes: int
    score: float
    version: str = ""
    url: str = ""

    @property
    def value(self) -> float:
        return self.score * FLAVOUR_FACTOR.get(self.flavour, 0.5)

    @property
    def gb(self) -> float:
        return max(self.size_bytes, 1) / 1e9

    @property
    def density(self) -> float:
        return self.value / self.gb

    @property
    def selector_name(self) -> str:
        return f"{self.name}@{self.flavour}" if self.kind == "kiwix" and self.flavour else self.name


@dataclass(frozen=True, slots=True)
class Held:
    source_id: str
    project: str
    size_bytes: int


@dataclass
class LanguagePlan:
    lang: str
    budget_bytes: int
    held: list[Held] = field(default_factory=list[Held])
    selected: list[LangCandidate] = field(default_factory=list[LangCandidate])
    skipped: list[str] = field(default_factory=list[str])

    @property
    def held_bytes(self) -> int:
        return sum(h.size_bytes for h in self.held)

    @property
    def selected_bytes(self) -> int:
        return sum(c.size_bytes for c in self.selected)

    @property
    def total_bytes(self) -> int:
        return self.held_bytes + self.selected_bytes

    @property
    def value(self) -> float:
        return sum(c.value for c in self.selected)


def plan_language(
    lang: str, candidates: Sequence[LangCandidate], budget_bytes: int, held: Sequence[Held] = ()
) -> LanguagePlan:
    """Greedy by value per GB with one variant per project, then flavour upgrades (pure)."""
    plan = LanguagePlan(lang, budget_bytes, list(held))
    remaining = budget_bytes - plan.held_bytes
    held_projects = {h.project for h in held}
    pool = [c for c in candidates if c.lang == lang and c.size_bytes > 0]
    chosen: dict[tuple[str, str], LangCandidate] = {}  # (project, kind): ZIM and text dump are complementary
    too_big: list[tuple[LangCandidate, int]] = []
    for c in sorted(pool, key=lambda c: (-c.density, c.name, c.flavour)):
        if (c.project, c.kind) in chosen:
            continue
        if c.kind == "dump" and c.project in held_projects:
            continue  # the raw text of that Wikipedia is already held
        if c.size_bytes <= remaining:
            chosen[(c.project, c.kind)] = c
            remaining -= c.size_bytes
        else:
            too_big.append((c, remaining))
    while True:  # upgrades: richer flavour of a chosen project, best marginal value per marginal GB
        best: tuple[float, LangCandidate, LangCandidate] | None = None
        for (project, _kind), cur in chosen.items():
            for alt in pool:
                if alt.project != project or alt.kind != cur.kind or alt.value <= cur.value:
                    continue
                extra = alt.size_bytes - cur.size_bytes
                if extra > remaining:
                    continue
                gain = (alt.value - cur.value) / max(extra / 1e9, 1e-9)
                if best is None or gain > best[0] or (gain == best[0] and alt.name < best[2].name):
                    best = (gain, cur, alt)
        if best is None:
            break
        _gain, cur, alt = best
        remaining -= alt.size_bytes - cur.size_bytes
        chosen[(alt.project, alt.kind)] = alt
    plan.selected = sorted(chosen.values(), key=lambda c: (-c.density, c.name))
    plan.skipped = [
        f"{c.selector_name} ({c.size_bytes / 1e9:.1f} GB > {left / 1e9:.1f} GB left)"
        for c, left in too_big
        if (c.project, c.kind) not in chosen
    ]
    return plan


def held_by_language(specs: Sequence[SourceSpec], wiki_sizes: Mapping[str, int]) -> dict[str, list[Held]]:
    """What the registry already holds per language (Wikipedia text dumps of ``wikipedia-*`` sources)."""
    out: dict[str, list[Held]] = {}
    for spec in specs:
        if spec.status.value in ("excluded", "retired") or not spec.id.startswith("wikipedia-"):
            continue
        if spec.adapter != "wikimedia-dumps" or spec.id.endswith("-history"):
            continue
        for lang in cast(list[str], spec.raw.get("languages") or []):
            size = wiki_sizes.get(f"{lang}wiki")
            if size:
                out.setdefault(lang, []).append(Held(spec.id, "wikipedia", size))
    return out


# --- gathering candidates (network: Kiwix catalog, Wikimedia dumpstatus) ------------------------------

_BOOK_RE = re.compile(r"^(?P<project>.+?)_(?P<lang>[a-z]{2,3})_all$")
# Non-Wikimedia books with a known open licence and an editorial process (research/SOURCES.md
# scores); arbitrary website scrapes ("zimit") are left out: their licences are unknown.
OTHER_SCORES: Mapping[str, float] = {
    "mdwiki": 4.2,
    "khanacademy": 3.9,
    "phet": 3.8,
    "ifixit": 3.8,
    "vikidia": 3.4,
}
STACK_EXCHANGE_SCORE = 4.0


def book_score(project: str, category: str) -> float | None:
    if project in PROJECT_SCORES:
        return PROJECT_SCORES[project]
    if category == "stack_exchange" or project.endswith(("stackexchange.com", "stackoverflow.com")):
        return STACK_EXCHANGE_SCORE
    return OTHER_SCORES.get(project)


def kiwix_candidates(fetcher: Fetcher, lang: str) -> list[LangCandidate]:
    iso3 = ISO3.get(lang, lang)
    url = f"{CATALOG_URL}?{urlencode({'lang': iso3, 'count': '-1'})}"
    entries, _total = parse_opds(fetcher.get_bytes(url))
    out: list[LangCandidate] = []
    for e in entries:
        m = _BOOK_RE.match(e.name)
        if not m or m.group("lang") != lang or not e.length:
            continue
        project = m.group("project")
        score = book_score(project, e.category)
        if score is None:
            continue
        out.append(
            LangCandidate(
                lang=lang,
                project=project,
                kind="kiwix",
                name=e.name,
                flavour=e.flavour,
                size_bytes=e.length,
                score=score,
                version=e.zim_date,
                url=e.file_url,
            )
        )
    return out


def dump_size(fetcher: Fetcher, wiki: str) -> tuple[str, int]:
    """Newest finished ``articlesmultistreamdump`` of ``wiki``: ``(run date, bytes)``."""
    listing = fetcher.get_bytes(f"https://dumps.wikimedia.org/{wiki}/").decode("utf-8", "replace")
    runs = sorted(set(re.findall(r'href="(\d{8})/"', listing)), reverse=True)
    for run in runs[:3]:
        try:
            status = json.loads(
                fetcher.get_bytes(f"https://dumps.wikimedia.org/{wiki}/{run}/dumpstatus.json")
            )
        except (FetchError, ValueError) as exc:
            print(f"note: {wiki}/{run}: no dump status ({exc})", file=sys.stderr)
            continue
        jobs = cast(Mapping[str, Any], cast(Mapping[str, Any], status).get("jobs") or {})
        for job in ("articlesmultistreamdumprecombine", "articlesmultistreamdump"):
            j = cast(Mapping[str, Any], jobs.get(job) or {})
            if j.get("status") == "done":
                files = cast(Mapping[str, Mapping[str, Any]], j.get("files") or {})
                return run, sum(int(f.get("size") or 0) for f in files.values())
    return "", 0


def gather(
    fetcher: Fetcher, languages: Sequence[str], specs: Sequence[SourceSpec], *, out: TextIO | None = None
) -> tuple[list[LangCandidate], dict[str, int]]:
    """Candidates for every language plus Wikipedia text-dump sizes (held and not yet held)."""
    cands: list[LangCandidate] = []
    sizes: dict[str, int] = {}
    held_langs = {
        lang
        for s in specs
        if s.id.startswith("wikipedia-") and s.adapter == "wikimedia-dumps"
        for lang in cast(list[str], s.raw.get("languages") or [])
    }
    for lang in languages:
        if out is not None:
            out.write(f"catalog {lang} ...\n")
            out.flush()
        cands += kiwix_candidates(fetcher, lang)
        run, size = dump_size(fetcher, f"{lang}wiki")
        sizes[f"{lang}wiki"] = size
        if size and lang not in held_langs:
            cands.append(
                LangCandidate(
                    lang, "wikipedia", "dump", f"{lang}wiki", "dump", size, PROJECT_SCORES["wikipedia"], run
                )
            )
    return cands, sizes


# --- output ----------------------------------------------------------------------------------------


def plan_document(plans: Sequence[LanguagePlan], *, budget_tb: float, at: str) -> dict[str, Any]:
    langs: dict[str, Any] = {}
    for p in plans:
        langs[p.lang] = {
            "budget_gb": round(p.budget_bytes / 1e9, 1),
            "held_gb": round(p.held_bytes / 1e9, 2),
            "selected_gb": round(p.selected_bytes / 1e9, 2),
            "total_gb": round(p.total_bytes / 1e9, 2),
            "value": round(p.value, 2),
            "held": [asdict(h) for h in p.held],
            "kiwix_selector": [c.selector_name for c in p.selected if c.kind == "kiwix"],
            "dumps": [c.name for c in p.selected if c.kind == "dump"],
            "selected": [
                {
                    "name": c.selector_name,
                    "project": c.project,
                    "kind": c.kind,
                    "gb": round(c.size_bytes / 1e9, 2),
                    "value_per_gb": round(c.density, 3),
                    "version": c.version,
                }
                for c in p.selected
            ],
            "skipped": p.skipped,
        }
    total = sum(p.total_bytes for p in plans)
    return {
        "generated_at": at,
        "decision": "PLAN D15",
        "budget_tb_per_language": budget_tb,
        "total_tb": round(total / 1e12, 3),
        "new_tb": round(sum(p.selected_bytes for p in plans) / 1e12, 3),
        "languages": langs,
    }


def render(plans: Sequence[LanguagePlan], out: TextIO) -> None:
    out.write(f"{'lang':<5} {'budget GB':>9} {'held GB':>8} {'new GB':>8} {'total GB':>9}  selection\n")
    for p in plans:
        names = ", ".join(f"{c.selector_name} ({c.size_bytes / 1e9:.1f})" for c in p.selected)
        out.write(
            f"{p.lang:<5} {p.budget_bytes / 1e9:>9.0f} {p.held_bytes / 1e9:>8.1f} "
            f"{p.selected_bytes / 1e9:>8.1f} {p.total_bytes / 1e9:>9.1f}  {names}\n"
        )
    new = sum(p.selected_bytes for p in plans)
    total = sum(p.total_bytes for p in plans)
    out.write(f"{'TOTAL':<5} {'':>9} {(total - new) / 1e9:>8.1f} {new / 1e9:>8.1f} {total / 1e9:>9.1f}\n")


def cmd_plan_multilingual(args: argparse.Namespace, out: TextIO) -> int:
    lay = bootstrap.layout(args.config_dir)
    cfg = bootstrap.load(lay)
    specs = bootstrap.load_sources(cfg, lay)
    ml = cast(
        Mapping[str, Any],
        bootstrap.section(cfg, "curator").get("multilingual") or {},
    )
    languages = [str(x) for x in cast(list[object], ml.get("languages") or [])]
    budget_tb = float(args.budget_tb if args.budget_tb is not None else ml.get("budget_tb_per_language", 0.3))
    if args.candidates:
        data = cast(Mapping[str, Any], json.loads(Path(args.candidates).read_text(encoding="utf-8")))
        cands = [LangCandidate(**c) for c in cast(list[dict[str, Any]], data["candidates"])]
        sizes = cast(dict[str, int], data.get("dump_sizes") or {})
    else:
        cands, sizes = gather(Throttle(bootstrap.build_fetcher(cfg)), languages, specs, out=sys.stderr)
        if args.save_candidates:
            Path(args.save_candidates).write_text(
                json.dumps({"candidates": [asdict(c) for c in cands], "dump_sizes": sizes}, indent=1),
                encoding="utf-8",
            )
    held = held_by_language(specs, sizes)
    budget = int(budget_tb * 1e12)
    plans = [plan_language(lang, cands, budget, held.get(lang, ())) for lang in languages]
    doc = plan_document(plans, budget_tb=budget_tb, at=datetime.now(UTC).isoformat(timespec="seconds"))
    if args.out != "-":
        path = Path(args.out) if args.out else (lay.root or Path.cwd()) / DEFAULT_PLAN_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "# Generated by `wsp plan multilingual` (PLAN D15). Review, then mirror the\n"
            "# kiwix_selector lists into the kiwix-multilingual-<lang> registry entries.\n"
            + yaml.safe_dump(doc, sort_keys=False, allow_unicode=True, width=110),
            encoding="utf-8",
        )
    if args.json:
        out.write(json.dumps(doc, indent=1) + "\n")
    else:
        render(plans, out)
    return 0


def register(sub: Any, add: Any) -> None:
    pl = add("plan", _usage, "Planners (curator).")
    pl_sub = pl.add_subparsers(dest="plan_command", required=True, metavar="COMMAND")
    s = add(
        "multilingual",
        cmd_plan_multilingual,
        "Per-language selection of the best value-per-GB material within the D15 budget.",
        pl_sub,
    )
    s.add_argument("--budget-tb", type=float, default=None, help="override budget_tb_per_language")
    s.add_argument("--candidates", default=None, help="offline: read candidates from this JSON file")
    s.add_argument("--save-candidates", default=None, help="write the fetched candidates to this JSON file")
    s.add_argument("--out", default=None, help=f"plan file (default: {DEFAULT_PLAN_PATH}; '-' = none)")
    s.add_argument("--json", action="store_true")


def _usage(_: argparse.Namespace, __: TextIO) -> int:  # pragma: no cover - argparse requires a subcommand
    return 2


__all__ = ["LangCandidate", "LanguagePlan", "plan_language"]
