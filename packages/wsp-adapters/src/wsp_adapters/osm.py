"""``osm-planet`` adapter: named places from the OpenStreetMap planet PBF (PLAN §6.2, §9.1).

No map rendering happens here: the planet file itself is the map (any OSM tool opens it). This
adapter makes OSM *searchable* and *linkable*: it emits one short document per named place.

**Discover.** Lists the planet directory (``access.endpoints``; default the official FAU mirror,
``https://ftp.fau.de/osm-planet/pbf/``) for ``planet-YYMMDD.osm.pbf`` and
reads each ``.md5`` sidecar; the newest file with a checksum is the release (version
``YYYY-MM-DD``). ``access.mirrors`` adds byte-identical mirror bases with the same layout.

**Documents.** The PBF is streamed with pyosmium's ``FileProcessor``: PBF blocks are decoded in
C++ threads and a ``KeyFilter("name")`` drops unnamed objects before they reach Python, which is
what keeps a ~95 GB planet tractable. An object becomes a document when it is a **settlement or
locality** (``place=*`` in :data:`PLACE_KINDS`) or carries a **``wikidata``** tag (POIs, parks,
buildings, admin areas, ...). ``native_id`` is ``node/123``, ``way/45`` or ``relation/67``;
``entities`` holds the Wikidata QID, so the place joins Wikipedia and Wikidata through the entity
lookup. Nodes carry coordinates; ways and relations do not (resolving them needs a node-location
index of tens of GB, see ``docs/components/adapters.md``). ``name:<lang>``, ``alt_name``,
``official_name`` and ``old_name`` become aliases (``access.languages``, default ``[en]``).

**Runtime.** pyosmium reads a planet's named objects at roughly 0.5-1.5M objects/s; the Python
step only touches the filtered objects. Measured figures are in ``docs/components/adapters.md``.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Iterator, Mapping
from pathlib import Path

import osmium
import osmium.filter
import osmium.osm

from wsp_adapters._common import access, fetch_all, plan_missing, str_list, upstream_dir, verify_file
from wsp_adapters._streams import listing_links
from wsp_core.ids import content_hash, make_doc_id
from wsp_core.model import (
    Checksum,
    DocRecord,
    FetchPlan,
    FetchResult,
    Generation,
    ReleaseRef,
    RemoteFile,
    Verdict,
)
from wsp_core.ports import Ctx

log = logging.getLogger(__name__)

# The FAU mirror: planet.openstreetmap.org's robots.txt disallows every agent but wget (2026-10-23).
PLANET_URL = "https://ftp.fau.de/osm-planet/pbf/"
FILE_RE = re.compile(r"^planet-(\d{2})(\d{2})(\d{2})\.osm\.pbf$")
_QID_RE = re.compile(r"^Q\d+$")
PLACE_KINDS = frozenset(
    {
        "country", "state", "region", "province", "district", "county", "municipality",
        "city", "borough", "suburb", "quarter", "neighbourhood", "town", "village", "hamlet",
        "island", "islet", "archipelago", "locality", "square",
    }
)  # fmt: skip
_KIND_KEYS = ("place", "amenity", "tourism", "historic", "natural", "leisure", "building", "boundary",
              "waterway", "railway", "aeroway", "man_made", "shop", "office")  # fmt: skip
_ALIAS_KEYS = ("alt_name", "official_name", "old_name", "short_name")


def version_of(name: str) -> str | None:
    match = FILE_RE.match(name)
    return f"20{match.group(1)}-{match.group(2)}-{match.group(3)}" if match else None


def place_record(
    kind: str,
    osm_id: int,
    tags: Mapping[str, str],
    location: tuple[float, float] | None,
    source_id: str,
    license_: str,
    languages: tuple[str, ...],
) -> DocRecord | None:
    """One named place as a document, or None if it is neither a settlement nor Wikidata-linked."""
    name = tags.get("name", "").strip()
    qid = tags.get("wikidata", "").strip()
    place = tags.get("place", "")
    if not name or not (place in PLACE_KINDS or _QID_RE.match(qid)):
        return None
    native = f"{kind}/{osm_id}"
    what = next((f"{k}={tags[k]}" for k in _KIND_KEYS if k in tags), kind)
    aliases = [tags[f"name:{lang}"] for lang in languages if tags.get(f"name:{lang}")]
    aliases += [tags[k] for k in _ALIAS_KEYS if tags.get(k)]
    aliases = [a for a in dict.fromkeys(aliases) if a != name]
    lines = [name, f"OpenStreetMap {kind} {osm_id}: {what}."]
    if aliases:
        lines.append("Also: " + "; ".join(aliases))
    for key, label in (("population", "Population"), ("is_in", "In"), ("addr:country", "Country"),
                       ("ele", "Elevation (m)"), ("description", "Description")):  # fmt: skip
        if tags.get(key):
            lines.append(f"{label}: {tags[key]}")
    if location is not None:
        lines.append(f"Coordinates: {location[0]:.6f}, {location[1]:.6f}")
    links: list[str] = []
    if wiki := tags.get("wikipedia", ""):
        lang, _, title = wiki.partition(":")
        if title:
            links.append(f"https://{lang}.wikipedia.org/wiki/{title.replace(' ', '_')}")
    text = "\n".join(lines)
    return DocRecord(
        doc_id=make_doc_id(source_id, native),
        source_id=source_id,
        native_id=native,
        title=name,
        lang="und",
        license=license_,
        text=text,
        url_native=f"https://www.openstreetmap.org/{native}",
        entities=(qid,) if _QID_RE.match(qid) else (),
        links=tuple(links),
        content_hash=content_hash(text),
        aliases=tuple(aliases),
    )


def read_places(path: Path, source_id: str, license_: str, languages: tuple[str, ...]) -> Iterator[DocRecord]:
    """Stream a ``.osm.pbf`` (or any format osmium reads) and yield its named places."""
    entities = osmium.osm.NODE | osmium.osm.WAY | osmium.osm.RELATION
    processor = osmium.FileProcessor(str(path), entities).with_filter(osmium.filter.KeyFilter("name"))
    for obj in processor:
        location: tuple[float, float] | None = None
        if isinstance(obj, osmium.osm.Node):
            kind = "node"
            loc = obj.location
            if loc.valid():
                location = (float(loc.lat), float(loc.lon))
        elif isinstance(obj, osmium.osm.Way):
            kind = "way"
        elif isinstance(obj, osmium.osm.Relation):
            kind = "relation"
        else:
            continue
        tags = {t.k: t.v for t in obj.tags}
        doc = place_record(kind, int(obj.id), tags, location, source_id, license_, languages)
        if doc is not None:
            yield doc


class OsmPlanetAdapter:
    """``SourceAdapter`` for the weekly OpenStreetMap planet PBF."""

    id = "osm-planet"

    def discover(self, ctx: Ctx) -> Iterable[ReleaseRef]:
        spec = ctx.source
        base = next((e for e in str_list(access(spec).get("endpoints")) if e.endswith("/pbf/")), PLANET_URL)
        mirrors = str_list(access(spec).get("mirrors"))
        names = sorted(
            (u.rsplit("/", 1)[-1] for u in listing_links(ctx.fetcher.get_bytes(base), base)),
            key=lambda n: version_of(n) or "",
            reverse=True,
        )
        for name in names:
            version = version_of(name)
            if version is None:
                continue
            try:
                text = ctx.fetcher.get_bytes(f"{base}{name}.md5", max_bytes=4096).decode("ascii", "replace")
            except Exception as exc:  # the .md5 appears once the file is complete
                log.info("no md5 for %s: %s", name, exc)
                continue
            digest = text.split()[0].lower() if text.split() else ""
            if not re.fullmatch(r"[0-9a-f]{32}", digest):
                continue
            remote = RemoteFile(
                url=base + name,
                name=name,
                checksums=(Checksum("md5", digest),),
                mirrors=tuple(m.rstrip("/") + "/" + name for m in mirrors),
            )
            return [ReleaseRef(spec.id, version, None, (remote,), format="osm-pbf", license="ODbL-1.0")]
        return []

    def plan(self, ctx: Ctx, have: Generation | None, want: ReleaseRef) -> FetchPlan:
        return plan_missing(have, want)

    def fetch(self, ctx: Ctx, plan: FetchPlan) -> Iterable[FetchResult]:
        return fetch_all(ctx, plan)

    def verify(self, ctx: Ctx, remote: RemoteFile, local: Path) -> Verdict:
        return verify_file(remote, local)

    def documents(self, ctx: Ctx, generation_dir: Path) -> Iterator[DocRecord]:
        spec = ctx.source
        languages = str_list(access(spec).get("languages")) or ("en",)
        for path in sorted(upstream_dir(generation_dir).iterdir()):
            if path.name.endswith((".osm.pbf", ".osm")):
                yield from read_places(path, spec.id, spec.license, languages)
