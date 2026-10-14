"""``open-library`` adapter: Internet Archive's Open Library dumps (PLAN §6.2, ``http-files``).

**These are catalog records, not books.** Each document is a short bibliographic record (a few
lines: title, authors, subjects, identifiers), and its text says so in its first line, so that
search ranking and the Oracle never mistake a record for the work itself. Full texts come from
Gutenberg, Standard Ebooks and the like; entity linking joins them through Wikidata QIDs.

**Discover.** ``https://openlibrary.org/data/ol_dump_<kind>_latest.txt.gz`` redirects to the dated
dump on archive.org; the release version is that date (``YYYY-MM-DD``) when the fetcher exposes
the final URL through ``head()``, else today's date. Files are ``ol_dump_{authors,works,editions}``
(``access.kinds``). Open Library publishes no checksums next to the dumps, so files are
*unverifiable* (the vault still records our own SHA-256/BLAKE3 at ingest).

**Format.** Tab-separated ``type, key, revision, last_modified, JSON`` (one record per line).
Redirect and delete records are skipped.

**Documents.** Authors first, then works, then editions (``native_id`` = key without the leading
slash, e.g. ``works/OL45804W``). Work and edition author keys are resolved to names through an
SQLite lookup on local disk (``ctx.workdir``) filled from the authors file in the first pass, so
memory stays bounded for ~15M authors. Wikidata ids in ``remote_ids``/``identifiers`` become
``entities``; alternate names and subtitles become ``aliases``. JSON decoding runs in worker
processes; ``.gz`` is decoded by ``pigz`` when installed.
"""

from __future__ import annotations

import gzip
import json
import logging
import re
import shutil
import sqlite3
import subprocess
from collections.abc import Generator, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any, cast

from wsp_adapters._common import (
    access,
    fetch_all,
    files_across_generations,
    head_release,
    head_version,
    holder_of,
    ordered_parallel_map,
    plan_by_head,
    str_list,
    verify_file,
    workers_for,
)
from wsp_core.ids import content_hash, make_doc_id
from wsp_core.model import (
    DocRecord,
    FetchPlan,
    FetchResult,
    Generation,
    ReleaseRef,
    RemoteFile,
    SourceSpec,
    Verdict,
)
from wsp_core.ports import Ctx, SupportsHead

log = logging.getLogger(__name__)

DATA_URL = "https://openlibrary.org/data/"
KINDS = ("authors", "works", "editions")
FILE_RE = re.compile(r"^ol_dump_(?P<kind>[a-z]+)_(?P<date>latest|\d{4}-\d{2}-\d{2})\.txt\.gz$")
_DATED_RE = re.compile(r"ol_dump_(\d{4}-\d{2}-\d{2})")
_BATCH = 2_000

# Open Library language keys (MARC codes) -> BCP 47 for the most common ones.
_LANGS = {
    "eng": "en", "ger": "de", "fre": "fr", "spa": "es", "ita": "it", "rus": "ru", "jpn": "ja",
    "chi": "zh", "por": "pt", "dut": "nl", "pol": "pl", "swe": "sv", "lat": "la", "ara": "ar",
}  # fmt: skip


def kind_of(name: str) -> str | None:
    match = FILE_RE.match(name)
    return match.group("kind") if match else None


class OpenLibraryAdapter:
    """``SourceAdapter`` for the monthly Open Library dumps."""

    id = "open-library"

    def discover(self, ctx: Ctx) -> Iterable[ReleaseRef]:
        spec = ctx.source
        base = (str_list(access(spec).get("endpoints")) or (DATA_URL,))[0]
        base = base if base.endswith("/") else base + "/"
        names = self.file_names(spec)
        files, meta = head_release(ctx.fetcher, [(f"{base}{n}", n) for n in names])
        # ``*_latest`` redirects to the dated dump; that date names the release. Without it the
        # version is the HEAD identity (audit H3); today's date only when HEAD says nothing.
        dated = ""
        if isinstance(ctx.fetcher, SupportsHead):
            for f in files:
                if match := _DATED_RE.search(ctx.fetcher.head(f.url).url):
                    dated = max(dated, match.group(1))
        version = dated or head_version(files, meta) or datetime.now(UTC).strftime("%Y-%m-%d")
        return [
            ReleaseRef(
                source_id=spec.id,
                version=version,
                published=None,
                files=tuple(files),
                meta=meta,
                format="openlibrary-tsv",
            )
        ]

    @staticmethod
    def file_names(spec: SourceSpec) -> tuple[str, ...]:
        return tuple(f"ol_dump_{k}_latest.txt.gz" for k in (str_list(access(spec).get("kinds")) or KINDS))

    def plan(self, ctx: Ctx, have: Generation | None, want: ReleaseRef) -> FetchPlan:
        """Skip dump files the previous generation holds unchanged (no checksums upstream)."""
        return plan_by_head(have, want)

    def depends_on(self, ctx: Ctx, generation_dir: Path) -> frozenset[str]:
        """Earlier generations holding dump files this one reads (``GenerationDependencies``)."""
        files = files_across_generations(generation_dir, self.file_names(ctx.source))
        return frozenset(holder_of(p) for p in files.values()) - {generation_dir.name}

    def fetch(self, ctx: Ctx, plan: FetchPlan) -> Iterable[FetchResult]:
        return fetch_all(ctx, plan)

    def verify(self, ctx: Ctx, remote: RemoteFile, local: Path) -> Verdict:
        return verify_file(remote, local)

    # --- documents -------------------------------------------------------------------------------

    def documents(self, ctx: Ctx, generation_dir: Path) -> Iterator[DocRecord]:
        spec = ctx.source
        workers = workers_for(spec)
        wanted = set(str_list(access(spec).get("kinds")) or KINDS)
        # One file per kind: the release's own names, from this or an earlier generation.
        files = {
            k: p
            for p in files_across_generations(generation_dir, self.file_names(spec)).values()
            if (k := kind_of(p.name))
        }
        ctx.workdir.mkdir(parents=True, exist_ok=True)
        names_db = ctx.workdir / "openlibrary-authors.sqlite"
        names_db.unlink(missing_ok=True)
        db = sqlite3.connect(names_db)
        try:  # the lookup is local scratch: removed however the pass ends (audit LOW)
            db.executescript(
                "PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF;"
                "CREATE TABLE authors (key TEXT PRIMARY KEY, name TEXT) WITHOUT ROWID;"
            )
            if "authors" in files:
                batches = _batches(files["authors"], spec.id, spec.license, workers)
                for docs in ordered_parallel_map(decode_batch, batches, workers):
                    db.executemany(
                        "INSERT OR REPLACE INTO authors VALUES (?, ?)",
                        [(d.native_id, d.title) for d in docs],
                    )
                    if "authors" in wanted:
                        yield from docs
                db.commit()
            for kind in ("works", "editions"):
                if kind in files and kind in wanted:
                    batches = _with_names(_batches(files[kind], spec.id, spec.license, workers), db)
                    for docs in ordered_parallel_map(decode_batch, batches, workers):
                        yield from docs
        finally:
            db.close()
            names_db.unlink(missing_ok=True)


def _with_names(batches: Iterable[_Batch], db: sqlite3.Connection) -> Iterator[_Batch]:
    """Attach the names of the authors a batch references (looked up in the main process)."""
    for batch in batches:
        keys = {k for line in batch.lines for k in _author_keys(line)}
        yield _Batch(batch.lines, batch.source_id, batch.license, _names(db, keys))


@contextmanager
def _open_gz(path: Path, workers: int) -> Generator[IO[bytes]]:
    tool = shutil.which("pigz")
    if tool is None or workers <= 1:
        with gzip.open(path, "rb") as fh:
            yield cast(IO[bytes], fh)
        return
    proc = subprocess.Popen([tool, "-dc", str(path)], stdout=subprocess.PIPE)  # noqa: S603 - fixed argv
    assert proc.stdout is not None
    try:
        yield proc.stdout
    finally:
        proc.stdout.close()
        if proc.wait() not in (0, -13):
            raise OSError(f"pigz failed on {path.name} with status {proc.returncode}")


@dataclass(frozen=True, slots=True)
class _Batch:
    lines: tuple[bytes, ...]
    source_id: str
    license: str
    names: Mapping[str, str]


def _batches(path: Path, source_id: str, license_: str, workers: int) -> Iterator[_Batch]:
    with _open_gz(path, workers) as fh:
        batch: list[bytes] = []
        for line in fh:
            batch.append(line)
            if len(batch) >= _BATCH:
                yield _Batch(tuple(batch), source_id, license_, {})
                batch = []
        if batch:
            yield _Batch(tuple(batch), source_id, license_, {})


_AUTHOR_KEY_RE = re.compile(rb'"/authors/(OL\d+A)"')


def _author_keys(line: bytes) -> list[str]:
    return [f"authors/{m.decode()}" for m in _AUTHOR_KEY_RE.findall(line)]


def _names(db: sqlite3.Connection, keys: set[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    ordered = sorted(keys)
    for i in range(0, len(ordered), 500):
        chunk = ordered[i : i + 500]
        marks = ",".join("?" * len(chunk))
        query = f"SELECT key, name FROM authors WHERE key IN ({marks})"  # noqa: S608 - placeholders only
        out.update(db.execute(query, chunk).fetchall())
    return out


def decode_batch(batch: _Batch) -> list[DocRecord]:
    """Worker-safe: dump lines -> documents."""
    out: list[DocRecord] = []
    for line in batch.lines:
        parts = line.rstrip(b"\n").split(b"\t", 4)
        if len(parts) != 5:
            continue
        kind = parts[0].decode()
        if kind not in ("/type/author", "/type/work", "/type/edition"):
            continue  # redirects, deletes, other types
        try:
            record = cast(dict[str, Any], json.loads(parts[4]))
        except ValueError:
            continue
        doc = record_doc(kind, record, batch.source_id, batch.license, batch.names, parts[3].decode())
        if doc is not None:
            out.append(doc)
    return out


# --- rendering ---------------------------------------------------------------------------------------


def _str(value: object) -> str:
    """Open Library text fields are strings or ``{"type": "/type/text", "value": ...}``."""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, Mapping):
        return str(cast(Mapping[str, object], value).get("value", "")).strip()
    return ""


def _strs(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [s for v in cast(list[object], value) if (s := _str(v))]


def _keys(value: object, field: str = "key") -> list[str]:
    """Keys from ``[{"key": "/authors/OL1A"}]`` or ``[{"author": {"key": ...}}]`` lists."""
    out: list[str] = []
    if not isinstance(value, list):
        return out
    for item in cast(list[object], value):
        if not isinstance(item, Mapping):
            continue
        entry = cast(Mapping[str, object], item)
        ref = entry.get("author", entry)
        if isinstance(ref, Mapping):
            key = cast(Mapping[str, object], ref).get(field)
            if isinstance(key, str):
                out.append(key.strip("/"))
    return out


def _wikidata(record: Mapping[str, Any]) -> tuple[str, ...]:
    found: list[str] = []
    for field in ("remote_ids", "identifiers"):
        ids = record.get(field)
        if isinstance(ids, Mapping):
            value = cast(Mapping[str, object], ids).get("wikidata")
            values = cast(list[object], value) if isinstance(value, list) else [value]
            found += [str(v) for v in values if isinstance(v, str) and re.fullmatch(r"Q\d+", v)]
    return tuple(dict.fromkeys(found))


def _stamp(value: str) -> datetime | None:
    try:
        stamp = datetime.fromisoformat(value)
    except ValueError:
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)


_EXTERNAL = (
    # identifier field -> (label, wsp link or URL template, None when the library has no such site)
    ("project_gutenberg", "Project Gutenberg #{id}", "wsp:gutenberg/{id}"),
    ("standard_ebooks", "Standard Ebooks {id}", "wsp:standard-ebooks/{id}"),
    ("wikisource", "Wikisource {id}", None),
    ("librivox", "LibriVox {id}", None),
)


def _external(record: Mapping[str, Any]) -> tuple[str, list[str]]:
    """The copies of the book elsewhere that a catalog card names (``identifiers`` and ``ocaid``): a
    text line for the reader and, where the library has a site for it, a ``wsp:`` link the API
    resolves to the book itself when the library holds it (Open Library cards go right to the
    book, or say why not; owner 2026-10-13)."""
    found: list[str] = []
    links: list[str] = []
    ids = record.get("identifiers")
    if isinstance(ids, Mapping):
        table = cast(Mapping[str, object], ids)
        for field, label, link in _EXTERNAL:
            for value in _strs(table.get(field)) or _strs([table.get(field)]):
                value = value.strip().strip("/")
                if not value:
                    continue
                found.append(label.format(id=value))
                if link is not None:
                    links.append(link.format(id=value))
    ocaid = _str(record.get("ocaid"))
    if ocaid:
        found.append(f"Internet Archive {ocaid}")
        links.append(f"https://archive.org/details/{ocaid}")
    return _join("Also at", found), links


def _join(label: str, values: Sequence[str]) -> str:
    return f"{label}: {'; '.join(values)}" if values else ""


def record_doc(
    kind: str,
    record: Mapping[str, Any],
    source_id: str,
    license_: str,
    names: Mapping[str, str],
    modified: str,
) -> DocRecord | None:
    key = str(record.get("key", "")).strip("/")
    if not key:
        return None
    aliases: list[str] = []
    lang = "en"
    if kind == "/type/author":
        title = _str(record.get("name")) or _str(record.get("personal_name")) or key
        aliases = [a for a in _strs(record.get("alternate_names")) if a != title]
        life = "-".join(x for x in (_str(record.get("birth_date")), _str(record.get("death_date"))) if x)
        lines = [
            f"{title}{f' ({life})' if life else ''}",
            "Open Library catalog record: author.",
            _join("Also known as", aliases),
            _str(record.get("bio")),
        ]
    elif kind == "/type/work":
        title = _str(record.get("title")) or key
        subtitle = _str(record.get("subtitle"))
        authors = [names.get(k, "") for k in _keys(record.get("authors"))]
        if subtitle:
            aliases.append(f"{title}: {subtitle}")
        lines = [
            f"{title}{': ' + subtitle if subtitle else ''}",
            "Open Library catalog record: work.",
            _join("By", [a for a in authors if a]),
            _join("First published", _strs([record.get("first_publish_date")])),
            _external(record)[0],
            _join("Subjects", _strs(record.get("subjects"))),
            _join("Places", _strs(record.get("subject_places"))),
            _join("People", _strs(record.get("subject_people"))),
            _join("Times", _strs(record.get("subject_times"))),
            _str(record.get("description")),
        ]
    else:
        title = _str(record.get("title")) or key
        subtitle = _str(record.get("subtitle"))
        authors = [names.get(k, "") for k in _keys(record.get("authors"))]
        langs = [k.rsplit("/", 1)[-1] for k in _keys(record.get("languages"))]
        lang = _LANGS.get(langs[0], langs[0]) if langs else "und"
        others = (_str(record.get("full_title")), *_strs(record.get("other_titles")))
        aliases = [a for a in others if a and a != title]
        isbns = _strs(record.get("isbn_13")) + _strs(record.get("isbn_10"))
        by = [a for a in authors if a] or _strs([record.get("by_statement")])
        published = _strs([", ".join(_strs(record.get("publishers"))), record.get("publish_date")])
        lines = [
            f"{title}{': ' + subtitle if subtitle else ''}",
            "Open Library catalog record: edition.",
            _join("By", by),
            _join("Published", published),
            _join("Format", _strs([record.get("physical_format"), record.get("pagination")])),
            _join("ISBN", isbns),
            _external(record)[0],
            _join("Subjects", _strs(record.get("subjects"))),
            _join("Work", _keys(record.get("works"))),
        ]
    text = "\n".join(line for line in lines if line)
    stamp = _stamp(modified)
    links = [*_external(record)[1], *(f"https://openlibrary.org/{k}" for k in _keys(record.get("works")))]
    return DocRecord(
        doc_id=make_doc_id(source_id, key),
        source_id=source_id,
        native_id=key,
        title=title,
        lang=lang,
        license=license_,
        text=text,
        revision=str(record.get("revision")) if record.get("revision") is not None else None,
        timestamp=stamp,
        url_native=f"https://openlibrary.org/{key}",
        entities=_wikidata(record),
        links=tuple(links),
        content_hash=content_hash(text),
        aliases=tuple(dict.fromkeys(aliases)),
    )
