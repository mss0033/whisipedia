"""Titles and aliases of the indexed library, read from where they already live (ADR-0011).

The search index used to copy every title, alias and suggest prior into ``meta.sqlite``: 256M titles
and 129M aliases, 77 GB on the system disk (2026-10-12). Each was a copy:

* title, source and language: stored in the Tantivy index and in the Refinery;
* aliases: the Refinery's ``aliases`` column plus each generation's ``redirects.sqlite`` (redirect
  pages, written when a ZIM is refined);
* the suggest prior: the static rank's feature (``wsp index rank``).

:class:`TitleSources` reads them from there: every alias (for the rank's redirect resolution) and the
suggest trie's entries.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import numpy as np
import pyarrow as pa

from wsp_core.ids import InvalidDocIdError, make_doc_id
from wsp_index.linkrank import StaticRank
from wsp_index.refinery import RefineryReader
from wsp_index.suggest import SuggestEntry
from wsp_vault.ratelimit import RateLimiter

SCAN_BATCH = 65_536
SUGGEST_TURN = 1_000
# Redirect aliases share the suggest budget: they were added after it was counted, and the live trie
# held 8.1 million keys against 3 million (2026-10-26 review #28), its build peaking near 2.4 GB.
SUGGEST_REDIRECT_SHARE = 0.4  # of the keys, kept for redirect aliases of the chosen titles
SUGGEST_REDIRECTS_PER_ENTRY = 8  # aliases of one document at most (a city has hundreds of redirects)
"""Unranked titles a source adds to the suggest trie per turn."""


@dataclass(frozen=True, slots=True)
class IndexedSource:
    """One source's indexed generation: its Refinery reader and its redirect tables (one per refined
    generation; a set generation has one per member)."""

    source_id: str
    reader: RefineryReader
    redirects: tuple[Path, ...] = ()


def _open_redirects(path: Path) -> sqlite3.Connection:
    # Written once by the refine step and never changed: ``immutable`` reads without locks, so the
    # file may live anywhere, the NAS included.
    return sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro&immutable=1", uri=True)


class TitleSources:
    """Titles, aliases and suggest entries of the indexed sources."""

    def __init__(self, sources: Sequence[IndexedSource], *, limiter: RateLimiter | None = None) -> None:
        """``limiter``: paces the Refinery scans by their decoded bytes (on the NAS the site reads
        documents over the same link; a suggest rebuild scans every indexed generation)."""
        self.sources = list(sources)
        self.limiter = limiter

    def _scan(self, src: IndexedSource, columns: list[str]) -> Iterator[pa.RecordBatch]:
        for batch in src.reader.scan(columns, batch_size=SCAN_BATCH):
            if self.limiter is not None:
                self.limiter.consume(batch.nbytes)
            yield batch

    # --- aliases -------------------------------------------------------------------------------

    def iter_aliases(self) -> Iterator[tuple[str, str]]:
        """Every ``(doc_id, alias)``: the Refinery's aliases, then the redirects, source by source."""
        for src in self.sources:
            for batch in self._scan(src, ["doc_id", "aliases"]):
                ids = cast(list[str], batch.column(0).to_pylist())
                names = cast(list[list[str] | None], batch.column(1).to_pylist())
                for doc_id, aliases in zip(ids, names, strict=True):
                    for alias in aliases or ():
                        if alias.strip():
                            yield doc_id, alias
            yield from self._redirects(src)

    def _redirects(self, src: IndexedSource, wanted: set[str] | None = None) -> Iterator[tuple[str, str]]:
        for path in src.redirects:
            if not path.is_file():
                continue
            with closing(_open_redirects(path)) as db:
                for target, alias in db.execute("SELECT target, alias FROM redirects"):
                    if not str(alias).strip():
                        continue
                    try:
                        doc_id = make_doc_id(src.source_id, str(target))
                    except InvalidDocIdError:
                        continue
                    if wanted is None or doc_id in wanted:
                        yield doc_id, str(alias)

    # --- suggest -------------------------------------------------------------------------------

    def suggest_entries(self, rank: StaticRank | None, budget: int) -> list[SuggestEntry]:
        """The suggest trie's entries, at most ``budget`` keys (a title or an alias each).

        Half the keys go to the best-ranked documents, best first; the rest is shared by the sources in
        turns of :data:`SUGGEST_TURN` titles, in Refinery order, so a library without ranks still offers
        titles of every source. Where redirect tables exist, :data:`SUGGEST_REDIRECT_SHARE` of the keys
        is kept for the chosen titles' redirect aliases. One scan of the Refinery's
        ``doc_id``/``title``/``aliases`` columns and one of each redirect table.
        """
        total = budget
        if any(p.is_file() for src in self.sources for p in src.redirects):
            budget -= int(budget * SUGGEST_REDIRECT_SHARE)  # the titles' part; the rest for redirects
        if budget <= 0:
            return []
        top = rank.top_keys(budget // 2) if rank is not None else np.zeros(0, np.uint64)
        ranked: list[SuggestEntry] = []  # prior > 0, collected while scanning, ordered below
        turns: dict[str, list[SuggestEntry]] = {}  # each source's first unranked titles
        allot = water_fill({s.source_id: len(s.reader) for s in self.sources}, budget)
        for src in self.sources:
            mine = turns.setdefault(src.source_id, [])
            cap = allot.get(src.source_id, 0)
            for batch in self._scan(src, ["doc_id", "title", "aliases"]):
                ids = cast(list[str], batch.column(0).to_pylist())
                hit = np.zeros(len(ids), dtype=np.bool_)
                if len(top):
                    keys = cast(StaticRank, rank).keys_of(ids)
                    hit = top[np.minimum(np.searchsorted(top, keys), len(top) - 1)] == keys
                room = cap - len(mine)
                if room <= 0 and not hit.any():
                    if not len(top):
                        break  # allotment full and nothing ranked to look for
                    continue
                titles = cast(list[str], batch.column(1).to_pylist())
                names = cast(list[list[str] | None], batch.column(2).to_pylist())
                rows = np.flatnonzero(hit).tolist()
                priors = (
                    cast(StaticRank, rank).features([ids[i] for i in rows])
                    if rows
                    else np.zeros(0, np.float32)
                )
                for i, prior in zip(rows, priors, strict=True):
                    if titles[i] and prior > 0:
                        ranked.append(SuggestEntry(ids[i], titles[i], _clean(names[i]), float(prior)))
                if room > 0:
                    for i in np.flatnonzero(~hit).tolist():
                        if titles[i]:
                            mine.append(SuggestEntry(ids[i], titles[i], _clean(names[i]), 0.0))
                            if len(mine) >= cap:
                                break
        if rank is not None:  # unranked here means "not in the top half"; most still have a rank
            for sid, entries in turns.items():
                feats = rank.features([e.doc_id for e in entries])
                turns[sid] = [
                    SuggestEntry(e.doc_id, e.title, e.aliases, float(f))
                    for e, f in zip(entries, feats, strict=True)
                ]
        ranked.sort(key=lambda e: (-e.prior, e.doc_id))
        chosen: list[SuggestEntry] = []
        used = 0
        for entry in ranked:
            if used >= budget // 2:
                break
            chosen.append(entry)
            used += 1 + len(entry.aliases)
        queues = {sid: iter(entries) for sid, entries in turns.items() if entries}
        while used < budget and queues:
            turn = max(1, min(SUGGEST_TURN, (budget - used) // len(queues)))
            for sid in list(queues):
                for _ in range(turn):
                    entry = next(queues[sid], None)
                    if entry is None:
                        del queues[sid]
                        break
                    chosen.append(entry)
                    used += 1 + len(entry.aliases)
                    if used >= budget:
                        break
                if used >= budget:
                    break
        return self._with_redirects(chosen, total - used)

    def _with_redirects(self, entries: list[SuggestEntry], room: int) -> list[SuggestEntry]:
        """``entries`` with their redirect aliases added, best entries first, ``room`` keys in all and
        :data:`SUGGEST_REDIRECTS_PER_ENTRY` per entry (one scan of each redirect table)."""
        if room <= 0:
            return entries
        wanted = {e.doc_id for e in entries}
        extra: dict[str, list[str]] = {}
        for src in self.sources:
            for doc_id, alias in self._redirects(src, wanted):
                mine = extra.setdefault(doc_id, [])
                if len(mine) < SUGGEST_REDIRECTS_PER_ENTRY:
                    mine.append(alias)
        if not extra:
            return entries
        out: list[SuggestEntry] = []
        for e in entries:
            more = extra.get(e.doc_id)
            if more and room > 0:
                new = [a for a in dict.fromkeys(more) if a not in e.aliases][:room]
                room -= len(new)
                e = SuggestEntry(e.doc_id, e.title, (*e.aliases, *new), e.prior)
            out.append(e)
        return out


def _clean(aliases: Sequence[str] | None) -> tuple[str, ...]:
    return tuple(a for a in (aliases or ()) if a.strip())


def water_fill(sizes: dict[str, int], total: int) -> dict[str, int]:
    """Share ``total`` among keys as equally as possible, none above its size (the turn order of
    :meth:`TitleSources.suggest_entries`, decided before the scan so each source keeps only its share)."""
    out = dict.fromkeys(sizes, 0)
    left = {k: v for k, v in sizes.items() if v > 0}
    remaining = total
    while left and remaining > 0:
        share = max(1, remaining // len(left))
        for k in sorted(left, key=lambda k: left[k]):
            take = min(share, left[k] - out[k], remaining)
            out[k] += take
            remaining -= take
            if out[k] >= left[k]:
                del left[k]
            if remaining <= 0:
                break
    return out
