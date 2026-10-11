"""``kiwix-opds`` adapter: every ZIM in the Kiwix catalog (PLAN §6.2).

Discovery reads the OPDS v2 catalog, which lists only the newest file of each book and flavour.
The registry selects books with ``access.selector``::

    selector: {name: wikipedia_en_all, flavour: maxi}          # one book
    selector: {names: [ifixit_en_all, wikibooks_en_all]}       # a bundle of books
    selector: {category: stack_exchange, lang: eng}            # every book in a category

Without ``flavour`` one file per book is kept, preferring ``maxi``, then the unflavoured build,
then ``nopic``, ``mini`` and ``nodet``. ``ReleaseRef.version`` is the newest ZIM date in the
selection (``YYYY-MM``).

Checksums come from the ``<file>.sha256`` published next to each ZIM. ``plan`` additionally
resolves the ``.meta4`` metalink of every file that will actually be downloaded: it gives the
exact size (the catalog rounds up to KiB), MD5/SHA-1, and the mirror list. The metalink URL is
``RemoteFile.metalink`` so a fetcher can use its per-piece SHA-1 hashes; ``RemoteFile.mirrors`` holds
only full URLs of byte-identical copies.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import re
import tempfile
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from urllib.parse import urlencode

from libzim.reader import Archive

from wsp_adapters import zimscan
from wsp_adapters._common import (
    DocRow,
    IncompleteGenerationError,
    WorkerPool,
    access,
    default_workers,
    doc_from_row,
    doc_row,
    fetch_all,
    int_option,
    normalize_algorithm,
    parse_checksum_file,
    plan_missing,
    str_list,
    upstream_dir,
    verify_file,
)
from wsp_adapters.entries import Entry, Redirect
from wsp_adapters.html_text import html_to_text
from wsp_core.errors import FetchError
from wsp_core.ids import content_hash, make_doc_id
from wsp_core.model import (
    Checksum,
    DocRecord,
    FetchPlan,
    FetchResult,
    Generation,
    ReleaseRef,
    RemoteFile,
    SourceSpec,
    Verdict,
)
from wsp_core.ports import Ctx

CATALOG_URL = "https://library.kiwix.org/catalog/v2/entries"
ZIM_FORMAT = "zim"
DEFAULT_MIRRORS = ("https://mirror.download.kiwix.org/",)
FLAVOUR_PREFERENCE = ("maxi", "", "nopic", "mini", "nodet")
_PAGE_SIZE = 500

_ATOM = "{http://www.w3.org/2005/Atom}"
_DC = "{http://purl.org/dc/terms/}"
_ACQUISITION = "http://opds-spec.org/acquisition/open-access"
_ZIM_DATE_RE = re.compile(r"_(\d{4}-\d{2})([a-z]?)\.zim$")  # "_2026-07", "_2026-07a" (a re-release)
_METALINK = "{urn:ietf:params:xml:ns:metalink}"

# ISO 639-3 (ZIM metadata) to the ISO 639-1 codes used in DocRecord.lang, for common languages.
_LANG3TO1 = {
    "ara": "ar", "ben": "bn", "ceb": "ceb", "ces": "cs", "dan": "da", "deu": "de", "ell": "el",
    "eng": "en", "fas": "fa", "fin": "fi", "fra": "fr", "heb": "he", "hin": "hi", "hun": "hu",
    "ind": "id", "ita": "it", "jpn": "ja", "kor": "ko", "nld": "nl", "nor": "no", "pol": "pl",
    "por": "pt", "ron": "ro", "rus": "ru", "spa": "es", "swe": "sv", "tha": "th", "tur": "tr",
    "ukr": "uk", "vie": "vi", "zho": "zh",
}  # fmt: skip


@dataclass(frozen=True, slots=True)
class OpdsEntry:
    """One book as listed in the Kiwix OPDS catalog."""

    name: str
    flavour: str
    category: str
    languages: tuple[str, ...]
    title: str
    issued: datetime | None
    meta4_url: str
    length: int | None
    tags: tuple[str, ...] = ()

    @property
    def file_url(self) -> str:
        return self.meta4_url.removesuffix(".meta4")

    @property
    def file_name(self) -> str:
        return self.file_url.rsplit("/", 1)[-1]

    @property
    def zim_date(self) -> str:
        match = _ZIM_DATE_RE.search(self.file_name)
        return match.group(1) if match else ""

    @property
    def release_date(self) -> str:
        """``zim_date`` with its re-release letter: ``2026-07a`` is a newer build than ``2026-07``."""
        match = _ZIM_DATE_RE.search(self.file_name)
        return match.group(1) + match.group(2) if match else ""


@dataclass(frozen=True, slots=True)
class Selector:
    """Catalog filter. A name may pin its own flavour as ``<name>@<flavour>`` (``wikipedia_de_all@nopic``),
    so one bundle can mix flavours (the multilingual plan, D15); ``flavour`` applies to the rest."""

    names: tuple[str, ...] = ()
    flavour: str | None = None
    category: str | None = None
    lang: str | None = None
    pinned: Mapping[str, str] = field(default_factory=dict[str, str])  # name -> flavour

    @classmethod
    def from_spec(cls, spec: SourceSpec) -> Selector:
        raw = access(spec).get("selector")
        if not isinstance(raw, Mapping):
            raise ValueError(f"{spec.id}: kiwix-opds needs access.selector")
        sel: Mapping[str, object] = raw  # pyright: ignore[reportUnknownVariableType]
        raw_names = str_list(sel.get("name")) + str_list(sel.get("names"))
        names = tuple(n.split("@", 1)[0] for n in raw_names)
        pinned = {n.split("@", 1)[0]: n.split("@", 1)[1] for n in raw_names if "@" in n}
        flavour, category, lang = sel.get("flavour"), sel.get("category"), sel.get("lang")
        if not names and category is None:
            raise ValueError(f"{spec.id}: selector needs name, names or category")
        return cls(
            names=names,
            flavour=None if flavour is None else str(flavour),
            category=None if category is None else str(category),
            lang=None if lang is None else str(lang),
            pinned=pinned,
        )

    @property
    def single_book(self) -> bool:
        """True when the source is exactly one book, so native ids need no book prefix."""
        return len(self.names) == 1 and self.category is None

    def queries(self) -> list[dict[str, str]]:
        base: dict[str, str] = {}
        if self.category is not None:
            base["category"] = self.category
        if self.lang is not None:
            base["lang"] = self.lang
        return [base | {"name": n} for n in self.names] if self.names else [base]

    def matches(self, entry: OpdsEntry) -> bool:
        if self.names and entry.name not in self.names:
            return False
        if entry.name in self.pinned:
            if entry.flavour != self.pinned[entry.name]:
                return False
        elif self.flavour is not None and entry.flavour != self.flavour:
            return False
        if self.category is not None and entry.category != self.category:
            return False
        return self.lang is None or self.lang in entry.languages


def _text(elem: ET.Element, tag: str) -> str:
    found = elem.find(tag)
    return (found.text or "").strip() if found is not None else ""


def _parse_dt(value: str) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def parse_opds(data: bytes) -> tuple[list[OpdsEntry], int]:
    """Parse one OPDS acquisition feed page into ``(entries, totalResults)``."""
    root = ET.fromstring(data)  # noqa: S314 - HTTPS catalog; stdlib expat rejects external entities
    entries: list[OpdsEntry] = []
    for e in root.iter(f"{_ATOM}entry"):
        link = next(
            (ln for ln in e.iter(f"{_ATOM}link") if ln.get("rel") == _ACQUISITION),
            None,
        )
        if link is None or not link.get("href"):
            continue
        length = link.get("length")
        entries.append(
            OpdsEntry(
                name=_text(e, f"{_ATOM}name"),
                flavour=_text(e, f"{_ATOM}flavour"),
                category=_text(e, f"{_ATOM}category"),
                languages=tuple(x for x in _text(e, f"{_ATOM}language").split(",") if x),
                title=_text(e, f"{_ATOM}title"),
                issued=_parse_dt(_text(e, f"{_DC}issued") or _text(e, f"{_ATOM}updated")),
                meta4_url=link.get("href", ""),
                length=int(length) if length and length.isdigit() else None,
                tags=tuple(t for t in _text(e, f"{_ATOM}tags").split(";") if t),
            )
        )
    total_text = _text(root, f"{_ATOM}totalResults")
    total = int(total_text) if total_text.isdigit() else len(entries)
    return entries, total


def select_entries(entries: Iterable[OpdsEntry], selector: Selector) -> list[OpdsEntry]:
    """Apply the selector and keep one file per book (newest; preferred flavour if unspecified)."""
    best: dict[str, OpdsEntry] = {}
    for entry in entries:
        if not selector.matches(entry):
            continue
        current = best.get(entry.name)
        if current is None or _rank(entry) < _rank(current):
            best[entry.name] = entry
    return sorted(best.values(), key=lambda x: x.name)


def _rank(entry: OpdsEntry) -> tuple[int, int, int]:
    """Lower is better: preferred flavour first, then the newest date."""
    flavour_rank = (
        FLAVOUR_PREFERENCE.index(entry.flavour)
        if entry.flavour in FLAVOUR_PREFERENCE
        else len(FLAVOUR_PREFERENCE)
    )
    return flavour_rank, -int(entry.zim_date.replace("-", "") or 0), _letter_rank(entry.release_date)


def _letter_rank(release_date: str) -> int:
    """Lower is newer: ``2026-07b`` before ``2026-07a`` before ``2026-07``."""
    letter = release_date[7:8]
    return -(ord(letter) - ord("a") + 1) if letter else 0


@dataclass(frozen=True, slots=True)
class Metalink:
    """The parts of a Metalink 4 (RFC 5854) file a fetcher needs."""

    name: str
    size: int | None
    checksums: tuple[Checksum, ...]
    urls: tuple[str, ...]  # by ascending priority
    piece_length: int | None
    piece_hashes: tuple[str, ...]  # SHA-1 per piece


def parse_metalink(data: bytes) -> Metalink:
    root = ET.fromstring(data)  # noqa: S314 - HTTPS catalog; stdlib expat rejects external entities
    file = root.find(f"{_METALINK}file")
    if file is None:
        raise ValueError("metalink has no <file>")
    size_text = _text(file, f"{_METALINK}size")
    checksums = tuple(
        Checksum(normalize_algorithm(h.get("type", "")), (h.text or "").strip().lower())
        for h in file.findall(f"{_METALINK}hash")
    )
    ranked = sorted(
        ((int(u.get("priority", "999999")), (u.text or "").strip()) for u in file.findall(f"{_METALINK}url")),
        key=lambda pu: pu[0],
    )
    pieces = file.find(f"{_METALINK}pieces")
    piece_length: int | None = None
    piece_hashes: tuple[str, ...] = ()
    if pieces is not None:
        piece_length = int(pieces.get("length", "0")) or None
        piece_hashes = tuple((h.text or "").strip() for h in pieces.findall(f"{_METALINK}hash"))
    return Metalink(
        name=file.get("name", ""),
        size=int(size_text) if size_text.isdigit() else None,
        checksums=checksums,
        urls=tuple(u for _, u in ranked if u),
        piece_length=piece_length,
        piece_hashes=piece_hashes,
    )


def _mirror_urls(file_url: str, bases: Sequence[str]) -> tuple[str, ...]:
    _, sep, rel = file_url.partition("/zim/")
    if not sep:
        return ()
    return tuple(f"{b.rstrip('/')}/zim/{rel}" for b in bases)


# --- release identity and content set (audit 2026-10-11 C1, H3) ------------------------------------

_BUNDLE_ID_RE = re.compile(r"^(?P<date>\d{4}-\d{2}[a-z]?)[-+](?P<digest>[0-9a-f]{8})$")
MAX_DROPPED_BOOKS = 2  # books a bundle may drop between two generations (content-set search bound)


def names_digest(names: Iterable[str]) -> str:
    """First 8 hex digits of the SHA-256 of the sorted file names, one per line."""
    return hashlib.sha256("\n".join(sorted(names)).encode()).hexdigest()[:8]


def release_version(names: Sequence[str], *, bundle: bool) -> str:
    """``ReleaseRef.version`` of a selection of ZIM files.

    A single book is its newest file's date with the re-release letter (``2026-07a``), so a
    re-release is a new version. A bundle is ``<newest date>+<names_digest>``: a re-release or an
    update of *any* book changes the file names and therefore the version (the newest date alone
    missed updates to every book but the newest), and the digest lets :func:`bundle_content`
    check a generation's content set against its id.
    """
    newest = max((d for n in names if (d := file_release_date(n))), default="0000-00")
    return f"{newest}+{names_digest(names)}" if bundle else newest


def file_release_date(file_name: str) -> str:
    """``..._2026-07a.zim`` -> ``2026-07a`` (``""`` when the name carries no date)."""
    m = _ZIM_DATE_RE.search(file_name)
    return m.group(1) + m.group(2) if m else ""


def book_key(file_name: str) -> str:
    """``wikipedia_en_all_maxi_2026-07a.zim`` -> ``wikipedia_en_all_maxi`` (book and flavour)."""
    return _ZIM_DATE_RE.sub("", file_name).removesuffix(".zim")


def _zims(generation_dir: Path) -> dict[str, Path]:
    up = upstream_dir(generation_dir)
    if not up.is_dir():
        return {}
    found: dict[str, Path] = {}
    for path in sorted(up.glob("*.zim"), key=lambda p: (file_release_date(p.name), p.name)):
        found[book_key(path.name)] = path  # the newest build of a book wins
    return found


def bundle_content(generation_dir: Path, *, single_book: bool = False) -> dict[str, Path]:
    """Every ZIM of a generation's release, by book key, in this generation or an earlier one.

    ``plan`` downloads only books the previous generation does not hold, so a bundle generation
    stores the new and changed books and reads the rest from earlier generations. The content set
    is built along the chain of the source's generations (directories with ``upstream/``) up to this
    one, in release order (:func:`_order_key`): each generation's own files, plus its predecessor's
    content set for books it does not hold itself.
    When a generation id carries the bundle digest (:func:`release_version`), the set must
    reproduce it; books dropped from the bundle since the predecessor (at most
    :data:`MAX_DROPPED_BOOKS`) are found by search. A set that cannot be reproduced raises
    :class:`IncompleteGenerationError`: refining a wrong set would delete or resurrect documents.
    A single-book source's content is its newest generation's file (a flavour change replaces the
    book rather than adding one).
    """
    me = _order_key(generation_dir)
    chain = sorted(
        (p for p in generation_dir.parent.iterdir() if p != generation_dir and (p / "upstream").is_dir()),
        key=_order_key,
    )
    chain = [p for p in chain if _order_key(p) < me]
    content: dict[str, Path] = {}
    for gen in [*chain, generation_dir]:
        content = _next_content(gen, content, single_book=single_book)
    return content


_DATE_PREFIX_RE = re.compile(r"^\d{4}-\d{2}[a-z]?")


def _order_key(gen: Path) -> tuple[str, str, str]:
    """Generations in release order: by date, then by when they were sealed, then by id.

    Two bundle releases with the same newest book date differ only in their digest, which says
    nothing about order; the vault's ``MANIFEST.json`` ``sealed_at`` does.
    """
    m = _DATE_PREFIX_RE.match(gen.name)
    sealed = ""
    try:
        doc: object = json.loads((gen / "MANIFEST.json").read_bytes())
        if isinstance(doc, dict):
            sealed = str(cast(dict[str, object], doc).get("sealed_at") or "")
    except (OSError, ValueError):
        pass
    return (m.group(0) if m else gen.name, sealed, gen.name)


def _next_content(gen: Path, previous: Mapping[str, Path], *, single_book: bool) -> dict[str, Path]:
    own = _zims(gen)
    if single_book:
        if not own:
            return dict(previous)
        newest = max(own.values(), key=lambda p: (file_release_date(p.name), p.name))
        return {book_key(newest.name): newest}
    carried = {k: p for k, p in previous.items() if k not in own}
    m = _BUNDLE_ID_RE.match(gen.name)
    if m is None:  # a single book, or a bundle sealed before ids carried the digest: unverifiable
        return dict(sorted({**carried, **own}.items()))
    names = [p.name for p in own.values()]
    keys = sorted(carried)
    for drops in range(min(MAX_DROPPED_BOOKS, len(keys)) + 1):
        for dropped in itertools.combinations(keys, drops):
            kept = [carried[k].name for k in keys if k not in dropped]
            if names_digest([*names, *kept]) == m.group("digest"):
                gone = set(dropped)
                return dict(sorted({**{k: p for k, p in carried.items() if k not in gone}, **own}.items()))
    raise IncompleteGenerationError(
        f"{gen.name}: no set of its {len(own)} own and {len(carried)} carried ZIM(s) matches the "
        f"bundle digest {m.group('digest')} (a carried book is missing from earlier generations, or "
        f"more than {MAX_DROPPED_BOOKS} books were dropped at once)"
    )


def _generation_of(path: Path) -> str:
    """The generation directory a stored file belongs to (``<gen>/upstream/<file>`` or ``<gen>/<file>``)."""
    parent = path.parent
    return parent.parent.name if parent.name == "upstream" else parent.name


def prefix_books(spec: SourceSpec) -> bool:
    """Whether native ids carry ``<book>/`` (audit 2026-10-11 H4).

    ``access.prefix_books`` pins it in the registry, so adding a book to a source never renames
    every id of the books it already had (millions of removals and additions in the diff). Without
    it the old rule holds: no prefix for a one-name selector, a prefix otherwise. Pin it for every
    Kiwix source: ``false`` for a single book, ``true`` for a bundle.
    """
    pinned = access(spec).get("prefix_books")
    if pinned is None:
        return not Selector.from_spec(spec).single_book
    if not isinstance(pinned, bool):
        raise ValueError(f"{spec.id}: access.prefix_books must be true or false")
    return pinned


class KiwixOpdsAdapter:
    """Reference ``SourceAdapter`` for Kiwix ZIM files."""

    id = "kiwix-opds"

    def __init__(self, catalog_url: str = CATALOG_URL) -> None:
        self.catalog_url = catalog_url

    # --- discovery -------------------------------------------------------------------------------

    def catalog(self, ctx: Ctx, selector: Selector) -> list[OpdsEntry]:
        """Every catalog entry returned for the selector's queries (unfiltered, paginated)."""
        found: list[OpdsEntry] = []
        for query in selector.queries():
            start = 0
            while True:
                url = f"{self.catalog_url}?{urlencode(query | {'count': _PAGE_SIZE, 'start': start})}"
                page, total = parse_opds(ctx.fetcher.get_bytes(url))
                found.extend(page)
                start += len(page)
                if not page or start >= total:
                    break
        return found

    def discover(self, ctx: Ctx) -> Iterable[ReleaseRef]:
        spec = ctx.source
        selector = Selector.from_spec(spec)
        chosen = select_entries(self.catalog(ctx, selector), selector)
        if not chosen:
            return []
        mirrors = str_list(access(spec).get("mirrors")) or DEFAULT_MIRRORS
        files: list[RemoteFile] = []
        meta: dict[str, str] = {"books": ",".join(e.name for e in chosen)}
        for entry in chosen:
            sums = parse_checksum_file(
                ctx.fetcher.get_bytes(entry.file_url + ".sha256", max_bytes=64 * 1024).decode()
            )
            digest = sums.get(entry.file_name)
            files.append(
                RemoteFile(
                    url=entry.file_url,
                    name=entry.file_name,
                    size=None,  # the catalog length is rounded up to KiB; plan() fills the exact size
                    checksums=(Checksum("sha256", digest),) if digest else (),
                    mirrors=_mirror_urls(entry.file_url, mirrors),
                    metalink=entry.meta4_url,
                )
            )
            if entry.length is not None:
                meta[f"approx_size:{entry.file_name}"] = str(entry.length)
        issued = [e.issued for e in chosen if e.issued is not None]
        release = ReleaseRef(
            source_id=spec.id,
            version=release_version([e.file_name for e in chosen], bundle=not selector.single_book),
            published=max(issued) if issued else None,
            files=tuple(files),
            meta=meta,
            format=ZIM_FORMAT,
        )
        return [release]

    def upstream_checksums(self, ctx: Ctx, file_url: str) -> tuple[Checksum, ...]:
        """The SHA-256 Kiwix publishes next to a ZIM (``<file>.sha256``); empty if there is none."""
        name = file_url.rsplit("/", 1)[-1]
        try:
            data = ctx.fetcher.get_bytes(file_url + ".sha256", max_bytes=64 * 1024)
        except FetchError:
            return ()
        digest = parse_checksum_file(data.decode("utf-8", "replace")).get(name)
        return (Checksum("sha256", digest),) if digest else ()

    def plan(self, ctx: Ctx, have: Generation | None, want: ReleaseRef) -> FetchPlan:
        """Plan missing files and enrich each with its metalink (exact size, more hashes, mirrors)."""
        base = plan_missing(have, want)
        return FetchPlan(
            release=want,
            files=tuple(self._resolve(ctx, f, want) for f in base.files),
            previous_generation=base.previous_generation,
        )

    def _resolve(self, ctx: Ctx, remote: RemoteFile, release: ReleaseRef) -> RemoteFile:
        meta4_url = remote.metalink
        if meta4_url is None:
            return remote
        link = parse_metalink(ctx.fetcher.get_bytes(meta4_url))
        algs = {c.algorithm for c in remote.checksums}
        extra = tuple(c for c in link.checksums if c.algorithm not in algs and c.hexdigest)
        for c in link.checksums:
            for mine in remote.checksums:
                if c.algorithm == mine.algorithm and c.hexdigest != mine.hexdigest.lower():
                    raise ValueError(f"{remote.name}: metalink {c.algorithm} disagrees with .sha256")
        mirrors = tuple(dict.fromkeys((*link.urls, *remote.mirrors)))
        return RemoteFile(
            url=remote.url,
            name=remote.name,
            size=link.size,
            checksums=remote.checksums + extra,
            mirrors=tuple(m for m in mirrors if m != remote.url),
            metalink=meta4_url,
        )

    def fetch(self, ctx: Ctx, plan: FetchPlan) -> Iterable[FetchResult]:
        return fetch_all(ctx, plan)

    def verify(self, ctx: Ctx, remote: RemoteFile, local: Path) -> Verdict:
        return verify_file(remote, local)

    # --- documents -------------------------------------------------------------------------------

    def documents(self, ctx: Ctx, generation_dir: Path) -> Iterator[DocRecord]:
        return (e for e in self.entries(ctx, generation_dir) if isinstance(e, DocRecord))

    def content(self, ctx: Ctx, generation_dir: Path) -> dict[str, Path]:
        """The generation's ZIMs by book key, in it or in earlier generations (:func:`bundle_content`)."""
        return bundle_content(generation_dir, single_book=Selector.from_spec(ctx.source).single_book)

    def depends_on(self, ctx: Ctx, generation_dir: Path) -> frozenset[str]:
        """Earlier generations holding ZIMs this generation reads (``GenerationDependencies``)."""
        holders = {_generation_of(p) for p in self.content(ctx, generation_dir).values()}
        return frozenset(holders - {generation_dir.name})

    def entries(self, ctx: Ctx, generation_dir: Path) -> Iterator[Entry]:
        spec = ctx.source
        prefix = prefix_books(spec)
        zims = self.content(ctx, generation_dir)
        if not prefix and len(zims) > 1:
            raise ValueError(
                f"{spec.id}: access.prefix_books is false but the generation holds {len(zims)} books "
                "(their ids would collide); pin prefix_books: true for a bundle"
            )
        with WorkerPool(int_option(spec, "workers", default_workers())) as pool:
            for _, zim in sorted(zims.items()):
                params = zim_params(zim, spec.id, spec.license, prefix)
                yield from read_zim(zim, params, workdir=ctx.workdir, pool=pool)


@dataclass(frozen=True, slots=True)
class ZimParams:
    """What every document of one ZIM shares."""

    source_id: str
    license: str
    prefix: str  # "" or "<book>/"
    lang: str
    revision: str
    timestamp: datetime | None


def book_name(archive: Archive, path: Path) -> str:
    """The stable book name (``Name`` metadata, else the file name without its date)."""
    if "Name" in archive.metadata_keys:
        name = archive.get_metadata("Name").decode("utf-8", errors="replace").strip()
        if name:
            return name
    return book_key(path.name)


def _metadata(archive: Archive, key: str) -> str:
    if key not in archive.metadata_keys:
        return ""
    return archive.get_metadata(key).decode("utf-8", errors="replace").strip()


def zim_params(path: Path, source_id: str, license_: str, prefix_books: bool) -> ZimParams:
    archive = Archive(path)  # metadata only; entries are read by wsp_adapters.zimscan
    lang3 = _metadata(archive, "Language").split(",")[0]
    return ZimParams(
        source_id=source_id,
        license=license_,
        prefix=f"{book_name(archive, path)}/" if prefix_books else "",
        lang=_LANG3TO1.get(lang3, lang3 or "und"),
        revision=path.stem,
        timestamp=_parse_dt(_metadata(archive, "Date")),
    )


def _stable(path: str) -> bool:
    """Representable as a stable id (PLAN §9.2): non-empty, no leading or trailing whitespace."""
    return bool(path) and path == path.strip()


@dataclass(frozen=True, slots=True)
class _HtmlTask:
    clusters: zimscan.ClusterTask
    params: ZimParams


def _read_html(task: _HtmlTask) -> list[DocRow]:
    """Worker: the documents of one run of clusters, as :func:`doc_row` tuples (separate process)."""
    p = task.params
    out: list[DocRow] = []
    for blob in zimscan.read_clusters(task.clusters):
        if not _stable(blob.path):
            continue
        native_id = p.prefix + blob.path
        text = html_to_text(blob.content)
        out.append(
            doc_row(
                DocRecord(
                    doc_id=make_doc_id(p.source_id, native_id),
                    source_id=p.source_id,
                    native_id=native_id,
                    title=blob.title,
                    lang=p.lang,
                    license=p.license,
                    text=text,
                    revision=p.revision,
                    timestamp=p.timestamp,
                    html_ref=f"zim:{native_id}",
                    content_hash=content_hash(text),
                )
            )
        )
    return out


def read_zim(path: Path, params: ZimParams, *, workdir: Path, pool: WorkerPool) -> Iterator[Entry]:
    """Every HTML document of a ZIM (cluster order), then every redirect (entry order).

    The directory is scanned once into ``workdir`` (local disk; removed afterwards), then clusters
    are decoded in runs of up to ``zimscan.TASK_BYTES`` compressed bytes, each once.
    """
    workdir.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix=f"zimscan-{path.stem[:40]}-", dir=workdir))
    try:
        scan, tasks = zimscan.scan_tasks(path, root)
        for _ in pool.map(zimscan.scan_window, tasks):
            pass
        with path.open("rb") as fh:
            offsets = zimscan.cluster_offsets(fh, scan.header, dirents_at=scan.dirents_at())
        html = (_HtmlTask(t, params) for t in zimscan.content_tasks(scan, offsets))
        # window: results in flight; each is at most zimscan.TASK_BYTES of compressed HTML as text
        for rows in pool.map(_read_html, html, window=pool.workers + 1):
            yield from map(doc_from_row, rows)
        for pairs in pool.map(zimscan.read_redirects, zimscan.redirect_tasks(scan)):
            for source, target in pairs:
                if _stable(source):
                    yield Redirect(params.prefix + source, params.prefix + target)
    finally:
        zimscan.remove_scan(root)
