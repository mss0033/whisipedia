"""``wikidata`` adapter: compact entity documents plus a local entity lookup (PLAN §6.2, §9.1).

Discovery, planning, fetching and verification are those of ``wikimedia-dumps`` for the entity
dumps (``wikidatawiki/entities/<date>/wikidata-<date>-all.json.bz2``, SHA-1 and MD5 from the
published checksum files). This adapter differs in what it reads out of the dump:

* **Documents.** One per entity (``native_id`` = ``Q42``/``P31``/``L7``): label, description and
  aliases in every language of ``access.languages`` (default: the registry ``languages`` or
  ``[en]``), plus the entity's sitelinks to the wikis of those languages as ``links``
  (``https://en.wikipedia.org/wiki/Douglas_Adams``). The first language that has a label gives the
  title; other labels and aliases become ``aliases``.
* **Entity lookup** for entity linking (PLAN §9.1): while documents stream, every *item* is also
  written to an SQLite database on local disk, ``<ctx.workdir>/wikidata-entities.sqlite``
  (table ``entities(id INTEGER PRIMARY KEY, qid, label, description, aliases, sitelinks)``,
  ``aliases``/``sitelinks`` as JSON). The integer key (``Q42`` -> 42) makes inserts nearly
  sequential, so the build runs at stream speed. The file is written as ``.partial`` and renamed
  only after the whole dump has been read; :class:`EntityLookup` reads it (``get``, ``label``,
  ``by_sitelink``). Set ``access.entity_lookup: false`` to skip it.

**Throughput.** The JSON dump is a *single* bz2 stream (checked on the 2026-10-05 dump: no stream
boundary in the first 200 MB), so the multistream trick of ``wikimedia-dumps`` does not apply.
:func:`wsp_adapters._streams.open_bz2` decodes it with ``lbzip2`` when installed, else with
``indexed_bzip2``'s block-parallel decoder (about 8x the speed of ``bz2`` on 15 threads). The
parent cuts each entity's ``claims`` (about three quarters of the bytes, never read) with one
structural scan (:func:`wsp_adapters.wikimedia.strip_claims`), and JSON decoding runs in
``access.workers`` processes in batches of at most ``ENTITY_BATCH_BYTES`` (8 MiB). On 5,000 real
entities: 414 -> 140 us of worker time per entity, 37 us in the parent, a quarter of the bytes
sent. Memory stays bounded: at most ``2 x workers`` batches in flight and SQLite's page cache.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote

from wsp_adapters._common import (
    DocRow,
    IncompleteGenerationError,
    access,
    doc_from_row,
    doc_row,
    ordered_parallel_map,
    str_list,
    upstream_dir,
    workers_for,
)
from wsp_adapters.wikimedia import WikimediaDumpsAdapter, entity_lines, parse_entity
from wsp_core.ids import content_hash, make_doc_id
from wsp_core.model import DocRecord, SourceSpec
from wsp_core.ports import Ctx

LOOKUP_NAME = "wikidata-entities.sqlite"
_DUMP_SUFFIX = "-all.json.bz2"


def languages_for(spec: SourceSpec) -> tuple[str, ...]:
    return str_list(access(spec).get("languages")) or str_list(spec.raw.get("languages")) or ("en",)


def _wiki(lang: str) -> str:
    return f"{lang.replace('-', '_')}wiki"


@dataclass(frozen=True, slots=True)
class LookupRow:
    numeric_id: int
    qid: str
    label: str
    description: str
    aliases: tuple[str, ...]
    sitelinks: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class _Batch:
    lines: tuple[bytes, ...]
    source_id: str
    license: str
    languages: tuple[str, ...]


def _values(by_lang: object, lang: str) -> list[str]:
    if not isinstance(by_lang, Mapping):
        return []
    entry = cast(Mapping[str, Any], by_lang).get(lang)
    if isinstance(entry, Mapping):
        value = cast(Mapping[str, Any], entry).get("value")
        return [str(value)] if value else []
    if isinstance(entry, list):
        return [str(e.get("value")) for e in cast(list[Mapping[str, Any]], entry) if e.get("value")]
    return []


def entity_doc(
    entity: Mapping[str, Any], source_id: str, license_: str, languages: Sequence[str]
) -> tuple[DocRecord, LookupRow | None] | None:
    """The compact document of one entity and, for items, its lookup row."""
    eid = str(entity.get("id", ""))
    if not eid:
        return None
    labels = entity.get("labels")
    descriptions = entity.get("descriptions")
    alias_map = entity.get("aliases")
    sitelinks = cast(Mapping[str, Mapping[str, Any]], entity.get("sitelinks") or {})
    title = ""
    lang_used = languages[0]
    lines: list[str] = []
    aliases: list[str] = []
    for lang in languages:
        label = next(iter(_values(labels, lang)), "")
        description = next(iter(_values(descriptions, lang)), "")
        alts = _values(alias_map, lang)
        if label and not title:
            title, lang_used = label, lang
        elif label:
            aliases.append(label)
        aliases.extend(alts)
        parts = [p for p in (label, description) if p]
        if parts or alts:
            line = f"[{lang}] " + " - ".join(parts)
            if alts:
                line += f" (also: {'; '.join(alts)})"
            lines.append(line)
    if not title:  # fall back to a multilingual or any label
        title = next(iter(_values(labels, "mul")), "") or eid
    links: dict[str, str] = {}
    for lang in languages:
        site = sitelinks.get(_wiki(lang))
        if site and site.get("title"):
            page = str(site["title"]).replace(" ", "_")
            links[_wiki(lang)] = f"https://{lang}.wikipedia.org/wiki/{quote(page, safe='/:()')}"
    if sitelinks:
        lines.append(f"Sitelinks: {len(sitelinks)} wiki pages")
    text = "\n".join([title if title != eid else eid, *lines])
    kind = str(entity.get("type", ""))
    page = {"property": "Property:", "lexeme": "Lexeme:"}.get(kind, "")
    stamp = entity.get("modified")
    doc = DocRecord(
        doc_id=make_doc_id(source_id, eid),
        source_id=source_id,
        native_id=eid,
        title=title,
        lang=lang_used,
        license=license_,
        text=text,
        revision=str(entity["lastrevid"]) if "lastrevid" in entity else None,
        timestamp=datetime.fromisoformat(str(stamp).replace("Z", "+00:00")) if stamp else None,
        url_native=f"https://www.wikidata.org/wiki/{page}{eid}",
        entities=(eid,) if eid.startswith("Q") else (),
        links=tuple(links.values()),
        content_hash=content_hash(text),
        aliases=tuple(dict.fromkeys(a for a in aliases if a and a != title)),
    )
    row = None
    if eid.startswith("Q") and eid[1:].isdigit():
        description = next((d for lang in languages for d in _values(descriptions, lang)), "")
        wikis = {_wiki(x) for x in languages}
        site_titles = {w: str(s.get("title", "")) for w, s in sitelinks.items() if w in wikis}
        label = title if title != eid else ""
        row = LookupRow(int(eid[1:]), eid, label, description, doc.aliases, site_titles)
    return doc, row


def decode_batch(batch: _Batch) -> list[tuple[DocRow, LookupRow | None]]:
    """Worker: claims-stripped entity lines (:func:`entity_lines`) -> document rows and lookup rows."""
    out: list[tuple[DocRow, LookupRow | None]] = []
    for line in batch.lines:
        result = entity_doc(parse_entity(line), batch.source_id, batch.license, batch.languages)
        if result is not None:
            out.append((doc_row(result[0]), result[1]))
    return out


class EntityLookupWriter:
    """Builds the lookup next to its final path and publishes it only when complete."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.partial = path.with_name(path.name + ".partial")
        self.partial.parent.mkdir(parents=True, exist_ok=True)
        self.partial.unlink(missing_ok=True)
        self._db = sqlite3.connect(self.partial)
        self._db.executescript(
            "PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF; PRAGMA cache_size=-262144;"
            "CREATE TABLE entities (id INTEGER PRIMARY KEY, qid TEXT NOT NULL, label TEXT,"
            " description TEXT, aliases TEXT, sitelinks TEXT);"
        )
        self._pending: list[tuple[object, ...]] = []

    def add(self, row: LookupRow) -> None:
        aliases = json.dumps(list(row.aliases), ensure_ascii=False)
        sitelinks = json.dumps(dict(row.sitelinks), ensure_ascii=False)
        self._pending.append((row.numeric_id, row.qid, row.label, row.description, aliases, sitelinks))
        if len(self._pending) >= 10_000:
            self._flush()

    def _flush(self) -> None:
        self._db.executemany("INSERT OR REPLACE INTO entities VALUES (?,?,?,?,?,?)", self._pending)
        self._pending.clear()

    def commit(self) -> Path:
        """Finish: index labels, close and atomically move the database into place."""
        self._flush()
        self._db.execute("CREATE INDEX entities_label ON entities(label)")
        self._db.commit()
        self._db.close()
        self.partial.replace(self.path)
        return self.path

    def abort(self) -> None:
        self._db.close()
        self.partial.unlink(missing_ok=True)


class EntityLookup:
    """Read side of the entity lookup: ``QID -> label, description, aliases, sitelinks``."""

    def __init__(self, path: Path) -> None:
        self._db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)

    def get(self, qid: str) -> LookupRow | None:
        if not (qid.startswith("Q") and qid[1:].isdigit()):
            return None
        row = self._db.execute(
            "SELECT id, qid, label, description, aliases, sitelinks FROM entities WHERE id = ?",
            (int(qid[1:]),),
        ).fetchone()
        if row is None:
            return None
        aliases = tuple(str(a) for a in json.loads(row[4]))
        return LookupRow(row[0], row[1], row[2] or "", row[3] or "", aliases, json.loads(row[5]))

    def label(self, qid: str) -> str | None:
        row = self.get(qid)
        return row.label if row is not None else None

    def by_label(self, label: str, *, limit: int = 10) -> list[str]:
        """QIDs whose label is exactly ``label`` (candidate generation for entity linking)."""
        rows = self._db.execute("SELECT qid FROM entities WHERE label = ? LIMIT ?", (label, limit)).fetchall()
        return [str(r[0]) for r in rows]

    def close(self) -> None:
        self._db.close()


class WikidataAdapter(WikimediaDumpsAdapter):
    """``SourceAdapter`` for the Wikidata JSON entity dumps (entity documents + lookup)."""

    id = "wikidata"

    def entries(self, ctx: Ctx, generation_dir: Path) -> Iterator[DocRecord]:
        return self.documents(ctx, generation_dir)

    def documents(self, ctx: Ctx, generation_dir: Path) -> Iterator[DocRecord]:
        spec = ctx.source
        workers = workers_for(spec)
        languages = languages_for(spec)
        dumps = sorted(p for p in upstream_dir(generation_dir).iterdir() if p.name.endswith(_DUMP_SUFFIX))
        if not dumps:
            # Never refine to nothing: an empty pass would publish an empty entity lookup and make
            # the snapshot diff delete every Wikidata document (audit 2026-10-11 C1).
            found = ", ".join(sorted(p.name for p in upstream_dir(generation_dir).iterdir())) or "nothing"
            raise IncompleteGenerationError(
                f"{generation_dir.name}: no *{_DUMP_SUFFIX} entity dump in this generation (found: {found})"
            )
        wants_lookup = bool(access(spec).get("entity_lookup", True))
        writer = EntityLookupWriter(ctx.workdir / LOOKUP_NAME) if wants_lookup else None
        try:
            for dump in dumps:
                batches = _batches(dump, spec, languages, workers)
                for results in ordered_parallel_map(decode_batch, batches, workers):
                    for doc, row in results:
                        if writer is not None and row is not None:
                            writer.add(row)
                        yield doc_from_row(doc)
        except BaseException:
            if writer is not None:
                writer.abort()
            raise
        if writer is not None:
            writer.commit()


def _batches(path: Path, spec: SourceSpec, languages: tuple[str, ...], workers: int) -> Iterator[_Batch]:
    for lines in entity_lines(path, workers):
        yield _Batch(lines, spec.id, spec.license, languages)
