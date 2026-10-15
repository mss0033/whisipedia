"""Document-level policy for curated document collections (PLAN §13, D13; research doc §3, §5.3).

This is the curator's deterministic gate for ``doc-collection`` sources. It never touches the
network and never reads more than metadata and front matter. Three stages, applied deny-first:

1. **Metadata** (:func:`classify_metadata`) on index/API/OAI records before anything is fetched:
   owner decisions, restricted markings in rights fields, the built-in safety deny list, series
   deny lists, source deny keywords, owner allow decisions, review keywords, series allow lists,
   then the source default (``review`` unless the registry says otherwise).
2. **Distribution** (:func:`parse_markings`, :func:`check_distribution`) on the front matter of a
   fetched document (first pages): Distribution Statements B-F, FOUO/CUI and similar banners and
   any export-control warning reject the document and raise an alert; a configured requirement
   (``A``, ``public-release``) that is not met holds it for review.
3. **Content** (:func:`classify_content`) on the same front matter: the safety deny list (title
   page, or several distinct hits in the front matter) rejects; other hits hold for review.

Unknown series default to review: nothing a rule does not clearly allow reaches the library
without the owner (``wsp proposals``). Every decision names the rule that made it, for audit.

The built-in safety lists encode the §13 boundary that holds *regardless of release status*:
CBRN production or weaponisation, explosives and improvised munitions, weapons and munitions
technical data (manufacture, guidance, firing tables), and offensive tooling. Employment doctrine
(gunnery, fires, targeting, crew drills) is in scope (owner decision 2026-10-12). They are phrase
lists, matched on word boundaries after neutral phrases (``bomb threat``, ``nuclear power``) are
blanked out, so a defensive or civil document is not caught by a bare noun. Bare nouns
(``explosive``, ``weapon``, ``chemical``) go to review instead.
"""

from __future__ import annotations

import functools
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date
from enum import StrEnum
from pathlib import Path
from typing import Any, cast

import yaml

from wsp_core.banners import HISTORICAL_BANNER


class Verdict(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    REVIEW = "review"


class Stage(StrEnum):
    METADATA = "metadata"
    DISTRIBUTION = "distribution"
    CONTENT = "content"


@dataclass(frozen=True, slots=True)
class Decision:
    verdict: Verdict
    rule: str  # e.g. "series=FM 5-0", "safety=shaped charges", "marking=distribution-C"
    stage: Stage = Stage.METADATA
    alert: bool = False  # an index listed a restricted document: that is news for the owner

    def __str__(self) -> str:
        return f"{self.verdict.value}:{self.rule}"


# --- built-in safety lists (PLAN §13; research doc §3.2) ------------------------------------------
#
# The owner's line (2026-10-11, narrowed 2026-10-12): officially public material is in scope, public
# military doctrine on weapons employment, marksmanship, gunnery, fires, targeting, fire control, air
# and missile defence and crew drills included. The deny core below is what stays out regardless of
# release status, one entry per excluded category (classified and restricted material is the marking
# screen's job, below). Each entry is a *pattern*: a space is a word gap (spaces or hyphens:
# ``mine field``, ``mine-field``), ``~`` an optional one (``booby~trap``: booby trap, booby-trap,
# boobytrap); the rest is regular-expression syntax, matched case-insensitively between word
# boundaries, so word forms are spelled out (``charges?``, ``weaponi[sz](?:e|ed|ing)``). A pattern
# names a *technique* (``shaped charges``, ``firing tables``), never a bare noun: histories,
# defensive guidance and health reports use the nouns too (2026-10-12 audit, CA-3/CA-4).
# ``tests/test_doccollection_policy.py`` holds must-deny and must-allow titles for every category.

_WEAPON = (
    r"(?:machine~guns?|mortars?|howitzers?|cannons?|rifles?|carbines?|pistols?|grenade launchers?|"
    r"rocket launchers?|anti~tank (?:weapons?|missiles?|guided missiles?)|guided missiles?|"
    r"missile systems?|weapons? systems?|crew~served weapons?|small arms|tank main guns?|artillery)"
)
_CBRN_AGENT = (
    r"(?:nerve agents?|chemical (?:warfare )?agents?|chemical weapons?|biological (?:warfare )?agents?|"
    r"biological weapons?|sarin|soman|tabun|vx|mustard (?:gas|agent)|ricin|anthrax|botulinum toxin)"
)
_WEAPONIZABLE = (
    r"(?:anthrax|pathogens?|agents?|toxins?|viruses|virus|bacteria|bacillus|spores|smallpox|plague|"
    r"ricin|sarin|biological|chemical)"
)

# Owner category -> patterns. The category names are the owner's list (PLAN §13, 2026-10-11; weapons
# employment and gunnery, targeting and fire control, and weapon-system crew drills left it 2026-10-12).
DENY_CORE: Mapping[str, tuple[str, ...]] = {
    "explosives and demolitions": (
        r"explosives? (?:manufactur\w*|production|formulations?|synthesis|fabrication)",
        r"(?:manufactur\w*|making|producing|production|synthesi[sz]\w*|fabricat\w*) (?:of )?"
        r"(?:high |improvised |home~made )?explosives?",
        r"home~made explosives?",
        r"improvised explosives? (?:devices? )?(?:construction|fabrication|assembly|manufactur\w*|making|"
        r"design)",
        r"(?:construct\w*|fabricat\w*|assembl\w*|building|making) (?:an? |of )?"
        r"(?:improvised explosive devices?|ieds?|pipe bombs?|car bombs?)",
        r"ieds? (?:construction|fabrication|assembly|making)",
        r"improvised munitions?",
        r"shaped charges?",
        r"(?:detonators?|blasting caps?) (?:assembly|construction|fabrication|manufactur\w*|making)",
        r"(?:military|combat|engineer|tactical|field) demolitions?",
        r"demolition (?:charges?|munitions?|kits?|blocks?|calculations?|firing systems?)",
        r"demolition materials? and (?:charges|explosives)",
        r"demolitions (?:handbook|manual)",
        r"(?:explosives|obstacles|mines) and demolitions?",
        r"demolitions? and (?:explosives|obstacles|mines|breaching)",
        r"breaching charges?",
        r"booby~trap(?:s|ped|ping)? (?:construction|fabrication|making|devices?|techniques?|emplacement|"
        r"assembly|employment)",
        r"(?:construct\w*|making|fabricat\w*|emplac\w*|setting|rigging) (?:of )?booby~traps?",
        r"(?:laying|emplac\w*|placing|planting) (?:of )?(?:land~)?(?:mine~fields?|mines)",
        r"mine~fields? (?:laying|emplacement)",
        r"mine emplacement",
    ),
    "weapons and munitions technical data": (
        r"(?:weapons?|firearms?|munitions?) manufacture",
        r"manufacture of (?:firearms|weapons|munitions|ammunition)",
        r"gunsmith\w*",
        r"ammunition (?:reloading|hand~loading)",
        r"(?:re|hand~)loading ammunition",
        r"munitions? effectiveness",
        r"(?:weapons?|munitions?|ammunition) technical (?:data|manuals?)",
        r"(?:technical|operator['\u2019]?s) manual (?:for )?(?:the )?(?:\w+ ){0,3}" + _WEAPON,
        r"firing tables?",
        r"(?:nuclear )?warhead design",
        # Guidance is engineering data of the weapon, not its employment; kept from the former
        # "targeting and fire control" category (owner decision 2026-10-12).
        r"(?:missile|weapons?|bomb) guidance",
    ),
    "CBRN weapons production": (
        r"(?:chemical|biological|bacteriological|toxin|nuclear|radiological) weapons? "
        r"(?:production|manufactur\w*|design|synthesis)",
        r"(?:production|manufactur\w*|synthesis|preparation) of (?:\w+ ){0,2}" + _CBRN_AGENT,
        _CBRN_AGENT + r" (?:production|synthesis|manufactur\w*|preparation)",
        r"weaponi[sz](?:e|ed|es|ing|ation) (?:of )?(?:an? |the )?(?:\w+ ){0,2}" + _WEAPONIZABLE,
        r"(?:chemical|biological) agent dissemination",
        r"(?:uranium|plutonium) (?:enrichment|production|separation|reprocessing) for (?:nuclear )?weapons?",
        r"weapons~grade (?:uranium|plutonium) production",
    ),
    "offensive cyber tooling": (
        r"exploit (?:development|writing|kits?|builders?)",
        r"(?:malware|ransomware|rootkits?|botnets?|trojans?|keyloggers?) "
        r"(?:development|authoring|writing|builders?|kits?|construction|source code)",
        r"writing (?:shellcode|exploits?|malware|ransomware|viruses|rootkits?)",
        r"shellcode (?:development|writing|engineering|encoders?)",
        r"offensive cyber(?:space)? (?:operations|tools?|tooling|capabilities)",
        r"cyberspace attacks?",
        r"(?:computer network|cyber) attack (?:tools?|techniques?|tooling)",
    ),
}
SAFETY_DENY: tuple[str, ...] = tuple(p for patterns in DENY_CORE.values() for p in patterns)

# History-prone nouns of the deny core. In histories, health reports and civil guidance they name
# events, not techniques ("Booby Traps in Vietnam", "armed groups plant booby traps on roads"), so
# alone they decide nothing; next to a core hit they count (:func:`classify_content`).
SAFETY_CONTEXT: tuple[str, ...] = (
    r"booby~trap(?:s|ped|ping)?",
    r"mine~lay(?:ing|ers?)",
    r"mine~fields?",
    r"mine warfare",
    r"demolitions?",
    r"weaponi[sz](?:e|ed|es|ing|ation)",
    r"shellcode",
    r"munitions",
)

SAFETY_REVIEW: tuple[str, ...] = (
    "chemical",
    "biological",
    "radiological",
    "nuclear",
    "cbrn",
    "explosive",
    "explosives",
    "bomb",
    "bombs",
    "improvised explosive",
    "weapon",
    "weapons",
    "firearm",
    "firearms",
    "missile",
    "missiles",
    "electronic warfare",
    "electronic attack",
    "jamming",
    "surveillance",
    "cyberspace operations",
    "information operations",
    "psychological operations",
    "sabotage",
    "toxin",
    "toxins",
    "pathogen",
    "pathogens",
    "exploit",
    "exploits",
    "penetration testing",
    "insurgency",
    "counterinsurgency",
    "unconventional warfare",
    "guerrilla",
    "combat",
    "targeting",
)

# Phrases blanked out before matching: civil or defensive uses of a listed word.
NEUTRAL_PHRASES: tuple[str, ...] = (
    "bomb threat",
    "bomb threats",
    "nuclear power",
    "nuclear power plant",
    "nuclear power plants",
    "nuclear reactor",
    "nuclear reactors",
    "nuclear physics",
    "nuclear medicine",
    "nuclear magnetic resonance",
    "nuclear regulatory commission",
    "biological sciences",
    "biological science",
    "biological resources",
    "biological survey",
    "biological opinion",
    "biological diversity",
    "chemical engineering",
    "chemical hazards",
    "chemical hazard",
    "chemical composition",
    "chemical analysis",
    "chemical analyses",
    "chemical properties",
    "chemical safety",
    "hazardous chemicals",
    "chemistry",
    "weather radar",
    "public health surveillance",
    "disease surveillance",
    "surveillance detection",
    "volcano surveillance",
    "earthquake surveillance",
    "medical management",
    "radiological emergency",
    "radiological emergencies",
)


# --- text normalization (every stage matches on normalized text) ---------------------------------
#
# PDF text layers and index pages spell the same words many ways: ligatures (``ﬁ``), fullwidth
# letters, non-breaking or soft hyphens, zero-width joiners, words hyphenated across a line break
# and letter-spaced headings (``F I R E  C O N T R O L``). Each of these used to slip past the
# phrase lists (2026-10-11 audit, adapters C3), so the screens match on :func:`normalize_text` and,
# in addition, on the joined and de-spaced copies :func:`screen_variants` derives from it.

# Unicode 15 general category Pd (dash punctuation), plus the minus sign and the hyphen bullet.
# ``test_dash_table_covers_unicode_pd`` checks this list against ``unicodedata``.
DASHES = (
    "-\u058a\u05be\u1400\u1806\u2010\u2011\u2012\u2013\u2014\u2015\u2e17\u2e1a\u2e3a\u2e3b"
    "\u2e40\u2e5d\u301c\u3030\u30a0\ufe31\ufe32\ufe58\ufe63\uff0d\U00010ead\u2212\u2043"
)
# Format characters that render as nothing: soft hyphen, zero-width space/joiners, word joiner, BOM.
INVISIBLE = "\u00ad\u200b\u200c\u200d\u2060\ufeff"
_TRANSLATE = {**dict.fromkeys(map(ord, INVISIBLE)), **{ord(d): "-" for d in DASHES}}
_HYPHEN_BREAK_RE = re.compile(r"(\w)-[ \t]*\r?\n\s*(\w)")
# Three or more single letters/digits separated by runs of 1-3 spaces: a letter-spaced heading.
_SPACED_RUN_RE = re.compile(r"(?<!\w)(?:\w[ \t]{1,3}){2,}\w(?!\w)")
_WIDE_GAP_RE = re.compile(r"[ \t]{2,}")


def normalize_text(text: str) -> str:
    """NFKC, invisible format characters removed, every Unicode dash mapped to ``-``."""
    return unicodedata.normalize("NFKC", text).translate(_TRANSLATE)


def screen_variants(text: str) -> tuple[str, str]:
    """``(words, squeezed)`` for already-normalized ``text``.

    ``words`` is the text followed by a copy with hyphenated line breaks joined
    (``weapon-\\nization`` -> ``weaponization``) and a copy of every letter-spaced run with the
    single spaces removed (``F I R E  C O N T R O L`` -> ``FIRE CONTROL``); the phrase lists match
    on it with their usual word boundaries. ``squeezed`` holds each letter-spaced run with all
    whitespace removed (``FIRECONTROL``), for the boundary-free pattern of :func:`_loose_re`:
    uniformly letter-spaced text has no word gaps left to match on.
    """
    parts = [text]
    joined = _HYPHEN_BREAK_RE.sub(r"\1\2", text)
    if joined != text:
        parts.append(joined)
    runs = [m.group(0) for m in _SPACED_RUN_RE.finditer(text)]
    if runs:
        parts.append(
            "\n".join(
                " ".join(w.replace(" ", "").replace("\t", "") for w in _WIDE_GAP_RE.split(r)) for r in runs
            )
        )
    squeezed = "\n".join(re.sub(r"\s+", "", r) for r in runs)
    return "\n\n".join(parts), squeezed


_GAP = r"[\s\-]+"  # a word gap: spaces, tabs, newlines or hyphens (every dash is "-" after normalize_text)
_OPT_GAP = r"[\s\-]*"


def _hit_name(text: str) -> str:
    return re.sub(r"[\s\-]+", " ", text.casefold()).strip()


@dataclass(frozen=True, slots=True)
class PhraseList:
    """Phrases or patterns compiled for matching: word-bounded on text, boundary-free on squeezed runs.

    ``loose`` has one named group per entry that can match without word gaps (``p<i>``); ``names``
    gives the entry's name for a hit there (``None``: report the matched text).
    """

    words: re.Pattern[str] | None
    loose: re.Pattern[str] | None
    names: tuple[str | None, ...]

    def hits(self, text: Screened) -> list[str]:
        """Distinct hits (casefolded, single-spaced), in order of first appearance."""
        seen: dict[str, None] = {}
        if self.words is not None:
            for m in self.words.finditer(text.words):
                seen.setdefault(_hit_name(m.group(0)))
        if self.loose is not None and text.squeezed:
            for m in self.loose.finditer(text.squeezed):
                name = self.names[int((m.lastgroup or "p0")[1:])]
                seen.setdefault(name if name is not None else _hit_name(m.group(0)))
        return list(seen)


def _compile(items: Sequence[tuple[str, str | None, str | None]]) -> PhraseList:
    """``(word pattern, gap-free pattern or None, name or None)`` entries, longest first."""
    if not items:
        return PhraseList(None, None, ())
    ordered = sorted(items, key=lambda it: len(it[0]), reverse=True)
    words = re.compile(rf"(?<![\w-])(?:{'|'.join(w for w, _l, _n in ordered)})(?![\w-])", re.IGNORECASE)
    loose_items = [(lo, name) for _w, lo, name in ordered if lo is not None]
    loose = (
        re.compile("|".join(f"(?P<p{i}>{lo})" for i, (lo, _n) in enumerate(loose_items)), re.IGNORECASE)
        if loose_items
        else None
    )
    return PhraseList(words, loose, tuple(name for _lo, name in loose_items))


@functools.lru_cache(maxsize=512)
def phrase_list(phrases: tuple[str, ...]) -> PhraseList:
    """Plain phrases (registry keywords): words in order, any gap between them, the last word in its
    singular or plural (``demolition`` also matches ``demolitions``)."""
    items: dict[str, tuple[str, str | None, str | None]] = {}
    for p in phrases:
        text = normalize_text(p).casefold().strip()
        tokens = [re.escape(t) for t in re.split(r"[\s\-]+", text) if t]
        if not tokens:
            continue
        plural = r"(?:e?s)?"
        items[text] = (_GAP.join(tokens) + plural, "".join(tokens) + plural, _hit_name(text))
    return _compile(list(items.values()))


@functools.lru_cache(maxsize=64)
def pattern_list(patterns: tuple[str, ...]) -> PhraseList:
    """Deny-core patterns (see :data:`DENY_CORE`): a space is a word gap, ``~`` an optional one.

    A pattern with a free word slot (``\\w``) has no gap-free form: a squeezed letter-spaced run has
    no word gaps left to count words by.
    """
    items: list[tuple[str, str | None, str | None]] = []
    for p in dict.fromkeys(patterns):
        words = p.replace(" ", _GAP).replace("~", _OPT_GAP)
        loose = None if "\\w" in p else p.replace(" ", "").replace("~", "")
        items.append((words, loose, None))
    return _compile(items)


def _neutralize(text: str, extra: tuple[str, ...]) -> str:
    out = text
    for phrases in (NEUTRAL_PHRASES, extra):
        pattern = phrase_list(phrases).words
        if pattern is not None:
            out = pattern.sub(" ", out)
    return out


@dataclass(frozen=True, slots=True)
class Screened:
    """Text prepared for phrase matching: normalized words (with variants) and squeezed runs."""

    words: str
    squeezed: str

    @staticmethod
    def of(text: str, neutral: tuple[str, ...] = ()) -> Screened:
        words, squeezed = screen_variants(normalize_text(text))
        # Neutral phrases are blanked in the word copy only: a squeezed run has no word boundaries
        # left, so blanking there could hide a deny phrase that shares letters with a neutral one.
        return Screened(_neutralize(words, neutral), squeezed)


# --- rules -----------------------------------------------------------------------------------------


def normalize_pub_number(value: str) -> str:
    """``fm 5-0`` / ``FM5-0`` / ``FM-5-0`` / ``FM\u00a05\u20110`` -> ``FM 5-0``.

    Series letters, one space, the number; NFKC, every Unicode dash as ``-`` and no spaces around
    a dash inside the number (``FM 3 - 22.9``), so a deny-series rule cannot be dodged by spelling.
    """
    text = re.sub(r"\s+", " ", normalize_text(value).strip()).upper()
    text = re.sub(r"^([A-Z][A-Z/&.]*)[\s\-_]*(?=\d)", r"\1 ", text)
    return re.sub(r"(?<=\w) ?- ?(?=\w)", "-", text)


@dataclass(frozen=True, slots=True)
class Rules:
    """A source's filter rules: the registry ``access.filter`` block merged with its rules file."""

    allow_series: frozenset[str] = frozenset()
    allow_series_prefix: tuple[str, ...] = ()
    deny_series: frozenset[str] = frozenset()
    deny_series_prefix: tuple[str, ...] = ()
    deny_keywords: tuple[str, ...] = ()
    review_keywords: tuple[str, ...] = ()
    neutral_phrases: tuple[str, ...] = ()
    allow_documents: frozenset[str] = frozenset()  # owner decisions (pub numbers or document keys)
    deny_documents: frozenset[str] = frozenset()
    # Owner released a content-screen hold (``review:safety=...``) for these documents. A content
    # *deny* stays a deny: only ``override_safety`` lifts a deny-core hit.
    allow_content_review: frozenset[str] = frozenset()
    # Owner decision that a document the deny core names is in scope after all; a mapping of key to
    # reason in the rules file (a reason is required, so the exception is never silent).
    override_safety: frozenset[str] = frozenset()
    allow_all: bool = False  # single-purpose collection: everything not denied or reviewed is in scope
    default: Verdict = Verdict.REVIEW
    require: tuple[str, ...] = ()  # distribution requirement: "A", "public-release", "license:<SPDX>"
    declassified_ok: bool = False  # FOIA/declassified sets: classification banners with a release stamp
    # Built-in noun-level review list (SAFETY_REVIEW): off by default (owner decision 2026-10-11:
    # publicly released material is in scope unless it falls in the narrow deny core above, so a
    # document is never held just for mentioning "chemical" or "combat"). A source may opt in.
    # The built-in deny list and the front-matter screens always apply.
    builtin_review: bool = False
    safety: bool = True  # built-in §13 lists; never disabled in the registry (tests only)
    # A fetched file with no text to screen (a scanned PDF without a text layer) is held for review
    # unless the source is known to publish image-only scans and says so here. Unsupported formats
    # and extraction errors are held regardless.
    allow_image_only: bool = False

    @property
    def deny_phrases(self) -> PhraseList:
        return pattern_list(SAFETY_DENY if self.safety else ())

    @property
    def context_phrases(self) -> PhraseList:
        return pattern_list(SAFETY_CONTEXT if self.safety else ())

    def merged(self, other: Mapping[str, Any]) -> Rules:
        """These rules plus a mapping (registry filter block or rules file); lists are unioned."""
        return replace(self, **_rules_kwargs(other, base=self))


# Every key a filter block or rules file may hold; anything else is a typo that would be ignored.
RULE_KEYS = frozenset(
    {
        "allow_series",
        "allow_series_prefix",
        "deny_series",
        "deny_series_prefix",
        "deny_keywords",
        "review_keywords",
        "neutral_phrases",
        "allow_documents",
        "deny_documents",
        "allow_content_review",
        "override_safety",
        "allow_all",
        "default",
        "distribution",
        "declassified_ok",
        "builtin_review",
        "allow_image_only",
    }
)
# What `wsp proposals decide` records, in the machine-owned file next to the rules file.
DECISION_KEYS = ("allow_documents", "allow_content_review", "deny_documents")
DECISIONS_DIR = "decisions"


def decisions_path(rules_file: Path) -> Path:
    """The machine-owned decisions file of a hand-written rules file: ``<dir>/decisions/<name>``."""
    return rules_file.parent / DECISIONS_DIR / rules_file.name


def _strs(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, list | tuple):
        return tuple(str(v) for v in cast(Sequence[object], value))
    raise ValueError(f"expected a string or a list, got {value!r}")


def _normalize_prefix(value: str) -> str:
    """A series prefix spelled as :func:`normalize_pub_number` spells numbers (trailing space kept)."""
    text = re.sub(r"\s+", " ", normalize_text(value)).upper().lstrip()
    text = re.sub(r"^([A-Z][A-Z/&.]*)[\s\-_]*(?=\d)", r"\1 ", text)
    return re.sub(r"(?<=\w) ?- ?(?=\w)", "-", text)


def _rules_kwargs(data: Mapping[str, Any], *, base: Rules) -> dict[str, Any]:
    unknown = sorted(str(k) for k in data if k not in RULE_KEYS)
    if unknown:
        raise ValueError(f"unknown filter key(s) {unknown}; known: {sorted(RULE_KEYS)}")
    out: dict[str, Any] = {}
    sets = ("allow_series", "deny_series", "allow_documents", "deny_documents", "allow_content_review")
    for key in sets:
        if key in data:
            norm = {normalize_pub_number(s) for s in _strs(data[key])}
            out[key] = frozenset(getattr(base, key) | norm)
    if "override_safety" in data:
        raw: object = data["override_safety"] or {}
        if not isinstance(raw, Mapping):
            raise ValueError("override_safety: expected a mapping of document key to reason")
        reasons = cast(Mapping[object, object], raw)
        silent = sorted(str(k) for k, why in reasons.items() if not str(why or "").strip())
        if silent:
            raise ValueError(f"override_safety: a reason is required for {silent}")
        keys = {normalize_pub_number(str(k)) for k in reasons}
        out["override_safety"] = frozenset(base.override_safety | keys)
    for key in ("allow_series_prefix", "deny_series_prefix"):
        if key in data:
            prefixes = (_normalize_prefix(s) for s in _strs(data[key]))
            out[key] = tuple(dict.fromkeys([*getattr(base, key), *prefixes]))
    for key in ("deny_keywords", "review_keywords", "neutral_phrases"):
        if key in data:
            out[key] = tuple(dict.fromkeys([*getattr(base, key), *_strs(data[key])]))
    if "allow_all" in data:
        out["allow_all"] = bool(data["allow_all"])
    if "declassified_ok" in data:
        out["declassified_ok"] = bool(data["declassified_ok"])
    if "builtin_review" in data:
        out["builtin_review"] = bool(data["builtin_review"])
    if "allow_image_only" in data:
        out["allow_image_only"] = bool(data["allow_image_only"])
    if "default" in data:
        out["default"] = Verdict(str(data["default"]))
    dist = data.get("distribution")
    if isinstance(dist, Mapping):
        dist_block = cast(Mapping[str, object], dist)
        extra = sorted(str(k) for k in dist_block if k != "require")
        if extra:
            raise ValueError(f"unknown distribution key(s) {extra}; known: ['require']")
        out["require"] = _strs(dist_block.get("require"))
    return out


def _yaml_mapping(path: Path) -> Mapping[str, Any]:
    data: object = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, Mapping):
        raise ValueError(f"{path}: expected a mapping")
    return cast(Mapping[str, Any], data)


def load_rules(filter_block: Mapping[str, Any] | None, *, repo_root: Path | None = None) -> Rules:
    """Rules for one source: the registry ``access.filter`` block plus its ``rules:`` file, if any.

    The rules file (``registry/filters/<source>.yaml``) holds the long series and keyword lists,
    written and commented by hand and reviewed in pull requests like the registry itself. The owner's
    per-document review decisions live beside it in the machine-owned :func:`decisions_path` file
    (``wsp proposals decide`` writes it); only :data:`DECISION_KEYS` are read from there. A source
    without a rules file has no place for decisions: the review tool refuses to record them.
    Unknown keys in any of them raise ``ValueError``.
    """
    rules = Rules()
    block = dict(filter_block or {})
    rules_file = block.pop("rules", None)
    if rules_file:
        path = Path(str(rules_file))
        if not path.is_absolute() and repo_root is not None:
            path = repo_root / path
        if not path.is_file():
            raise ValueError(f"filter.rules names {path}, which does not exist")
        rules = rules.merged(_yaml_mapping(path))
        decided = decisions_path(path)
        if decided.is_file():
            data = _yaml_mapping(decided)
            rules = rules.merged({k: data[k] for k in DECISION_KEYS if k in data})
    return rules.merged(block)


def _series_matches(pub: str, series: frozenset[str], prefixes: Sequence[str]) -> str | None:
    if not pub:
        return None
    if pub in series:
        return pub
    # "FM 5-0" also matches "FM 5-0 C1" (a change to the same publication)
    base = pub.split(" C")[0] if re.search(r" C\d+$", pub) else pub
    if base in series:
        return base
    return next((p for p in prefixes if pub.startswith(p)), None)


@dataclass(frozen=True, slots=True)
class DocFacts:
    """What the metadata stage can see about one document: no bytes of the document itself."""

    key: str
    pub_number: str = ""
    title: str = ""
    subjects: tuple[str, ...] = ()
    abstract: str = ""
    rights: str = ""  # repository rights / distribution field, verbatim

    @property
    def heading(self) -> str:
        """What names the document's primary content: number, title, subjects."""
        return " \n".join(x for x in (self.pub_number, self.title, *self.subjects) if x)

    @property
    def text(self) -> str:
        return " \n".join(x for x in (self.heading, self.abstract) if x)


def classify_metadata(doc: DocFacts, rules: Rules) -> Decision:
    """Metadata-stage decision (research doc §5.3), deny-first.

    The safety deny list decides on the heading (number, title, subjects): that is what names a
    document's primary content. A deny phrase only in an abstract holds the document for review.
    An owner ``allow_documents`` decision does not lift a deny-core hit on the heading (a future
    edition of an allowed publication may be retitled into the core); ``override_safety`` does.
    """
    pub = normalize_pub_number(doc.pub_number) if doc.pub_number else ""
    keys = {pub, doc.key, normalize_pub_number(doc.key)} - {""}
    if keys & rules.deny_documents:
        return Decision(Verdict.DENY, "owner=deny")
    for field_text in (doc.rights, doc.title):
        if not field_text:
            continue
        marking = parse_markings(field_text)
        gate = check_distribution(marking, (), declassified_ok=rules.declassified_ok)
        if gate.verdict is Verdict.DENY:
            return replace(gate, stage=Stage.METADATA)
    owner_allowed = bool(keys & rules.allow_documents)
    overridden = bool(keys & rules.override_safety)
    heading = Screened.of(doc.heading, rules.neutral_phrases)
    text = Screened.of(doc.text, rules.neutral_phrases)
    if not overridden and (hits := rules.deny_phrases.hits(heading)):
        return Decision(Verdict.DENY, f"safety={hits[0]}")
    if not (owner_allowed or overridden) and (hits := rules.deny_phrases.hits(text)):
        return Decision(Verdict.REVIEW, f"safety-abstract={hits[0]}")
    if hit := _series_matches(pub, rules.deny_series, rules.deny_series_prefix):
        return Decision(Verdict.DENY, f"deny-series={hit.strip()}")
    if not owner_allowed and (hits := phrase_list(rules.deny_keywords).hits(text)):
        return Decision(Verdict.DENY, f"keyword={hits[0]}")
    if owner_allowed:
        return Decision(Verdict.ALLOW, "owner=allow")
    builtin = rules.safety and rules.builtin_review
    review = (*SAFETY_REVIEW, *rules.review_keywords) if builtin else rules.review_keywords
    if hits := phrase_list(review).hits(text):
        return Decision(Verdict.REVIEW, f"keyword={hits[0]}")
    if hit := _series_matches(pub, rules.allow_series, rules.allow_series_prefix):
        return Decision(Verdict.ALLOW, f"series={hit.strip()}")
    if rules.allow_all:
        return Decision(Verdict.ALLOW, "collection=allow-all")
    return Decision(rules.default, "default")


# --- markings (front matter) -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Marking:
    """Distribution and handling markings found in a document's front matter or rights field."""

    statement: str | None = None  # the most restrictive "A".."F" of every "DISTRIBUTION STATEMENT X"
    public_release: bool = False  # "approved for public release" (distribution unlimited), not negated
    restricted: tuple[str, ...] = ()  # FOUO, CUI, classification banners, limited distribution ...
    export_control: tuple[str, ...] = ()  # export warnings ("subject to the ITAR", "whose export is ...")
    released: bool = False  # a declassification / FOIA release stamp (not a bare "declassified")
    text: str = ""  # the verbatim distribution sentence, for audit
    public_domain: bool = False  # "public domain" / "not subject to copyright" (not negated)
    # The normalized text the marking was read from, kept to test ``license:<SPDX>`` requirements.
    screened: str = field(default="", repr=False, compare=False)
    # Restrictive markings on a page that carries no release stamp: what a stamp cannot lift.
    unreleased: tuple[str, ...] = ()
    # Export-control regimes the text only *mentions* (a CRS report about the EAR): held for review.
    export_mentions: tuple[str, ...] = ()

    @property
    def distribution(self) -> str:
        if self.statement:
            return self.statement
        if self.public_release:
            return "public-release"
        return "PD" if self.public_domain else ""

    def has_license(self, spdx: str) -> bool:
        """The front matter states licence ``spdx`` (see :func:`license_pattern`)."""
        return license_pattern(spdx).search(self.screened) is not None


_I = re.IGNORECASE
_STATEMENT_RE = re.compile(r"distribution\s+statement\s*[:\-]?\s*['\"\u2018\u201c]?([A-F])\b", _I)
_PUBLIC_RE = re.compile(
    r"approved\s+for\s+public\s+release[\s;,.:]*(?:its\s+)?(?:distribution\s+(?:is\s+)?unlimited)?", _I
)
# Every restrictive pattern is case-insensitive unless the marking is only a marking in capitals
# (a "CUI" line, "NOFORN", "OFFICIAL-SENSITIVE" at the start of a line): in running text the same
# words are guidance *about* the scheme.
_RESTRICTED: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "distribution-limited",
        re.compile(r"distribution\s+(?:is\s+)?(?:authori[sz]ed|limited|restricted)\s+to\b", _I),
    ),
    (
        "fouo",
        re.compile(
            r"\bfor\s+official\s+(?:government\s+)?use\s+only\b|\(\s*(?:u\s*//\s*)?fouo\s*\)|\bfouo\b", _I
        ),
    ),
    ("government-use-only", re.compile(r"\bfor\s+(?:u\.?\s?s\.?\s+)?government\s+use\s+only\b", _I)),
    (
        "cui",
        re.compile(
            r"\bCONTROLLED UNCLASSIFIED INFORMATION\b|(?m:^\s*CUI\s*(?://.*)?$)|\bCUI\s*//|"
            r"(?i:\(\s*(?:u\s*//\s*)?cui\s*\))"
        ),
    ),
    (
        "not-for-public-release",
        re.compile(
            r"not\s+(?:yet\s+)?(?:been\s+)?(?:approved\s+|cleared\s+)?for\s+public\s+release|"
            r"pending\s+(?:approval\s+for\s+)?public\s+release|not\s+releasable\s+to\s+the\s+public",
            _I,
        ),
    ),
    ("limited-distribution", re.compile(r"\bLIMITED DISTRIBUTION\b|\bLIMDIS\b")),
    ("law-enforcement-sensitive", re.compile(r"law\s+enforcement\s+sensitive", _I)),
    ("sbu", re.compile(r"(?i:\(\s*(?:u\s*//\s*)?sbu\s*\))|\bSBU\s*//|(?m:^\s*SENSITIVE BUT UNCLASSIFIED\b)")),
    ("noforn", re.compile(r"\bNOFORN\b")),
    ("mod-use-only", re.compile(r"for\s+uk\s+government\s+and\s+mod\s+use\s+only", _I)),
    # UK Government Security Classifications: the handling caveat as a banner (a line that starts
    # with it, or with its descriptor), and the old RESTRICTED level with a national prefix.
    (
        "official-sensitive",
        re.compile(
            r"(?m:^[ \t]*OFFICIAL[ \t]*-?[ \t]*SENSITIVE\b)|\bOFFICIAL[ \t]*-?[ \t]*SENSITIVE\s*(?::|//)"
        ),
    ),
    ("restricted", re.compile(r"\b(?:NATO|UK|EU)\s+RESTRICTED\b")),
    # "SECRET//NOFORN" anywhere; a bare "SECRET" line only at a page edge (_edge_banner): guidance
    # *about* classification ("Government Security Classifications") has SECRET as a heading.
    ("classified", re.compile(r"\b(?:TOP SECRET|SECRET|CONFIDENTIAL)\s*//")),
    # portion markings in titles, as index pages print them: "(S/NF)", "(S//NF)", "(C/REL)", "(TS)"
    ("classified-portion", re.compile(r"\((?:TS|S|C)\s*/{1,2}\s*(?:NF|NOFORN|REL[^)]*)\)|\(TS\)", _I)),
)
# Export control. A *warning* printed on a document restricts it (deny, alert); a document that only
# *names* a regime (a CRS overview of the EAR, NIST guidance on export-controlled data) is held.
_EXPORT: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "itar",
        re.compile(
            r"subject\s+to\s+(?:the\s+)?(?:international\s+traffic\s+in\s+arms\s+regulations?|itar)\b", _I
        ),
    ),
    (
        "ear",
        re.compile(
            r"subject\s+to\s+(?:the\s+)?(?:u\.?\s?s\.?\s+)?(?:export\s+administration\s+regulations?|ear)\b|"
            r"\bEAR99\b|\bECCN\s*:?\s*\d[A-E]\d{3}\b",
            _I,
        ),
    ),
    (
        "export-warning",
        re.compile(
            r"(?:contains|includes)\s+technical\s+data\s+whose\s+export|"
            r"export\s+of\s+this\s+(?:document|information|data|publication|report)\s+is\s+"
            r"(?:restricted|controlled|prohibited)|"
            r"this\s+(?:document|publication|report)\s+(?:is|contains)\s+(?:\w+\s+){0,2}export[\s-]+controlled",
            _I,
        ),
    ),
)
_EXPORT_MENTIONS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("itar", re.compile(r"\bITAR\b|international\s+traffic\s+in\s+arms\s+regulations?", _I)),
    ("ear", re.compile(r"export\s+administration\s+regulations?", _I)),
    ("arms-export-control-act", re.compile(r"arms\s+export\s+control\s+act", _I)),
    ("export-controlled", re.compile(r"\bexport[\s-]+controlled\b", _I)),
)
# A release *stamp* (CA-6): the CIA CREST footer, a sanitized-copy stamp, a NARA or E.O.
# declassification stamp, a FOIA release line. A bare "declassified" ("will not be declassified",
# "a declassified cable of 1962") is not one.
_RELEASE_RE = re.compile(
    r"approved\s+for\s+release\s*:?\s*\d{4}\s*/\s*\d{1,2}\s*/\s*\d{1,2}|"
    r"sanitized\s+copy\s+approved\s+for\s+release|"
    r"declassified\s*(?:and\s+approved\s+for\s+release|under\s+(?:the\s+)?authority|per\s+e\.?\s*o\.?|"
    r"by\s+authority|[:\-]?\s*authority\b)|"
    r"released\s+under\s+the\s+(?:freedom\s+of\s+information\s+act|foia)",
    _I,
)


_PD_RE = re.compile(
    r"\bpublic\s+domain\b|\bnot\s+subject\s+to\s+(?:u\.?\s?s\.?\s+)?copyright\b|"
    r"\bno\s+copyright\s+(?:is\s+)?claimed\b",
    _I,
)
_NEGATION_RE = re.compile(r"\b(?:not|never|no\s+longer)\b(?:\s+\w+){0,3}\s*$", _I)
# Before "approved for public release": "has not (yet) been", "pending", "prior to", "before".
_RELEASE_NEGATION_RE = re.compile(
    r"\b(?:not|never|no\s+longer|pending|awaiting|prior\s+to|before|until)\b(?:\s+\w+){0,3}\s*$", _I
)
_CC_PARTS = {"BY": "attribution", "NC": "non-?commercial", "SA": "share-?alike", "ND": "no-?deriv\\w*"}
_LICENSE_NAMES: dict[str, str] = {
    "OGL-UK-3.0": r"open\s+government\s+licen[cs]e(?:\s+for\s+public\s+sector\s+information)?[\s,]*"
    r"v(?:ersion)?\.?\s*3(?:\.0)?\b|open-government-licen[cs]e/version/3\b",
}


@functools.lru_cache(maxsize=64)
def license_pattern(spdx: str) -> re.Pattern[str]:
    """How front matter states licence ``spdx``: its SPDX id with any separators, a Creative Commons
    deed URL or long name, or a known name (``Open Government Licence v3.0``)."""
    tokens = [t for t in re.split(r"[\s\-_]+", spdx.strip()) if t]
    sep = r"[\s\-_/]*"
    alts = [r"(?<![\w.])" + sep.join(re.escape(t) for t in tokens) + r"(?!\w|\.\d)"]
    if tokens and tokens[0].upper() == "CC" and len(tokens) > 1:
        rest = tokens[1:]
        alts.append(
            r"creativecommons\.org/licenses/" + "[-/]".join(re.escape(t.lower()) for t in rest) + r"\b"
        )
        long = [_CC_PARTS.get(t.upper(), re.escape(t)) for t in rest]
        alts.append(r"creative\s+commons\s+" + r"[\s\-]*".join(long) + r"\b")
    if spdx in _LICENSE_NAMES:
        alts.append(_LICENSE_NAMES[spdx])
    return re.compile("|".join(f"(?:{a})" for a in alts), _I)


def _unnegated(
    rx: re.Pattern[str], text: str, negation: re.Pattern[str] = _NEGATION_RE
) -> list[re.Match[str]]:
    """Matches of ``rx`` whose preceding 40 characters do not negate them ("has not been ...")."""
    return [m for m in rx.finditer(text) if not negation.search(text[max(0, m.start() - 40) : m.start()])]


def _public_domain(text: str) -> bool:
    """A public-domain statement that is not negated ("is not in the public domain")."""
    return bool(_unnegated(_PD_RE, text))


_BARE_BANNER_RE = re.compile(r"(?:TOP SECRET|SECRET|CONFIDENTIAL)(?:\s*//.*)?")
# "RESTRICTED" opening a page edge line, followed by nothing or by ordinary text: a banner. Followed
# by another word in capitals ("RESTRICTED AREA") it is a sign, not a marking.
_RESTRICTED_EDGE_RE = re.compile(r"RESTRICTED(?:\s*$|\s+(?![A-Z]{2,}\b))")
_LETTER_SPACED_RE = re.compile(r"(?:\w[ \t]{1,3}){2,}\w")


def _banner_line(line: str) -> str | None:
    """The marking a page-edge line is (``classified`` / ``restricted``), or ``None``."""
    text = line.strip()
    if _LETTER_SPACED_RE.fullmatch(text):  # "S E C R E T"
        text = re.sub(r"\s+", "", text)
    if _BARE_BANNER_RE.fullmatch(text):
        return "classified"
    if _RESTRICTED_EDGE_RE.match(text):
        return "restricted"
    return None


def _edge_banners(page: str) -> list[str]:
    """Bare banners as the first or last line of a page (pages split on form feed, as
    :func:`wsp_adapters.doccollection.adapter.pdf_text` joins them): where banners are printed."""
    lines = [ln for ln in page.splitlines() if ln.strip()]
    if not lines:
        return []
    found = (_banner_line(lines[0]), _banner_line(lines[-1]))
    return [name for name in dict.fromkeys(found) if name is not None]


def _page_restrictions(page: str) -> tuple[list[str], bool]:
    """``(restrictive markings, carries a release stamp)`` for one page of normalized text."""
    words, _squeezed = screen_variants(page)
    names = [name for name, rx in _RESTRICTED if rx.search(words)]
    names += [b for b in _edge_banners(page) if b not in names]
    stamped = bool(_unnegated(_RELEASE_RE, words))
    return names, stamped


def parse_markings(text: str) -> Marking:
    """Find distribution, handling and export markings in front-matter text (deterministic).

    The text is normalized first, and its joined and de-spaced copies are read as well
    (:func:`screen_variants`). Every ``DISTRIBUTION STATEMENT`` is read and the most restrictive
    one wins: a page that says "A" and later "D" is a "D" document. Restrictive markings are read
    page by page (form feeds), so a release stamp lifts only the banners on its own page.
    """
    normalized = normalize_text(text)
    words, _squeezed = screen_variants(normalized)
    statements = sorted({m.group(1).upper() for m in _STATEMENT_RE.finditer(words)})
    statement = statements[-1] if statements else None  # "A" < "B" < ... < "F" (most restrictive)
    excerpt = ""
    if statement is not None:
        first = next(m for m in _STATEMENT_RE.finditer(words) if m.group(1).upper() == statement)
        excerpt = _sentence(words, first.start())
    publics = _unnegated(_PUBLIC_RE, words, _RELEASE_NEGATION_RE)
    public = publics[0] if publics else None
    if public and not excerpt:
        excerpt = _sentence(words, public.start())
    restricted: dict[str, None] = {}
    unreleased: dict[str, None] = {}
    released = False
    for page in normalized.split("\f"):
        names, stamped = _page_restrictions(page)
        restricted.update(dict.fromkeys(names))
        released = released or stamped
        if not stamped:
            unreleased.update(dict.fromkeys(names))
    export = tuple(name for name, rx in _EXPORT if rx.search(words))
    mentions = tuple(name for name, rx in _EXPORT_MENTIONS if rx.search(words)) if not export else ()
    if statement is None and public is not None and "distribution-limited" not in restricted:
        statement = "A" if re.search(r"unlimited", public.group(0), _I) else None
    return Marking(
        statement=statement,
        public_release=public is not None,
        restricted=tuple(restricted),
        export_control=export,
        released=released,
        text=excerpt,
        public_domain=_public_domain(words),
        screened=words,
        unreleased=tuple(unreleased),
        export_mentions=mentions,
    )


def _sentence(text: str, at: int) -> str:
    start = max(text.rfind("\n", 0, at), text.rfind(". ", 0, at) + 1, 0)
    end_candidates = [i for i in (text.find("\n\n", at), text.find(". ", at + 30)) if i != -1]
    end = min(end_candidates) + 1 if end_candidates else min(len(text), at + 300)
    return re.sub(r"\s+", " ", text[start:end]).strip()[:300]


def check_distribution(
    marking: Marking, require: Sequence[str], *, declassified_ok: bool = False
) -> Decision:
    """The distribution gate (research doc §3.1, §5.2 ``verify`` step 2).

    Restrictive markings deny. Otherwise every configured requirement is a marking the front
    matter must actually carry (any one of them suffices): ``A`` a Distribution Statement A,
    ``public-release`` an "approved for public release" statement, ``PD`` a public-domain
    statement, ``license:<SPDX>`` that licence named, ``declassified`` a release stamp. A
    requirement that is not met holds the document for review; nothing is allowed on trust.

    ``declassified_ok`` (FOIA and declassified collections) lifts the restrictive markings on pages
    that carry a release stamp, never those on other pages. A document that only names an export
    regime is held for review.

    ``published`` is the registry's explicit statement that the issuer posted the document openly (its
    public site, or a verified one-to-one mirror of its release), which is the release: no printed
    statement is needed (owner decision 2026-10-11; agency web PDFs rarely print one). Listed after the
    marking requirements, so a printed statement still names the decision. The restrictive screens
    above apply first either way.
    """
    st = Stage.DISTRIBUTION
    if marking.export_control:
        return Decision(Verdict.DENY, f"marking=export-control:{marking.export_control[0]}", st, alert=True)
    if marking.statement in {"B", "C", "D", "E", "F"}:
        return Decision(Verdict.DENY, f"marking=distribution-{marking.statement}", st, alert=True)
    restricted = marking.unreleased if declassified_ok else marking.restricted
    if restricted:
        return Decision(Verdict.DENY, f"marking={restricted[0]}", st, alert=True)
    if marking.export_mentions:
        return Decision(Verdict.REVIEW, f"marking=export-mention:{marking.export_mentions[0]}", st)
    if not require:
        return Decision(Verdict.ALLOW, "marking=none-restrictive", st)
    for req in require:
        if req.upper() == "A" and marking.statement == "A":
            return Decision(Verdict.ALLOW, "marking=distribution-A", st)
        if req == "public-release" and marking.public_release:
            return Decision(Verdict.ALLOW, "marking=public-release", st)
        if req == "PD" and marking.public_domain:
            return Decision(Verdict.ALLOW, "marking=PD", st)
        if req.startswith("license:") and marking.has_license(req.removeprefix("license:")):
            return Decision(Verdict.ALLOW, f"marking={req}", st)
        if req == "declassified" and marking.released:
            return Decision(Verdict.ALLOW, "marking=declassified", st)
        if req == "published":
            return Decision(Verdict.ALLOW, "marking=published-by-issuer", st)
    return Decision(Verdict.REVIEW, f"marking=missing-{'|'.join(require)}", st)


def classify_content(
    front_matter: str, rules: Rules, *, title_page_chars: int = 3000, overridden: bool = False
) -> Decision:
    """Content-stage screen over the front matter (title page, contents, preface).

    A deny-core pattern on the title page means the document's primary content is out of scope:
    deny. So do a core hit anywhere together with a history-prone noun (:data:`SAFETY_CONTEXT`) on
    the title page, or three distinct hits in the front matter of which at least one is a core hit.
    Fewer: review. History-prone nouns alone decide nothing (a situation report that mentions booby
    traps, a history of minelaying). ``overridden``: the owner's ``override_safety`` for this document.
    """
    st = Stage.CONTENT
    normalized = normalize_text(front_matter)
    title = Screened.of(normalized[:title_page_chars], rules.neutral_phrases)
    full = Screened.of(normalized, rules.neutral_phrases)
    if not overridden:
        deny, context = rules.deny_phrases, rules.context_phrases
        title_hits, all_hits = deny.hits(title), deny.hits(full)
        if title_hits:
            return Decision(Verdict.DENY, f"safety={title_hits[0]}", st)
        if all_hits and (title_context := context.hits(title)):
            return Decision(Verdict.DENY, f"safety={title_context[0]}+{all_hits[0]}", st)
        combined = [*all_hits, *(h for h in context.hits(full) if h not in all_hits)]
        if all_hits and len(combined) >= 3:
            return Decision(Verdict.DENY, f"safety={'+'.join(combined[:3])}", st)
    else:
        all_hits = []
    source_deny = phrase_list(rules.deny_keywords).hits(title)
    if source_deny:
        return Decision(Verdict.DENY, f"keyword={source_deny[0]}", st)
    if all_hits:
        return Decision(Verdict.REVIEW, f"safety={all_hits[0]}", st)
    return Decision(Verdict.ALLOW, "content=clean", st)


# --- currency (research doc §2.2, §2.3) ----------------------------------------------------------


class Currency(StrEnum):
    CURRENT = "current"
    SUPERSEDED = "superseded"
    HISTORICAL = "historical"
    RESCINDED = "rescinded"
    WITHDRAWN = "withdrawn-from-index"


@dataclass(frozen=True, slots=True)
class CurrencyRules:
    collection: Currency | None = None  # e.g. every Farmers' Bulletin is historical
    historical_before: str | None = None  # ISO date: older editions are historical
    historical_series_prefix: tuple[str, ...] = ()
    # Editions of one publication that carry no number: each pattern's groups, matched against the
    # title, name the series ("2026-2030" + "Technician" for every release of that question pool), so
    # an older release is labelled superseded (field exam 2026-10-14). Doc keys are not changed.
    series: tuple[re.Pattern[str], ...] = ()

    @staticmethod
    def from_access(acc: Mapping[str, Any]) -> CurrencyRules:
        cur: object = acc.get("currency")
        block: Mapping[str, Any] = cast(Mapping[str, Any], cur) if isinstance(cur, Mapping) else {}
        collection: object = cur if isinstance(cur, str) else block.get("collection")
        return CurrencyRules(
            collection=Currency(str(collection)) if collection else None,
            historical_before=str(block["historical_before"]) if block.get("historical_before") else None,
            historical_series_prefix=tuple(s.upper() for s in _strs(block.get("historical_series_prefix"))),
            series=tuple(re.compile(p, re.IGNORECASE) for p in _strs(block.get("series"))),
        )


@dataclass(frozen=True, slots=True)
class Edition:
    key: str
    pub_number: str
    edition_date: str  # ISO date or "" when unknown
    fetched_at: str = ""  # when the library fetched it (ISO timestamp): places an undated edition
    title: str = ""  # matched by ``CurrencyRules.series``


_MONTHS = {
    m: i
    for i, names in enumerate(
        (
            ("jan", "january"),
            ("feb", "february"),
            ("mar", "march"),
            ("apr", "april"),
            ("may",),
            ("jun", "june"),
            ("jul", "july"),
            ("aug", "august"),
            ("sep", "sept", "september"),
            ("oct", "october"),
            ("nov", "november"),
            ("dec", "december"),
        ),
        start=1,
    )
    for m in names
}
_NAMED_DATE = re.compile(r"\b([A-Za-z]{3,9})\.?\s+(\d{1,2}),?\s+((?:19|20)\d\d)\b")


def title_date(title: str) -> str:
    """The last "Feb 19 2026" / "December 18, 2025" date in a title as ``YYYY-MM-DD``, else ``""``: the
    release date of a series edition, for ordering only (edition dates are part of doc keys)."""
    found = ""
    for m in _NAMED_DATE.finditer(title):
        month = _MONTHS.get(m.group(1).lower())
        if month is None:
            continue
        try:
            found = date(int(m.group(3)), month, int(m.group(2))).isoformat()
        except ValueError:
            continue
    return found


def series_of(title: str, rules: CurrencyRules) -> str:
    """The series a numberless edition belongs to (``CurrencyRules.series``), else ``""``."""
    for rx in rules.series:
        if m := rx.search(title):
            parts = [g.strip().lower() for g in m.groups() if g] or [m.group(0).strip().lower()]
            return "series:" + " ".join(parts)
    return ""


def edition_order(editions: Sequence[Edition]) -> list[Edition]:
    """Oldest first. Dated editions go by date. An undated edition is newer than every dated one the
    library fetched no later than it, and older than those fetched after it (2026-10-12 audit, CA-19):
    an issuer's newest listing often has no date, and a 2014 edition must not supersede it."""
    ordered = sorted(
        (e for e in editions if e.edition_date), key=lambda e: (e.edition_date, e.fetched_at, e.key)
    )
    for undated in sorted((e for e in editions if not e.edition_date), key=lambda e: (e.fetched_at, e.key)):
        at = 0
        for i, ed in enumerate(ordered):
            if ed.fetched_at <= undated.fetched_at:
                at = i + 1
        ordered.insert(at, undated)
    return ordered


@dataclass(frozen=True, slots=True)
class CurrencyLabel:
    currency: Currency
    historical: bool = False
    supersedes: str | None = None
    superseded_by: str | None = None
    banner: str | None = None
    meta: Mapping[str, str] = field(default_factory=dict[str, str])


def assign_currency(editions: Sequence[Edition], rules: CurrencyRules) -> dict[str, CurrencyLabel]:
    """Label every edition: newest of a publication number is current, older ones superseded.

    Superseded editions are kept (history), each pointing at its immediate successor. Historical
    rules (whole collection, date cutoff, series) override ``current`` and set the banner flag.
    """
    groups: dict[str, list[Edition]] = {}
    for ed in editions:
        series = "" if ed.pub_number else series_of(ed.title, rules)
        if series:  # ordered by the release date the title names (dated editions sort first)
            ed = replace(ed, edition_date=title_date(ed.title) or ed.edition_date)
        groups.setdefault(normalize_pub_number(ed.pub_number) or series or ed.key, []).append(ed)
    out: dict[str, CurrencyLabel] = {}
    for pub, eds in groups.items():
        ordered = edition_order(eds)
        for i, ed in enumerate(ordered):
            newer = ordered[i + 1] if i + 1 < len(ordered) else None
            older = ordered[i - 1] if i > 0 else None
            currency = Currency.SUPERSEDED if newer is not None else Currency.CURRENT
            historical = (
                rules.collection is Currency.HISTORICAL
                or bool(
                    rules.historical_before and ed.edition_date and ed.edition_date < rules.historical_before
                )
                or any(pub.startswith(p) for p in rules.historical_series_prefix)
            )
            if historical and currency is Currency.CURRENT:
                currency = Currency.HISTORICAL
            banner = HISTORICAL_BANNER if historical else None
            if currency is Currency.SUPERSEDED and newer is not None and not historical:
                when = f" ({newer.edition_date})" if newer.edition_date else ""
                banner = f"Superseded by {newer.pub_number or newer.title or newer.key}{when}."
            out[ed.key] = CurrencyLabel(
                currency=currency,
                historical=historical,
                supersedes=older.key if older else None,
                superseded_by=newer.key if newer else None,
                banner=banner,
            )
    return out
