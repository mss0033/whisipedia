"""Checks on every machine-translated segment before a reader sees it (ADR-0013, rule 3).

A segment that fails is shown in its own language with the reason, never silently. The checks are the
ones the 2026-10-24 bake-off found useful (docs/research/MT-BAKEOFF-2026-10-24.md):

* numbers: every number of the source is in the translation and none is added. Digits are compared
  across localisation (``1.234,5`` is ``1,234.5``). Numbers written as words from 2 to 90 are compared
  too ("elf Länder" must not become "twelve countries", as Gemma made it); "one" is left out, being
  every language's article;
* placeholders: the ``<xN>…</xN>`` pairs and ``⟦N⟧`` tokens that stand for the segment's links,
  formatting and pictures come back exactly once each, well nested;
* length: the translation's length is within a wide band of the source's (by language);
* sentences: no more than one sentence fewer than the source (an omission), except for Arabic;
* not translated: the output still reads as the source language, or is the source copied;
* framing: no "Here is the translation" or translator's note.
"""

# ruff: noqa: RUF001 - digits, quotes and spaces of other scripts are what the checks look for

from __future__ import annotations

import difflib
import re
import unicodedata
from dataclasses import dataclass

CJK = frozenset({"ja", "zh", "ko"})
PLACEHOLDER = re.compile(r"</?x\d+>|⟦\d+⟧")

_AR_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
# Digits grouped by spaces only from a group of one to three ("12 345 678"): "1990 300" is two numbers
# (a year, then a count), not 1990300 (2026-10-26 review #27).
_NUM = re.compile(
    r"(?<![\d⟦<x/])(?:\d{1,3}(?: \d{3}(?![\d.,]\d))+(?:[.,٫]\d+)?(?!\d)|\d+(?:[.,\u00a0\u202f'’٫٬]\d+)*)"
)
# A dotted date (9.11.1989, 09.11.89): day and year are numbers; the month becomes a name in English.
_DOT_DATE = re.compile(r"(?<![\d.,])(\d{1,2})\.(\d{1,2})\.(\d{4}|\d{2})(?!\d|[.,]\d)")
# A dotted time (10.15 Uhr) where a decimal is written with a comma: two digits after a point are
# neither a decimal nor a thousands group there.
_DOT_TIME = re.compile(r"(?<![\d.,])([01]?\d|2[0-4])\.([0-5]\d)(?!\d|[.,]\d)")
# Japanese and Chinese month numbers become month names in English; era years are optional glosses.
_CJK_SKIP = re.compile(r"\d+(?=\s*月)|(?<=明治|大正|昭和|平成|令和)\s*\d+(?=\s*年)")

# Number words 2-20 and the tens, per language. Words that are also common other words are left out
# (French "neuf" is also "new"; "un", "one" and the like are every language's article).
_TENS = (30, 40, 50, 60, 70, 80, 90)
_WORD_TEXT: dict[str, tuple[str, tuple[int, ...]]] = {
    "en": (
        "two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen "
        "seventeen eighteen nineteen twenty thirty forty fifty sixty seventy eighty ninety",
        (*range(2, 21), *_TENS),
    ),
    "de": (
        "zwei drei vier fünf sechs sieben acht neun zehn elf zwölf dreizehn vierzehn fünfzehn sechzehn "
        "siebzehn achtzehn neunzehn zwanzig dreißig vierzig fünfzig sechzig siebzig achtzig neunzig",
        (*range(2, 21), *_TENS),
    ),
    "fr": (
        "deux trois quatre cinq six sept huit dix onze douze treize quatorze quinze seize vingt trente "
        "quarante cinquante soixante",
        (2, 3, 4, 5, 6, 7, 8, 10, 11, 12, 13, 14, 15, 16, 20, 30, 40, 50, 60),
    ),
    "es": (
        "dos tres cuatro cinco seis siete ocho nueve diez once doce trece catorce quince dieciséis "
        "diecisiete dieciocho diecinueve veinte treinta cuarenta cincuenta sesenta setenta ochenta noventa",
        (*range(2, 21), *_TENS),
    ),
    "it": (
        # "sei" (you are), "otto" (the name Otto) and "venti" (winds) are left out: as words they are
        # far more often not numbers (2026-10-26 review #102)
        "due tre quattro cinque sette nove dieci undici dodici tredici quattordici quindici "
        "sedici diciassette diciotto diciannove trenta quaranta cinquanta sessanta settanta "
        "ottanta novanta",
        (2, 3, 4, 5, 7, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, *_TENS),
    ),
    "pt": (
        "dois duas três quatro cinco seis sete oito nove dez onze doze treze catorze quinze dezesseis "
        "dezessete dezoito dezenove vinte trinta quarenta cinquenta sessenta setenta oitenta noventa",
        (2, *range(2, 21), *_TENS),
    ),
    "ru": (
        "два две три четыре пять шесть семь восемь девять десять одиннадцать двенадцать тринадцать "
        "четырнадцать пятнадцать шестнадцать семнадцать восемнадцать девятнадцать двадцать тридцать "
        "сорок пятьдесят шестьдесят семьдесят восемьдесят девяносто",
        (2, *range(2, 21), *_TENS),
    ),
}


# "twenty-six" is one number, 26, not 20 and 6.
_EN_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80,
            "ninety": 90}  # fmt: skip
_EN_UNITS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
             "nine": 9}  # fmt: skip
_EN_COMPOUND = re.compile(rf"\b({'|'.join(_EN_TENS)})[- ]({'|'.join(_EN_UNITS)})\b", re.IGNORECASE)


def _en_compound(m: re.Match[str]) -> str:
    return str(_EN_TENS[m.group(1).lower()] + _EN_UNITS[m.group(2).lower()])


def _table(words: str, values: tuple[int, ...]) -> dict[str, int]:
    names = words.split()
    if len(names) != len(values):
        raise ValueError(f"number words and values differ: {len(names)} != {len(values)}")
    return dict(zip(names, values, strict=True))


_WORDS = {lang: _table(*pair) for lang, pair in _WORD_TEXT.items()}

# Target characters per source character: generous, catching only gross omission or invention.
_LENGTH_BAND = {
    "de": (0.6, 1.5), "fr": (0.6, 1.4), "es": (0.6, 1.4), "it": (0.6, 1.45), "pt": (0.6, 1.45),
    "ru": (0.65, 1.7), "pl": (0.6, 1.6), "ar": (0.75, 2.3), "ja": (1.2, 5.0), "zh": (1.6, 7.0),
    "ko": (1.0, 4.5),
}  # fmt: skip
_DEFAULT_BAND = (0.5, 2.0)

_SCRIPT = {"ru": "[Ѐ-ӿ]", "uk": "[Ѐ-ӿ]", "ja": "[぀-ヿ一-鿿]", "zh": "[一-鿿]", "ar": "[؀-ۿ]", "ko": "[가-힯]"}
_STOP = {
    "de": r"\b(der|die|das|und|ist|nicht|mit|von|den|dem|des|wird|sich|auf|für)\b",
    "fr": r"\b(le|la|les|des|est|et|du|une|dans|pour|qui|que|sur|avec)\b",
    "es": r"\b(el|los|las|del|es|una|que|por|para|con|como|su|sus|se)\b",
    "it": r"\b(il|lo|gli|della|che|è|per|con|una|sono|nel|alla)\b",
    "pt": r"\b(os|das|dos|que|uma|para|com|não|por|mais|como|são)\b",
}
# "The translation of the Bible ..." is a sentence, not a frame: a frame is followed by a colon or a
# line break ("Here is the translation:", "English translation:\n") (2026-10-26 review #107).
_FRAMING = re.compile(
    r"^(here is|here's|sure[,!]|the translation|english translation)[^.\n]{0,40}(:|\n)|^translation:|"
    r"\n\s*(note|notes|translator'?s note|explanation)\s*:|\(note:|\[note",
    re.IGNORECASE,
)
_ABBR = (  # a full stop after an abbreviation or an initial does not end a sentence
    r"(?<!\b[A-ZÄÖÜa-zäöü]\.)(?<!\bz\.B\.)(?<!\bbzw\.)(?<!\bca\.)(?<!\bvgl\.)"
    r"(?<!\bNr\.)(?<!\bSt\.)(?<!\bDr\.)(?<!\betc\.)"
)


@dataclass(frozen=True, slots=True)
class Verdict:
    ok: bool
    reason: str | None = None  # why the segment is not shown translated


# Magnitude words after a number: "1400万" is "14 million" (2026-10-24: the digits alone, 1400 and
# 14, differed). Each number's value with its magnitude is one of its keys.
_MAGNITUDE = {
    "thousand": 1e3, "million": 1e6, "millions": 1e6, "billion": 1e9, "billions": 1e9, "trillion": 1e12,
    "tausend": 1e3, "mio": 1e6, "millionen": 1e6, "milliarde": 1e9, "milliarden": 1e9,
    "mrd": 1e9, "mille": 1e3, "milliard": 1e9, "milliards": 1e9, "mil": 1e3, "millón": 1e6,
    "millones": 1e6, "mila": 1e3, "milione": 1e6, "milioni": 1e6, "miliardo": 1e9, "miliardi": 1e9,
    "milhão": 1e6, "milhões": 1e6, "bilhão": 1e9, "bilhões": 1e9, "тысяча": 1e3, "тысячи": 1e3,
    "тысяч": 1e3, "миллион": 1e6, "миллиона": 1e6, "миллионов": 1e6, "миллиард": 1e9,
    "миллиарда": 1e9, "миллиардов": 1e9, "千": 1e3, "万": 1e4, "萬": 1e4, "億": 1e8, "亿": 1e8, "兆": 1e12,
}  # fmt: skip
_DECIMAL_COMMA = frozenset(
    {"de", "fr", "es", "it", "pt", "ru", "pl", "nl", "uk", "cs", "ro", "tr", "sv", "da"}
)
# A CJK magnitude first: "1400万人" is 1400 万 then 人 (\w would take "万人" as one word).
_AFTER = re.compile(r"\s*(?:([千万萬億亿兆])|(\w+)\.?)", re.UNICODE)


def _value(raw: str, lang: str) -> float | None:
    """The number ``raw`` means in ``lang`` (a decimal comma in German, a thousands comma in English)."""
    t = raw.replace("\u00a0", "").replace("\u202f", "").replace("'", "").replace("’", "").replace(" ", "")
    t = t.replace("٫", ".").replace("٬", "")
    t = t.replace(".", "").replace(",", ".") if lang in _DECIMAL_COMMA else t.replace(",", "")
    try:
        return float(t)
    except ValueError:
        return None


def _key(value: float) -> str:
    return str(round(value)) if abs(value - round(value)) < 1e-9 else f"{value:.6g}"


def _numbers(text: str, lang: str) -> list[set[str]]:
    """Each number of ``text`` as its set of keys: its digits, and its value with the magnitude word
    that follows it; number words as their value."""
    t = PLACEHOLDER.sub(" ", unicodedata.normalize("NFKC", text).translate(_AR_DIGITS))
    if lang in CJK:
        t = _CJK_SKIP.sub(" ", t)
    if lang == "en":
        t = _EN_COMPOUND.sub(_en_compound, t)
    out: list[set[str]] = []

    def date(m: re.Match[str]) -> str:
        day, month = int(m.group(1)), int(m.group(2))
        if not (1 <= day <= 31 and 1 <= month <= 12):
            return m.group(0)
        out.extend(({str(day)}, {m.group(3), str(int(m.group(3)))}))
        return " "

    t = _DOT_DATE.sub(date, t)
    if lang in _DECIMAL_COMMA:
        t = _DOT_TIME.sub(lambda m: out.extend(({str(int(m.group(1)))}, {str(int(m.group(2)))})) or " ", t)
    for m in _NUM.finditer(t):
        keys = {re.sub(r"[.,\u00a0\u202f'’٫٬ ]", "", m.group(0))}
        value = _value(m.group(0), lang)
        after = _AFTER.match(t, m.end())
        word = (after.group(1) or after.group(2) or "").lower() if after else ""
        if value is not None:
            keys.add(_key(value))
            if word in _MAGNITUDE:
                keys.add(_key(value * _MAGNITUDE[word]))
        out.append(keys)
    words = _WORDS.get(lang)
    if words:
        for w in re.findall(r"[^\W\d_]+", t.lower()):
            if w in words:
                out.append({str(words[w])})
    return out


# Chinese and Japanese numerals: 二年A班 is "Class 2-A" (2026-10-25, a Chinese anime article). They are
# also in names and words (一郎, 九州, 千葉, 一起, 十分), so they never count as missing from the
# English; they only explain a number the English has.
_HAN_DIGIT = {"〇": 0, "零": 0, "一": 1, "二": 2, "两": 2, "兩": 2, "三": 3, "四": 4, "五": 5, "六": 6,
              "七": 7, "八": 8, "九": 9}  # fmt: skip
_HAN_UNIT = {"十": 10, "百": 100, "千": 1000}
_HAN_RUN = re.compile("[〇零一二两兩三四五六七八九十百千]+")


def _han_value(run: str) -> int | None:
    if all(c in _HAN_DIGIT for c in run):  # 二〇〇六: digit by digit
        return int("".join(str(_HAN_DIGIT[c]) for c in run))
    total, digit = 0, None
    for c in run:
        if c in _HAN_DIGIT:
            if digit is not None:
                return None  # 二三十: not a number
            digit = _HAN_DIGIT[c]
        else:
            total += (1 if digit is None else digit) * _HAN_UNIT[c]
            digit = None
    return total + (digit or 0)


def _han_numbers(text: str) -> list[set[str]]:
    out: list[set[str]] = []
    for m in _HAN_RUN.finditer(text):
        value = _han_value(m.group(0))
        if value is None:
            continue
        keys = {str(value)}
        after = _AFTER.match(text, m.end())
        word = (after.group(1) or "") if after else ""
        if word in _MAGNITUDE:
            keys.add(_key(value * _MAGNITUDE[word]))
        out.append(keys)
    return out


# "Both" words: German "der beiden Quelläste" is rightly "the two source streams" (2026-10-24, the
# Rhine article), so an English "two" may stand for one of them.
_BOTH = re.compile(
    r"\b(beide|beiden|beider|beides|les deux|ambos|ambas|entrambi|entrambe|ambedue|оба|обе|обоих|обеих)\b",
    re.IGNORECASE,
)


def numbers_differ(source: str, target: str, lang: str) -> str | None:
    """Why the numbers do not match, or None. A number may change script (digits, words) or how it
    is written ("1400万", "14 million"), not its value."""
    # "les deux pays" is "both countries": a "both" phrase is not the number two (review #102).
    src, tgt = _numbers(_BOTH.sub(" ", source), lang), _numbers(target, "en")
    unmatched_tgt = list(range(len(tgt)))
    missing: list[str] = []
    for keys in src:
        hit = next((k for k in unmatched_tgt if keys & tgt[k]), None)
        if hit is None:
            missing.append(min(keys, key=len))
        else:
            unmatched_tgt.remove(hit)
    if lang in ("zh", "ja"):
        for keys in _han_numbers(source):
            hit = next((k for k in unmatched_tgt if keys & tgt[k]), None)
            if hit is not None:
                unmatched_tgt.remove(hit)
    added = [min(tgt[k], key=len) for k in unmatched_tgt]
    for _ in _BOTH.finditer(source):
        if "2" in added:
            added.remove("2")
    if not missing and not added:
        return None
    parts: list[str] = []
    if missing:
        parts.append("missing " + ", ".join(sorted(missing)[:4]))
    if added:
        parts.append("added " + ", ".join(sorted(added)[:4]))
    return "numbers differ (" + "; ".join(parts) + ")"


def placeholders_differ(source: str, target: str) -> str | None:
    s, t = sorted(PLACEHOLDER.findall(source)), sorted(PLACEHOLDER.findall(target))
    if s != t:
        return "links or formatting lost"
    for n in re.findall(r"<x(\d+)>", target):
        if target.find(f"<x{n}>") > target.find(f"</x{n}>"):
            return "links or formatting out of order"
    return None


def sentences(text: str, lang: str) -> int:
    parts: list[str] = []
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        if lang in CJK:
            parts += re.split(r"(?<=[。！？!?])\s*", line)
        else:
            parts += re.split(_ABBR + r"(?<!\d\.)(?<=[.!?؟])\s+(?=[\"«„“(]?[A-ZÄÖÜÀ-ÝА-ЯЁ0-9؀-ۿ<⟦])", line)
    return sum(1 for p in parts if len(re.sub(r"[\W\d_]", "", p)) >= 3)


def still_source(target: str, lang: str) -> float:
    """The share of the output that still reads as the source language."""
    t = PLACEHOLDER.sub(" ", target)
    if lang in _SCRIPT:
        letters = [c for c in t if c.isalpha()]
        return sum(1 for c in letters if re.match(_SCRIPT[lang], c)) / len(letters) if letters else 0.0
    if lang in _STOP:
        words = re.findall(r"\w+", t.lower())
        return len(re.findall(_STOP[lang], " ".join(words))) / len(words) if words else 0.0
    return 0.0


def check(source: str, target: str, lang: str) -> Verdict:
    """The verdict on one translated segment (``lang``: the source's language code)."""
    if not target.strip():
        return Verdict(False, "no translation came back")
    if _FRAMING.search(target):
        return Verdict(False, "the translator added words of its own")
    if (why := placeholders_differ(source, target)) is not None:
        return Verdict(False, why)
    if (why := numbers_differ(source, target, lang)) is not None:
        return Verdict(False, why)
    bare_s, bare_t = PLACEHOLDER.sub("", source), PLACEHOLDER.sub("", target)
    if len(bare_s) >= 20:
        lo, hi = _LENGTH_BAND.get(lang, _DEFAULT_BAND)
        ratio = len(bare_t) / max(1, len(bare_s))
        if lang in CJK and lang in _SCRIPT:
            # The band is for the dense script; dates, digits and Latin names come out as long as they
            # went in (2026-10-25: a broadcast schedule, "東京電視台 • 2006年10月2日 ... 星期一 25:30").
            dense = len(re.findall(_SCRIPT[lang], bare_s))
            ratio = (len(bare_t) - (len(bare_s) - dense)) / dense if dense >= 10 else lo
        if not lo <= ratio <= hi:
            return Verdict(
                False, "much shorter than the original" if ratio < lo else "much longer than the original"
            )
        if difflib.SequenceMatcher(None, bare_s, bare_t, autojunk=False).ratio() > 0.9:
            return Verdict(False, "not translated")
        if still_source(target, lang) > 0.3:
            return Verdict(False, "not translated")
    if lang != "ar" and sentences(source, lang) - sentences(target, "en") > 1:
        return Verdict(False, "a sentence may be missing")
    return Verdict(True)
