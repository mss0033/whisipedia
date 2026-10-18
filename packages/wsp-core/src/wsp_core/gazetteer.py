"""The Atlas's gazetteer: named places with coordinates, for "find a place" and "what's near here".

Built by ``wsp maps gazetteer`` from the OpenStreetMap planet in the vault (named nodes that are a
settlement or locality, or carry a Wikidata id) into one SQLite file on the local disk (SQLite does
not work on the NAS): a table of places, an FTS5 index of their names and aliases, and an R*Tree of
their positions. ``rank`` orders places of the same name: a country before a town of that name, a
city of millions before a village (:func:`place_rank`).

Searches are prefix searches on the last word ("interl" finds Interlaken), best rank first among the
matches, an exact name first of all. Nearby places come from the R*Tree, nearest first.
"""

from __future__ import annotations

import math
import re
import sqlite3
import threading
import unicodedata
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

SCHEMA = """
CREATE TABLE places (
  id INTEGER PRIMARY KEY,
  osm TEXT NOT NULL,          -- node/123
  name TEXT NOT NULL,
  kind TEXT NOT NULL,         -- place=city, tourism=museum, ...
  lat REAL NOT NULL,
  lon REAL NOT NULL,
  rank REAL NOT NULL,
  population INTEGER,
  qid TEXT,
  wikipedia TEXT              -- "en:Interlaken"
);
CREATE VIRTUAL TABLE names USING fts5(name, aliases, content='', tokenize='unicode61 remove_diacritics 2');
CREATE VIRTUAL TABLE spots USING rtree(id, min_lat, max_lat, min_lon, max_lon);
"""

KIND_RANK = {
    "place=country": 100, "place=state": 80, "place=region": 75, "place=province": 75,
    "place=city": 70, "place=archipelago": 60, "place=county": 55, "place=district": 50,
    "place=town": 50, "place=island": 45, "place=municipality": 45, "place=borough": 30,
    "place=village": 30, "place=suburb": 25, "place=quarter": 20, "place=islet": 20,
    "place=hamlet": 15, "place=square": 15, "place=neighbourhood": 10, "place=locality": 5,
}  # fmt: skip
DEFAULT_RANK = 20  # a feature with a Wikidata id (museum, peak, park, ...)
MAX_LIMIT = 50
CANDIDATES = 400  # FTS matches re-ranked per search


def place_rank(kind: str, population: int | None, has_article: bool) -> float:
    rank = float(KIND_RANK.get(kind, DEFAULT_RANK))
    if population and population > 0:
        rank += 4 * math.log10(population)
    if has_article:
        rank += 5
    return rank


@dataclass(frozen=True, slots=True)
class Place:
    osm: str
    name: str
    kind: str
    lat: float
    lon: float
    rank: float
    population: int | None = None
    qid: str | None = None
    wikipedia: str | None = None
    aliases: tuple[str, ...] = ()
    distance_m: float | None = None


def write_gazetteer(path: Path, places: Iterable[Place], *, batch: int = 50_000) -> int:
    """Write a new gazetteer at ``path`` (built beside it, renamed when complete); the place count."""
    tmp = path.with_name(path.name + ".building")
    tmp.unlink(missing_ok=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(tmp)
    count = 0
    try:
        db.executescript("PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF;" + SCHEMA)
        rows: list[Place] = []

        def flush() -> None:
            db.executemany(
                "INSERT INTO places VALUES (?,?,?,?,?,?,?,?,?,?)",
                ((count - len(rows) + i + 1, p.osm, p.name, p.kind, p.lat, p.lon, p.rank, p.population,
                  p.qid, p.wikipedia) for i, p in enumerate(rows)),
            )  # fmt: skip
            db.executemany(
                "INSERT INTO names(rowid, name, aliases) VALUES (?,?,?)",
                ((count - len(rows) + i + 1, p.name, " ; ".join(p.aliases)) for i, p in enumerate(rows)),
            )
            db.executemany(
                "INSERT INTO spots VALUES (?,?,?,?,?)",
                ((count - len(rows) + i + 1, p.lat, p.lat, p.lon, p.lon) for i, p in enumerate(rows)),
            )
            rows.clear()

        for p in places:
            rows.append(p)
            count += 1
            if len(rows) >= batch:
                flush()
        if rows:
            flush()
        db.execute("CREATE INDEX places_name ON places(name COLLATE NOCASE)")
        db.commit()
        db.execute("INSERT INTO names(names) VALUES ('optimize')")
        db.commit()
    finally:
        db.close()
    tmp.replace(path)
    return count


def _fold(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", text.casefold()) if not unicodedata.combining(c))


_WORD = re.compile(r"\w+", re.UNICODE)


def fts_query(text: str) -> str | None:
    """An FTS5 query for ``text``: every word, the last one as a prefix ("new yo" -> "new" "yo"*)."""
    words = _WORD.findall(_fold(text))
    if not words:
        return None
    quoted = [f'"{w}"' for w in words]
    if len(words[-1]) >= 2:
        quoted[-1] += "*"
    return " ".join(quoted)


def distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance (haversine, mean Earth radius)."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6_371_008.8 * math.asin(min(1.0, math.sqrt(a)))


class Gazetteer:
    """Read-only queries on a gazetteer file; one connection per thread."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._local = threading.local()

    def _db(self) -> sqlite3.Connection:
        db = getattr(self._local, "db", None)
        if db is None:
            db = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, check_same_thread=False)
            self._local.db = db
        return db

    def _rows(self, sql: str, args: tuple[object, ...]) -> Iterator[Place]:
        for r in self._db().execute(sql, args):
            yield Place(
                osm=r[0], name=r[1], kind=r[2], lat=r[3], lon=r[4], rank=r[5], population=r[6],
                qid=r[7], wikipedia=r[8],
            )  # fmt: skip

    def search(self, text: str, *, limit: int = 10) -> list[Place]:
        q = fts_query(text)
        if q is None:
            return []
        limit = max(1, min(limit, MAX_LIMIT))
        found = list(
            self._rows(
                "SELECT p.osm, p.name, p.kind, p.lat, p.lon, p.rank, p.population, p.qid, p.wikipedia "
                "FROM names JOIN places p ON p.id = names.rowid WHERE names MATCH ? "
                "ORDER BY names.rank LIMIT ?",
                (q, CANDIDATES),
            )
        )
        want = _fold(text.strip())
        found.sort(key=lambda p: (_fold(p.name) != want, not _fold(p.name).startswith(want), -p.rank))
        return found[:limit]

    def nearby(self, lat: float, lon: float, *, radius_m: float = 5000, limit: int = 10) -> list[Place]:
        limit = max(1, min(limit, MAX_LIMIT))
        dlat = radius_m / 111_320
        dlon = radius_m / max(1.0, 111_320 * math.cos(math.radians(lat)))
        rows = self._rows(
            "SELECT p.osm, p.name, p.kind, p.lat, p.lon, p.rank, p.population, p.qid, p.wikipedia "
            "FROM spots s JOIN places p ON p.id = s.id "
            "WHERE s.min_lat >= ? AND s.max_lat <= ? AND s.min_lon >= ? AND s.max_lon <= ?",
            (lat - dlat, lat + dlat, lon - dlon, lon + dlon),
        )
        near: list[Place] = []
        for p in rows:
            d = distance_m(lat, lon, p.lat, p.lon)
            if d <= radius_m:
                near.append(
                    Place(
                        p.osm, p.name, p.kind, p.lat, p.lon, p.rank, p.population, p.qid, p.wikipedia, (), d
                    )
                )
        near.sort(key=lambda p: p.distance_m or 0.0)
        return near[:limit]
