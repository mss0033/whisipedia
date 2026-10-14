"""Refinery-backed :class:`~wsp_core.ports.Catalog` for the Navigator's portal, directory and random pages.

Everything slow is precomputed when a generation is indexed (:func:`build_source_catalog`), into one
small JSON file per source on local disk::

    <index root>/catalog/<source_id>.json
        {"format": 1, "source_id", "generation_id", "doc_count",
         "scheme": "prefix" | "initial" | <provider name>,
         "categories": [{"slug", "name", "count", "docs": [doc ids, alphabetical by title]}],
         "featured":   [doc ids of well-linked (or, without link data, long) articles of the
                        default language],
         "featured_by_lang": {lang: [doc ids, best first]},
         "on_this_day": {"MM-DD": [{"year", "text", "doc_id"}]}}

Featured articles are chosen per language, from the link-analysis rank *within* the language
(``pr_group`` of :mod:`wsp_index.linkrank`): mixing languages let Cebuano bot stubs, which link to
each other, fill the start page. Without a rank, inbound links are counted over row groups spread
evenly over every part, never over the first parts in storage order. A category lists its
best-ranked documents (alphabetically), and titles are filed under their first letter or digit in
any script, leading punctuation skipped.

Serving (:class:`RefineryCatalog`) only reads those files (reloaded when they change), the Refinery's
Parquet footers (for uniform random pages) and the change-feed tail, so every call stays well under
the 50 ms budget even for multi-million-document sources.

Directory categories come from a :class:`CategoryProvider`. The defaults split a source by its native
id prefix (ZIM book, Wikipedia language) when it has one, else A-Z by title. Wikidata classes and
Library of Congress / LCC trees plug in as further providers (TODO: Phase 5+, they need entity data).
"""

from __future__ import annotations

import hashlib
import heapq
import json
import os
import random
import re
import threading
import time
import unicodedata
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Protocol, cast, runtime_checkable

import numpy as np
import numpy.typing as npt
import pyarrow as pa
import pyarrow.compute as _pc
import pyarrow.parquet as pq

from wsp_core.ids import DocId
from wsp_core.model import Category, ChangeRecord, DayEvent, SourceSpec
from wsp_core.ports import ChangeFeed
from wsp_index.linkrank import StaticRank
from wsp_index.refinery import RefineryReader, current_generation, generation_dir, id_map_dir, is_sealed
from wsp_index.text import primary_lang

pc = cast(Any, _pc)  # pyarrow-stubs type compute kernels too narrowly for column arrays

CATALOG_FORMAT = 1
CATALOG_DIR = "catalog"

_MONTHS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)
_DATE_PAGE = re.compile(r"^(" + "|".join(_MONTHS) + r")[_ ](\d{1,2})$")
_EVENT_LINE = re.compile(r"^[*\u2022\-\s]*(\d{1,4})(\s*BC)?\s*[\u2013\u2014-]\s+(.{8,})$")
_SECTION_END = frozenset({"Births", "Deaths", "Holidays and observances", "References", "External links"})
_NOT_FEATURED = re.compile(r"(^List_of|^Lists_of|\(disambiguation\)|^Index_of|^Outline_of|^\d+$|^\d+s$)")
_SLUG_BAD = re.compile(r"[^a-z0-9]+")
_LANG_NAMES = {
    "en": "English",
    "de": "German",
    "fr": "French",
    "es": "Spanish",
    "it": "Italian",
    "nl": "Dutch",
    "pl": "Polish",
    "ru": "Russian",
    "sv": "Swedish",
    "ceb": "Cebuano",
    "ja": "Japanese",
    "zh": "Chinese",
    "pt": "Portuguese",
    "uk": "Ukrainian",
    "ar": "Arabic",
}


def slugify(text: str) -> str:
    return _SLUG_BAD.sub("-", text.casefold()).strip("-") or "other"


def is_featured_source(spec: SourceSpec) -> bool:
    """Sources whose articles may be "featured" on the start page: Wikipedia snapshots."""
    return spec.id.startswith("wikipedia") and "history" not in spec.id and "encyclopedia" in spec.domain


# --- category providers (the directory seam) ---------------------------------------------------------


@runtime_checkable
class CategoryProvider(Protocol):
    """Assigns a document to one top-level directory category of its source.

    Returns ``(slug, display name)`` or ``None`` (not listed). A Wikidata-class or LCC provider
    implements the same method from entity data.
    """

    name: str

    def key(self, doc_id: str, native_id: str, title: str) -> tuple[str, str] | None: ...


def _prefix_name(prefix: str) -> str:
    if prefix in _LANG_NAMES:
        return _LANG_NAMES[prefix]
    core = re.sub(r"_(?:[a-z]{2,3}|mul)_all$", "", prefix)
    return core.replace("_", " ").replace(".", " ").strip().title() or prefix


class PrefixCategories:
    """First native-id segment: the ZIM book of a bundle, the language of a multi-wiki source."""

    name = "prefix"

    def key(self, doc_id: str, native_id: str, title: str) -> tuple[str, str] | None:
        head, sep, _ = native_id.partition("/")
        if not sep or not head:
            return None
        return slugify(head), _prefix_name(head)


_SCRIPT_BUCKETS = {"CJK": "Han", "HIRAGANA": "Kana", "KATAKANA": "Kana", "THAI": "Thai", "LAO": "Lao"}


def initial_of(title: str) -> tuple[str, str]:
    """``(slug, display name)`` of a title's directory initial.

    The first letter or digit (leading punctuation skipped), accents folded ("Émile" under E),
    Hangul by its leading consonant, scripts of thousands of characters (Han, Kana, Thai) as one
    bucket each, and any other alphabet by its own letter (Cyrillic, Greek, Arabic, Devanagari, ...).
    """
    first = next((ch for ch in title.strip() if ch.isalnum()), "")
    if not first:
        return "other", "Other"
    if first.isdigit():
        return "0-9", "0-9"
    name = unicodedata.name(first, "")
    script = name.split(" ", 1)[0] if name else ""
    if script in _SCRIPT_BUCKETS:
        label = _SCRIPT_BUCKETS[script]
        return label.lower(), label
    base = unicodedata.normalize("NFKD", first)[0].upper()  # accents and Hangul syllables decompose
    if "A" <= base <= "Z":
        return base.lower(), base
    return f"u{ord(base):04x}", base


class InitialCategories:
    """A-Z by title (Yahoo-style index) and the initials of other scripts, digits under ``0-9``."""

    name = "initial"

    def key(self, doc_id: str, native_id: str, title: str) -> tuple[str, str] | None:
        return initial_of(title)


# --- build ------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CatalogConfig:
    featured: bool = False  # compute start-page candidates (encyclopedias)
    featured_candidates: int = 730  # two years of distinct daily picks
    min_featured_chars: int = 2_000
    max_link_docs: int = 1_000_000  # inbound links are counted over at most this many documents
    rank: StaticRank | None = None  # link-analysis rank (wsp_index.linkrank); preferred over inlinks
    per_category: int = 200  # documents listed per category page
    providers: Sequence[CategoryProvider] | None = None  # None: prefix if the source has one, else A-Z
    max_prefixes: int = 200


def _title_key(title: str) -> tuple[int, str]:
    """Directory order: titles starting with a letter or digit first, leading punctuation ignored."""
    folded = title.casefold()
    start = next((i for i, ch in enumerate(folded) if ch.isalnum()), len(folded))
    return (0 if start == 0 else 1, folded[start:])


_Entry3 = tuple[float, tuple[int, str], str]


@dataclass
class _Bucket:
    name: str
    count: int = 0
    docs: list[_Entry3] = field(default_factory=list[_Entry3])  # (-score, title key, doc id)

    def add(self, title: str, doc_id: str, limit: int, score: float = 0.0) -> None:
        self.count += 1
        self.docs.append((-score, _title_key(title), doc_id))
        if len(self.docs) > 2 * limit:
            self.docs.sort()
            del self.docs[limit:]

    def top(self, limit: int) -> list[str]:
        """The ``limit`` best-ranked documents (by title among equals), listed alphabetically."""
        self.docs.sort()
        return [d for _, _, d in sorted(self.docs[:limit], key=lambda t: t[1])]


class _Grouping:
    def __init__(self, provider: CategoryProvider, limit: int, max_keys: int | None) -> None:
        self.provider = provider
        self.limit = limit
        self.max_keys = max_keys
        self.buckets: dict[str, _Bucket] = {}
        self.unkeyed = 0
        self.overflow = False

    def add(self, doc_id: str, native_id: str, title: str, score: float = 0.0) -> None:
        if self.overflow:
            return
        found = self.provider.key(doc_id, native_id, title)
        if found is None:
            self.unkeyed += 1
            return
        slug, name = found
        bucket = self.buckets.get(slug)
        if bucket is None:
            if self.max_keys is not None and len(self.buckets) >= self.max_keys:
                self.overflow = True
                return
            bucket = self.buckets[slug] = _Bucket(name)
        bucket.add(title, doc_id, self.limit, score)

    def usable(self, total: int) -> bool:
        if self.overflow or not self.buckets or total == 0:
            return False
        if self.max_keys is None:  # explicit providers are always used
            return True
        biggest = max(b.count for b in self.buckets.values())
        return len(self.buckets) >= 2 and biggest <= 0.95 * total and self.unkeyed <= 0.05 * total

    def to_json(self) -> list[dict[str, object]]:
        out: list[dict[str, object]] = [
            {"slug": slug, "name": b.name, "count": b.count, "docs": b.top(self.limit)}
            for slug, b in self.buckets.items()
        ]
        return sorted(out, key=lambda c: str(c["name"]).casefold())


def parse_date_page(text: str) -> list[tuple[int, str]]:
    """``(year, text)`` events from a Wikipedia date page's "Events" section (plain text)."""
    lines = [ln.strip() for ln in text.splitlines()]
    try:
        start = lines.index("Events") + 1
    except ValueError:
        start = 0
    out: list[tuple[int, str]] = []
    for line in lines[start:]:
        if line in _SECTION_END:
            break
        m = _EVENT_LINE.match(line)
        if m:
            year = int(m[1]) * (-1 if m[2] else 1)
            out.append((year, m[3].strip()))
    return out


def _top_k(values: npt.NDArray[np.int64], k: int) -> npt.NDArray[np.intp]:
    if len(values) <= k:
        return np.argsort(-values, kind="stable")
    idx = np.argpartition(-values, k)[:k]
    return idx[np.argsort(-values[idx], kind="stable")]


def _spread_row_groups(reader: RefineryReader, max_docs: int) -> list[tuple[int, int]]:
    """``(part, row group)`` pairs holding about ``max_docs`` rows, spread evenly over every part."""
    groups: list[tuple[int, int, int]] = []
    for part, path in enumerate(reader.parts):
        meta = pq.ParquetFile(path).metadata
        groups.extend((part, rg, meta.row_group(rg).num_rows) for rg in range(meta.num_row_groups))
    total = sum(n for _, _, n in groups)
    if total <= max_docs:
        return [(p, rg) for p, rg, _ in groups]
    want = max(1, round(len(groups) * max_docs / total))
    return [(groups[i][0], groups[i][1]) for i in sorted({int(j * len(groups) / want) for j in range(want)})]


def _inlink_ranking(reader: RefineryReader, source_id: str, max_docs: int) -> list[tuple[str, int]]:
    """Inbound links per document of this source, counted over about ``max_docs`` documents from
    row groups spread evenly over the generation (never its first parts in storage order)."""
    prefix = f"wsp:{source_id}/"
    pending: list[pa.Table] = []
    pending_rows = 0
    acc: pa.Table | None = None

    def compact() -> None:
        nonlocal acc, pending, pending_rows
        parts = [*([acc] if acc is not None else []), *pending]
        if parts:
            merged = pa.concat_tables(parts)
            agg = merged.group_by("v").aggregate([("c", "sum")])  # pyright: ignore[reportUnknownMemberType]
            acc = agg.rename_columns(["v", "c"])
        pending, pending_rows = [], 0

    open_part: tuple[int, pq.ParquetFile] | None = None
    for part, rg in _spread_row_groups(reader, max_docs):
        if open_part is None or open_part[0] != part:
            open_part = (part, pq.ParquetFile(reader.parts[part]))
        col = open_part[1].read_row_group(rg, columns=["links"]).column(0)  # pyright: ignore[reportUnknownMemberType]
        flat = pc.list_flatten(col)
        flat = flat.filter(pc.starts_with(flat, prefix))
        if len(flat):
            vc = pc.value_counts(flat)
            vals: pa.Array[Any] = vc.field("values")
            counts: pa.Array[Any] = pc.cast(vc.field("counts"), pa.int64())
            pending.append(pa.Table.from_arrays([vals, counts], names=["v", "c"]))
            pending_rows += len(vc)
            if pending_rows > 4_000_000:
                compact()
    compact()
    if acc is None or acc.num_rows == 0:
        return []
    counts_np = cast(npt.NDArray[np.int64], acc.column("c").to_numpy())
    order = _top_k(counts_np, 20_000)
    values = acc.column("v")
    return [(cast(str, values[int(i)].as_py()), int(counts_np[int(i)])) for i in order]


def _length_ranking(reader: RefineryReader, k: int) -> list[tuple[str, int]]:
    """Fallback without link data: the longest articles (a rough proxy for well-developed ones)."""
    best_ids: list[str] = []
    best_len: npt.NDArray[np.int64] = np.zeros(0, dtype=np.int64)
    for batch in reader.scan(["doc_id", "text"], batch_size=4096):
        lens = cast(npt.NDArray[np.int64], pc.utf8_length(batch.column(1)).to_numpy().astype(np.int64))
        ids = cast(list[str], batch.column(0).to_pylist())
        all_len = np.concatenate([best_len, lens])
        keep = _top_k(all_len, k)
        pool = [*best_ids, *ids]
        best_ids = [pool[int(i)] for i in keep]
        best_len = all_len[keep]
    return list(zip(best_ids, (int(x) for x in best_len), strict=True))


def rank_by_language(reader: RefineryReader, rank: StaticRank, k: int) -> dict[str, list[tuple[str, float]]]:
    """The ``k`` documents with the highest PageRank *within their language* (the per-group rank of
    :mod:`wsp_index.linkrank`), per language, best first."""
    best: dict[str, list[tuple[float, str]]] = {}
    for batch in reader.scan(["doc_id", "lang"], batch_size=65536):
        ids = cast(list[str], batch.column(0).to_pylist())
        langs = cast(list[str], batch.column(1).to_pylist())
        found = rank.pageranks(ids)
        if found is None:
            return {}
        per_group = np.nan_to_num(found[0], nan=0.0)
        for i in cast(list[int], np.argsort(-per_group, kind="stable")[: k * 4].tolist()):
            if per_group[i] <= 0:
                break
            heap = best.setdefault(primary_lang(langs[i]), [])
            item = (float(per_group[i]), ids[i])
            if len(heap) < k:
                heapq.heappush(heap, item)
            elif item > heap[0]:
                heapq.heapreplace(heap, item)
    return {lang: [(d, f) for f, d in sorted(heap, reverse=True)] for lang, heap in best.items()}


def _featurable(reader: RefineryReader, doc_id: str, cfg: CatalogConfig) -> str | None:
    """The document's language when it may be featured (long enough, not a list or date page)."""
    native = doc_id.split("/", 1)[1] if "/" in doc_id else doc_id
    leaf = native.rsplit("/", 1)[-1]
    if _NOT_FEATURED.search(leaf) or _DATE_PAGE.match(leaf):
        return None
    row = reader.lookup(doc_id, ("text", "lang"))
    if row is None or len(cast(str, row["text"])) < cfg.min_featured_chars:
        return None
    return primary_lang(cast(str, row["lang"]))


def _featured(reader: RefineryReader, source_id: str, cfg: CatalogConfig) -> dict[str, list[str]]:
    """Featured candidates per language, best first."""
    want = cfg.featured_candidates
    out: dict[str, list[str]] = {}
    if cfg.rank is not None:  # PageRank inside each language
        for lang, ranked in rank_by_language(reader, cfg.rank, want * 4).items():
            for doc_id, _ in ranked:
                if _featurable(reader, doc_id, cfg) is not None:
                    out.setdefault(lang, []).append(doc_id)
                    if len(out[lang]) >= want:
                        break
        if out:
            return out
    ranking: Sequence[tuple[str, float]] = _inlink_ranking(reader, source_id, cfg.max_link_docs)
    if not ranking:
        ranking = _length_ranking(reader, want * 4)
    for doc_id, _ in ranking:
        lang = _featurable(reader, doc_id, cfg)
        if lang is not None and len(out.setdefault(lang, [])) < want:
            out[lang].append(doc_id)
    return {lang: ids for lang, ids in out.items() if ids}


def _default_featured(by_lang: Mapping[str, Sequence[str]], prefer: str = "en") -> list[str]:
    """The ``featured`` list of format 1 readers: English when present, else the longest list."""
    if prefer in by_lang:
        return list(by_lang[prefer])
    return list(max(by_lang.values(), key=len)) if by_lang else []


def _date_key(native_id: str) -> str | None:
    head, _, leaf = native_id.rpartition("/")
    if head not in ("", "A", "en"):
        return None
    m = _DATE_PAGE.match(leaf)
    if not m:
        return None
    month, day = _MONTHS.index(m[1]) + 1, int(m[2])
    if not 1 <= day <= 31:
        return None
    return f"{month:02d}-{day:02d}"


def _job_reader(
    refinery_root: Path, source_id: str, generation_id: str, map_root: Path | None
) -> RefineryReader:
    gen_dir = generation_dir(refinery_root, source_id, generation_id)
    return RefineryReader(gen_dir, map_dir=None if map_root is None else id_map_dir(map_root, gen_dir))


def build_source_catalog(
    refinery_root: Path,
    source_id: str,
    generation_id: str,
    out_root: Path,
    cfg: CatalogConfig = CatalogConfig(),  # noqa: B008 - immutable
    map_root: Path | None = None,
) -> Path:
    """Scan one Refinery generation and write ``<out_root>/<source_id>.json`` atomically.

    ``map_root``: local id-map sidecars (:func:`~wsp_index.refinery.id_map_dir`); the featured pick
    does thousands of point lookups, each a whole ``_ids.parquet`` row group over the NAS without one."""
    reader = _job_reader(refinery_root, source_id, generation_id, map_root)
    groupings = (
        [_Grouping(p, cfg.per_category, None) for p in cfg.providers]
        if cfg.providers
        else [
            _Grouping(PrefixCategories(), cfg.per_category, cfg.max_prefixes),
            _Grouping(InitialCategories(), cfg.per_category, None),
        ]
    )
    date_pages: dict[str, str] = {}
    total = 0
    for batch in reader.scan(["doc_id", "native_id", "title"], batch_size=8192):
        ids = cast(list[str], batch.column(0).to_pylist())
        natives = cast(list[str], batch.column(1).to_pylist())
        titles = cast(list[str], batch.column(2).to_pylist())
        scores = cfg.rank.features(ids).tolist() if cfg.rank is not None else [0.0] * len(ids)
        for doc_id, native, title, score in zip(ids, natives, titles, scores, strict=True):
            total += 1
            for g in groupings:
                g.add(doc_id, native, title, score)
            if cfg.featured:
                key = _date_key(native)
                if key is not None:
                    date_pages.setdefault(key, doc_id)
    chosen = next((g for g in groupings if g.usable(total)), None)
    on_this_day: dict[str, list[dict[str, object]]] = {}
    for key, doc_id in sorted(date_pages.items()):
        row = reader.lookup(doc_id, ("text",))
        events = parse_date_page(cast(str, row["text"])) if row else []
        if events:
            on_this_day[key] = [{"year": y, "text": t, "doc_id": doc_id} for y, t in events]
    by_lang = _featured(reader, source_id, cfg) if cfg.featured else {}
    payload: dict[str, object] = {
        "format": CATALOG_FORMAT,
        "source_id": source_id,
        "generation_id": generation_id,
        "doc_count": total,
        "scheme": chosen.provider.name if chosen else None,
        "categories": chosen.to_json() if chosen else [],
        "featured": _default_featured(by_lang),
        "featured_by_lang": by_lang,
        "on_this_day": on_this_day,
    }
    out_root.mkdir(parents=True, exist_ok=True)
    path = out_root / f"{source_id}.json"
    tmp = path.with_name(path.name + ".partial")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return path


def refresh_featured(
    refinery_root: Path,
    source_id: str,
    generation_id: str,
    out_root: Path,
    cfg: CatalogConfig,
    map_root: Path | None = None,
) -> bool:
    """Recompute only the ``featured`` list of an existing catalog file (after ``wsp index rank``)."""
    path = out_root / f"{source_id}.json"
    if not path.is_file():
        return False
    payload = cast(dict[str, object], json.loads(path.read_text(encoding="utf-8")))
    if payload.get("generation_id") != generation_id:
        return False
    reader = _job_reader(refinery_root, source_id, generation_id, map_root)
    by_lang = _featured(reader, source_id, cfg)
    payload["featured"] = _default_featured(by_lang)
    payload["featured_by_lang"] = by_lang
    tmp = path.with_name(path.name + ".partial")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return True


# --- serve -----------------------------------------------------------------------------------------


class _Tailable(Protocol):
    def tail(
        self, limit: int, *, sources: Sequence[str] | None = None
    ) -> tuple[Sequence[ChangeRecord], str]: ...


@dataclass(frozen=True, slots=True)
class _Entry:
    mtime_ns: int
    data: Mapping[str, Any]
    categories: tuple[Category, ...]


def _stable_index(day: date, n: int, salt: str = "") -> int:
    digest = hashlib.blake2b(f"{salt}{day.isoformat()}".encode(), digest_size=8).digest()
    return int.from_bytes(digest, "little") % n


class RefineryCatalog:
    """Implements :class:`wsp_core.ports.Catalog` and :class:`wsp_core.ports.PortalSource`."""

    def __init__(
        self,
        refinery_root: Path,
        catalog_root: Path,
        feed: ChangeFeed,
        *,
        sources: Iterable[str] = (),
        featured_sources: Sequence[str] = (),
        reload_seconds: float = 5.0,
        monotonic: Callable[[], float] = time.monotonic,
        map_root: Path | None = None,
    ) -> None:
        """``map_root``: a local directory for the id-map sidecars, for a Refinery on the NAS (ADR-0011).
        Without one a reader there loads each source's whole id index into memory (~20 bytes a
        document, read over the network) on its first random pick."""
        self.refinery_root = refinery_root
        self.map_root = map_root
        self.catalog_root = catalog_root
        self.feed = feed
        self.sources = tuple(sources)
        self.featured_sources = tuple(featured_sources)
        self.reload_seconds = reload_seconds
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._entries: dict[str, tuple[float, _Entry | None]] = {}
        self._readers: dict[tuple[str, str], RefineryReader] = {}  # current generation of each source
        self._current: dict[str, tuple[float, str | None]] = {}  # source -> (asked at, generation)

    # --- precomputed data ---

    def _entry(self, source_id: str) -> _Entry | None:
        now = self._monotonic()
        with self._lock:
            cached = self._entries.get(source_id)
        if cached is not None and now - cached[0] < self.reload_seconds:
            return cached[1]
        path = self.catalog_root / f"{source_id}.json"
        try:
            mtime = path.stat().st_mtime_ns
        except OSError:
            entry = None
        else:
            if cached is not None and cached[1] is not None and cached[1].mtime_ns == mtime:
                entry = cached[1]
            else:
                entry = self._load(path, mtime)
        with self._lock:
            self._entries[source_id] = (now, entry)
        return entry

    @staticmethod
    def _load(path: Path, mtime: int) -> _Entry | None:
        try:
            data = cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            return None
        if data.get("format") != CATALOG_FORMAT:
            return None
        cats = tuple(
            Category(slug=str(c["slug"]), name=str(c["name"]), count=int(c["count"]))
            for c in cast(list[dict[str, Any]], data.get("categories", []))
        )
        return _Entry(mtime, data, cats)

    def _known_sources(self) -> list[str]:
        """The registry's sources; only without one (tests, ad-hoc tools) is the Refinery listed, a
        directory walk that is many round trips on the NAS and ran on every random pick."""
        if self.sources:
            return sorted(self.sources)
        if not self.refinery_root.is_dir():
            return []
        return sorted(p.name for p in self.refinery_root.iterdir() if p.is_dir())

    def _current_generation(self, source_id: str) -> str | None:
        """Remembered for ``reload_seconds``: three metadata round trips on the NAS, asked once per
        source on every random pick and start page."""
        now = self._monotonic()
        with self._lock:
            hit = self._current.get(source_id)
            if hit is not None and now - hit[0] < self.reload_seconds:
                return hit[1]
        gen = current_generation(self.refinery_root, source_id)
        with self._lock:
            self._current[source_id] = (now, gen)
        return gen

    def _reader(self, source_id: str) -> RefineryReader | None:
        """The current generation's reader (cheap until a random pick memory-maps its id index)."""
        gen = self._current_generation(source_id)
        if gen is None:
            return None
        key = (source_id, gen)
        with self._lock:
            cached = self._readers.get(key)
        if cached is not None:
            return cached
        gen_dir = generation_dir(self.refinery_root, source_id, gen)
        if not is_sealed(gen_dir):
            return None
        map_dir = None if self.map_root is None else id_map_dir(self.map_root, gen_dir)
        reader = RefineryReader(gen_dir, cache_row_groups=2, map_dir=map_dir)
        with self._lock:
            for old in [k for k in self._readers if k[0] == source_id]:
                del self._readers[old]
            self._readers[key] = reader
        return reader

    # --- Catalog ---

    def doc_count(self, source_id: str) -> int | None:
        reader = self._reader(source_id)
        if reader is not None:
            return len(reader)  # from _SUCCESS.json: no Parquet footer is read
        entry = self._entry(source_id)
        return None if entry is None else int(entry.data.get("doc_count", 0))

    def random_doc(self, *, sources: Sequence[str] | None, rng: random.Random) -> DocId | None:
        """A uniformly random live document of ``sources`` (all known sources by default).

        The source is drawn first, by its document count from the catalog on local disk, and only
        that source's reader is opened. Opening every source's reader (57 of them, a few NAS round
        trips each) took a cold API past the start page's deadline while the lanes loaded the link,
        and the health check restarted it before the readers warmed (2026-10-14 03:00). A source the
        catalog does not count yet is weighed by its reader, the old way."""
        candidates = sorted(sources) if sources else self._known_sources()
        weighted: list[tuple[str, int]] = []
        for source_id in candidates:
            entry = self._entry(source_id)
            n = int(entry.data.get("doc_count", 0)) if entry is not None else 0
            if n > 0:
                weighted.append((source_id, n))
        if not weighted:  # no catalog yet (tests, ad-hoc tools): weigh the sources by their readers.
            # Not otherwise: a registry source without data yet costs three NAS round trips to ask
            # for a generation it does not have, and the registry lists ninety of them.
            for source_id in candidates:
                reader = self._reader(source_id)
                if reader is not None and len(reader) > 0:
                    weighted.append((source_id, len(reader)))
        while weighted:
            total = sum(n for _, n in weighted)
            k = rng.randrange(total)
            for i, (source_id, n) in enumerate(weighted):
                if k >= n:
                    k -= n
                    continue
                reader = self._reader(source_id)
                if reader is None or len(reader) == 0:
                    del weighted[i]  # counted by the catalog, gone from the Refinery: draw again
                    break
                return DocId(reader.doc_id_at(min(k, len(reader) - 1)))
            else:  # pragma: no cover - k is always within the total
                return None
        return None

    def recent(
        self, *, limit: int, sources: Sequence[str] | None = None
    ) -> tuple[Sequence[ChangeRecord], str | None]:
        tail = getattr(self.feed, "tail", None)
        if callable(tail):
            records, cursor = cast(_Tailable, self.feed).tail(limit, sources=sources)
            return records, cursor if records or cursor else None
        # Generic feed: replay (fine for small feeds and tests).
        out: list[ChangeRecord] = []
        cursor: str | None = None
        wanted = set(sources) if sources else None
        while True:
            batch, cursor = self.feed.since(cursor, limit=5000)
            out.extend(r for r in batch if wanted is None or r.doc_id[4:].partition("/")[0] in wanted)
            if len(batch) < 5000:
                break
        return out[::-1][:limit], cursor

    def categories(self, source_id: str) -> Sequence[Category]:
        entry = self._entry(source_id)
        return () if entry is None else entry.categories

    def category_docs(self, source_id: str, slug: str, *, limit: int) -> Sequence[DocId]:
        entry = self._entry(source_id)
        if entry is None:
            return ()
        for c in cast(list[dict[str, Any]], entry.data.get("categories", [])):
            if c.get("slug") == slug:
                return [DocId(str(d)) for d in cast(list[str], c.get("docs", []))[:limit]]
        return ()

    # --- PortalSource ---

    def featured(self, day: date, *, lang: str | None = None) -> DocId | None:
        """The day's featured article: from ``lang``'s own candidates where a source has them
        (``featured_by_lang``), else from each source's default-language list."""
        pool: list[str] = []
        for sid in self.featured_sources or self._known_sources():
            entry = self._entry(sid)
            if entry is None:
                continue
            by_lang = cast(dict[str, list[str]], entry.data.get("featured_by_lang", {}))
            if lang is not None and by_lang.get(primary_lang(lang)):
                pool.extend(by_lang[primary_lang(lang)])
            else:
                pool.extend(cast(list[str], entry.data.get("featured", [])))
        if not pool:
            return None
        return DocId(pool[_stable_index(day, len(pool))])

    def on_this_day(self, day: date, *, limit: int = 5) -> Sequence[DayEvent]:
        key = f"{day.month:02d}-{day.day:02d}"
        for sid in self.featured_sources or self._known_sources():
            entry = self._entry(sid)
            if entry is None:
                continue
            events = cast(dict[str, list[dict[str, Any]]], entry.data.get("on_this_day", {})).get(key)
            if not events:
                continue
            chosen = _spread(events, limit, day)
            return [
                DayEvent(
                    year=int(e["year"]),
                    text=str(e["text"]),
                    doc_id=DocId(str(e["doc_id"])) if e.get("doc_id") else None,
                )
                for e in sorted(chosen, key=lambda e: int(e["year"]))
            ]
        return ()


def _spread(events: list[dict[str, Any]], limit: int, day: date) -> list[dict[str, Any]]:
    """``limit`` events spread across the list (it is in year order), rotating with the year."""
    if len(events) <= limit:
        return list(events)
    step = len(events) / limit
    offset = _stable_index(day, max(1, int(step)), salt="otd")
    return [events[min(len(events) - 1, int(i * step) + offset)] for i in range(limit)]
