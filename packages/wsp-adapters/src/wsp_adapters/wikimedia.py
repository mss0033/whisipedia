"""``wikimedia-dumps`` adapter: XML page dumps and Wikidata entity dumps (PLAN §6.2).

Which wikis a source covers:

* ``access.wikis: [enwiki, dewiki]`` names them explicitly;
* ``wikipedia-*`` sources with ``languages: [en, de, ...]`` map to ``enwiki``, ``dewiki``, ...;
* ``<project>-<lang>`` ids (``wiktionary-en``, ``wikisource-en``) map to ``enwiktionary``, ...;
* ``wikidata`` (or an endpoint under ``/wikidatawiki/entities/``) uses the entity dumps.

Discovery lists ``https://dumps.wikimedia.org/<wiki>/`` and reads ``<date>/dumpstatus.json`` of
the newest runs. A run is a release only when ``articlesmultistreamdump`` is ``done`` on every wiki
of the source. Files come from the recombined job when it is done (one ``.xml.bz2`` + one index
per wiki) or from the per-part job otherwise (``access.layout: parts`` forces parts). Checksums
are the SHA-1 and MD5 in ``dumpstatus.json``, falling back to ``<wiki>-<date>-sha1sums.txt``.
Every file also lists the Wikimedia mirrors as alternates.

Native ids are page titles in URL form (``Alan_Turing``). Sources spanning several wikis prefix the
language: ``en/Alan_Turing``. Wikidata native ids are entity ids (``Q42``).

**Parallel decompression.** ``pages-articles-multistream`` files are a concatenation of
independent bz2 streams of 100 pages each, and the companion index gives every stream's byte
offset. ``entries()`` cuts the file into ~8 MiB groups of whole streams and decodes them in worker
processes (bz2 decoding and wikitext stripping are CPU bound, and bz2 is the slowest part at
~30-60 MB/s per core). With N cores this is close to N times faster than a sequential pass, and
memory stays bounded because at most ``2 x workers`` groups are in flight. Without an index the
file is streamed sequentially (through ``lbzip2``/``pbzip2`` when installed).
"""

from __future__ import annotations

import bz2
import json
import logging
import re
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any, cast
from urllib.parse import quote
from xml.etree import ElementTree as ET

from wsp_adapters._common import (
    DocRow,
    access,
    doc_from_row,
    doc_row,
    fetch_all,
    int_option,
    ordered_parallel_map,
    parse_checksum_file,
    plan_missing,
    str_list,
    upstream_dir,
    verify_file,
    workers_for,
)
from wsp_adapters._streams import open_bz2
from wsp_adapters.entries import Entry, Redirect
from wsp_adapters.wikitext import wikitext_to_text
from wsp_core.ids import InvalidDocIdError, content_hash, make_doc_id
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

log = logging.getLogger(__name__)

DUMPS_BASE = "https://dumps.wikimedia.org/"
# Full mirrors of dumps.wikimedia.org/ with the same layout (https://dumps.wikimedia.org/mirrors.html).
# Mirrors can lag; every download is verified against the checksums from the primary site.
DEFAULT_MIRRORS = (
    "https://ftp.acc.umu.se/mirror/wikimedia.org/dumps/",
    "https://dumps.wikimedia.your.org/",
)
ENTITIES_PATH = "wikidatawiki/entities/"
DEFAULT_ENTITY_FILES = ("all.json.bz2",)

# Project family name -> dump database suffix. Longest suffixes are matched first.
_FAMILY_SUFFIX = {
    "wikipedia": "wiki",
    "wiktionary": "wiktionary",
    "wikisource": "wikisource",
    "wikibooks": "wikibooks",
    "wikiquote": "wikiquote",
    "wikivoyage": "wikivoyage",
    "wikinews": "wikinews",
    "wikiversity": "wikiversity",
}
_SUFFIX_FAMILY = {v: k for k, v in _FAMILY_SUFFIX.items()}

_DATE_DIR_RE = re.compile(r'href="(\d{8})/"')
_LISTING_RE = re.compile(r'<a href="([^"/]+)">[^<]*</a>\s+\S+\s+\S+\s+(\d+)')
_MULTISTREAM_RE = re.compile(r"^(?P<wiki>[a-z0-9_]+)-(?P<date>\d{8})-pages-articles-multistream(?P<rest>.*)$")
_INDEX_NAME_RE = re.compile(r"^(.*-pages-articles-multistream)(\d*)\.xml(.*)\.bz2$")
_ENTITY_DUMP_RE = re.compile(r"^wikidata-\d{8}-all\.json\.bz2$")
_PAGE_RE = re.compile(rb"<page>.*?</page>", re.DOTALL)
_GROUP_BYTES = 8 * 1024 * 1024


# --- wiki naming ---------------------------------------------------------------------------------


def wikis_for(spec: SourceSpec) -> tuple[str, ...]:
    """Dump database names (``enwiki``) for a source; empty for the Wikidata entity dumps."""
    explicit = str_list(access(spec).get("wikis"))
    if explicit:
        return explicit
    if is_entity_source(spec):
        return ()
    family, _, lang = spec.id.partition("-")
    if family == "wikipedia":
        langs = str_list(spec.raw.get("languages")) or ((lang,) if lang else ())
        if langs:
            return tuple(f"{lang.replace('-', '_')}wiki" for lang in langs)
    if family in _FAMILY_SUFFIX and lang:
        return (f"{lang.replace('-', '_')}{_FAMILY_SUFFIX[family]}",)
    raise ValueError(f"{spec.id}: cannot derive wiki names; set access.wikis")


def is_entity_source(spec: SourceSpec) -> bool:
    endpoints = str_list(access(spec).get("endpoints"))
    return spec.id == "wikidata" or any(ENTITIES_PATH in e for e in endpoints)


@dataclass(frozen=True, slots=True)
class WikiSite:
    wiki: str
    lang: str
    domain: str
    capitalize: bool  # first letter of titles is case-insensitive (all projects but Wiktionary)


def wiki_site(wiki: str) -> WikiSite:
    if wiki == "wikidatawiki":
        return WikiSite(wiki, "mul", "www.wikidata.org", True)
    for suffix in sorted(_SUFFIX_FAMILY, key=len, reverse=True):
        if wiki.endswith(suffix) and len(wiki) > len(suffix):
            lang = wiki[: -len(suffix)].replace("_", "-")
            family = _SUFFIX_FAMILY[suffix]
            return WikiSite(wiki, lang, f"{lang}.{family}.org", family != "wiktionary")
    raise ValueError(f"unknown wiki database name: {wiki!r}")


def _iso_date(yyyymmdd: str) -> str:
    return f"{yyyymmdd[:4]}-{yyyymmdd[4:6]}-{yyyymmdd[6:]}"


def _mirrors(path: str, bases: Sequence[str]) -> tuple[str, ...]:
    return tuple(f"{b.rstrip('/')}/{path.lstrip('/')}" for b in bases)


# --- discovery -----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DumpRun:
    wiki: str
    date: str  # YYYYMMDD
    updated: datetime | None
    files: tuple[RemoteFile, ...]


def run_dates(ctx: Ctx, base: str, path: str) -> list[str]:
    """Run directories (``YYYYMMDD``) listed under ``base + path``, oldest first."""
    html = ctx.fetcher.get_bytes(base + path, max_bytes=4 * 1024 * 1024).decode("utf-8", "replace")
    return sorted(set(_DATE_DIR_RE.findall(html)))


def _parse_updated(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
    except ValueError:
        return None


def parse_dumpstatus(
    data: bytes, *, wiki: str, date: str, base: str, mirrors: Sequence[str], layout: str = "combined"
) -> DumpRun | None:
    """A finished pages-articles-multistream run from ``dumpstatus.json``, or None if unfinished."""
    jobs = cast(Mapping[str, Mapping[str, Any]], json.loads(data).get("jobs", {}))
    parts = jobs.get("articlesmultistreamdump", {})
    if parts.get("status") != "done":
        return None
    combined = jobs.get("articlesmultistreamdumprecombine", {})
    job = parts
    if layout == "combined" and combined.get("status") == "done" and combined.get("files"):
        job = combined
    files: list[RemoteFile] = []
    for name, info in sorted(cast(Mapping[str, Mapping[str, Any]], job.get("files", {})).items()):
        rel = str(info.get("url") or f"/{wiki}/{date}/{name}")
        checksums = tuple(Checksum(alg, str(info[alg]).lower()) for alg in ("sha1", "md5") if info.get(alg))
        size = info.get("size")
        files.append(
            RemoteFile(
                url=base.rstrip("/") + rel,
                name=name,
                size=size if isinstance(size, int) else None,
                checksums=checksums,
                mirrors=_mirrors(rel, mirrors),
            )
        )
    if not files:
        return None
    return DumpRun(wiki=wiki, date=date, updated=_parse_updated(job.get("updated")), files=tuple(files))


class WikimediaDumpsAdapter:
    """Reference ``SourceAdapter`` for dumps.wikimedia.org."""

    id = "wikimedia-dumps"

    def __init__(self, base_url: str = DUMPS_BASE) -> None:
        self.base_url = base_url if base_url.endswith("/") else base_url + "/"

    def discover(self, ctx: Ctx) -> Iterable[ReleaseRef]:
        if is_entity_source(ctx.source):
            return self._discover_entities(ctx)
        return self._discover_pages(ctx)

    def _discover_pages(self, ctx: Ctx) -> list[ReleaseRef]:
        spec = ctx.source
        wikis = wikis_for(spec)
        mirrors = str_list(access(spec).get("mirrors")) or DEFAULT_MIRRORS
        layout = str(access(spec).get("layout", "combined"))
        max_runs = int_option(spec, "max_runs", 3)
        max_releases = int_option(spec, "max_releases", 1)
        common: set[str] | None = None
        for wiki in wikis:
            dates = set(run_dates(ctx, self.base_url, f"{wiki}/"))
            common = dates if common is None else common & dates
        releases: list[ReleaseRef] = []
        for date in sorted(common or (), reverse=True)[:max_runs]:
            runs: list[DumpRun] = []
            for wiki in wikis:
                run = self._run(ctx, wiki, date, mirrors, layout)
                if run is None:
                    break
                runs.append(run)
            else:
                releases.append(self._release(spec, date, runs))
                if len(releases) >= max_releases:
                    break
        return releases

    def _run(self, ctx: Ctx, wiki: str, date: str, mirrors: Sequence[str], layout: str) -> DumpRun | None:
        url = f"{self.base_url}{wiki}/{date}/dumpstatus.json"
        try:
            data = ctx.fetcher.get_bytes(url)
        except Exception as exc:  # an unfinished run may not have a status file yet
            log.info("no dumpstatus for %s %s: %s", wiki, date, exc)
            return None
        run = parse_dumpstatus(
            data, wiki=wiki, date=date, base=self.base_url.rstrip("/"), mirrors=mirrors, layout=layout
        )
        if run is None or all(f.checksums for f in run.files):
            return run
        sums = parse_checksum_file(
            ctx.fetcher.get_bytes(f"{self.base_url}{wiki}/{date}/{wiki}-{date}-sha1sums.txt").decode()
        )
        files = tuple(
            f
            if f.checksums or f.name not in sums
            else RemoteFile(f.url, f.name, f.size, (Checksum("sha1", sums[f.name]),), f.mirrors)
            for f in run.files
        )
        return DumpRun(run.wiki, run.date, run.updated, files)

    @staticmethod
    def _release(spec: SourceSpec, date: str, runs: Sequence[DumpRun]) -> ReleaseRef:
        updated = [r.updated for r in runs if r.updated is not None]
        return ReleaseRef(
            source_id=spec.id,
            version=_iso_date(date),
            published=max(updated) if updated else None,
            files=tuple(f for r in runs for f in r.files),
            meta={"run": date, "wikis": ",".join(r.wiki for r in runs)},
            format="mediawiki-xml-multistream",
        )

    def _discover_entities(self, ctx: Ctx) -> list[ReleaseRef]:
        spec = ctx.source
        mirrors = str_list(access(spec).get("mirrors")) or DEFAULT_MIRRORS
        kinds = str_list(access(spec).get("entity_files")) or DEFAULT_ENTITY_FILES
        max_runs = int_option(spec, "max_runs", 3)
        max_releases = int_option(spec, "max_releases", 1)
        releases: list[ReleaseRef] = []
        for date in sorted(run_dates(ctx, self.base_url, ENTITIES_PATH), reverse=True)[:max_runs]:
            release = self._entity_release(ctx, spec, date, kinds, mirrors)
            if release is not None:
                releases.append(release)
                if len(releases) >= max_releases:
                    break
        return releases

    def _entity_release(
        self, ctx: Ctx, spec: SourceSpec, date: str, kinds: Sequence[str], mirrors: Sequence[str]
    ) -> ReleaseRef | None:
        rel_dir = f"/{ENTITIES_PATH}{date}/"
        try:
            sha1 = parse_checksum_file(
                ctx.fetcher.get_bytes(
                    f"{self.base_url}{ENTITIES_PATH}{date}/wikidata-{date}-sha1sums.txt"
                ).decode()
            )
            md5 = parse_checksum_file(
                ctx.fetcher.get_bytes(
                    f"{self.base_url}{ENTITIES_PATH}{date}/wikidata-{date}-md5sums.txt"
                ).decode()
            )
            listing = ctx.fetcher.get_bytes(f"{self.base_url}{ENTITIES_PATH}{date}/").decode(
                "utf-8", "replace"
            )
        except Exception as exc:  # checksum files appear only once the dump is complete
            log.info("entity dump %s not ready: %s", date, exc)
            return None
        sizes = {name: int(size) for name, size in _LISTING_RE.findall(listing)}
        files: list[RemoteFile] = []
        for kind in kinds:
            name = f"wikidata-{date}-{kind}"
            if name not in sha1:
                return None
            checksums = (Checksum("sha1", sha1[name]),) + (
                (Checksum("md5", md5[name]),) if name in md5 else ()
            )
            files.append(
                RemoteFile(
                    url=f"{self.base_url.rstrip('/')}{rel_dir}{name}",
                    name=name,
                    size=sizes.get(name),
                    checksums=checksums,
                    mirrors=_mirrors(rel_dir + name, mirrors),
                )
            )
        return ReleaseRef(
            source_id=spec.id,
            version=_iso_date(date),
            published=None,
            files=tuple(files),
            meta={"run": date, "wikis": "wikidatawiki"},
            format="wikidata-json",
        )

    def plan(self, ctx: Ctx, have: Generation | None, want: ReleaseRef) -> FetchPlan:
        return plan_missing(have, want)

    def fetch(self, ctx: Ctx, plan: FetchPlan) -> Iterable[FetchResult]:
        return fetch_all(ctx, plan)

    def verify(self, ctx: Ctx, remote: RemoteFile, local: Path) -> Verdict:
        return verify_file(remote, local)

    # --- documents -------------------------------------------------------------------------------

    def documents(self, ctx: Ctx, generation_dir: Path) -> Iterator[DocRecord]:
        return (e for e in self.entries(ctx, generation_dir) if isinstance(e, DocRecord))

    def entries(self, ctx: Ctx, generation_dir: Path) -> Iterator[Entry]:
        spec = ctx.source
        workers = workers_for(spec)
        files = sorted(upstream_dir(generation_dir).iterdir())
        if is_entity_source(spec):
            for path in files:
                if _ENTITY_DUMP_RE.match(path.name):
                    yield from read_entities(path, spec, workers=workers)
            return
        multi = len(wikis_for(spec)) > 1
        namespaces = frozenset(int(n) for n in str_list(access(spec).get("namespaces")) or ("0",))
        for path in files:
            match = _MULTISTREAM_RE.match(path.name)
            if match is None or "-index" in match.group("rest") or not path.name.endswith(".bz2"):
                continue
            site = wiki_site(match.group("wiki"))
            cfg = _PageConfig(spec.id, spec.license, site, f"{site.lang}/" if multi else "", namespaces)
            yield from read_multistream(path, index_path_for(path), cfg, workers=workers)


# --- XML page dumps ------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _PageConfig:
    source_id: str
    license: str
    site: WikiSite
    prefix: str
    namespaces: frozenset[int]


@dataclass(frozen=True, slots=True)
class _Range:
    path: Path
    start: int
    stop: int
    cfg: _PageConfig


def index_path_for(path: Path) -> Path | None:
    """``...-multistream[N].xml[-pA-pB].bz2`` -> ``...-multistream-index[N].txt[-pA-pB].bz2``."""
    match = _INDEX_NAME_RE.match(path.name)
    if match is None:
        return None
    candidate = path.with_name(f"{match.group(1)}-index{match.group(2)}.txt{match.group(3)}.bz2")
    return candidate if candidate.is_file() else None


def stream_offsets(index: Path) -> list[int]:
    """Distinct bz2 stream start offsets from a multistream index (``offset:page_id:title`` lines)."""
    offsets: list[int] = []
    last = -1
    with bz2.open(index, "rb") as fh:
        for line in fh:
            offset = int(line.split(b":", 1)[0])
            if offset != last:
                offsets.append(offset)
                last = offset
    return offsets


def group_ranges(offsets: Sequence[int], file_size: int, target: int = _GROUP_BYTES) -> list[tuple[int, int]]:
    """Group consecutive streams into byte ranges of about ``target`` bytes. Skips the header stream."""
    bounds = [*sorted(set(offsets)), file_size]
    ranges: list[tuple[int, int]] = []
    start = bounds[0]
    for i in range(1, len(bounds)):
        if bounds[i] - start >= target or i == len(bounds) - 1:
            ranges.append((start, bounds[i]))
            start = bounds[i]
    return [(a, b) for a, b in ranges if b > a]


def read_multistream(
    path: Path, index: Path | None, cfg: _PageConfig, *, workers: int = 1
) -> Iterator[Entry]:
    """Pages of one dump file, decoded in parallel when the multistream index is available."""
    if index is None:
        with open_bz2(path, workers) as fh:
            for chunk in _page_chunks(fh):
                yield from _parse_pages(chunk, cfg)
        return
    tasks = (_Range(path, a, b, cfg) for a, b in group_ranges(stream_offsets(index), path.stat().st_size))
    for entries in ordered_parallel_map(_decode_range, tasks, workers):
        yield from entries


def _decode_range(task: _Range) -> list[Entry]:
    """Worker: decompress whole bz2 streams in ``[start, stop)`` and parse their pages."""
    with task.path.open("rb") as fh:
        fh.seek(task.start)
        raw = fh.read(task.stop - task.start)
    return list(_parse_pages(bz2.decompress(raw), task.cfg))


def _page_chunks(fh: IO[bytes], chunk: int = 16 * 1024 * 1024) -> Iterator[bytes]:
    """Split a decompressed XML stream into buffers that each end on a ``</page>`` boundary."""
    tail = b""
    while block := fh.read(chunk):
        buf = tail + block
        cut = buf.rfind(b"</page>")
        if cut < 0:
            tail = buf
            continue
        cut += len(b"</page>")
        yield buf[:cut]
        tail = buf[cut:]
    if tail:
        yield tail


def _parse_pages(xml: bytes, cfg: _PageConfig) -> Iterator[Entry]:
    for match in _PAGE_RE.finditer(xml):
        page = ET.fromstring(match.group())  # noqa: S314 - checksum-verified Wikimedia dump
        entry = _page_entry(page, cfg)
        if entry is not None:
            yield entry


def _native(cfg: _PageConfig, title: str) -> str:
    return cfg.prefix + title.strip().replace(" ", "_")


def _page_entry(page: ET.Element, cfg: _PageConfig) -> Entry | None:
    ns_text = page.findtext("ns", "0")
    if int(ns_text) not in cfg.namespaces:
        return None
    title = page.findtext("title", "").strip()
    if not title:
        return None
    native_id = _native(cfg, title)
    redirect = page.find("redirect")
    if redirect is not None:
        target = redirect.get("title", "").partition("#")[0].strip()
        return Redirect(native_id, _native(cfg, target)) if target else None
    rev = page.find("revision")
    if rev is None:
        return None
    wikitext = rev.findtext("text") or ""
    text, link_titles = wikitext_to_text(wikitext, capitalize_links=cfg.site.capitalize)
    links: list[str] = []
    for target in link_titles:
        try:
            links.append(make_doc_id(cfg.source_id, cfg.prefix + target))
        except InvalidDocIdError:
            continue
    stamp = rev.findtext("timestamp")
    return DocRecord(
        doc_id=make_doc_id(cfg.source_id, native_id),
        source_id=cfg.source_id,
        native_id=native_id,
        title=title,
        lang=cfg.site.lang,
        license=cfg.license,
        text=text,
        revision=rev.findtext("id"),
        timestamp=datetime.fromisoformat(stamp.replace("Z", "+00:00")) if stamp else None,
        url_native=f"https://{cfg.site.domain}/wiki/{quote(title.replace(' ', '_'), safe='/:()')}",
        links=tuple(links),
        content_hash=content_hash(text),
    )


# --- Wikidata entity dumps -----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _EntityBatch:
    lines: tuple[bytes, ...]
    source_id: str
    license: str
    lang: str


def read_entities(path: Path, spec: SourceSpec, *, workers: int = 1) -> Iterator[DocRecord]:
    """Entities of a ``wikidata-*-all.json.bz2`` dump (one JSON object per line inside an array).

    Decompression is sequential (the file is not multistream) but parallel per block
    (:func:`wsp_adapters._streams.open_bz2`); claims are cut in the parent (:func:`entity_lines`)
    and JSON parsing and rendering run in worker processes.
    """
    lang = str(access(spec).get("lang", "en"))
    batches = (_EntityBatch(lines, spec.id, spec.license, lang) for lines in entity_lines(path, workers))
    for rows in ordered_parallel_map(_decode_entities, batches, workers):
        yield from map(doc_from_row, rows)


def _decode_entities(batch: _EntityBatch) -> list[DocRow]:
    out: list[DocRow] = []
    for line in batch.lines:
        doc = entity_record(parse_entity(line), batch.source_id, batch.license, batch.lang)
        if doc is not None:
            out.append(doc_row(doc))
    return out


# Top-level layout of a dump line (2026-10-05, every entity of a 5,000-line sample): type, id,
# labels, descriptions, aliases, claims, sitelinks, pageid, ns, title, lastrevid, modified.
_CLAIMS = b'"claims":'
_NEXT = b',"sitelinks":'
_BEFORE = (b'"labels":', b'"descriptions":', b'"aliases":')  # read keys that must precede claims
_AFTER = (b'"lastrevid":', b'"modified":')  # read keys that must follow sitelinks
ENTITY_BATCH_BYTES = 8 << 20  # bytes of (claims-stripped) entity JSON per worker task
ENTITY_BATCH_LINES = 10_000


class EntityFormatError(ValueError):
    """A claims-stripped entity line is not valid JSON: the dump's layout changed."""


def strip_claims(line: bytes) -> bytes:
    """One entity's JSON without its top-level ``claims`` member (audit 2026-10-11 H2).

    Claims are about three quarters of the dump's bytes and no adapter reads them. The cut is
    structural: in valid JSON, ``"claims":`` and ``,"sitelinks":`` with unescaped quotes can only
    be key tokens. It is made only when every key this module reads is outside the cut: labels,
    descriptions and aliases before ``"claims":``, lastrevid and modified after ``,"sitelinks":``
    (an object has no duplicate keys, so they cannot also be inside it). The claims bytes are
    scanned once. :func:`parse_entity` rejects a result that does not parse or lacks ``id``;
    a line of any other layout is returned whole.
    """
    a = line.find(_CLAIMS)
    if a < 0 or any(line.find(key, 0, a) < 0 for key in _BEFORE):
        return line
    b = line.find(_NEXT, a)
    if b < 0 or any(line.find(key, b) < 0 for key in _AFTER):
        return line
    return line[:a] + line[b + 1 :]  # drop '"claims":{...},' (the comma before "sitelinks")


def parse_entity(line: bytes) -> Mapping[str, Any]:
    """The JSON object of one (possibly claims-stripped) dump line."""
    try:
        entity: object = json.loads(line)
    except ValueError as exc:
        raise EntityFormatError(f"entity line does not parse after cutting claims: {line[:120]!r}") from exc
    if not isinstance(entity, dict) or "id" not in entity:
        raise EntityFormatError(f"entity line has no id: {line[:120]!r}")
    return cast(Mapping[str, Any], entity)


def entity_lines(path: Path, workers: int) -> Iterator[tuple[bytes, ...]]:
    """Claims-stripped entity lines of a JSON dump in batches of at most ``ENTITY_BATCH_BYTES``.

    Batches are bounded by bytes, not lines: the first entities of the dump are hundreds of KB
    each (a 1,000-line batch pickled to 79 MB), later ones a few KB.
    """
    with open_bz2(path, workers) as fh:
        batch: list[bytes] = []
        size = 0
        for raw in fh:
            line = raw.strip().rstrip(b",")
            if not line or line in (b"[", b"]"):
                continue
            line = strip_claims(line)
            batch.append(line)
            size += len(line)
            if size >= ENTITY_BATCH_BYTES or len(batch) >= ENTITY_BATCH_LINES:
                yield tuple(batch)
                batch, size = [], 0
        if batch:
            yield tuple(batch)


def _lang_value(values: object, lang: str) -> str:
    if not isinstance(values, Mapping):
        return ""
    by_lang = cast(Mapping[str, Mapping[str, Any]], values)
    chosen = by_lang.get(lang) or by_lang.get("mul") or next(iter(by_lang.values()), None)
    return str(chosen.get("value", "")) if chosen else ""


def entity_record(entity: Mapping[str, Any], source_id: str, license_: str, lang: str) -> DocRecord | None:
    """Render one Wikidata entity as a short searchable document (label, description, aliases)."""
    eid = str(entity.get("id", ""))
    if not eid:
        return None
    label = _lang_value(entity.get("labels"), lang)
    description = _lang_value(entity.get("descriptions"), lang)
    alias_map = cast(Mapping[str, list[Mapping[str, Any]]], entity.get("aliases") or {})
    aliases = [str(a.get("value", "")) for a in alias_map.get(lang, [])]
    lines = [label or eid, description]
    if aliases:
        lines.append("Also known as: " + "; ".join(aliases))
    text = "\n".join(line for line in lines if line)
    page = {"property": "Property:", "lexeme": "Lexeme:"}.get(str(entity.get("type", "")), "")
    stamp = entity.get("modified")
    return DocRecord(
        doc_id=make_doc_id(source_id, eid),
        source_id=source_id,
        native_id=eid,
        title=label or eid,
        lang=lang,
        license=license_,
        text=text,
        revision=str(entity["lastrevid"]) if "lastrevid" in entity else None,
        timestamp=datetime.fromisoformat(str(stamp).replace("Z", "+00:00")) if stamp else None,
        url_native=f"https://www.wikidata.org/wiki/{page}{eid}",
        entities=(eid,) if eid.startswith("Q") else (),
        content_hash=content_hash(text),
        aliases=tuple(dict.fromkeys(a for a in aliases if a and a != label)),
    )
