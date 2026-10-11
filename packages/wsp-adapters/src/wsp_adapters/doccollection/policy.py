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
CBRN production or weaponisation, explosives and improvised munitions, weapon manufacture,
weapons guidance/targeting/fire control, and offensive tooling. They are phrase lists, matched on
word boundaries after neutral phrases (``bomb threat``, ``nuclear power``) are blanked out, so a
defensive or civil document is not caught by a bare noun. Bare nouns (``explosive``, ``weapon``,
``chemical``) go to review instead.
"""

from __future__ import annotations

import functools
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
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
    rule: str  # e.g. "series=FM 5-0", "safety=targeting", "marking=distribution-C"
    stage: Stage = Stage.METADATA
    alert: bool = False  # an index listed a restricted document: that is news for the owner

    def __str__(self) -> str:
        return f"{self.verdict.value}:{self.rule}"


# --- built-in safety lists (PLAN §13; research doc §3.2) ------------------------------------------

SAFETY_DENY: tuple[str, ...] = (
    # CBRN production, enhancement, dissemination, weaponisation
    "chemical agent production",
    "chemical weapons production",
    "chemical weapon production",
    "nerve agent synthesis",
    "synthesis of nerve agents",
    "biological weapons production",
    "biological weapon production",
    "weaponization",
    "weaponisation",
    "weaponizing",
    "weaponising",
    "agent dissemination",
    "nuclear weapon design",
    "nuclear weapons design",
    "warhead design",
    # explosives, improvised munitions, weapon manufacture and modification
    "explosives manufacture",
    "manufacture of explosives",
    "explosive formulation",
    "explosive formulations",
    "improvised munitions",
    "improvised weapons",
    "weapon manufacture",
    "weapons manufacture",
    "firearm manufacture",
    "firearms manufacture",
    "gunsmithing",
    "ammunition reloading",
    "munitions manufacture",
    "manufacture of munitions",
    "munitions effectiveness",
    "munition effectiveness",
    "demolitions",
    "booby traps",
    "mine laying",
    "minelaying",
    "mine emplacement",
    "emplacing mines",
    "marksmanship",
    "sniper",
    "sniping",
    "gunnery",
    # weapons guidance, targeting, fire control, effects for planning attacks
    "military targeting",
    "joint targeting",
    "targeting process",
    "targeting cycle",
    "target development",
    "lethal targeting",
    "target acquisition",
    "weaponeering",
    "fire control",
    "fire direction",
    "missile guidance",
    "weapons guidance",
    "kill chain",
    "weapons employment",
    "weapon employment",
    "weapons effects",
    # offensive tooling against computer systems or people
    "exploit development",
    "exploit code",
    "malware development",
    "offensive cyberspace operations",
    "cyberspace attack",
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


def _squeeze(phrase: str) -> str:
    return re.sub(r"[\s\-]+", "", phrase)


@dataclass(frozen=True, slots=True)
class PhraseList:
    """A phrase list compiled for matching: word-bounded on text, boundary-free on squeezed runs."""

    words: re.Pattern[str] | None
    loose: re.Pattern[str] | None
    names: Mapping[str, str]  # squeezed phrase -> phrase (to report a squeezed hit by its name)

    def hits(self, text: Screened) -> list[str]:
        """Distinct phrases found (casefolded, single-spaced), in order of first appearance."""
        seen: dict[str, None] = {}
        if self.words is not None:
            for m in self.words.finditer(text.words):
                seen.setdefault(re.sub(r"[\s\-]+", " ", m.group(0).casefold()))
        if self.loose is not None and text.squeezed:
            for m in self.loose.finditer(text.squeezed):
                seen.setdefault(self.names[m.group(0).casefold()])
        return list(seen)


@functools.lru_cache(maxsize=512)
def phrase_list(phrases: tuple[str, ...]) -> PhraseList:
    items = sorted(
        {normalize_text(p).casefold().strip() for p in phrases if p.strip()}, key=len, reverse=True
    )
    if not items:
        return PhraseList(None, None, {})
    body = "|".join(re.escape(p).replace(r"\ ", r"[\s\-]+") for p in items)
    words = re.compile(rf"(?<![\w-])(?:{body})(?![\w-])", re.IGNORECASE)
    names = {_squeeze(p): re.sub(r"[\s\-]+", " ", p) for p in items}
    loose = re.compile("|".join(re.escape(k) for k in sorted(names, key=len, reverse=True)), re.IGNORECASE)
    return PhraseList(words, loose, names)


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
        return phrase_list(SAFETY_DENY if self.safety else ())

    def merged(self, other: Mapping[str, Any]) -> Rules:
        """These rules plus a mapping (registry filter block or rules file); lists are unioned."""
        return replace(self, **_rules_kwargs(other, base=self))


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
    out: dict[str, Any] = {}
    sets = ("allow_series", "deny_series", "allow_documents", "deny_documents")
    for key in sets:
        if key in data:
            norm = {normalize_pub_number(s) for s in _strs(data[key])}
            out[key] = frozenset(getattr(base, key) | norm)
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
        out["require"] = _strs(cast(Mapping[str, object], dist).get("require"))
    return out


def load_rules(filter_block: Mapping[str, Any] | None, *, repo_root: Path | None = None) -> Rules:
    """Rules for one source: the registry ``access.filter`` block plus its ``rules:`` file, if any.

    The rules file (``registry/filters/<source>.yaml``) holds the long series and keyword lists and
    the owner's per-document decisions, reviewed in pull requests like the registry itself.
    """
    rules = Rules()
    block = dict(filter_block or {})
    rules_file = block.pop("rules", None)
    if rules_file:
        path = Path(str(rules_file))
        if not path.is_absolute() and repo_root is not None:
            path = repo_root / path
        if path.is_file():
            data: object = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            if not isinstance(data, Mapping):
                raise ValueError(f"{path}: expected a mapping")
            rules = rules.merged(cast(Mapping[str, Any], data))
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
    heading = Screened.of(doc.heading, rules.neutral_phrases)
    text = Screened.of(doc.text, rules.neutral_phrases)
    if not owner_allowed and (hits := rules.deny_phrases.hits(heading)):
        return Decision(Verdict.DENY, f"safety={hits[0]}")
    if not owner_allowed and (hits := rules.deny_phrases.hits(text)):
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
    public_release: bool = False  # "approved for public release" (distribution unlimited)
    restricted: tuple[str, ...] = ()  # FOUO, CUI, classification banners, limited distribution ...
    export_control: tuple[str, ...] = ()  # ITAR, EAR, export warnings
    released: bool = False  # declassification / FOIA release stamp
    text: str = ""  # the verbatim distribution sentence, for audit
    public_domain: bool = False  # "public domain" / "not subject to copyright" (not negated)
    # The normalized text the marking was read from, kept to test ``license:<SPDX>`` requirements.
    screened: str = field(default="", repr=False, compare=False)

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
_STATEMENT_RE = re.compile(r"distribution\s+statement\s*[:\-]?\s*([A-F])\b", _I)
_PUBLIC_RE = re.compile(
    r"approved\s+for\s+public\s+release[\s;,.:]*(?:its\s+)?(?:distribution\s+(?:is\s+)?unlimited)?", _I
)
_RESTRICTED: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "distribution-limited",
        re.compile(r"distribution\s+authori[sz]ed\s+to\s+(?:u\.?\s?s\.?|dod|department)", _I),
    ),
    ("fouo", re.compile(r"\bFOR OFFICIAL USE ONLY\b|\(FOUO\)|\bFOUO\b")),
    ("cui", re.compile(r"\bCONTROLLED UNCLASSIFIED INFORMATION\b|(?m:^\s*CUI\s*(?://.*)?$)|\bCUI//")),
    ("not-for-public-release", re.compile(r"not\s+(?:approved\s+)?for\s+public\s+release", _I)),
    ("limited-distribution", re.compile(r"\bLIMITED DISTRIBUTION\b|\bLIMDIS\b")),
    ("law-enforcement-sensitive", re.compile(r"law\s+enforcement\s+sensitive", _I)),
    ("noforn", re.compile(r"\bNOFORN\b")),
    ("mod-use-only", re.compile(r"for\s+uk\s+government\s+and\s+mod\s+use\s+only", _I)),
    (
        "classified",
        re.compile(r"\b(?:TOP SECRET|SECRET|CONFIDENTIAL)\s*//|(?m:^\s*(?:TOP SECRET|SECRET)\s*$)"),
    ),
    # portion markings in titles, as index pages print them: "(S/NF)", "(S//NF)", "(C/REL)", "(TS)"
    ("classified-portion", re.compile(r"\((?:TS|S|C)\s*/{1,2}\s*(?:NF|NOFORN|REL[^)]*)\)|\(TS\)", _I)),
)
_EXPORT: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("itar", re.compile(r"\bITAR\b|international\s+traffic\s+in\s+arms\s+regulations?", _I)),
    ("ear", re.compile(r"export\s+administration\s+regulations?|\bEAR99\b", _I)),
    ("arms-export-control-act", re.compile(r"arms\s+export\s+control\s+act", _I)),
    ("export-warning", re.compile(r"(?:contains|includes)\s+technical\s+data\s+whose\s+export", _I)),
    ("export-controlled", re.compile(r"\bexport[\s-]+controlled\b", _I)),
)
_RELEASE_RE = re.compile(
    r"approved\s+for\s+release\s*:?\s*\d{4}|sanitized\s+copy\s+approved\s+for\s+release|"
    r"declassified\s+(?:and\s+approved\s+for\s+release|under\s+authority|per\b)|"
    r"\bdeclassified\b|released\s+under\s+the\s+(?:freedom\s+of\s+information\s+act|foia)",
    _I,
)


_PD_RE = re.compile(
    r"\bpublic\s+domain\b|\bnot\s+subject\s+to\s+(?:u\.?\s?s\.?\s+)?copyright\b|"
    r"\bno\s+copyright\s+(?:is\s+)?claimed\b",
    _I,
)
_NEGATION_RE = re.compile(r"\b(?:not|never|no\s+longer)\b(?:\s+\w+){0,3}\s*$", _I)
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


def _public_domain(text: str) -> bool:
    """A public-domain statement that is not negated ("is not in the public domain")."""
    return any(
        not _NEGATION_RE.search(text[max(0, m.start() - 40) : m.start()]) for m in _PD_RE.finditer(text)
    )


def parse_markings(text: str) -> Marking:
    """Find distribution, handling and export markings in front-matter text (deterministic).

    The text is normalized first, and its joined and de-spaced copies are read as well
    (:func:`screen_variants`). Every ``DISTRIBUTION STATEMENT`` is read and the most restrictive
    one wins: a page that says "A" and later "D" is a "D" document.
    """
    words, _squeezed = screen_variants(normalize_text(text))
    statements = sorted({m.group(1).upper() for m in _STATEMENT_RE.finditer(words)})
    statement = statements[-1] if statements else None  # "A" < "B" < ... < "F" (most restrictive)
    excerpt = ""
    if statement is not None:
        first = next(m for m in _STATEMENT_RE.finditer(words) if m.group(1).upper() == statement)
        excerpt = _sentence(words, first.start())
    public = _PUBLIC_RE.search(words)
    if public and not excerpt:
        excerpt = _sentence(words, public.start())
    restricted = tuple(name for name, rx in _RESTRICTED if rx.search(words))
    export = tuple(name for name, rx in _EXPORT if rx.search(words))
    if statement is None and public is not None and "distribution-limited" not in restricted:
        statement = "A" if re.search(r"unlimited", public.group(0), _I) else None
    return Marking(
        statement=statement,
        public_release=public is not None,
        restricted=restricted,
        export_control=export,
        released=_RELEASE_RE.search(words) is not None,
        text=excerpt,
        public_domain=_public_domain(words),
        screened=words,
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
    restricted = [r for r in marking.restricted if not (declassified_ok and marking.released)]
    if restricted:
        return Decision(Verdict.DENY, f"marking={restricted[0]}", st, alert=True)
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


def classify_content(front_matter: str, rules: Rules, *, title_page_chars: int = 3000) -> Decision:
    """Content-stage screen over the front matter (title page, contents, preface).

    A safety phrase on the title page, or three or more distinct safety phrases anywhere in the
    front matter, mean the document's primary content is out of scope: deny. Fewer hits: review.
    """
    st = Stage.CONTENT
    normalized = normalize_text(front_matter)
    title = Screened.of(normalized[:title_page_chars], rules.neutral_phrases)
    full = Screened.of(normalized, rules.neutral_phrases)
    deny = rules.deny_phrases
    title_hits = deny.hits(title)
    all_hits = deny.hits(full)
    if title_hits:
        return Decision(Verdict.DENY, f"safety={title_hits[0]}", st)
    if len(all_hits) >= 3:
        return Decision(Verdict.DENY, f"safety={'+'.join(all_hits[:3])}", st)
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

    @staticmethod
    def from_access(acc: Mapping[str, Any]) -> CurrencyRules:
        cur: object = acc.get("currency")
        block: Mapping[str, Any] = cast(Mapping[str, Any], cur) if isinstance(cur, Mapping) else {}
        collection: object = cur if isinstance(cur, str) else block.get("collection")
        return CurrencyRules(
            collection=Currency(str(collection)) if collection else None,
            historical_before=str(block["historical_before"]) if block.get("historical_before") else None,
            historical_series_prefix=tuple(s.upper() for s in _strs(block.get("historical_series_prefix"))),
        )


@dataclass(frozen=True, slots=True)
class Edition:
    key: str
    pub_number: str
    edition_date: str  # ISO date or "" when unknown


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
        groups.setdefault(normalize_pub_number(ed.pub_number) or ed.key, []).append(ed)
    out: dict[str, CurrencyLabel] = {}
    for pub, eds in groups.items():
        ordered = sorted(eds, key=lambda e: (e.edition_date, e.key))
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
                banner = f"Superseded by {newer.pub_number or newer.key}{when}."
            out[ed.key] = CurrencyLabel(
                currency=currency,
                historical=historical,
                supersedes=older.key if older else None,
                superseded_by=newer.key if newer else None,
                banner=banner,
            )
    return out
